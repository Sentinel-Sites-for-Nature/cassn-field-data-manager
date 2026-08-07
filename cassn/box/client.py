"""
A small, Qt-free façade over the Box folder/upload API.

The original repeated the same four primitives inside every Box thread:
paginate a folder's children, find a child folder by name, find-or-create a
folder, and upload a file while honoring the immutable-``raw_data`` /
mutable-sidecar distinction. Each copy carried its own ad-hoc caches and its
own ``while True: ... offset += 1000`` paginator, and they had already drifted
(one checked ``len(entries)``, another ``len(page.entries)``).

:class:`BoxStorage` collapses those into one object with two caches:

* ``_resolved_folders`` — ``(parent_id, name) -> folder_id`` so a given
  intermediate folder is resolved once per session;
* ``_folder_files`` — ``folder_id -> {filename: file_id}`` returned **by
  reference**, so an upload that adds a file to a folder updates the cache the
  next lookup will see.

The behavior — pagination size, the find-or-create flow, the skip/version/
upload decision, and which files are uploadable — is identical to the original;
only the duplication is gone.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from cassn.config import CHUNKED_UPLOAD_THRESHOLD
from cassn.core.classification import sanitize_box_folder_name


class BoxStorage:
    """Stateful helper wrapping a :class:`BoxClient` for one operation session.

    A fresh instance should be created per thread/operation: its caches assume
    the underlying Box tree does not change underneath it for the duration.
    """

    PAGE_LIMIT = 1000  # Box's max page size for folder listings

    def __init__(self, client):
        self.client = client
        self._resolved_folders: dict[tuple, str] = {}
        self._folder_files: dict[str, dict[str, str]] = {}

    # -- listing -----------------------------------------------------------

    def iter_folder_items(self, folder_id, *, fields: list[str] | None = None) -> Iterator:
        """Yield every child of ``folder_id``, transparently paginating.

        ``fields`` is forwarded to the Box API only when given, so callers can
        request the lean ``["id", "type", "name", "sha1"]`` projection (used by
        the verify/fixity walks) or the default full item otherwise.
        """
        offset = 0
        while True:
            if fields is not None:
                page = self.client.folders.get_folder_items(
                    folder_id, fields=fields, limit=self.PAGE_LIMIT, offset=offset
                )
            else:
                page = self.client.folders.get_folder_items(
                    folder_id, limit=self.PAGE_LIMIT, offset=offset
                )
            entries = page.entries or []
            for item in entries:
                yield item
            if len(entries) < self.PAGE_LIMIT:
                return
            offset += self.PAGE_LIMIT

    def find_child_folder(self, parent_id, name, *, fields: list[str] | None = None) -> str | None:
        """Return the id of the child folder named ``name``, or ``None``."""
        for item in self.iter_folder_items(parent_id, fields=fields):
            if item.type == "folder" and item.name == name:
                return item.id
        return None

    def find_or_create_folder(self, parent_id, folder_name) -> str:
        """Return the id of ``folder_name`` under ``parent_id``, creating it if needed.

        The name is sanitized to Box's constraints first, and each resolved
        ``(parent_id, name)`` pair is cached so repeated lookups during one
        upload session cost no API calls.
        """
        folder_name = sanitize_box_folder_name(folder_name)
        cache_key = (parent_id, folder_name)
        cached = self._resolved_folders.get(cache_key)
        if cached is not None:
            return cached
        try:
            for item in self.iter_folder_items(parent_id):
                if item.type == "folder" and item.name == folder_name:
                    self._resolved_folders[cache_key] = item.id
                    return item.id

            from box_sdk_gen import CreateFolderParent

            new_folder = self.client.folders.create_folder(
                folder_name, CreateFolderParent(id=parent_id)
            )
            self._resolved_folders[cache_key] = new_folder.id
            return new_folder.id
        except Exception as e:
            raise Exception(f"Error creating folder '{folder_name}': {e}")

    def folder_file_map(self, folder_id) -> dict[str, str]:
        """Return ``{filename: file_id}`` for ``folder_id`` (cached by reference).

        The returned dict is the cached object itself, so callers that record a
        freshly uploaded file in it keep the cache current for later lookups.
        """
        cached = self._folder_files.get(folder_id)
        if cached is not None:
            return cached
        existing: dict[str, str] = {}
        for item in self.iter_folder_items(folder_id):
            if item.type == "file":
                existing[item.name] = item.id
        self._folder_files[folder_id] = existing
        return existing

    def collect_file_paths(self, folder_id, *, fields: list[str] | None = None) -> set[str]:
        """Recursively collect every file's path under ``folder_id``.

        Paths are relative to ``folder_id`` and always use POSIX separators
        (``"sub/dir/file.wav"``), matching the portable inventory and manifest
        ``relative_path`` format used for reconciliation.
        """
        paths: set[str] = set()

        def _walk(fid, prefix: tuple) -> None:
            for item in self.iter_folder_items(fid, fields=fields):
                if item.type == "file":
                    paths.add(PurePosixPath(*prefix, item.name).as_posix())
                elif item.type == "folder":
                    _walk(item.id, (*prefix, item.name))

        _walk(folder_id, ())
        return paths

    # -- upload ------------------------------------------------------------

    def upload_file_with_path(
        self,
        local_path: Path,
        parent_folder_id,
        relative_path: Path,
        *,
        chunked_threshold: int = CHUNKED_UPLOAD_THRESHOLD,
    ) -> str:
        """Upload ``local_path`` to ``parent_folder_id`` preserving subfolders.

        Returns one of ``"skipped"``, ``"versioned"``, or ``"uploaded"``:

        * files under ``raw_data/`` are immutable — if already on Box, skip;
        * other (sidecar) files that already exist are re-uploaded as a new
          version, so regenerated CSVs/JSONs refresh on Box;
        * new files use a chunked upload at/above ``chunked_threshold`` bytes,
          otherwise a single-shot upload.
        """
        current_folder_id = parent_folder_id
        if len(relative_path.parts) > 1:
            for folder_name in relative_path.parts[:-1]:
                current_folder_id = self.find_or_create_folder(current_folder_id, folder_name)

        file_name = relative_path.name
        folder_map = self.folder_file_map(current_folder_id)
        existing_id = folder_map.get(file_name)
        is_raw_data = "raw_data" in relative_path.parts

        if existing_id and is_raw_data:
            return "skipped"

        file_size = local_path.stat().st_size

        with open(local_path, "rb") as file_stream:
            if existing_id and not is_raw_data:
                from box_sdk_gen import UploadFileVersionAttributes

                self.client.uploads.upload_file_version(
                    existing_id,
                    attributes=UploadFileVersionAttributes(name=file_name),
                    file=file_stream,
                )
                return "versioned"
            elif file_size >= chunked_threshold:
                self.client.chunked_uploads.upload_big_file(
                    file=file_stream,
                    file_name=file_name,
                    file_size=file_size,
                    parent_folder_id=current_folder_id,
                )
                folder_map.setdefault(file_name, "")
                return "uploaded"
            else:
                result = self.client.uploads.upload_file(
                    attributes={"name": file_name, "parent": {"id": current_folder_id}},
                    file=file_stream,
                )
                try:
                    folder_map[file_name] = result.entries[0].id  # type: ignore[attr-defined]
                except Exception:
                    folder_map.setdefault(file_name, "")
                return "uploaded"

    # -- classification ----------------------------------------------------

    @staticmethod
    def should_upload_file(file_path: Path) -> bool:
        """Skip macOS/Finder junk and internal app files from Box uploads.

        Excludes dotfiles/AppleDouble files, the local ``session.json``, and
        retired per-device ``*_manifest.json`` sidecars that may still exist in
        historical ``raw_data/`` folders.
        """
        name = file_path.name
        if name.startswith(".") or name.startswith("._") or name == "session.json":
            return False
        if name.endswith("_manifest.json") and "raw_data" in file_path.parts:
            return False
        return True
