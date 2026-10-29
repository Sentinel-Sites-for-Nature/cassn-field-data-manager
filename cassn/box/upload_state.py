"""Atomic local-only upload diagnostics and recovery records."""
import json
import os
from pathlib import Path
import tempfile


STATE_DIRECTORY = 'box_upload_sessions'


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Make the rename durable too, where supported by this filesystem.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_uploads_complete(folder):
    """An old verification pass cannot authorize cleanup of a pending session."""
    directory = Path(folder) / 'qc' / STATE_DIRECTORY
    if not directory.exists():
        return
    for path in directory.glob('*.json'):
        if path.name == 'metrics.json':
            continue
        try:
            state = json.loads(path.read_text())
            if (state.get('version') != 1 or state.get('state') != 'complete'
                    or not state.get('file_id')):
                raise ValueError('upload is pending or needs recovery')
        except (OSError, ValueError, AttributeError) as exc:
            raise ValueError(f'Retain staging: review upload recovery record {path.name}: {exc}') from exc
