"""Read the names of an event's originals on Box without changing the event."""
import json

from PySide6.QtCore import QObject, QRunnable, Signal

from cassn.box.auth import get_box_client
from cassn.box.client import BoxStorage
from cassn.box.publication import STATE_PATH
from cassn.core.classification import sanitize_box_folder_name


def event_paths_on_box(storage, root_id, folder, metadata, saved_id):
    fields = ["id", "type", "name"]
    parent = root_id
    for name in (metadata["deployment_event_end_date"][:4],
                 sanitize_box_folder_name(metadata["site_name"]), folder.name):
        parent = storage.find_child_folder(parent, name, fields=fields)
        if not parent:
            if saved_id:
                raise ValueError("The saved Box event folder could not be found.")
            return set()
    if saved_id and saved_id != parent:
        raise ValueError("The Box event folder differs from the saved upload destination.")
    raw = storage.find_child_folder(parent, "raw_data", fields=fields)
    if not raw:
        return set()
    return {"raw_data/" + path for path in storage.collect_file_paths(raw, fields=fields, use_markers=True)}


class _CountSignals(QObject):
    result = Signal(object, object, str)


class BoxSessionCount(QRunnable):
    def __init__(self, folder, metadata, config):
        super().__init__()
        self.folder = folder
        self.metadata = dict(metadata)
        self.config = config
        self.signals = _CountSignals()

    def run(self):
        try:
            state_file = self.folder / STATE_PATH
            saved_id = json.loads(state_file.read_text()).get("folder_id") if state_file.exists() else None
            client = get_box_client(self.config)
            if not client:
                raise ValueError("Box sign-in is required.")
            paths = event_paths_on_box(BoxStorage(client), self.config.field_data_folder_id,
                                       self.folder, self.metadata, saved_id)
        except Exception:
            self.signals.result.emit(self, None, "Could not read the event folder on Box.")
            return
        self.signals.result.emit(self, paths, "")
