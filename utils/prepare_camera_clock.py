"""Prepare corrected ingest copies without a session or writes to the source.

The output is a dedicated package: originals/, receipts/, working/, and finally
ready/. Select only ready/ in FDM, after preparation reports completion.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from datetime import datetime, timedelta
from uuid import uuid4

FORMAT = '%Y:%m:%d %H:%M:%S'
DATE_TAGS = {'DateTimeOriginal', 'CreateDate', 'ModifyDate'}
READ_TAGS = ['DateTimeOriginal', 'CreateDate', 'ModifyDate', 'OffsetTime',
             'OffsetTimeOriginal', 'OffsetTimeDigitized', 'SerialNumber']


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def canonical(path):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError(f'Use a real path without symlinks: {path}')
    return path


def scan(root):
    """Include nonhidden regular files; reject links and special files."""
    def raise_walk_error(error):
        raise error

    found = []
    for parent, dirs, files in os.walk(root, onerror=raise_walk_error):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for name in dirs + [f for f in files if not f.startswith('.')]:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError(f'Symlink is not a preparation source: {path}')
        for name in sorted(files):
            if name.startswith('.'):
                continue
            path = Path(parent) / name
            if not path.is_file():
                raise ValueError(f'Not a regular file: {path}')
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def safe(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('Unsafe saved path')
    path = root / rel
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Unsafe preparation path: {path}')
    return path


def save(path, value):
    if path.is_symlink():
        raise ValueError(f'Unsafe journal: {path}')
    temp = path.with_name('.' + path.name + '.' + uuid4().hex)
    try:
        with temp.open('x') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        flush_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def flush_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install(source, destination):
    """Install a prepared local file atomically without overwriting any target."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, destination)
    flush_dir(destination.parent)


def copy_verified(source, destination, expected):
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file() or digest(destination) != expected:
            raise ValueError(f'Existing copy conflicts: {destination}')
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('.' + destination.name + '.' + uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        if digest(temporary) != expected:
            raise ValueError(f'Copy did not verify: {source}')
        install(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class Metadata:
    def __enter__(self):
        from exiftool import ExifToolHelper
        self.tool = ExifToolHelper(common_args=['-G1', '-a'])
        self.tool.__enter__()
        return self

    def __exit__(self, *args):
        return self.tool.__exit__(*args)

    def read(self, path):
        row = self.tool.get_tags(str(path), READ_TAGS)[0]
        result = {key: value for key, value in row.items()
                  if key.split(':')[-1] in set(READ_TAGS)}
        if not any(key.endswith(':DateTimeOriginal') for key in result):
            raise ValueError(f'No original camera date: {path}')
        for key, value in result.items():
            if key.split(':')[-1] in DATE_TAGS:
                datetime.strptime(value, FORMAT)
        return result

    def shift(self, path, seconds):
        direction = '+=' if seconds > 0 else '-='
        days, remainder = divmod(abs(seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        result = self.tool.execute(f'-AllDates{direction}0:0:{days} {hours}:{minutes}:{seconds}',
                                   '-overwrite_original', str(path))
        if not re.search(r'^\s*1 image files? updated\s*$', result, re.MULTILINE):
            raise ValueError(f'ExifTool did not confirm one image changed: {result}')


def shifted(tags, seconds):
    return {key: ((datetime.strptime(value, FORMAT) + timedelta(seconds=seconds)).strftime(FORMAT)
                  if key.split(':')[-1] in DATE_TAGS else value)
            for key, value in tags.items()}


def plan(source, seconds, metadata):
    files = []
    for index, relative in enumerate(scan(source)):
        path = source / relative
        before = path.stat()
        tags = metadata.read(path) if path.suffix.lower() in {'.jpg', '.jpeg'} else None
        item = {'path': relative, 'sha256': digest(path), 'size': before.st_size, 'tags': tags}
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f'Source changed while reading: {path}')
        if tags:
            shifted(tags, seconds)  # Validate expected dates before any writes.
        files.append(item)
        if (index + 1) % 100 == 0:
            print(f'Planned and hashed {index + 1} source files', flush=True)
    if not files or not any(item['tags'] for item in files):
        raise ValueError('No camera images found')
    return files


def verify_file(path, expected_hash, tags, metadata):
    if path.is_symlink() or not path.is_file() or digest(path) != expected_hash:
        raise ValueError(f'File evidence differs: {path}')
    if tags is not None and metadata.read(path) != tags:
        raise ValueError(f'Camera dates, offsets, or serial differ: {path}')


def execute(source, output, seconds, *, dry_run=False, metadata_factory=Metadata):
    source, output = canonical(source), canonical(output)
    if not source.is_dir() or source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('Source and output must be separate directories')
    if not seconds:
        raise ValueError('A nonzero correction is required')
    with metadata_factory() as metadata:
        if dry_run:
            files = plan(source, seconds, metadata)
            print(f'Dry run: {len(files)} files; shift {seconds} seconds; source unchanged; no output written.')
            for item in (files[0], files[-1]):
                if item['tags']:
                    print(item['path'], item['tags'], '->', shifted(item['tags'], seconds))
            return
        if output.exists() and (not output.is_dir() or not (output / 'manifest.json').is_file()):
            raise ValueError('Output already exists without an owned preparation manifest; choose a new directory')
        new = not output.exists()
        if new:
            # Finish the complete source plan before creating the output.
            files = plan(source, seconds, metadata)
            ancestor = output.parent
            while not ancestor.exists():
                ancestor = ancestor.parent
            required = 2 * sum(item['size'] for item in files) + max(item['size'] for item in files)
            if shutil.disk_usage(ancestor).free < required + 1024 * 1024 * 1024:
                raise ValueError('Insufficient space for originals, corrected copies, and a 1 GiB margin')
            output.mkdir(parents=True, exist_ok=False)
        lockpath = safe(output, '.lock')
        with lockpath.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            manifest_path = safe(output, 'manifest.json')
            request = {'source': str(source), 'output': str(output), 'seconds': seconds}
            if new:
                manifest = {'schema_version': 1, 'request': request, 'status': 'preparing', 'files': files}
                save(manifest_path, manifest)
            else:
                manifest = json.loads(manifest_path.read_text())
                if manifest.get('schema_version') != 1 or manifest.get('request') != request:
                    raise ValueError('Resume requires the original source, output, and correction')
                files = manifest['files']
            paths = [item['path'] for item in files]
            if len(set(paths)) != len(paths) or scan(source) != sorted(paths):
                raise ValueError('Source file membership differs from the saved plan')
            for name in ['originals', 'receipts', 'working', 'ready']:
                safe(output, name)
            ready, working = output / 'ready', output / 'working'
            tree = ready if ready.exists() else working
            if ready.exists() and working.exists():
                raise ValueError('Both ready and working folders exist; review before continuing')
            for index, item in enumerate(files):
                relative = item['path']
                src, original = safe(source, relative), safe(output / 'originals', relative)
                verify_file(src, item['sha256'], item['tags'], metadata)
                copy_verified(src, original, item['sha256'])
                verify_file(original, item['sha256'], item['tags'], metadata)
                destination = safe(tree, relative)
                receipt_path = safe(output, f'receipts/{index:06d}.json')
                expected_tags = shifted(item['tags'], seconds) if item['tags'] else None
                if receipt_path.exists():
                    receipt = json.loads(receipt_path.read_text())
                    if receipt.get('source') != item or receipt.get('after_tags') != expected_tags:
                        raise ValueError('Receipt does not match the source plan')
                else:
                    if destination.exists() or ready.exists():
                        raise ValueError(f'Unrecorded corrected file: {destination}')
                    receipt_path.parent.mkdir(exist_ok=True)
                    temp = receipt_path.parent / ('.work-' + uuid4().hex + original.suffix)
                    try:
                        shutil.copy2(original, temp)
                        if item['tags']:
                            metadata.shift(temp, seconds)
                            if metadata.read(temp) != expected_tags:
                                raise ValueError(f'Correction did not verify all date fields: {relative}')
                        after_hash = digest(temp)
                        with temp.open('rb') as stream:
                            os.fsync(stream.fileno())
                        receipt = {'source': item, 'after_sha256': after_hash, 'after_tags': expected_tags}
                        # Preserve prepared evidence before installing; recovery
                        # always rebuilds from the unmodified original.
                        receipt['prepared'] = temp.name
                        save(receipt_path, receipt)
                    except BaseException:
                        temp.unlink(missing_ok=True)
                        raise
                if not destination.exists():
                    prepared = safe(output / 'receipts', receipt['prepared'])
                    if not prepared.is_file():
                        raise ValueError(f'Prepared copy missing; retain package for review: {relative}')
                    verify_file(prepared, receipt['after_sha256'], expected_tags, metadata)
                    install(prepared, destination)
                verify_file(destination, receipt['after_sha256'], expected_tags, metadata)
                prepared = safe(output / 'receipts', receipt['prepared'])
                prepared.unlink(missing_ok=True)
                if (index + 1) % 100 == 0:
                    print(f'Prepared and verified {index + 1}/{len(files)} files', flush=True)
            if scan(tree) != sorted(paths) or scan(output / 'originals') != sorted(paths):
                raise ValueError('Output membership differs from saved plan')
            if tree == working:
                # The ready folder is exposed only after every file verified.
                os.rename(working, ready)
                flush_dir(output)
            manifest['status'] = 'complete'
            save(manifest_path, manifest)
            print(f'Complete: {len(files)} files verified. Ingest from {ready}. Originals retained in {output / "originals"}.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    for part in ['days', 'hours', 'minutes', 'seconds']:
        parser.add_argument('--' + part, type=int, default=0)
    parser.add_argument('--subtract', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    values = [args.days, args.hours, args.minutes, args.seconds]
    if any(v < 0 for v in values) or not any(values):
        parser.error('Use nonnegative components with a nonzero correction')
    seconds = int(timedelta(days=args.days, hours=args.hours, minutes=args.minutes, seconds=args.seconds).total_seconds())
    execute(args.source, args.output, -seconds if args.subtract else seconds, dry_run=args.dry_run)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
