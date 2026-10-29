"""Recoverable new-file chunk uploads with bounded, independently checked parts.

SDK session/part APIs are retained. Commit uses the same SDK transport directly
so HTTP 202 and Retry-After survive the generated convenience method.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
from pathlib import Path
import threading

from cassn.box.transfer import UploadPaused, error_response, retry_after, uncertain_request
from cassn.box.upload_state import atomic_json


def _fingerprint(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _hash_file(path, runtime):
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            runtime.check_cancelled()
            sha1.update(block)
            sha256.update(block)
    return sha1.hexdigest(), sha256.hexdigest()


def _receipt(part):
    if isinstance(part, dict):
        result = {k: part[k] for k in ('part_id', 'offset', 'size')}
        hashes = {k: part.get(k) for k in ('sha1', 'sha512')}
    else:
        result = dict(part_id=part.part_id, offset=part.offset, size=part.size)
        hashes = dict(sha1=part.sha_1, sha512=getattr(part, 'sha512', None))
    result.update({k: str(v).lower() for k, v in hashes.items() if v is not None})
    return result


def _check_receipt(receipt, offset, data):
    hashes = dict(sha1=hashlib.sha1(data).hexdigest(), sha512=hashlib.sha512(data).hexdigest())
    if (not receipt['part_id'] or receipt['offset'] != offset or receipt['size'] != len(data)
            or not any(receipt.get(k) is not None for k in hashes)
            or any(str(receipt[k]).lower() != digest for k, digest in hashes.items()
                   if receipt.get(k) is not None)):
        raise UploadPaused('Box part receipt does not match source bytes; retain staging for review')
    return dict(receipt, **hashes)


def _matches_checked_receipt(remote, checked):
    # Box can list SHA-512 even when the PUT receipt contains SHA-1. Compare
    # every returned digest to hashes independently checked against source bytes.
    return (all(remote.get(k) == checked.get(k) for k in ('part_id', 'offset', 'size'))
            and any(remote.get(k) is not None for k in ('sha1', 'sha512'))
            and all(str(remote[k]).lower() == checked.get(k) for k in ('sha1', 'sha512')
                    if remote.get(k) is not None))


class SDKChunkBackend:
    def __init__(self, client):
        self.client = client

    @property
    def manager(self):
        return self.client.chunked_uploads

    def create(self, parent, size, name):
        return self.manager.create_file_upload_session(parent, size, name)

    def status(self, session_id):
        return self.manager.get_file_upload_session_by_id(session_id)

    def parts(self, session):
        offset = 0
        while True:
            page = self.manager.get_file_upload_session_parts_by_url(
                session.session_endpoints.list_parts, offset=offset, limit=1000)
            entries = page.entries or []
            for part in entries:
                yield _receipt(part)
            # Box's total_count is an upper offset boundary, not an entry
            # count. Pages can be short or empty within that boundary. Advance
            # by the returned limit, as required by Box offset pagination.
            limit = getattr(page, 'limit', None)
            limit = 1000 if limit is None else limit
            if not isinstance(limit, int) or limit <= 0:
                raise UploadPaused('Box returned an invalid part listing page size')
            offset += limit
            total = page.total_count
            if total is not None and offset >= total:
                return
            if total is None and len(entries) < limit:
                return

    def put(self, session, offset, data, total):
        digest = 'sha=' + base64.b64encode(hashlib.sha1(data).digest()).decode('ascii')
        result = self.manager.upload_file_part_by_url(
            session.session_endpoints.upload_part, io.BytesIO(data), digest,
            f'bytes {offset}-{offset + len(data) - 1}/{total}')
        return _receipt(result.part)

    def commit(self, session, receipts, sha1):
        from box_sdk_gen.networking.fetch_options import FetchOptions
        digest = 'sha=' + base64.b64encode(bytes.fromhex(sha1)).decode('ascii')
        return self.manager.network_session.network_client.fetch(FetchOptions(
            url=session.session_endpoints.commit, method='POST',
            headers={'digest': digest}, data={'parts': receipts},
            auth=self.manager.auth, network_session=self.manager.network_session))

    def find_file(self, parent, name, size, sha1):
        offset = 0
        while True:
            page = self.client.folders.get_folder_items(
                parent, fields=['id','type','name'], limit=1000, offset=offset)
            entries = page.entries or []
            for item in entries:
                if item.type == 'file' and item.name == name:
                    return self.verify_file(item.id, parent, name, size, sha1)
            if len(entries) < 1000:
                return None
            offset += len(entries)

    def verify_file(self, file_id, parent, name, size, sha1):
        item = self.client.files.get_file_by_id(
            file_id, fields=['id','name','parent','size','sha1'])
        if (item.name != name or not item.parent or str(item.parent.id) != str(parent)
                or item.size != size or str(item.sha_1).lower() != sha1):
            raise UploadPaused('Destination file does not match source identity, size and checksum')
        return str(item.id)


class ChunkUploader:
    def __init__(self, backend, runtime, journal_dir, *, part_workers=None):
        self.backend, self.runtime = backend, runtime
        self.journal_dir = Path(journal_dir)
        self.part_workers = runtime.part_workers if part_workers is None else part_workers
        if self.part_workers not in (1, 2, 3, 4):
            raise ValueError('Box part concurrency must be between one and four')

    @contextmanager
    def _locked(self, key):
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        with (self.journal_dir / (key + '.lock')).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UploadPaused('This file is already being uploaded by another process') from exc
            try:
                yield self.journal_dir / (key + '.json')
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def upload(self, path, parent, name):
        runtime = self.runtime
        runtime.check_cancelled()
        if not runtime.actor:
            raise UploadPaused('Authenticated Box account is required for resumable uploads')
        path, parent = Path(path), str(parent)
        # Stable across publication attempts; raw-data destination identity is unchanged.
        key = self._key(path, parent, name)
        with self._locked(key) as journal:
            return self._upload(path, parent, name, journal)

    def _key(self, path, parent, name):
        return hashlib.sha256(json.dumps([self.runtime.actor, str(parent), name,
                                          str(Path(path).resolve())]).encode()).hexdigest()

    def needs_resume(self, path, parent, name):
        journal = self.journal_dir / (self._key(path, parent, name) + '.json')
        return journal.exists() and json.loads(journal.read_text()).get('state') != 'complete'

    def _upload(self, path, parent, name, journal):
        runtime = self.runtime
        runtime.check_cancelled()
        before = _fingerprint(path)
        sha1, sha256 = _hash_file(path, runtime)
        if _fingerprint(path) != before:
            raise UploadPaused('Source changed while preparing the upload')
        size = before[2]
        identity = dict(actor=runtime.actor, parent=parent, name=name, size=size,
                        sha1=sha1, sha256=sha256)
        state = None
        if journal.exists():
            state = json.loads(journal.read_text())
            if (state.get('version') != 1 or state.get('identity') != identity
                    or state.get('source_stat') != before):
                raise UploadPaused('Upload journal belongs to a changed source or destination; review before restarting')
            if state.get('state') == 'needs_review':
                raise UploadPaused('This upload needs review; local recovery records retained')
            if state.get('state') == 'complete':
                return self.backend.verify_file(state['file_id'], parent, name, size, sha1)
            if not state.get('session_id'):
                # Creation may have reached Box before a crash/lost response. Do
                # not silently create another session on this ambiguous path.
                found = self.backend.find_file(parent, name, size, sha1)
                if found:
                    return self._complete(journal, state, found)
                raise UploadPaused('Upload session creation outcome is unknown; recovery record needs review')
            try:
                session = self.backend.status(state['session_id'])
            except Exception as exc:
                status, _ = error_response(exc)
                if status not in (404, 410):
                    raise
                found = self.backend.find_file(parent, name, size, sha1)
                if found:
                    return self._complete(journal, state, found)
                raise UploadPaused('Box upload session expired or disappeared; review before restarting') from exc
        else:
            state = dict(version=1, identity=identity, source_stat=before,
                         state='creating', receipts={}, session_id=None)
            atomic_json(journal, state)  # intent before creating the server session
            session = self.backend.create(parent, size, name)
            state.update(state='uploading', session_id=session.id)
            atomic_json(journal, state)

        part_size = session.part_size
        if not isinstance(part_size, int) or part_size < 1 or size < 1:
            raise UploadPaused('Box returned an invalid upload part size')
        if session.total_parts != (size + part_size - 1) // part_size:
            raise UploadPaused('Box session part count does not cover the source file')
        expiry = getattr(session, 'session_expires_at', None)
        if expiry is not None:
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise UploadPaused('Box upload session expired; retain records for review')
            state['expires_at'] = expiry.isoformat()

        remote = self._remote_parts(session, size, part_size)
        # The server is authoritative after a crash or lost acknowledgement.
        state['receipts'] = {str(o): r for o, r in remote.items()}
        atomic_json(journal, state)
        state_lock = threading.Lock()

        def record(receipt):
            with state_lock:
                state['receipts'][str(receipt['offset'])] = receipt
                atomic_json(journal, state)
            runtime.metrics.add(acknowledged_bytes=receipt['size'], acknowledged_parts=1)

        def send(offset, data):
            try:
                receipt = self._send(session, offset, data, size)
                record(receipt)
                return receipt
            finally:
                runtime.release_buffer(len(data))

        ordered_hash = hashlib.sha1()
        pending = set()
        with ThreadPoolExecutor(max_workers=self.part_workers, thread_name_prefix='box-part') as pool:
            with path.open('rb') as source:
                for offset in range(0, size, part_size):
                    runtime.check_cancelled()
                    count = min(part_size, size - offset)
                    # Allocate only after reserving bytes in the operation-wide budget.
                    runtime.reserve_buffer(count)
                    handed_off = False
                    try:
                        data = source.read(count)
                        if len(data) != count:
                            raise UploadPaused('Source file was truncated during upload')
                        ordered_hash.update(data)
                        if offset in remote:
                            state['receipts'][str(offset)] = _check_receipt(remote[offset], offset, data)
                        else:
                            pending.add(pool.submit(send, offset, data))
                            handed_off = True
                        del data
                    finally:
                        if not handed_off:
                            runtime.release_buffer(count)
                    if len(pending) >= self.part_workers:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            future.result()
                for future in pending:
                    future.result()

        if ordered_hash.hexdigest() != sha1 or _fingerprint(path) != before:
            state['state'] = 'needs_review'
            atomic_json(journal, state)
            raise UploadPaused('Source changed during upload; no commit was sent')
        # Validate the final remote list, including processing/pagination, against
        # the receipts checked with source bytes. Missing receipts pause safely.
        final = self._remote_parts(session, size, part_size)
        expected = {int(o): r for o, r in state['receipts'].items()}
        atomic_json(journal, state)
        if (set(final) != set(expected) or len(final) != session.total_parts
                or any(not _matches_checked_receipt(final[o], expected[o]) for o in final)):
            raise UploadPaused('Box has not confirmed all checked upload parts; resume later')
        receipts = [{k: expected[o][k] for k in ('part_id', 'offset', 'size', 'sha1')}
                    for o in sorted(final)]
        state['state'] = 'committing'
        atomic_json(journal, state)
        commit_deadline = runtime.clock() + runtime.max_wait
        for _ in range(20):
            runtime.check_cancelled()
            if _fingerprint(path) != before:
                state['state'] = 'needs_review'
                atomic_json(journal, state)
                raise UploadPaused('Source changed before commit; retain staging for review')
            try:
                response = self.backend.commit(session, receipts, sha1)
            except Exception as exc:
                status, _ = error_response(exc)
                if not uncertain_request(exc) and status not in (404, 410):
                    raise
                found = self.backend.find_file(parent, name, size, sha1)
                if found:
                    return self._complete(journal, state, found)
                # Keep the same session and commit state. No whole-file restart.
                raise UploadPaused('Box commit outcome is not confirmed; re-run to resume this session') from exc
            if response.status == 202:
                delay = retry_after(response.headers)
                delay = delay if delay is not None else 1
                if runtime.clock() + delay > commit_deadline:
                    raise UploadPaused('Box commit wait budget exceeded; resume this session later')
                runtime.sleep(delay)
                continue
            if response.status != 201:
                raise UploadPaused('Unexpected Box commit result; retain recovery records')
            entries = (response.data or {}).get('entries') or []
            if len(entries) != 1 or not entries[0].get('id'):
                raise UploadPaused('Box commit returned no unique file identity; resume to reconcile')
            found = self.backend.verify_file(entries[0]['id'], parent, name, size, sha1)
            return self._complete(journal, state, found)
        raise UploadPaused('Box is still processing the upload; re-run to resume its commit')

    def _complete(self, journal, state, file_id):
        state.update(state='complete', file_id=str(file_id))
        atomic_json(journal, state)
        return str(file_id)

    def _remote_parts(self, session, size, part_size):
        result = {}
        for receipt in self.backend.parts(session):
            offset = receipt['offset']
            if (not isinstance(offset, int) or offset < 0 or offset >= size
                    or offset % part_size or offset in result
                    or receipt['size'] != min(part_size, size - offset)):
                raise UploadPaused('Invalid/overlapping Box upload part coverage')
            result[offset] = receipt
        return result

    def _send(self, session, offset, data, total):
        for attempt in range(3):
            self.runtime.check_cancelled()
            try:
                receipt = self.backend.put(session, offset, data, total)
                return _check_receipt(receipt, offset, data)
            except UploadPaused:
                raise
            except Exception as exc:
                status, headers = error_response(exc)
                if not uncertain_request(exc):
                    raise
                # An immutable part may have landed before the reply was lost.
                for receipt in self.backend.parts(session):
                    if receipt['offset'] == offset:
                        return _check_receipt(receipt, offset, data)
                if attempt == 2:
                    raise UploadPaused('Part upload is not confirmed; re-run to resume the same session') from exc
                delay = retry_after(headers)
                self.runtime.sleep(delay if delay is not None else 2 ** attempt)
        raise AssertionError('Part retry loop exhausted without an outcome')
