"""Cross-process ownership of an open deployment and its writers."""
import hashlib
import os
from pathlib import Path

from PySide6.QtCore import QLockFile


class ResourceBusy(RuntimeError):
    pass


class ResourceLease:
    """Crash-recoverable locks outside deployment folders, also honored by other processes."""

    def __init__(self, directory: Path, keys):
        self.directory = Path(directory).resolve()
        self.keys = tuple(sorted(set(keys)))
        self._locks = []
        directory.mkdir(parents=True, exist_ok=True)
        try:
            for key in self.keys:
                name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
                lock = QLockFile(str(directory / name))
                # A long upload is not stale. Qt can still recover dead-process locks.
                lock.setStaleLockTime(0)
                if not lock.tryLock(0):
                    if lock.error() != QLockFile.LockFailedError:
                        raise OSError(f"Cannot create an application lock in {directory}")
                    raise ResourceBusy(key)
                self._locks.append(lock)
        except Exception:
            self.release()
            raise

    def owns(self, keys):
        return bool(self._locks) and set(keys) <= set(self.keys)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()

    def release(self):
        for lock in reversed(self._locks):
            lock.unlock()
        self._locks.clear()


def deployment_keys(folder: Path, event_id: str, box_root: str | None):
    return (
        "deployment-path:" + os.path.normcase(str(folder.resolve())),
        f"deployment-event:{box_root or 'default'}:{event_id}",
    )
