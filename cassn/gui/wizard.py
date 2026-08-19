"""
The CA-SSN Field Data Manager main window.

This is a faithful port of the original monolith's ``FieldDataWizard`` with one
structural change: every value the wizard used to reach through a module global
is now injected. The constructor takes a :class:`~cassn.lookups.LookupTables`
and a :class:`~cassn.box.auth.BoxConfig`; the inline file-processing,
metadata-CSV, Wildlife-Insights, QC, and Box primitives that used to live in the
same 5,000-line file are now imported from :mod:`cassn.core`,
:mod:`cassn.export`, and :mod:`cassn.box`.

Behavior is unchanged: the same three tabs, the same session/resume flow, the
same per-file rename/hash/QC pipeline, and the same Box upload + verification
threads. Only the duplication and the import-time side effects are gone.
"""

from __future__ import annotations

import csv
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QMenu,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cassn.box.auth import BOX_AVAILABLE, BoxConfig, get_box_client
from cassn.box.status import has_box_upload_history
from cassn.box.threads import (
    BoxUploadThread,
    BoxVerifyThread,
    FixityCheckThread,
    ProvenanceUploadThread,
)
from cassn.gui.soundhub_thread import SoundHubStageThread, SoundHubUploadThread
from cassn.soundhub.export import read_bd_audio_rows, write_deployment_copy
from cassn.soundhub.staging import flac_available
from cassn.soundhub.upload import (
    boto3_available,
    load_soundhub_config,
    project_prefix,
    staged_objects,
    verify_project,
)
from cassn.gui.wi_split_thread import WISplitThread
from cassn.config import (
    APP_TITLE,
    AUDIO_DEVICE_TYPES,
    AUDIO_FIELDS,
    BOX_TOKEN_FILE,
    BUNDLE_DIR,
    CONFIG_JSON,
    DEVICE_TYPES,
    DOWNLOADERS,
    IMAGE_FIELDS,
    LOCAL_DATA_DIR,
    ORGANIZATIONS,
    SOUNDHUB_DEVICE_TYPE,
)
from cassn.core.audio_metadata import (
    parse_audiomoth_config_file,
    parse_audiomoth_device_id,
    parse_audiomoth_guano,
    parse_audiomoth_recorded_datetime,
    parse_audiomoth_wav_comment,
)
from cassn.core.image_metadata import (
    EXIF_AVAILABLE,
    ReconyxExtractor,
    extract_exif_data,
    extract_reconyx_sequence,
    parse_camera_recorded_datetime,
)
from cassn.core.classification import classify_file, file_size_floor_for, format_size_floor
from cassn.core.file_transfer import (
    FileTransferError,
    copy_file_verified,
    hash_file_with_retries,
)
from cassn.core.inventory import (
    SESSION_SCHEMA_VERSION,
    build_inventory_record,
    build_renamed_filename,
    deduplicate_exact_storage_entries,
    generate_session_summary,
    index_inventory_by_storage_relpath,
    inventory_by_source_relpath,
    inventory_storage_relpath,
    next_plain_file_sequence,
    write_session,
)
from cassn.core.inventory import (
    count_expected_files,
    find_all_sessions_multi as _find_all_sessions_multi,
    reconcile_device_dir,
    sorted_walk,
)
from cassn.core.quality_control import (
    append_qc_report,
    build_box_verification_record,
    check_camera_serial,
    check_expected_count,
    check_file_size_floor,
    check_recording_stop_reasons,
    check_required_lookups,
    check_sequence_integrity,
    is_duplicate_media,
    migrate_qc_sidecars,
    qc_path_for,
    validate_coordinates,
    validate_datetimes,
)
from cassn.export.metadata_csv import write_metadata_outputs
from cassn.export.wildlife_insights import generate_wi_deployments_from_image_csv
from cassn.lookups import LookupTables


# How often, at most, to persist session.json from inside the copy loop. The save
# rewrites the whole cumulative file_inventory, so its cost grows with everything
# ingested so far; a fixed file-count cadence made that quadratic (see save_session).
# A wall-clock interval decouples save frequency from inventory size. Each device
# also forces a save when it completes, so nothing past this interval is ever lost.
SESSION_SAVE_INTERVAL_SEC = 30.0


class FieldDataWizard(QMainWindow):
    """Three-tab wizard for staging, renaming, and uploading a field deployment.

    Lookup tables and Box credentials are injected (``lookups`` and
    ``box_config``) instead of read from module globals, so the same window can
    be constructed in tests or alternate entry points with fabricated data.
    """

    def __init__(self, *, lookups: LookupTables, box_config: BoxConfig):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(900, 700)

        # Injected dependencies (replace the original module globals)
        self.lookups = lookups
        self.box_config = box_config

        # Data storage
        self.metadata = {}
        self.devices = []
        self.staging_root = Path.home() / "Desktop" / "CASSN_field_data_staging"
        # Every staging location the user has used, remembered across launches so
        # the resume dialog can surface partial sessions from any of them (e.g. an
        # SSD/Desktop copy and the external G-DRIVE), not just the active root.
        self.known_staging_roots: list[str] = []
        self.current_deployment_folder = None
        self.file_inventory = []
        self.seen_file_hashes = set()  # session-wide duplicate detection
        self._copy_in_progress = False
        self._session_save_error_shown = False
        self._last_session_save = 0.0  # time.monotonic() of the last session.json write
        self.upload_thread = None
        self.wi_split_thread = None
        self.provenance_thread = None
        self.soundhub_stage_thread = None
        self.soundhub_upload_thread = None
        self.box_verify_thread = None
        self.fixity_thread = None
        self._post_upload_box_summary = ""
        self._post_upload_box_issues = []
        self.fixity_check_run = False  # set True once fixity check completes this session
        self.box_upload_complete = False  # set True once BoxUploadThread finishes successfully

        # Load saved config (staging_root lives alongside the Box credentials)
        self.config_file = CONFIG_JSON
        self.load_config()

        # Check Box authentication
        self.box_authenticated = self.check_box_auth()

        # Build UI
        self.init_ui()

        # Offer to resume an in-progress session
        sessions = self.find_all_sessions()
        if sessions:
            self.offer_resume_session(sessions)

    # ------------------------------------------------------------------
    # Dependency helpers
    # ------------------------------------------------------------------

    def _valid_site_names(self) -> set[str]:
        """Canonical formal site-name set passed to Box threads for validation."""
        return set(self.lookups.site_names)

    # ------------------------------------------------------------------
    # Box authentication / lookup sync
    # ------------------------------------------------------------------

    def check_box_auth(self):
        """Check if Box is authenticated"""
        if not BOX_AVAILABLE:
            return False

        # Look for tokens next to .app (or script folder when not bundled)
        if not BOX_TOKEN_FILE.exists():
            return False

        try:
            client = get_box_client(self.box_config)
            if client:
                client.users.get_user_me()
                return True
        except Exception:
            pass

        return False

    def load_config(self):
        """Load saved configuration"""
        try:
            if self.config_file.exists():
                with open(self.config_file, "r") as f:
                    config = json.load(f)
                    if "staging_root" in config:
                        self.staging_root = Path(config["staging_root"])
                    if isinstance(config.get("known_staging_roots"), list):
                        self.known_staging_roots = [
                            str(p) for p in config["known_staging_roots"]
                        ]
        except Exception:
            pass

    def save_config(self):
        """Save configuration"""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            config = {}
            if self.config_file.exists():
                try:
                    with open(self.config_file, "r") as f:
                        config = json.load(f)
                except Exception:
                    config = {}
            config["staging_root"] = str(self.staging_root)
            # Remember the active root among the known locations so a later launch
            # can still scan it for partial sessions even after the default changes.
            self._remember_staging_root(self.staging_root)
            config["known_staging_roots"] = list(self.known_staging_roots)
            with open(self.config_file, "w") as f:
                json.dump(config, f, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def save_session(self):
        """Persist current app state to session.json in the deployment folder."""
        if self.current_deployment_folder is None:
            return

        device_statuses = {}
        for i in range(self.device_tree.topLevelItemCount()):
            item = self.device_tree.topLevelItem(i)
            if i < len(self.devices):
                device_label = self.devices[i][3]
                device_statuses[device_label] = {
                    "status": item.text(2),
                    "files_copied": item.text(3),
                }

        session = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "saved_at": datetime.now().isoformat(),
            "metadata": self.metadata,
            "devices": self.devices,
            "device_statuses": device_statuses,
            "file_inventory": self.file_inventory,
            "deployment_folder": str(self.current_deployment_folder),
        }

        error = write_session(self.current_deployment_folder, session)
        if error:
            if hasattr(self, "log_text"):
                self.log(f"  ERROR: {error}")
            if not self._session_save_error_shown:
                self._session_save_error_shown = True
                QMessageBox.critical(
                    self,
                    "Session Save Failed",
                    f"{error}\n\nCollection has been stopped so the saved "
                    "inventory cannot drift from the staged files. Check the "
                    "destination drive and retry.",
                )
            return False
        # Any write resets the interval, so a boundary save (e.g. device complete)
        # keeps the copy loop from immediately re-saving on its next tick.
        self._last_session_save = time.monotonic()
        self._session_save_error_shown = False
        return True

    def _remember_staging_root(self, root) -> None:
        """Record ``root`` in the known-staging-roots list (de-duplicated, order-stable)."""
        s = str(root)
        if s not in self.known_staging_roots:
            self.known_staging_roots.append(s)

    def staging_roots(self) -> list:
        """Candidate staging locations to scan for resumable sessions.

        Includes the active ``staging_root``, the built-in Desktop default, and
        every location the user has staged to before (remembered in config), so a
        partial session is found whether it lives on the Desktop/SSD or the
        external G-DRIVE. Deduplicated by resolved path, order-stable, and limited
        to paths that currently exist (a disconnected drive is skipped silently).
        """
        candidates = [
            self.staging_root,
            Path.home() / "Desktop" / "CASSN_field_data_staging",
        ]
        candidates += [Path(p) for p in self.known_staging_roots]

        roots: list = []
        seen: set = set()
        for c in candidates:
            try:
                key = c.expanduser().resolve()
            except Exception:
                key = c
            if key in seen:
                continue
            seen.add(key)
            if c.exists():
                roots.append(c)
        return roots

    def find_all_sessions(self) -> list:
        """Scan every known staging location for session.json files.

        Delegates to core.inventory's multi-root scanner so the resume dialog
        surfaces partial sessions from the Desktop/SSD and the G-DRIVE alike.
        """
        return _find_all_sessions_multi(self.staging_roots())

    def offer_resume_session(self, sessions: list):
        """Show a selection dialog listing all deployments in staging. Lets user open any of them
        — whether to resume in-progress work or to inspect/verify a completed deployment."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Open Deployment")
        dlg.setMinimumWidth(560)
        layout = QVBoxLayout(dlg)
        help_text = (
            "Select a deployment to open. You can resume in-progress sessions or reopen\n"
            "completed deployments to run integrity checks (Verify Box Upload, etc.)."
        )
        layout.addWidget(QLabel(help_text))

        list_widget = QListWidget()
        for s in sessions:
            if s["status"] == "ok":
                meta = s["data"].get("metadata", {})
                statuses = s["data"].get("device_statuses", {})
                total = len(statuses)
                completed = sum(1 for v in statuses.values() if v.get("status") in ("Complete", "Skipped"))
                site_name = meta.get("site_name") or meta.get("reserve_name", "Unknown Site")
                start = meta.get("deployment_start", "?")
                end = meta.get("deployment_end", "?")
                label = f"✓  {site_name}  |  {start} → {end}  |  {completed}/{total} devices done"
            else:
                folder_name = s["path"].parent.name
                label = f"⚠  CORRUPTED — {folder_name}  (truncated file, may be repairable)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, s)
            list_widget.addItem(item)

        list_widget.setCurrentRow(0)
        layout.addWidget(list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Open Selected")
        buttons.button(QDialogButtonBox.Cancel).setText("Start New")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.Accepted:
            return

        selected = list_widget.currentItem()
        if not selected:
            return
        chosen = selected.data(Qt.UserRole)

        if chosen["status"] == "corrupted":
            QMessageBox.warning(
                self,
                "Corrupted Session",
                f"The session file at:\n{chosen['path']}\n\ncould not be parsed "
                f"({chosen['error_msg']}).\n\n"
                "You can try opening it in a text editor to manually repair the JSON, "
                "or discard it and start a new session.",
            )
            return

        self.restore_session(chosen["data"])

    def restore_session(self, session_data):
        """Restore app state from a saved session dict."""
        self.metadata = session_data.get("metadata", {})
        migrated_site_metadata = False
        if "site_short_name" not in self.metadata:
            # One-time migration for sessions created before the canonical site
            # schema. Active runtime paths below use only the canonical keys.
            legacy_short_name = self.metadata.pop("site", "")
            legacy_site_name = self.metadata.pop("reserve_name", "")
            site = (
                self.lookups.site_by_short_name.get(legacy_short_name)
                or self.lookups.site_by_name.get(legacy_site_name)
            )
            self.metadata["site_name"] = site.site_name if site else legacy_site_name
            self.metadata["site_short_name"] = (
                site.site_short_name if site else legacy_short_name
            )
            self.metadata["site_code"] = site.site_code if site else ""
            migrated_site_metadata = True
        self.devices = [tuple(d) for d in session_data.get("devices", [])]
        self.file_inventory = session_data.get("file_inventory", [])
        self.current_deployment_folder = Path(session_data["deployment_folder"])
        try:
            removed_duplicates = deduplicate_exact_storage_entries(self.file_inventory)
        except ValueError as exc:
            QMessageBox.critical(
                self,
                "Session Inventory Conflict",
                f"{exc}\n\nThis is not an exact duplicate and cannot be "
                "repaired automatically. Collection and upload remain blocked.",
            )
            return
        self.seen_file_hashes = {
            e["file_hash_sha256"] for e in self.file_inventory if e.get("file_hash_sha256")
        }

        # Migrate any pre-existing QC sidecars from the deployment root into qc/.
        # No-op for fresh sessions or already-migrated deployments.
        migrate_qc_sidecars(self.current_deployment_folder)

        # Restore Tab 0 UI widgets
        idx = self.org_combo.findText(self.metadata.get("organization", ""))
        if idx >= 0:
            self.org_combo.setCurrentIndex(idx)

        idx = self.site_name_combo.findText(self.metadata.get("site_name", ""))
        if idx >= 0:
            self.site_name_combo.setCurrentIndex(idx)

        self.site_short_name_edit.setText(self.metadata.get("site_short_name", ""))
        self.site_code_edit.setText(self.metadata.get("site_code", ""))

        # Restore the exact new-system round. Older saved sessions did not store
        # the round ID, so map their dates to a Survey123 round without consulting
        # legacy device files.
        round_id = self.metadata.get("deployment_round_id", "")
        event_id = self.metadata.get("deployment_event_id", "")
        for combo_index in range(self.deploy_event_combo.count()):
            event = self.deploy_event_combo.itemData(combo_index)
            if not event:
                continue
            same_round = round_id and event["deployment_round_id"] == round_id
            same_event = event_id and event["deployment_event_id"] == event_id
            same_dates = (
                event["deployment_start"] == self.metadata.get("deployment_start", "")
                and event["deployment_end"] == self.metadata.get("deployment_end", "")
            )
            if same_round or same_event or same_dates:
                self.deploy_event_combo.setCurrentIndex(combo_index)
                self.on_deploy_event_changed(combo_index)
                self.metadata["deployment_round_id"] = event["deployment_round_id"]
                self.metadata["deployment_event_id"] = event["deployment_event_id"]
                break

        start = QDate.fromString(self.metadata.get("deployment_start", ""), "yyyy-MM-dd")
        if start.isValid():
            self.deploy_start_date.setDate(start)
        end = QDate.fromString(self.metadata.get("deployment_end", ""), "yyyy-MM-dd")
        if end.isValid():
            self.deploy_end_date.setDate(end)

        observer = self.metadata.get("observer", "")
        idx = self.observer_combo.findText(observer)
        if idx >= 0:
            self.observer_combo.setCurrentIndex(idx)
            self.observer_other_edit.hide()
        else:
            other_idx = self.observer_combo.findText("Other")
            if other_idx >= 0:
                self.observer_combo.setCurrentIndex(other_idx)
            self.observer_other_edit.setText(observer)
            self.observer_other_edit.show()

        self.clear_all_devices()
        for plot_num, _plot_label, dev_code, _dev_label in self.devices:
            if plot_num in self.device_checkboxes and dev_code in self.device_checkboxes[plot_num]:
                self.device_checkboxes[plot_num][dev_code].setChecked(True)

        # Rebuild collection tree and restore per-device statuses
        self.populate_collection_list()
        statuses = session_data.get("device_statuses", {})
        for i in range(self.device_tree.topLevelItemCount()):
            if i < len(self.devices):
                device_label = self.devices[i][3]
                if device_label in statuses:
                    self.device_tree.topLevelItem(i).setText(2, statuses[device_label].get("status", "Pending"))
                actual_count = sum(
                    1 for entry in self.file_inventory
                    if entry.get("device_label") == device_label
                )
                self.device_tree.topLevelItem(i).setText(3, str(actual_count))

        self.tabs.setCurrentIndex(1)
        self.log(f"Resumed session: {len(self.file_inventory)} files already in inventory.")
        if removed_duplicates:
            self.log(
                f"  Repaired {len(removed_duplicates)} exact duplicate inventory "
                "record(s) left by an interrupted retry."
            )
        if removed_duplicates or migrated_site_metadata:
            self.save_session()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def init_ui(self):
        """Initialize the user interface"""
        # Set window icon
        icon_path = BUNDLE_DIR / "assets" / "cassn_icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # Central widget and main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Logo banner at top - logo + title
        banner_layout = QHBoxLayout()
        banner_layout.setContentsMargins(0, 0, 0, 0)
        banner_layout.addStretch()

        # CA-SSN Logo
        cassn_logo_path = BUNDLE_DIR / "assets" / "cassn_icon.png"
        if cassn_logo_path.exists():
            cassn_label = QLabel()
            cassn_pixmap = QPixmap(str(cassn_logo_path))
            cassn_scaled = cassn_pixmap.scaledToHeight(140, Qt.SmoothTransformation)
            cassn_label.setPixmap(cassn_scaled)
            cassn_label.setAlignment(Qt.AlignCenter)
            banner_layout.addWidget(cassn_label)

        banner_layout.addSpacing(10)

        # App title
        title_label = QLabel("CA-SSN Field Data Manager")
        title_font = QFont("Arial", 24, QFont.Bold)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignCenter)
        banner_layout.addWidget(title_label)

        banner_layout.addStretch()
        main_layout.addLayout(banner_layout)
        main_layout.addSpacing(10)

        # Tab widget
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # Create tabs
        self.create_metadata_tab()
        self.create_collection_tab()
        self.create_review_tab()

    def create_metadata_tab(self):
        """Create the deployment metadata entry tab"""
        tab = QWidget()
        self.tabs.addTab(tab, "1. Deployment Metadata")

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)

        # Title
        title = QLabel("Deployment Metadata")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        layout.addWidget(title)

        # Form layout for metadata fields
        form_group = QGroupBox("Deployment Information")
        form_layout = QFormLayout()

        # Organization
        self.org_combo = QComboBox()
        self.org_combo.addItems(self.lookups.program_config.get("organizations") or ORGANIZATIONS)
        form_layout.addRow("Organization:", self.org_combo)

        # Formal site name (with autocomplete)
        self.site_name_combo = QComboBox()
        self.site_name_combo.setEditable(True)
        site_names = self.lookups.site_names
        self.site_name_combo.addItems(site_names)

        completer = QCompleter(site_names)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.site_name_combo.setCompleter(completer)
        self.site_name_combo.currentTextChanged.connect(self.on_site_changed)
        form_layout.addRow("Site Name:", self.site_name_combo)

        # Stable relational key and deployment-ID token
        self.site_short_name_edit = QLineEdit()
        self.site_short_name_edit.setReadOnly(True)
        form_layout.addRow("Site Short Name:", self.site_short_name_edit)

        # Acronym
        self.site_code_edit = QLineEdit()
        self.site_code_edit.setReadOnly(True)
        form_layout.addRow("Site Code:", self.site_code_edit)

        # Survey123 deployment-round picker. Device availability and placement
        # metadata are scoped to this selection; there is no legacy/manual
        # device-lookup fallback.
        self.deploy_event_combo = QComboBox()
        self.deploy_event_combo.setToolTip(
            "Pick the Survey123 card-return round. The device grid and metadata "
            "will use only placements in that round."
        )
        self.deploy_event_combo.currentIndexChanged.connect(self.on_deploy_event_changed)
        form_layout.addRow("Returned-Card Round:", self.deploy_event_combo)

        # Open placements are useful field context, but they are not actionable
        # downloads and must never supply an invented retrieval/end date.
        self.current_deployment_status_label = QLabel("None recorded")
        self.current_deployment_status_label.setWordWrap(True)
        self.current_deployment_status_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self.current_deployment_status_label.setToolTip(
            "Read-only Survey123 inventory for devices that remain in the field."
        )
        form_layout.addRow("Currently Deployed (read only):", self.current_deployment_status_label)

        # Deployment dates
        self.deploy_start_date = QDateEdit()
        self.deploy_start_date.setCalendarPopup(True)
        self.deploy_start_date.setDate(QDate.currentDate())
        form_layout.addRow("Deployment Event Start Date:", self.deploy_start_date)

        self.deploy_end_date = QDateEdit()
        self.deploy_end_date.setCalendarPopup(True)
        self.deploy_end_date.setDate(QDate.currentDate())
        form_layout.addRow("Deployment Event End Date:", self.deploy_end_date)

        # Observer/Downloader
        self.observer_combo = QComboBox()
        self.observer_combo.setEditable(True)
        self.observer_combo.addItems(self.lookups.program_config.get("observers") or DOWNLOADERS)
        self.observer_combo.currentTextChanged.connect(self.on_observer_changed)
        form_layout.addRow("Who is downloading data?", self.observer_combo)

        # Other observer entry
        self.observer_other_edit = QLineEdit()
        self.observer_other_edit.setPlaceholderText("Enter name...")
        self.observer_other_edit.hide()
        form_layout.addRow("", self.observer_other_edit)

        form_group.setLayout(form_layout)
        layout.addWidget(form_group)

        # Device selection
        device_group = QGroupBox("Select Devices for Each Plot")
        device_layout = QVBoxLayout()

        instructions = QLabel("Check which devices you are downloading data from.")
        device_layout.addWidget(instructions)

        # Device grid — header row only here; plot rows are built/rebuilt
        # dynamically in _rebuild_plot_grid() based on the chosen site.
        self.grid_layout = QGridLayout()

        self.grid_layout.addWidget(QLabel("Plot"), 0, 0)
        col = 1
        for dev_code, dev_name in DEVICE_TYPES.items():
            label = QLabel(dev_name)
            label.setWordWrap(True)
            self.grid_layout.addWidget(label, 0, col)
            col += 1

        self.device_checkboxes = {}
        self.plot_labels = {}

        # Reflect the combo's default selection. addItems() already selected the
        # first site, but on_site_changed was connected afterward, so its
        # site-code + plot-grid setup never ran for that initial value — the user
        # would otherwise have to re-select the site to proceed. Fall back to an
        # empty grid when there is no default (e.g. lookups failed to load).
        if self.site_name_combo.currentText():
            self.on_site_changed(self.site_name_combo.currentText())
        else:
            self._rebuild_plot_grid(site_short_name=None)

        device_layout.addLayout(self.grid_layout)

        # Quick select buttons
        button_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All Devices")
        select_all_btn.clicked.connect(self.select_all_devices)
        clear_all_btn = QPushButton("Clear All Devices")
        clear_all_btn.clicked.connect(self.clear_all_devices)
        button_layout.addWidget(select_all_btn)
        button_layout.addWidget(clear_all_btn)
        button_layout.addStretch()
        device_layout.addLayout(button_layout)

        device_group.setLayout(device_layout)
        layout.addWidget(device_group)

        # Staging location
        staging_group = QGroupBox("Local Staging Location")
        staging_layout = QHBoxLayout()

        self.staging_label = QLineEdit(str(self.staging_root))
        self.staging_label.setReadOnly(True)
        staging_layout.addWidget(self.staging_label)

        change_btn = QPushButton("Change...")
        change_btn.clicked.connect(self.choose_staging_location)
        staging_layout.addWidget(change_btn)

        self.set_default_cb = QCheckBox("Set as default")
        staging_layout.addWidget(self.set_default_cb)

        staging_group.setLayout(staging_layout)
        layout.addWidget(staging_group)

        # Box connection status
        box_group = QGroupBox("Box Cloud Storage")
        box_layout = QVBoxLayout()

        # Box connection status indicator
        box_status_layout = QHBoxLayout()
        box_status_label = QLabel()
        if self.box_authenticated:
            box_status_label.setText("✓ Box Connected")
            box_status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            box_status_label.setText("⚠ Box Not Connected")
            box_status_label.setStyleSheet("color: orange; font-weight: bold;")
        box_status_layout.addWidget(box_status_label)
        box_status_layout.addStretch()
        box_layout.addLayout(box_status_layout)

        upload_note = QLabel(
            "Uploads are started by hand from the Review & Finalize tab — "
            "nothing transfers on its own."
        )
        upload_note.setWordWrap(True)
        box_layout.addWidget(upload_note)

        if not self.box_authenticated:
            auth_note = QLabel("⚠ Box not connected. Run box_auth_setup.py to authenticate.")
            auth_note.setStyleSheet("color: orange;")
            box_layout.addWidget(auth_note)

        box_group.setLayout(box_layout)
        layout.addWidget(box_group)

        # Navigation button
        nav_layout = QHBoxLayout()
        nav_layout.addStretch()
        next_btn = QPushButton("Next: Collect SD Card Data →")
        next_btn.clicked.connect(self.validate_and_next)
        nav_layout.addWidget(next_btn)
        layout.addLayout(nav_layout)

        layout.addStretch()

        scroll.setWidget(scroll_widget)

        tab_layout = QVBoxLayout(tab)
        tab_layout.addWidget(scroll)

    def create_collection_tab(self):
        """Create the SD card data collection tab"""
        tab = QWidget()
        self.tabs.addTab(tab, "2. Collect SD Card Data")

        layout = QVBoxLayout(tab)

        # Title
        title = QLabel("SD Card Data Collection")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        layout.addWidget(title)

        instructions = QLabel(
            "Insert SD card for each device, select it below, and click 'Copy Files'.\n"
            "Files will be automatically renamed and organized in the staging folder."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        # Device tree
        device_group = QGroupBox("Devices")
        device_layout = QVBoxLayout()

        self.device_tree = QTreeWidget()
        self.device_tree.setHeaderLabels(["Plot", "Device Type", "Status", "Files Copied"])
        self.device_tree.setColumnWidth(0, 150)
        self.device_tree.setColumnWidth(1, 200)
        self.device_tree.setColumnWidth(2, 100)
        device_layout.addWidget(self.device_tree)

        device_group.setLayout(device_layout)
        layout.addWidget(device_group)

        # Control buttons
        control_layout = QHBoxLayout()
        self.copy_btn = QPushButton("Select SD Card && Copy Files")
        self.copy_btn.clicked.connect(self.copy_sd_card_data)
        skip_btn = QPushButton("Skip Selected Device")
        skip_btn.clicked.connect(self.skip_device)
        add_device_btn = QPushButton("Add Device…")
        add_device_btn.clicked.connect(self.add_device)
        add_device_btn.setToolTip("Add a new device (plot + type) to this deployment, e.g. a 5th plot or a missed device.")
        control_layout.addWidget(self.copy_btn)
        control_layout.addWidget(skip_btn)
        control_layout.addWidget(add_device_btn)
        control_layout.addStretch()
        layout.addLayout(control_layout)

        # Progress log
        log_group = QGroupBox("Progress Log")
        log_layout = QVBoxLayout()

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)

        log_group.setLayout(log_layout)
        layout.addWidget(log_group)

        # Navigation
        nav_layout = QHBoxLayout()
        back_btn = QPushButton("← Back")
        back_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        next_btn = QPushButton("Next: Review && Finalize →")
        next_btn.clicked.connect(self.validate_and_next_collection)
        nav_layout.addWidget(back_btn)
        nav_layout.addStretch()
        nav_layout.addWidget(next_btn)
        layout.addLayout(nav_layout)

    def create_review_tab(self):
        """Create the review and finalize tab"""
        tab = QWidget()
        self.tabs.addTab(tab, "3. Review & Finalize")

        layout = QVBoxLayout(tab)

        # Title
        title = QLabel("Deployment Summary")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        layout.addWidget(title)

        # Summary text
        summary_group = QGroupBox("Summary")
        summary_layout = QVBoxLayout()

        self.summary_text = QTextEdit()
        self.summary_text.setReadOnly(True)
        summary_layout.addWidget(self.summary_text)

        summary_group.setLayout(summary_layout)
        layout.addWidget(summary_group)

        # Box upload progress (hidden by default)
        self.upload_group = QGroupBox("WI Preparation & Box Upload Progress")
        upload_layout = QVBoxLayout()

        self.upload_progress_bar = QProgressBar()
        self.upload_progress_bar.setMinimum(0)
        self.upload_progress_bar.setMaximum(100)
        upload_layout.addWidget(self.upload_progress_bar)

        self.upload_status_label = QLabel("")
        upload_layout.addWidget(self.upload_status_label)

        # Cancel button (shown only while an upload is running)
        self.cancel_upload_btn = QPushButton("Cancel Upload")
        self.cancel_upload_btn.clicked.connect(self.cancel_upload)
        self.cancel_upload_btn.setToolTip(
            "Stop after the in-flight files finish. Already-uploaded files stay on Box; "
            "re-running the upload resumes from where it stopped."
        )
        self.cancel_upload_btn.hide()
        upload_layout.addWidget(self.cancel_upload_btn)

        self.upload_group.setLayout(upload_layout)
        self.upload_group.hide()
        layout.addWidget(self.upload_group)

        # Box verification progress (hidden by default)
        self.hash_group = QGroupBox("Box Verification Progress")
        hash_layout = QVBoxLayout()

        self.hash_progress_bar = QProgressBar()
        self.hash_progress_bar.setMinimum(0)
        self.hash_progress_bar.setMaximum(100)
        hash_layout.addWidget(self.hash_progress_bar)

        self.hash_status_label = QLabel("")
        hash_layout.addWidget(self.hash_status_label)

        self.hash_group.setLayout(hash_layout)
        self.hash_group.hide()
        layout.addWidget(self.hash_group)

        # SoundHub progress (hidden by default)
        self.soundhub_group = QGroupBox("SoundHub Preparation & Upload Progress")
        soundhub_layout = QVBoxLayout()

        self.soundhub_progress_bar = QProgressBar()
        self.soundhub_progress_bar.setMinimum(0)
        self.soundhub_progress_bar.setMaximum(100)
        soundhub_layout.addWidget(self.soundhub_progress_bar)

        self.soundhub_status_label = QLabel("")
        soundhub_layout.addWidget(self.soundhub_status_label)

        self.cancel_soundhub_btn = QPushButton("Cancel")
        self.cancel_soundhub_btn.clicked.connect(self.cancel_soundhub)
        self.cancel_soundhub_btn.setToolTip(
            "Stop after the current file. Completed work is kept — re-running "
            "resumes from where it stopped."
        )
        self.cancel_soundhub_btn.hide()
        soundhub_layout.addWidget(self.cancel_soundhub_btn)

        self.soundhub_group.setLayout(soundhub_layout)
        self.soundhub_group.hide()
        layout.addWidget(self.soundhub_group)

        # ------------------------------------------------------------------
        # Action menus
        #
        # These were a flat row of six buttons in which a destructive upload sat
        # next to "Exit". Grouping them by what they do makes the destination of
        # each action explicit and leaves room to add platforms without the row
        # growing sideways.
        # ------------------------------------------------------------------
        self.upload_now_btn = self._menu_action(
            "Upload to Box Now",
            self.upload_to_box_manual,
            "Upload this deployment's raw data and metadata to Box.",
            enabled=self.box_authenticated,
        )
        self.soundhub_stage_btn = self._menu_action(
            "Prepare Bird Audio for SoundHub…",
            self.stage_for_soundhub,
            "Convert this deployment's bird (BD) WAVs to FLAC and build the "
            "SoundHub deployment.csv and recording.csv. Source WAVs are not modified.",
        )
        self.soundhub_upload_btn = self._menu_action(
            "Upload Bird Data to SoundHub",
            self.upload_to_soundhub,
            "Push the staged FLAC tree and project manifests to the SoundHub S3 bucket.",
        )

        self.box_verify_btn = self._menu_action(
            "Verify Box Upload",
            self.run_box_verify,
            "List the deployment folder on Box and reconcile against local "
            "inventory. Reports missing or unexpected files.",
            enabled=self.box_authenticated,
        )
        self.fixity_btn = self._menu_action(
            "Verify Box ↔ Local Hashes",
            self.run_fixity_check,
            "End-to-end Box/local verification: compare each local raw file's "
            "SHA-1 against the SHA-1 reported by Box.",
        )
        self.soundhub_verify_btn = self._menu_action(
            "Verify SoundHub Upload",
            self.verify_soundhub,
            "Reconcile the staged SoundHub tree against the S3 bucket. Only "
            "meaningful right after an upload — SoundHub empties the landing "
            "zone once it ingests a submission.",
        )

        self.open_btn = self._menu_action(
            "Open Staging Folder", self.open_staging_folder,
            "Reveal this deployment's folder in Finder.",
        )
        self.open_soundhub_btn = self._menu_action(
            "Open SoundHub Staging Folder", self.open_soundhub_folder,
            "Reveal the local tree that mirrors the SoundHub S3 bucket.",
        )
        self.switch_deployment_btn = self._menu_action(
            "Open Different Deployment…", self.open_different_deployment,
            "Switch to a different deployment in staging — useful for "
            "re-verifying past uploads.",
        )
        self.new_btn = self._menu_action(
            "Start New Deployment", self.start_new_deployment,
            "Clear this session and begin a new deployment.",
        )

        button_layout = QHBoxLayout()
        button_layout.addWidget(self._menu_button("Uploads ▾", [
            self.upload_now_btn,
            None,
            self.soundhub_stage_btn,
            self.soundhub_upload_btn,
        ]))
        button_layout.addWidget(self._menu_button("QC Checks ▾", [
            self.box_verify_btn,
            self.fixity_btn,
            None,
            self.soundhub_verify_btn,
        ]))
        button_layout.addWidget(self._menu_button("Deployment ▾", [
            self.open_btn,
            self.open_soundhub_btn,
            self.switch_deployment_btn,
            None,
            self.new_btn,
        ]))
        button_layout.addStretch()

        self.exit_btn = QPushButton("Exit")
        self.exit_btn.clicked.connect(self.close)
        button_layout.addWidget(self.exit_btn)
        layout.addLayout(button_layout)

    def _menu_action(self, text, handler, tooltip="", *, enabled=True):
        """Build one menu entry.

        Returns a ``QAction`` rather than a button so the rest of the wizard can
        keep calling ``setEnabled`` on these by name exactly as it did when they
        were buttons.
        """
        action = QAction(text, self)
        action.triggered.connect(handler)
        action.setEnabled(enabled)
        if tooltip:
            action.setToolTip(tooltip)
        return action

    def _menu_button(self, label, actions):
        """A dropdown button holding ``actions``; ``None`` inserts a separator."""
        button = QToolButton()
        button.setText(label)
        button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(button)
        menu.setToolTipsVisible(True)
        for action in actions:
            if action is None:
                menu.addSeparator()
            else:
                menu.addAction(action)
        button.setMenu(menu)
        return button

    # =========================================================================
    # Event Handlers
    # =========================================================================

    def on_site_changed(self, text):
        """Update canonical site identifiers and plot names when site changes."""
        site = self.lookups.site_by_name.get(text)
        if site:
            self.site_short_name_edit.setText(site.site_short_name)
            self.site_code_edit.setText(site.site_code)
            self._populate_deploy_events(site.site_short_name)
            return
        self.site_short_name_edit.setText("")
        self.site_code_edit.setText("")
        self._populate_deploy_events("")

    def _populate_deploy_events(self, site_short_name):
        """Show returned-card rounds separately from read-only open placements."""
        if not hasattr(self, "deploy_event_combo"):
            return
        combo = self.deploy_event_combo
        combo.blockSignals(True)
        combo.clear()
        events = self.lookups.returned_rounds(site_short_name)
        for ev in events:
            start, end = ev["deployment_start"], ev["deployment_end"]
            count = ev["device_count"]
            label = f"{start} → {end}  ({count} devices)"
            combo.addItem(label, ev)
        if not events:
            combo.addItem("— No returned-card rounds —", None)
        combo.setCurrentIndex(0)
        combo.blockSignals(False)

        current = self.lookups.current_rounds(site_short_name)
        if current:
            self.current_deployment_status_label.setText(
                "\n".join(
                    f"Since {event['deployment_start']}: "
                    f"{event['device_count']} device"
                    f"{'s' if event['device_count'] != 1 else ''} in the field"
                    for event in current
                )
            )
        else:
            self.current_deployment_status_label.setText("None recorded")
        self.on_deploy_event_changed(0)

    def on_deploy_event_changed(self, index):
        """Auto-fill the deployment start/end date pickers from the chosen event."""
        ev = self.deploy_event_combo.itemData(index)
        if not ev:
            self.lookups.clear_active_deployment_round()
            self._rebuild_plot_grid(self.site_short_name_edit.text() or None)
            return
        self.lookups.activate_deployment_round(ev["deployment_round_id"])
        start = QDate.fromString(ev["deployment_start"], "yyyy-MM-dd")
        if start.isValid():
            self.deploy_start_date.setDate(start)
        if ev["deployment_end"]:
            end = QDate.fromString(ev["deployment_end"], "yyyy-MM-dd")
            if end.isValid():
                self.deploy_end_date.setDate(end)
        self._rebuild_plot_grid(self.site_short_name_edit.text() or None)

    def on_observer_changed(self, text):
        """Show/hide other observer entry"""
        if text == "Other":
            self.observer_other_edit.show()
        else:
            self.observer_other_edit.hide()

    def update_plot_labels(self, site_short_name):
        """Rebuild the plot/device grid for the chosen site."""
        self._rebuild_plot_grid(site_short_name)

    def _rebuild_plot_grid(self, site_short_name):
        """Tear down existing plot rows and rebuild them based on plots.csv for the
        selected site. Sites can have any number of plots (4, 5, etc.); the
        grid sizes itself to the data."""
        # Tear down existing plot rows (everything below row 0, which is the header)
        for plot_label in self.plot_labels.values():
            plot_label.setParent(None)
            plot_label.deleteLater()
        for cb_row in self.device_checkboxes.values():
            for cb in cb_row.values():
                cb.setParent(None)
                cb.deleteLater()
        self.plot_labels = {}
        self.device_checkboxes = {}

        # Determine plot numbers to show
        plot_names_for_site = (
            self.lookups.plot_names.get(site_short_name, {}) if site_short_name else {}
        )
        plot_numbers = sorted(plot_names_for_site.keys())
        available = self.lookups.available_device_keys()

        # Build a row per plot. row_idx maps to grid row (header is row 0).
        for row_idx, plot_num in enumerate(plot_numbers, start=1):
            name = plot_names_for_site.get(plot_num, "")
            label_text = f"Plot {plot_num}: {name}" if name else f"Plot {plot_num}"
            plot_label = QLabel(label_text)
            self.plot_labels[plot_num] = plot_label
            self.grid_layout.addWidget(plot_label, row_idx, 0)

            self.device_checkboxes[plot_num] = {}
            col = 1
            for dev_code in DEVICE_TYPES.keys():
                cb = QCheckBox()
                is_available = (site_short_name, plot_num, dev_code) in available
                cb.setEnabled(is_available)
                cb.setChecked(is_available)
                if not is_available:
                    cb.setToolTip("No Survey123 device placement in the selected round")
                self.device_checkboxes[plot_num][dev_code] = cb
                self.grid_layout.addWidget(cb, row_idx, col, Qt.AlignCenter)
                col += 1

    def select_all_devices(self):
        """Select all device checkboxes"""
        for plot_num in self.device_checkboxes:
            for dev_code in DEVICE_TYPES.keys():
                checkbox = self.device_checkboxes[plot_num][dev_code]
                if checkbox.isEnabled():
                    checkbox.setChecked(True)

    def clear_all_devices(self):
        """Clear all device checkboxes"""
        for plot_num in self.device_checkboxes:
            for dev_code in DEVICE_TYPES.keys():
                self.device_checkboxes[plot_num][dev_code].setChecked(False)

    def choose_staging_location(self):
        """Choose staging directory"""
        directory = QFileDialog.getExistingDirectory(
            self, "Choose Staging Location", str(self.staging_root)
        )
        if directory:
            self.staging_root = Path(directory)
            self._remember_staging_root(self.staging_root)
            self.staging_label.setText(str(self.staging_root))

    def validate_and_next(self):
        """Validate metadata and proceed to collection tab"""
        # Validation
        if not self.site_name_combo.currentText():
            QMessageBox.warning(self, "Missing Information", "Please select a site.")
            return

        if not self.site_short_name_edit.text() or not self.site_code_edit.text():
            QMessageBox.warning(self, "Missing Information", "Please select a valid site.")
            return

        observer = self.observer_combo.currentText()
        if not observer:
            QMessageBox.warning(self, "Missing Information", "Please select who is downloading data.")
            return

        if observer == "Other" and not self.observer_other_edit.text().strip():
            QMessageBox.warning(self, "Missing Information", "Please enter a name for 'Other' option.")
            return

        selected_event = self.deploy_event_combo.currentData()
        if not selected_event:
            QMessageBox.warning(
                self,
                "Missing Deployment Round",
                "This site has no selectable Survey123 deployment round. "
                "The app will not use the old camera or ARU lookup files.",
            )
            return

        # Store metadata
        site_name = self.site_name_combo.currentText()
        site_short_name = self.site_short_name_edit.text()
        site_code = self.site_code_edit.text()

        self.metadata = {
            "organization": self.org_combo.currentText(),
            "site_name": site_name,
            "site_short_name": site_short_name,
            "site_code": site_code,
            "deployment_round_id": selected_event["deployment_round_id"],
            "deployment_event_id": selected_event["deployment_event_id"],
            "deployment_start": self.deploy_start_date.date().toString("yyyy-MM-dd"),
            "deployment_end": self.deploy_end_date.date().toString("yyyy-MM-dd"),
            "observer": self.observer_other_edit.text() if observer == "Other" else observer,
        }

        # Build device list. Iterate over the plot numbers actually shown in the
        # grid (which were sized from plots.csv when the reserve was selected),
        # so a 5-plot reserve produces 5 plot entries and a 4-plot reserve produces 4.
        plot_names = self.lookups.plot_names.get(site_short_name, {}) or {}

        self.devices = []
        for plot_num in sorted(self.device_checkboxes.keys()):
            plot_label = plot_names.get(plot_num) or str(plot_num)

            for dev_code in DEVICE_TYPES.keys():
                if self.device_checkboxes[plot_num][dev_code].isChecked():
                    device_label = f"p{plot_num}_{dev_code}"
                    self.devices.append((plot_num, plot_label, dev_code, device_label))

        if not self.devices:
            QMessageBox.warning(self, "No Devices Selected", "Please select at least one device.")
            return

        # Save config if requested
        if self.set_default_cb.isChecked():
            self.save_config()

        # Create deployment folder
        self.create_deployment_folder()

        # Populate collection list
        self.populate_collection_list()

        # Go to collection tab
        self.tabs.setCurrentIndex(1)

    def create_deployment_folder(self):
        """Create deployment folder in staging location based on deployment end date"""
        folder_name = self.metadata["deployment_event_id"]

        self.current_deployment_folder = self.staging_root / folder_name
        self.current_deployment_folder.mkdir(parents=True, exist_ok=True)

        (self.current_deployment_folder / "raw_data").mkdir(exist_ok=True)

        self.save_session()

    def populate_collection_list(self):
        """Populate device tree with selected devices"""
        self.device_tree.clear()

        for plot_num, plot_label, dev_code, device_label in self.devices:
            item = QTreeWidgetItem([
                f"Plot {plot_num} ({plot_label})",
                DEVICE_TYPES[dev_code],
                "Pending",
                "0",
            ])
            self.device_tree.addTopLevelItem(item)

    def log(self, message):
        """Add message to log. QC warnings/errors are colored red so they stand out."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        full = f"[{timestamp}] {message}"
        # Detect QC findings, errors, and warnings (use red); everything else is default
        stripped = message.lstrip()
        is_alert = (
            "QC [" in message
            or stripped.startswith(("ERROR:", "✗", "⚠", "Warning:", "WARNING:"))
        )
        if is_alert:
            self.log_text.setTextColor(QColor("#cc0000"))
        else:
            self.log_text.setTextColor(QColor("#000000"))
        self.log_text.append(full)
        # Restore default color for any subsequent direct writes to the widget
        self.log_text.setTextColor(QColor("#000000"))
        QApplication.processEvents()

    # ------------------------------------------------------------------
    # SD-card ingest
    # ------------------------------------------------------------------

    def copy_sd_card_data(self):
        """Copy data from SD card for selected device"""
        if self._copy_in_progress:
            return

        selected = self.device_tree.currentItem()
        if not selected:
            QMessageBox.information(self, "No Selection", "Please select a device from the list.")
            return

        index = self.device_tree.indexOfTopLevelItem(selected)
        plot_num, plot_label, dev_code, device_label = self.devices[index]

        # Check if already complete
        if selected.text(2) == "Complete":
            reply = QMessageBox.question(
                self,
                "Already Complete",
                "This device is already complete. Copy again?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return

        # Select SD card
        self._copy_in_progress = True
        self.copy_btn.setEnabled(False)
        sd_path = QFileDialog.getExistingDirectory(
            self, f"Select SD Card for Plot {plot_num} - {DEVICE_TYPES[dev_code]}"
        )

        if not sd_path:
            self._copy_in_progress = False
            self.copy_btn.setEnabled(True)
            return

        # Auto-count the files the copy loop will keep (anything classify_file()
        # doesn't call "other"), used silently as expected_file_count for the
        # post-copy comparison that catches files skipped during copy (hash
        # mismatch, duplicate). count_expected_files keeps this rule identical to
        # the copy loop, so the two only differ on genuine drops.
        try:
            expected_file_count = count_expected_files(sd_path)
        except Exception:
            expected_file_count = None
        if expected_file_count is not None:
            self.log(f"  Auto-counted {expected_file_count} media file(s) in source folder.")

        self.log(f"Starting copy for Plot {plot_num} ({plot_label}) - {DEVICE_TYPES[dev_code]}...")
        self.log(f"Source: {sd_path}")

        # Create device folder
        device_folder = self.current_deployment_folder / "raw_data" / device_label
        device_folder.mkdir(parents=True, exist_ok=True)

        # One persistent ExifTool process for this whole device copy, instead of
        # spawning one per image (slow on a card of thousands). Closed in finally.
        reconyx = None
        try:
            reconyx = ReconyxExtractor().start()
            _files_copied, duplicate_count, hash_mismatch_count = self.process_sd_card_files(
                Path(sd_path), device_folder, plot_num, plot_label, dev_code, device_label,
                reconyx,
            )

            selected.setText(2, "Complete")
            device_entries = [e for e in self.file_inventory if e["device_label"] == device_label]
            total_for_device = len(device_entries)
            selected.setText(3, str(total_for_device))
            if not self.save_session():
                selected.setText(2, "Incomplete")
                raise RuntimeError("Session recovery file could not be saved")

            try:
                write_metadata_outputs(
                    self.current_deployment_folder,
                    self.metadata,
                    self.file_inventory,
                    self.devices,
                    self.lookups,
                    log=self.log,
                )
            except Exception:
                pass  # never block device completion on a CSV write error

            if expected_file_count is not None:
                # Compare against total inventoried files for this device, not files
                # copied in this run alone — otherwise resume runs falsely warn because
                # already-copied files are skipped and don't count toward files_copied.
                if check_expected_count(expected_file_count, total_for_device):
                    append_qc_report(self.current_deployment_folder, "expected_file_count", device_label, "pass",
                                     f"Expected {expected_file_count}, inventory has {total_for_device}")
                else:
                    # Drop-aware: a shortfall is expected when files were intentionally
                    # dropped (byte-identical duplicates, or copies that failed hash
                    # verification after retries). Only raise the modal on an
                    # *unexplained* shortfall; an accounted-for gap is logged, not popped.
                    accounted_drops = duplicate_count + hash_mismatch_count
                    unexplained_shortfall = expected_file_count - total_for_device - accounted_drops
                    if unexplained_shortfall > 0:
                        self.log(f"  ⚠ File count mismatch: expected {expected_file_count}, inventory has {total_for_device}. Check SD card!")
                        append_qc_report(self.current_deployment_folder, "expected_file_count", device_label, "warning",
                                         f"Expected {expected_file_count}, inventory has {total_for_device}")
                        QMessageBox.warning(self, "File Count Mismatch",
                            f"Expected {expected_file_count} files in source but device inventory has {total_for_device}.\n\n"
                            f"Please verify the SD card for missing files before ejecting.")
                    else:
                        self.log(f"  File count: expected {expected_file_count}, inventory {total_for_device} — "
                                 f"{accounted_drops} duplicate(s)/hash-drop(s) dropped, accounted for")
                        append_qc_report(self.current_deployment_folder, "expected_file_count", device_label, "pass",
                                         f"Expected {expected_file_count}, inventory has {total_for_device} "
                                         f"({accounted_drops} intentional drop(s) accounted for)")

            # Required-lookup policy (see check_required_lookups): missing device
            # identity or plot coordinates blocks (QC error); a missing ARU row
            # warns. Values are read from the device's inventory records, which
            # already hold the resolved lookup values.
            if device_entries:
                first_record = device_entries[0]
                dev_type = first_record.get("device_type", "")
                plot_num_val = first_record.get("plot_number", "")
                site_short_name = self.metadata.get("site_short_name", "")
                aru_key = (
                    site_short_name,
                    int(plot_num_val) if str(plot_num_val).isdigit() else plot_num_val,
                    dev_type,
                )
                for check, severity, message in check_required_lookups(
                    device_label,
                    is_audio=dev_type in AUDIO_DEVICE_TYPES,
                    has_device_identity=any(e.get("device_id") for e in device_entries),
                    has_coordinates=bool(
                        first_record.get("latitude") and first_record.get("longitude")
                    ),
                    has_aru_row=aru_key in self.lookups.arus,
                ):
                    append_qc_report(
                        self.current_deployment_folder, check, device_label, severity, message
                    )
                    self.log(f"  {'⚠' if severity == 'warning' else '✗'} {message}")

            # Run QC checks and log findings to qc_report.json
            self._run_device_qc_checks(device_entries, device_label)

            self.log(f"✓ Completed! {total_for_device} files copied. Manifest written.\n")

        except Exception as e:
            self.log(f"✗ Error: {str(e)}\n")
            QMessageBox.critical(self, "Copy Error", f"Error copying files: {str(e)}")
        finally:
            if reconyx is not None:
                reconyx.close()
            self._copy_in_progress = False
            self.copy_btn.setEnabled(True)

    def process_sd_card_files(self, source_dir, dest_dir, plot_num, plot_label, dev_code, device_label, reconyx):
        """Process files from SD card.

        Walks the source tree alphabetically, renames each media/config file to the
        deployment convention via :func:`build_renamed_filename`, copies it,
        verifies the copy by re-hashing (source vs. dest must match), flags small
        files and duplicates, and appends one :func:`build_inventory_record` per
        accepted file. Per-device QC aggregates are written to ``qc_report.json``.
        """
        files_copied = 0
        small_file_count = 0
        hash_mismatch_count = 0
        duplicate_count = 0
        small_file_names: list[str] = []
        small_file_thresholds: Counter[str] = Counter()
        hash_mismatch_names: list[str] = []
        duplicate_names: list[str] = []

        # Reconcile disk against the persisted inventory before resuming: a crash
        # between the 30s session flushes can leave files staged on disk that
        # aren't in file_inventory. Left in place, their source would be re-copied
        # under fresh event numbers, stranding the first copies as orphans and
        # gapping the event sequence. Deleting them makes the inventory
        # authoritative so the re-copy below stays contiguous.
        orphans = reconcile_device_dir(dest_dir, self.file_inventory, device_label)
        if orphans:
            self.log(f"  Removed {len(orphans)} orphaned file(s) from {device_label} left by a prior interrupted copy.")

        # Resume support is keyed by source-relative identity. Existing records
        # whose staged file is missing or size-inconsistent are restored to their
        # original inventory path instead of being assigned a fresh filename.
        inventoried_sources = inventory_by_source_relpath(
            self.file_inventory, device_label
        )
        inventoried_storage = index_inventory_by_storage_relpath(self.file_inventory)
        file_sequence = next_plain_file_sequence(self.file_inventory, device_label)

        # Event counter for sequence-aware camera naming (increments per trigger, not per photo)
        prior_event_nums = [
            entry["sequence_event_num"]
            for entry in self.file_inventory
            if entry["device_label"] == device_label
            and entry.get("sequence_event_num") not in (None, "")
        ]
        event_sequence = (max(prior_event_nums) + 1) if prior_event_nums else 1
        current_event_num = None

        if inventoried_sources:
            self.log(
                f"  {len(inventoried_sources)} files already inventoried for "
                f"{device_label}, resuming..."
            )

        # Deployment end date for media filenames (YYYYMMDD)
        deploy_date = datetime.strptime(self.metadata["deployment_end"], "%Y-%m-%d")
        date_str = deploy_date.strftime("%Y%m%d")

        # Deployment start date for config filenames (YYYYMMDD)
        deploy_start = datetime.strptime(self.metadata["deployment_start"], "%Y-%m-%d")
        start_date_str = deploy_start.strftime("%Y%m%d")

        org = self.metadata["organization"]
        site_short_name = self.metadata["site_short_name"]
        plot_metadata = self.lookups.plot_metadata.get((site_short_name, plot_num), {})

        # Resolve physical device identifier
        if dev_code in AUDIO_DEVICE_TYPES:
            aru_key = (site_short_name, int(plot_num) if str(plot_num).isdigit() else plot_num, dev_code)
            deployment_device_id = self.lookups.arus.get(aru_key, {}).get("device_id", "")
            device_id = parse_audiomoth_device_id(source_dir) or deployment_device_id
            if not device_id:
                self.log(f"  Warning: could not find AudioMoth Device ID in {source_dir}")
        else:
            cam_key = (site_short_name, int(plot_num) if str(plot_num).isdigit() else plot_num, dev_code)
            cam_meta = self.lookups.cameras.get(cam_key, {})
            device_id = cam_meta.get("camera_id", "")
            if not device_id:
                self.log(
                    f"  Warning: camera_id missing for {site_short_name} plot {plot_num} "
                    f"{dev_code} in the selected Survey123 deployment round"
                )

        # CONFIG.TXT — parse once per device folder (audio only; fast, critical for schedule fields)
        config_data = {}
        if dev_code in AUDIO_DEVICE_TYPES:
            config_files = sorted(
                f
                for pattern in ("*CONFIG*.txt", "*CONFIG*.TXT")
                for f in Path(source_dir).glob(pattern)
            )
            if config_files:
                config_data = parse_audiomoth_config_file(config_files[0])

        # Walk in deterministic order (sorted dirs + files) so RECONYX RCNX*.JPG
        # bursts arrive in capture order and per-device event numbering is
        # reproducible regardless of filesystem order (see inventory.sorted_walk).
        for root, dirs, files in sorted_walk(source_dir):
            for filename in files:
                if filename.startswith(".") or filename.startswith("_"):
                    continue

                source_path = Path(root) / filename
                file_ext = source_path.suffix.lower()
                file_type = classify_file(filename)

                if file_type == "other":
                    continue

                # Resume by card-relative path. A healthy staged file is skipped;
                # a missing/truncated one is restored to the exact path already in
                # the inventory and verified against its recorded hashes.
                source_relpath = str(source_path.relative_to(source_dir))
                prior_entry = inventoried_sources.get(source_relpath)
                if prior_entry is not None:
                    prior_dest = (
                        self.current_deployment_folder
                        / inventory_storage_relpath(prior_entry)
                    )
                    expected_size = int(prior_entry.get("file_size_bytes", 0) or 0)
                    staged_is_complete = (
                        prior_dest.is_file()
                        and (not expected_size or prior_dest.stat().st_size == expected_size)
                    )
                    if staged_is_complete:
                        continue

                    expected_sha256 = str(prior_entry.get("file_hash_sha256", ""))
                    expected_sha1 = str(prior_entry.get("file_hash_sha1", ""))
                    if not expected_sha256 or not expected_sha1:
                        raise ValueError(
                            f"Cannot safely restore {source_relpath}: inventory hashes missing"
                        )
                    self.log(
                        f"  Restoring missing/incomplete staged file: "
                        f"{prior_dest.name}"
                    )
                    result = copy_file_verified(
                        source_path,
                        prior_dest,
                        expected_sha256=expected_sha256,
                        expected_sha1=expected_sha1,
                    )
                    if result.attempts > 1:
                        self.log(
                            f"  Note: restored on retry {result.attempts - 1} — "
                            f"{filename}"
                        )
                    files_copied += 1
                    continue

                # For images: read EXIF + Reconyx metadata from source before
                # naming (the sequence position drives the renamed filename).
                exif_data = {}
                reconyx_data = {}
                trigger_type, seq_pos, seq_total = None, None, None
                if file_type == "image":
                    if EXIF_AVAILABLE:
                        exif_data, raw_exif = extract_exif_data(source_path)
                        trigger_type, seq_pos, seq_total = extract_reconyx_sequence(raw_exif)
                    # ExifTool supplies the richer Reconyx fields (temperature,
                    # moon, battery) and, model-agnostically, backstops the
                    # sequence triple for cameras whose MakerNote layout the
                    # fixed-offset byte reader above doesn't match (e.g.
                    # HyperFire 2). Best-effort; absent ExifTool yields {}.
                    reconyx_data = reconyx.parse(source_path)
                    if seq_pos is None and reconyx_data.get("sequence_position") is not None:
                        trigger_type = reconyx_data.get("sequence_trigger_type") or None
                        seq_pos = reconyx_data["sequence_position"]
                        seq_total = reconyx_data["sequence_total"]

                if file_type == "config":
                    # Rename: ORG_SITE_plotN_DEVCODE_YYYYMMDD_CONFIG_01.txt
                    # plotN must be in the name so each plot's CONFIG is uniquely identifiable
                    # (otherwise BD plots 1, 2, 3, 4 all collide on the same filename).
                    new_filename = build_renamed_filename(
                        org, site_short_name, plot_num, dev_code, start_date_str,
                        "CONFIG_01", file_ext
                    )
                    dest_path = dest_dir / new_filename
                elif file_type == "image" and seq_pos is not None:
                    # Sequence-aware naming: {EVENTNO:05d}_{POS}
                    if seq_pos == 1 or current_event_num is None:
                        current_event_num = event_sequence
                        event_sequence += 1
                    seq_str = f"{current_event_num:05d}_{seq_pos}"
                    new_filename = build_renamed_filename(
                        org, site_short_name, plot_num, dev_code, date_str, seq_str, file_ext
                    )
                    dest_path = dest_dir / new_filename
                    file_sequence += 1
                else:
                    # Audio or image without sequence data: standard sequential naming
                    seq_str = f"{file_sequence:05d}"
                    new_filename = build_renamed_filename(
                        org, site_short_name, plot_num, dev_code, date_str, seq_str, file_ext
                    )
                    dest_path = dest_dir / new_filename
                    file_sequence += 1

                # Read/hash retries cover OS-level I/O failures as well as the
                # post-copy verification. Duplicate media is rejected before any
                # destination is touched.
                try:
                    source_result = hash_file_with_retries(source_path)
                    source_hash = source_result.sha256
                    source_sha1 = source_result.sha1
                    if source_result.attempts > 1:
                        self.log(
                            f"  Note: source read recovered on retry "
                            f"{source_result.attempts - 1} — {filename}"
                        )

                    if is_duplicate_media(
                        source_hash, file_type, self.seen_file_hashes
                    ):
                        self.log(
                            f"  Warning: duplicate file skipped — {new_filename} "
                            "matches an already-inventoried file"
                        )
                        duplicate_count += 1
                        duplicate_names.append(new_filename)
                        append_qc_report(
                            self.current_deployment_folder,
                            "duplicate_detection",
                            device_label,
                            "warning",
                            f"Duplicate hash: {new_filename} matches an "
                            "already-inventoried file",
                        )
                        continue

                    relative_dest = dest_path.relative_to(
                        self.current_deployment_folder
                    ).as_posix()
                    prior_at_destination = inventoried_storage.get(relative_dest)
                    if prior_at_destination is not None:
                        if prior_at_destination.get("file_hash_sha256") == source_hash:
                            expected_size = int(
                                prior_at_destination.get("file_size_bytes", 0) or 0
                            )
                            if (
                                not dest_path.is_file()
                                or (expected_size and dest_path.stat().st_size != expected_size)
                            ):
                                copy_file_verified(
                                    source_path,
                                    dest_path,
                                    expected_sha256=source_hash,
                                    expected_sha1=source_sha1,
                                )
                            self.log(
                                f"  Already inventoried at {relative_dest}; "
                                "not creating a duplicate record."
                            )
                            continue
                        raise ValueError(
                            f"Refusing to overwrite inventory-owned destination: "
                            f"{relative_dest}"
                        )

                    copy_result = copy_file_verified(
                        source_path,
                        dest_path,
                        expected_sha256=source_hash,
                        expected_sha1=source_sha1,
                    )
                    if copy_result.attempts > 1:
                        self.log(
                            f"  Note: copy verified on retry "
                            f"{copy_result.attempts - 1} — {filename}"
                        )
                    file_hash, file_sha1 = source_hash, source_sha1
                except FileTransferError as exc:
                    hash_mismatch_count += 1
                    hash_mismatch_names.append(filename)
                    append_qc_report(
                        self.current_deployment_folder,
                        "hash_verification",
                        device_label,
                        "error",
                        str(exc),
                    )
                    self.save_session()
                    raise

                # File size floor: flag suspiciously small files using device-aware thresholds.
                dest_size = dest_path.stat().st_size
                size_hit = check_file_size_floor(dest_size, file_type, dev_code)
                if size_hit is not None:
                    _size_floor, floor_label = size_hit
                    self.log(
                        f"  Warning: {new_filename} is only {format_size_floor(dest_size)} "
                        f"(below {floor_label} floor) — possible corrupted write"
                    )
                    small_file_count += 1
                    small_file_names.append(new_filename)
                    small_file_thresholds[floor_label] += 1

                # Recorded datetime: authoritative time from device
                if file_type == "image":
                    recorded_datetime = parse_camera_recorded_datetime(exif_data)
                elif file_type == "audio":
                    recorded_datetime = parse_audiomoth_recorded_datetime(filename)
                else:
                    recorded_datetime = ""

                # AudioMoth WAV comment (per-file, overrides CONFIG.TXT where redundant)
                wav_data = {}
                if file_type == "audio":
                    wav_data = parse_audiomoth_wav_comment(source_path)
                    # WAV comment is authoritative for device_id if found
                    if wav_data.get("device_id"):
                        device_id = wav_data["device_id"]

                    # GUANO ('guan') chunk — firmware >= 1.10.0 — is the primary
                    # source for the four fields it carries unambiguously. Its
                    # Timestamp already encodes the device's configured UTC offset
                    # (no DST guesswork, unlike the filename); serial, battery, and
                    # temperature come straight from the device. The ICMT/CONFIG
                    # values parsed above remain the fallback for older files.
                    guano_data = parse_audiomoth_guano(source_path)
                    if guano_data.get("recorded_datetime"):
                        recorded_datetime = guano_data["recorded_datetime"]
                    if guano_data.get("device_id"):
                        device_id = guano_data["device_id"]
                    # GUANO is authoritative for hardware identity too, so it
                    # overrides the ICMT-derived make and supplies the model.
                    for _guano_field in ("battery_voltage", "temperature_c", "ARU_make", "ARU_model"):
                        if guano_data.get(_guano_field):
                            wav_data[_guano_field] = guano_data[_guano_field]

                file_info = build_inventory_record(
                    original_filename=filename,
                    new_filename=new_filename,
                    plot_num=plot_num,
                    plot_label=plot_label,
                    dev_code=dev_code,
                    device_label=device_label,
                    device_id=device_id,
                    file_type=file_type,
                    file_size_bytes=dest_size,
                    file_hash_sha256=file_hash,
                    file_hash_sha1=file_sha1,
                    recorded_datetime=recorded_datetime,
                    source_path=str(source_path),
                    source_relpath=source_relpath,
                    plot_metadata=plot_metadata,
                    exif_data=exif_data,
                    config_data=config_data,
                    wav_data=wav_data,
                    reconyx_data=reconyx_data,
                    trigger_type=trigger_type,
                    seq_pos=seq_pos,
                    seq_total=seq_total,
                    event_num=current_event_num,
                    date_installed=self.metadata.get("deployment_start", ""),
                    soundhub_config=self.lookups.soundhub_config,
                )

                if file_type != "config":
                    self.seen_file_hashes.add(file_hash)
                self.file_inventory.append(file_info)
                inventoried_sources[source_relpath] = file_info
                inventoried_storage[file_info["storage_relpath"]] = file_info
                files_copied += 1

                if file_type == "audio":
                    size_mb = dest_size / 1_000_000
                    self.log(f"  [{files_copied}] {new_filename}  ({size_mb:.1f} MB)")
                elif files_copied % 10 == 0:
                    self.log(f"  ...{files_copied} files processed")

                # Persist on a wall-clock interval, not every N files: save_session
                # rewrites the whole cumulative inventory, so a per-file cadence made
                # ingest quadratic. The device-complete save (in the caller) always
                # flushes the tail, bounding crash rework to this interval.
                if time.monotonic() - self._last_session_save >= SESSION_SAVE_INTERVAL_SEC:
                    if not self.save_session():
                        raise FileTransferError(
                            "Collection stopped because session.json could not be saved"
                        )

        expected_floor = (
            file_size_floor_for("audio", dev_code)
            if dev_code in AUDIO_DEVICE_TYPES
            else file_size_floor_for("image", dev_code)
        )
        floor_summary = (
            ", ".join(f"{count} under {floor}" for floor, count in small_file_thresholds.items())
            if small_file_thresholds
            else format_size_floor(expected_floor) if expected_floor is not None else "no configured threshold"
        )

        if small_file_count:
            self.log(f"  Warning: {small_file_count} file(s) below size floor(s): {floor_summary}. Review before proceeding.")

        # Per-file aggregate summaries for qc_report.json
        if files_copied > 0:
            append_qc_report(self.current_deployment_folder, "hash_verification", device_label, "pass",
                             f"{files_copied} file(s) copied; all source/dest hashes matched")
        if hash_mismatch_count == 0 and files_copied == 0:
            # No copy attempts logged — don't write a hash_verification entry
            pass
        if small_file_count > 0:
            append_qc_report(self.current_deployment_folder, "file_size_floor", device_label, "warning",
                             f"{small_file_count} file(s) below device-aware size floor(s): {floor_summary}")
        elif expected_floor is not None:
            append_qc_report(self.current_deployment_folder, "file_size_floor", device_label, "pass",
                             f"All {files_copied} file(s) above {format_size_floor(expected_floor)} threshold")
        else:
            append_qc_report(self.current_deployment_folder, "file_size_floor", device_label, "pass",
                             "No device-specific file-size floor applied")
        if duplicate_count > 0:
            append_qc_report(self.current_deployment_folder, "duplicate_detection", device_label, "warning",
                             f"{duplicate_count} duplicate file(s) skipped")
        else:
            append_qc_report(self.current_deployment_folder, "duplicate_detection", device_label, "pass",
                             "No duplicate file hashes encountered")

        # Return the intentional-drop counts alongside files_copied so the caller's
        # expected-count reconciliation can treat them as accounted-for shortfall.
        return files_copied, duplicate_count, hash_mismatch_count

    def skip_device(self):
        """Skip selected device"""
        selected = self.device_tree.currentItem()
        if not selected:
            QMessageBox.information(self, "No Selection", "Please select a device from the list.")
            return

        index = self.device_tree.indexOfTopLevelItem(selected)
        plot_num, plot_label, dev_code, device_label = self.devices[index]

        selected.setText(2, "Skipped")
        selected.setText(3, "0")
        self.save_session()

        self.log(f"Skipped Plot {plot_num} - {DEVICE_TYPES[dev_code]}\n")

    def add_device(self):
        """Add a new device (plot + device type) to the current deployment.

        Useful when a deployment was originally configured with N plots and a new
        plot or device type needs to be added later (e.g., a 5th experimental plot,
        or a device that was missed during initial setup). The new device appears
        in the collection tree as Pending and can be processed normally.
        """
        if not self.metadata or not self.current_deployment_folder:
            QMessageBox.information(self, "No Deployment",
                "Start or open a deployment before adding a device.")
            return

        site_short_name = self.metadata.get("site_short_name", "")
        plot_names_for_reserve = self.lookups.plot_names.get(site_short_name, {}) or {}

        # Build the dialog
        dlg = QDialog(self)
        dlg.setWindowTitle("Add Device to Deployment")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            f"Add a new device to deployment <b>{self.current_deployment_folder.name}</b>.\n"
            "Select the plot and device type. The new device will appear in the\n"
            "collection list as Pending and can be processed normally."
        ))

        # Plot picker — authoritative plots only. Device placement must also
        # exist in the selected Survey123 round (validated below).
        plot_row = QHBoxLayout()
        plot_row.addWidget(QLabel("Plot:"))
        plot_combo = QComboBox()
        for num in sorted(plot_names_for_reserve.keys()):
            name = plot_names_for_reserve.get(num, "")
            plot_combo.addItem(f"{num} — {name}" if name else str(num), userData=num)
        plot_row.addWidget(plot_combo, stretch=1)
        layout.addLayout(plot_row)

        # Device-type picker
        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Device type:"))
        dev_combo = QComboBox()
        for code, name in DEVICE_TYPES.items():
            dev_combo.addItem(f"{code} — {name}", userData=code)
        dev_row.addWidget(dev_combo, stretch=1)
        layout.addLayout(dev_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Add Device")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.Accepted:
            return

        # Resolve plot number — accept either the combo's userData (known plot)
        # or whatever the user typed (free-form integer for a brand-new plot).
        plot_data = plot_combo.currentData()
        if plot_data is not None:
            try:
                plot_num = int(plot_data)
            except (TypeError, ValueError):
                plot_num = None
        else:
            plot_num = None
        if plot_num is None:
            text = plot_combo.currentText().strip().split()[0] if plot_combo.currentText().strip() else ""
            try:
                plot_num = int(text)
            except (TypeError, ValueError):
                QMessageBox.warning(self, "Invalid Plot",
                    f"'{plot_combo.currentText()}' is not a valid plot number. Enter an integer.")
                return
        if plot_num < 1:
            QMessageBox.warning(self, "Invalid Plot", "Plot number must be 1 or greater.")
            return

        dev_code = dev_combo.currentData()
        device_label = f"p{plot_num}_{dev_code}"

        if (site_short_name, plot_num, dev_code) not in self.lookups.available_device_keys():
            QMessageBox.warning(
                self,
                "Device Not in Deployment Round",
                f"{device_label} has no placement in the selected Survey123 round.",
            )
            return

        # Reject duplicates
        if any(existing[3] == device_label for existing in self.devices):
            QMessageBox.warning(self, "Already Exists",
                f"Device {device_label} is already in this deployment.")
            return

        plot_label = plot_names_for_reserve.get(plot_num) or str(plot_num)

        # Append to devices list and the device tree
        self.devices.append((plot_num, plot_label, dev_code, device_label))
        item = QTreeWidgetItem([
            f"Plot {plot_num} ({plot_label})",
            DEVICE_TYPES[dev_code],
            "Pending",
            "0",
        ])
        self.device_tree.addTopLevelItem(item)
        self.device_tree.setCurrentItem(item)

        self.save_session()
        self.log(f"Added device: Plot {plot_num} ({plot_label}) - {DEVICE_TYPES[dev_code]} → {device_label}\n")

    # ------------------------------------------------------------------
    # Review / metadata generation
    # ------------------------------------------------------------------

    def validate_and_next_collection(self):
        """Validate collection and proceed to review"""
        pending = False
        for i in range(self.device_tree.topLevelItemCount()):
            item = self.device_tree.topLevelItem(i)
            if item.text(2) == "Pending":
                pending = True
                break

        if pending:
            reply = QMessageBox.question(
                self,
                "Incomplete Collection",
                "Some devices are still pending. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return

        # Coordinate validation: latitude/longitude and elevation non-null and
        # within the California study area. All three come from plots.csv, so this
        # catches a bad value baked into the lookup table before it propagates
        # into the metadata CSVs.
        coord_warnings = validate_coordinates(self.file_inventory)
        if coord_warnings:
            detail = "\n".join(f"  {w}" for w in coord_warnings)
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Coordinate Validation Warning")
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setText(
                "Some coordinates or elevations are missing or outside the "
                "California study area. Review before generating CSVs."
            )
            msg_box.setDetailedText(detail)
            msg_box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
            msg_box.button(QMessageBox.Ok).setText("Continue Anyway")
            if msg_box.exec() == QMessageBox.Cancel:
                return
            for w in coord_warnings:
                append_qc_report(self.current_deployment_folder, "coordinate_validation", "", "warning", w)
        else:
            append_qc_report(self.current_deployment_folder, "coordinate_validation", "", "pass",
                             "All coordinates and elevations non-null and within study-area bounds")

        self.generate_metadata_files()
        self.update_review_tab()
        self.tabs.setCurrentIndex(2)
        # Uploads are operator-initiated. Reaching this tab used to start the
        # Box transfer on its own, which gave no chance to review the summary
        # first and no way to choose a destination.

    def generate_metadata_files(self):
        """Generate CSVs, deployment_event_record.json, and WI deployment exports."""
        write_metadata_outputs(
            self.current_deployment_folder,
            self.metadata,
            self.file_inventory,
            self.devices,
            self.lookups,
            log=self.log,
        )
        self._wi_status_lines = generate_wi_deployments_from_image_csv(
            self.current_deployment_folder, log=self.log
        )

        # SoundHub's two CSVs are projections of the audio metadata, so they can
        # be written now — before any upload — and travel to Box with the rest of
        # the deployment. FLAC staging later rewrites the identical files.
        try:
            audio_rows = read_bd_audio_rows(self.current_deployment_folder)
            if audio_rows:
                out = write_deployment_copy(self.current_deployment_folder, audio_rows)
                self.log(f"Generated SoundHub deployment.csv and recording.csv in {out.name}/")
        except Exception as e:
            self.log(f"Warning: could not generate SoundHub CSVs: {e}")

    def update_review_tab(self):
        """Update review summary"""
        summary = []
        summary.append("=" * 60)
        summary.append("DEPLOYMENT SUMMARY")
        summary.append("=" * 60)
        summary.append("")

        summary.append("DEPLOYMENT INFORMATION")
        summary.append("-" * 60)
        summary.append(f"Organization: {self.metadata['organization']}")
        summary.append(f"Site Name: {self.metadata['site_name']}")
        summary.append(f"Site Short Name: {self.metadata['site_short_name']}")
        summary.append(f"Site Code: {self.metadata['site_code']}")
        summary.append(f"Deployment Event Period: {self.metadata['deployment_start']} to {self.metadata['deployment_end']}")
        summary.append(f"Observer: {self.metadata['observer']}")
        summary.append("")

        summary.append("DEVICES COLLECTED")
        summary.append("-" * 60)
        for plot_num, plot_label, dev_code, device_label in self.devices:
            device_files = [f for f in self.file_inventory if f["device_label"] == device_label]
            summary.append(f"  Plot {plot_num} ({plot_label}) - {DEVICE_TYPES[dev_code]}: {len(device_files)} files")
        summary.append("")

        summary.append("FILES PROCESSED")
        summary.append("-" * 60)
        total_files = len(self.file_inventory)
        total_size = sum(f["file_size_bytes"] for f in self.file_inventory)
        total_size_mb = total_size / (1024 * 1024)

        summary.append(f"Total Files: {total_files}")
        summary.append(f"Total Size: {total_size_mb:.2f} MB")
        summary.append("")

        file_types = {}
        for f in self.file_inventory:
            ftype = f["file_type"]
            file_types[ftype] = file_types.get(ftype, 0) + 1

        summary.append("File Types:")
        for ftype, count in file_types.items():
            summary.append(f"  {ftype}: {count}")
        summary.append("")

        summary.append("OUTPUT LOCATION")
        summary.append("-" * 60)
        summary.append(f"Local Staging: {self.current_deployment_folder}")
        summary.append("")
        summary.append("Files generated:")
        summary.append("  - deployment_event_record.json")
        summary.append("  - file_metadata.csv")
        summary.append(f"  - raw_data/ ({total_files} files in device subfolders)")
        summary.append("")

        summary.append("WILDLIFE INSIGHTS")
        summary.append("-" * 60)
        wi_lines = getattr(self, "_wi_status_lines", [])
        if wi_lines:
            for line in wi_lines:
                summary.append(line)
        else:
            summary.append("  Skipped (wi_config.json not found in local_data/)")
        summary.append("")

        summary.append("")
        summary.append("=" * 60)
        summary.append("NEXT STEPS")
        summary.append("=" * 60)
        summary.append("1. Review the files in the staging folder")
        summary.append("2. Verify Box upload completed successfully")
        summary.append("3. Keep a backup of the original SD cards until transfer is verified")

        self.summary_text.setText("\n".join(summary))

    # ------------------------------------------------------------------
    # Box upload + post-upload verification
    # ------------------------------------------------------------------

    def upload_to_box(self):
        """Prepare new data for WI, then upload the deployment folder to Box."""
        if not self.box_authenticated:
            QMessageBox.warning(self, "Box Not Connected", "Please authenticate with Box first.")
            return
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Data", "No deployment data to upload.")
            return
        if self.wi_split_thread and self.wi_split_thread.isRunning():
            return
        if self.upload_thread and self.upload_thread.isRunning():
            return

        self.upload_group.show()
        self.upload_progress_bar.setValue(0)
        self.upload_status_label.setStyleSheet("")

        # Disable lifecycle buttons throughout both preparation and upload.
        self.open_btn.setEnabled(False)
        self.switch_deployment_btn.setEnabled(False)
        self.upload_now_btn.setEnabled(False)
        self.new_btn.setEnabled(False)
        self.exit_btn.setEnabled(False)
        self.cancel_upload_btn.setEnabled(True)
        self.cancel_upload_btn.show()

        if has_box_upload_history(
            self.current_deployment_folder,
            current_upload_complete=self.box_upload_complete,
        ):
            self.log(
                "Prior Box upload activity detected; preserving the existing local "
                "folder layout and starting Box upload."
            )
            self._start_box_upload_thread()
            return

        self.upload_status_label.setText("Planning Wildlife Insights image folders…")
        self.cancel_upload_btn.setText("Cancel Preparation")
        self.wi_split_thread = WISplitThread(
            self.current_deployment_folder,
            self.file_inventory,
        )
        self.wi_split_thread.progress.connect(self._on_wi_split_progress)
        self.wi_split_thread.completed.connect(self._on_wi_split_completed)
        self.wi_split_thread.start()

    def _on_wi_split_progress(self, current: int, total: int, path: str):
        """Show visible progress while local camera folders are prepared."""
        if total <= 0:
            self.upload_progress_bar.setRange(0, 0)
            self.upload_status_label.setText(path or "Planning WI image folders…")
            return

        self.upload_progress_bar.setRange(0, 100)
        percent = int((current / total) * 100)
        self.upload_progress_bar.setValue(percent)
        if path:
            self.upload_status_label.setText(
                f"Preparing WI folders: {current}/{total} ({percent}%) — {path}"
            )
        else:
            self.upload_status_label.setText(
                f"Preparing WI folders: {current}/{total} ({percent}%)"
            )

    def _on_wi_split_completed(self, success: bool, cancelled: bool, message: str):
        """Persist preparation state and gate the Box upload on its result."""
        self.upload_progress_bar.setRange(0, 100)
        self.save_session()
        self.log(message)

        if success:
            append_qc_report(
                self.current_deployment_folder,
                "wi_image_split",
                "",
                "pass",
                message,
            )
            self._start_box_upload_thread()
            return

        append_qc_report(
            self.current_deployment_folder,
            "wi_image_split",
            "",
            "warning" if cancelled else "error",
            message,
        )
        self.open_btn.setEnabled(True)
        self.switch_deployment_btn.setEnabled(True)
        self.upload_now_btn.setEnabled(self.box_authenticated)
        self.new_btn.setEnabled(True)
        self.exit_btn.setEnabled(True)
        self.cancel_upload_btn.hide()
        self.upload_status_label.setText(f"{'⚠' if cancelled else '✗'} {message}")
        self.upload_status_label.setStyleSheet(
            "color: orange; font-weight: bold;"
            if cancelled
            else "color: red; font-weight: bold;"
        )
        if cancelled:
            QMessageBox.information(self, "WI Preparation Cancelled", message)
        else:
            QMessageBox.warning(self, "WI Preparation Failed", message)

    def _start_box_upload_thread(self):
        """Launch the existing uploader after preparation has cleared its gate."""
        self.upload_progress_bar.setRange(0, 100)
        self.upload_progress_bar.setValue(0)
        self.upload_status_label.setText("Starting upload to Box…")
        self.upload_status_label.setStyleSheet("")
        self.cancel_upload_btn.setEnabled(True)
        self.cancel_upload_btn.setText("Cancel Upload")
        self.cancel_upload_btn.show()

        self.upload_thread = BoxUploadThread(
            self.current_deployment_folder,
            self.metadata,
            self.file_inventory,
            box_config=self.box_config,
            valid_site_names=self._valid_site_names(),
        )
        self.upload_thread.progress.connect(self.on_upload_progress)
        self.upload_thread.finished.connect(self.on_upload_finished)
        self.upload_thread.start()

    def cancel_upload(self):
        """Cooperatively cancel WI preparation or the running Box upload."""
        if self.wi_split_thread and self.wi_split_thread.isRunning():
            self.wi_split_thread.cancel()
            self.cancel_upload_btn.setText("Cancelling…")
            self.cancel_upload_btn.setEnabled(False)
            self.upload_status_label.setText(
                "Cancellation requested — finishing the current file move…"
            )
            return
        if self.upload_thread and self.upload_thread.isRunning():
            self.upload_thread.cancel()
            self.cancel_upload_btn.setText("Cancelling…")
            self.cancel_upload_btn.setEnabled(False)
            self.upload_status_label.setText("Cancellation requested — waiting for in-flight files to finish…")

    def upload_to_box_manual(self):
        """Manual Box upload trigger"""
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Data", "No deployment data to upload.")
            return

        self.upload_to_box()

    def on_upload_progress(self, current, total, filename):
        """Update upload progress"""
        percent = int((current / total) * 100)
        self.upload_progress_bar.setValue(percent)
        self.upload_status_label.setText(f"Uploading: {filename} ({current}/{total})")

    def on_upload_finished(self, success, message):
        """Handle upload completion"""
        # Re-enable buttons, hide the cancel button
        self.open_btn.setEnabled(True)
        self.switch_deployment_btn.setEnabled(True)
        self.exit_btn.setEnabled(True)
        self.cancel_upload_btn.hide()

        if success:
            self.upload_progress_bar.setValue(100)
            self.upload_status_label.setText(f"✓ {message}")
            self.upload_status_label.setStyleSheet("color: green; font-weight: bold;")
            self.box_upload_complete = True
            # Update provenance in both metadata CSVs
            self._write_upload_provenance()
        else:
            self.upload_now_btn.setEnabled(True)
            self.new_btn.setEnabled(True)
            self.upload_status_label.setText(f"✗ {message}")
            self.upload_status_label.setStyleSheet("color: red; font-weight: bold;")
            QMessageBox.warning(self, "Upload Failed", message)

    def _write_upload_provenance(self):
        """Set is_uploaded_to_box=True + uploader + timestamp in both metadata CSVs,
        then re-upload the updated CSVs to Box."""
        if not self.current_deployment_folder:
            return
        observer = self.metadata.get("observer", "")
        upload_dt = datetime.now(timezone.utc).isoformat()
        updated_paths = []

        for csv_path, fields in [
            (self.current_deployment_folder / "image_file_metadata.csv", IMAGE_FIELDS),
            (self.current_deployment_folder / "audio_file_metadata.csv", AUDIO_FIELDS),
        ]:
            if not csv_path.exists():
                continue
            try:
                with open(csv_path, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                for row in rows:
                    row["is_uploaded_to_box"] = True
                    row["box_uploader"] = observer
                    row["box_upload_datetime"] = upload_dt
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(rows)
                self.log(f"Updated provenance locally: {csv_path.name}")
                updated_paths.append(csv_path)
            except Exception as e:
                self.log(f"Warning: could not update provenance in {csv_path.name}: {e}")

        # Re-upload the provenance-stamped CSVs to the same Box folder
        deploy_folder_id = self.upload_thread.deploy_folder_id if self.upload_thread else None
        if updated_paths and deploy_folder_id:
            self.provenance_thread = ProvenanceUploadThread(
                updated_paths, deploy_folder_id, box_config=self.box_config
            )
            self.provenance_thread.finished.connect(self._on_provenance_upload_finished)
            self.provenance_thread.start()
        elif updated_paths:
            self.log("Warning: deploy_folder_id not available — provenance CSVs updated locally only")
            self._start_post_upload_verification()
        else:
            self._start_post_upload_verification()

    def _on_provenance_upload_finished(self, success, message):
        """Handle completion of provenance CSV re-upload to Box."""
        if success:
            self.log(f"✓ {message}")
        else:
            self.log(f"Warning: {message}")
        self._start_post_upload_verification()

    def _start_post_upload_verification(self):
        """Run Box file-list verification, then Box/local hash verification."""
        if not self.current_deployment_folder or not self.file_inventory:
            self.upload_now_btn.setEnabled(self.box_authenticated)
            self.new_btn.setEnabled(True)
            return
        if self.box_verify_thread and self.box_verify_thread.isRunning():
            return
        if self.fixity_thread and self.fixity_thread.isRunning():
            return

        self._post_upload_box_summary = ""
        self._post_upload_box_issues = []
        self.hash_group.setTitle("Post-upload Verification Progress")
        self.hash_group.show()
        self.hash_progress_bar.setRange(0, 100)
        self.hash_progress_bar.setValue(0)
        self.hash_status_label.setStyleSheet("")
        self.hash_status_label.setText("Starting post-upload verification...")
        self.upload_status_label.setStyleSheet("")
        self.upload_status_label.setText("Upload complete. Running post-upload verification...")
        self.log("Starting automatic post-upload verification...")

        self.upload_now_btn.setEnabled(False)
        self.box_verify_btn.setEnabled(False)
        self.fixity_btn.setEnabled(False)
        self.new_btn.setEnabled(False)

        self.hash_progress_bar.setValue(5)
        self.hash_status_label.setText("Checking Box file list and folder contents...")
        self.box_verify_thread = BoxVerifyThread(
            self.current_deployment_folder,
            self.file_inventory,
            self.metadata,
            box_config=self.box_config,
            valid_site_names=self._valid_site_names(),
        )
        self.box_verify_thread.finished.connect(self._on_auto_box_verify_finished)
        self.box_verify_thread.start()

    def _on_auto_box_verify_finished(self, ok: bool, summary: str, issues: list):
        """Continue automatic post-upload verification after Box file-list check."""
        self._post_upload_box_summary = summary
        self._post_upload_box_issues = issues or []
        self.log(summary)

        severity = "pass" if ok and not issues else ("warning" if ok else "error")
        append_qc_report(self.current_deployment_folder, "box_verify", "", severity, summary)

        if not ok:
            self._finish_post_upload_verification(False, "Post-upload verification failed during Box file-list check.", [])
            return

        self.hash_progress_bar.setValue(10)
        self.hash_status_label.setText("Box file list checked. Starting Box ↔ local hash verification...")
        self.log("Starting automatic Box ↔ local hash verification...")

        self.fixity_thread = FixityCheckThread(
            self.current_deployment_folder,
            self.file_inventory,
            self.metadata,
            box_config=self.box_config,
            valid_site_names=self._valid_site_names(),
            hash_retry=2,  # automatic run: absorb Box's post-upload SHA-1 lag
        )
        self.fixity_thread.progress.connect(self._on_auto_fixity_progress)
        self.fixity_thread.finished.connect(self._on_auto_fixity_finished)
        self.fixity_thread.start()

    def _on_auto_fixity_progress(self, checked: int, total: int, filename: str):
        if total <= 0:
            if filename:
                self.hash_status_label.setText(filename)
                self.log(f"  Hash verify: {filename}")
            return

        hash_percent = int((checked / total) * 90)
        percent = min(100, 10 + hash_percent)
        self.hash_progress_bar.setValue(percent)
        if filename:
            self.hash_status_label.setText(
                f"Verifying Box ↔ local hashes: {checked}/{total} ({percent}%) — {filename}"
            )
        else:
            self.hash_status_label.setText(
                f"Verifying Box ↔ local hashes: {checked}/{total} ({percent}%)"
            )

        if filename and (checked == 0 or checked % 500 == 0):
            self.log(f"  Hash verify: {checked}/{total} — {filename}")

    def _on_auto_fixity_finished(self, ok: bool, summary: str, issues: list):
        self.fixity_check_run = True
        self.log(summary)
        self.save_session()

        severity = "pass" if ok else "error"
        append_qc_report(self.current_deployment_folder, "file_hash_verification_run", "", severity, summary)
        for issue in issues:
            append_qc_report(
                self.current_deployment_folder,
                "file_hash_" + issue["type"],
                "",
                "error",
                issue["filename"],
            )

        self._finish_post_upload_verification(ok, summary, issues or [])

    def _finish_post_upload_verification(self, hash_ok: bool, hash_summary: str, hash_issues: list):
        box_missing = [i for i in self._post_upload_box_issues if i["type"] == "missing_from_box"]
        box_extra = [i for i in self._post_upload_box_issues if i["type"] == "extra_on_box"]
        has_box_warning = bool(box_extra)
        all_ok = hash_ok and not box_missing and not box_extra and not hash_issues

        # Persist the verification result as an audit artifact (previously this
        # file was referenced by the readiness check but never actually written).
        try:
            record = build_box_verification_record(
                hash_ok=hash_ok,
                hash_summary=hash_summary,
                hash_issues=hash_issues,
                box_summary=self._post_upload_box_summary or "",
                box_issues=self._post_upload_box_issues,
            )
            qc_path_for(self.current_deployment_folder, "box_upload_verification.json").write_text(
                json.dumps(record, indent=2)
            )
        except Exception:
            pass  # never block the UI on writing the audit artifact

        self.upload_now_btn.setEnabled(self.box_authenticated)
        self.box_verify_btn.setEnabled(self.box_authenticated)
        self.fixity_btn.setEnabled(True)
        self.new_btn.setEnabled(True)

        self.hash_progress_bar.setValue(100 if hash_ok else self.hash_progress_bar.value())
        final_summary = (
            "Post-upload verification complete.\n"
            f"Box file list: {self._post_upload_box_summary or 'Not run'}\n"
            f"Box/local hashes: {hash_summary}"
        )
        if all_ok:
            self.hash_status_label.setText("✓ Post-upload verification complete: Box file list and hashes verified.")
            self.hash_status_label.setStyleSheet("color: green; font-weight: bold;")
            self.upload_status_label.setText("✓ Upload and post-upload verification complete.")
            QMessageBox.information(self, "Post-upload Verification Complete", final_summary)
            return

        if has_box_warning and hash_ok and not box_missing and not hash_issues:
            self.hash_status_label.setText("⚠ Post-upload verification complete with Box extras.")
            self.hash_status_label.setStyleSheet("color: orange; font-weight: bold;")
            self.upload_status_label.setText("⚠ Upload verified with Box extras.")
            msg = QMessageBox(self)
            msg.setWindowTitle("Post-upload Verification Warning")
            msg.setIcon(QMessageBox.Warning)
            msg.setText(final_summary)
            msg.setDetailedText(self._format_post_upload_issues(hash_issues))
            msg.exec()
            return

        self.hash_status_label.setText("✗ Post-upload verification found issues.")
        self.hash_status_label.setStyleSheet("color: red; font-weight: bold;")
        self.upload_status_label.setText("✗ Upload completed, but post-upload verification found issues.")
        msg = QMessageBox(self)
        msg.setWindowTitle("Post-upload Verification Issues")
        msg.setIcon(QMessageBox.Critical)
        msg.setText(final_summary)
        msg.setDetailedText(self._format_post_upload_issues(hash_issues))
        msg.exec()

    def _format_post_upload_issues(self, hash_issues: list) -> str:
        lines = []
        box_missing = [i for i in self._post_upload_box_issues if i["type"] == "missing_from_box"]
        box_extra = [i for i in self._post_upload_box_issues if i["type"] == "extra_on_box"]
        hash_mismatches = [i for i in hash_issues if i["type"] == "mismatch"]
        hash_missing = [i for i in hash_issues if i["type"] == "missing"]
        hash_unavailable = [i for i in hash_issues if i["type"] == "box_hash_unavailable"]

        if box_missing:
            lines.append(f"FILES IN INVENTORY BUT MISSING FROM BOX ({len(box_missing)}):")
            lines.extend(f"  ✗ {m['filename']}" for m in box_missing[:100])
        if box_extra:
            if lines:
                lines.append("")
            lines.append(f"FILES ON BOX BUT NOT IN LOCAL INVENTORY ({len(box_extra)}):")
            lines.extend(f"  ⚠ {m['filename']}" for m in box_extra[:100])
        if hash_mismatches:
            if lines:
                lines.append("")
            lines.append(f"HASH MISMATCHES ({len(hash_mismatches)}):")
            lines.extend(f"  ✗ {m['filename']}" for m in hash_mismatches[:100])
        if hash_missing:
            if lines:
                lines.append("")
            lines.append(f"FILES LOCAL BUT MISSING FROM BOX DURING HASH CHECK ({len(hash_missing)}):")
            lines.extend(f"  ✗ {m['filename']}" for m in hash_missing[:100])
        if hash_unavailable:
            if lines:
                lines.append("")
            lines.append(f"BOX HASHES UNAVAILABLE ({len(hash_unavailable)}):")
            lines.extend(f"  ✗ {m['filename']}" for m in hash_unavailable[:100])
        return "\n".join(lines)

    def _run_device_qc_checks(self, device_entries: list, device_label: str):
        """Run QC checks after a device completes. Logs each check (pass + fail) to qc_report.json."""
        if not self.current_deployment_folder:
            return
        deploy_start = self.metadata.get("deployment_start", "")
        deploy_end = self.metadata.get("deployment_end", "")

        check_results = [
            ("sequence_gap", check_sequence_integrity(device_entries, device_label)),
            ("temporal_plausibility", validate_datetimes(device_entries, deploy_start, deploy_end)),
            ("camera_serial_match", check_camera_serial(device_entries, device_label)),
            ("recording_stop_reason", check_recording_stop_reasons(device_entries, device_label)),
        ]

        any_warnings = False
        for check_name, warnings_list in check_results:
            if warnings_list:
                any_warnings = True
                for msg in warnings_list:
                    self.log(f"  QC [{device_label}] {msg}")
                    append_qc_report(self.current_deployment_folder, check_name, device_label, "warning", msg)
            else:
                append_qc_report(self.current_deployment_folder, check_name, device_label, "pass", "OK")

        if not any_warnings:
            self.log(f"  QC [{device_label}] All checks passed.")

    # -- verification (manual) ---------------------------------------------

    def run_fixity_check(self):
        """Compare raw_data/ SHA-1 values against Box-reported SHA-1 values."""
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Session", "Load a session first.")
            return
        if not self.file_inventory:
            QMessageBox.information(self, "No Inventory", "No file inventory in session — run SD card copy first.")
            return
        if self.fixity_thread and self.fixity_thread.isRunning():
            QMessageBox.information(self, "In Progress", "Fixity check is already running.")
            return

        self.fixity_btn.setEnabled(False)
        self.fixity_btn.setText("Checking…")
        self.hash_group.setTitle("Box ↔ Local Hash Verification Progress")
        self.hash_group.show()
        self.hash_progress_bar.setValue(0)
        self.hash_status_label.setStyleSheet("")
        self.hash_status_label.setText("Starting Box ↔ local hash verification...")
        self.log("Starting Box ↔ local hash verification…")

        self.fixity_thread = FixityCheckThread(
            self.current_deployment_folder,
            self.file_inventory,
            self.metadata,
            box_config=self.box_config,
            valid_site_names=self._valid_site_names(),
        )
        self.fixity_thread.progress.connect(self._on_fixity_progress)
        self.fixity_thread.finished.connect(self._on_fixity_finished)
        self.fixity_thread.start()

    def _on_fixity_progress(self, checked: int, total: int, filename: str):
        if total <= 0:
            if filename:
                self.hash_status_label.setText(filename)
                self.log(f"  Hash verify: {filename}")
            return

        percent = int((checked / total) * 100)
        self.hash_progress_bar.setValue(percent)
        if filename:
            self.hash_status_label.setText(
                f"Verifying Box ↔ local hashes: {checked}/{total} ({percent}%) — {filename}"
            )
        else:
            self.hash_status_label.setText(
                f"Verifying Box ↔ local hashes: {checked}/{total} ({percent}%)"
            )

        if filename and (checked == 0 or checked % 500 == 0):
            self.log(f"  Hash verify: {checked}/{total} — {filename}")

    def _on_fixity_finished(self, ok: bool, summary: str, issues: list):
        self.fixity_btn.setEnabled(True)
        self.fixity_btn.setText("Verify Box ↔ Local Hashes")
        self.fixity_check_run = True
        self.log(summary)
        self.save_session()
        self.hash_progress_bar.setValue(100 if ok else self.hash_progress_bar.value())
        self.hash_status_label.setText(("✓ " if ok else "✗ ") + summary)
        self.hash_status_label.setStyleSheet(
            "color: green; font-weight: bold;" if ok else "color: red; font-weight: bold;"
        )

        # Aggregate fixity_check entry covering the whole run
        severity = "pass" if ok else "error"
        append_qc_report(self.current_deployment_folder, "file_hash_verification_run", "", severity, summary)

        if ok:
            QMessageBox.information(self, "File Hashes Verified", summary)
            return

        lines = []
        mismatches = [i for i in issues if i["type"] == "mismatch"]
        missing = [i for i in issues if i["type"] == "missing"]
        unavailable = [i for i in issues if i["type"] == "box_hash_unavailable"]
        if mismatches:
            lines.append(f"HASH MISMATCHES ({len(mismatches)}) — local bytes differ from Box bytes:")
            for m in mismatches:
                lines.append(f"  ✗ {m['filename']}")
        if missing:
            if lines:
                lines.append("")
            lines.append(f"FILES LOCAL BUT MISSING FROM BOX ({len(missing)}):")
            for m in missing:
                lines.append(f"  ✗ {m['filename']}")
        if unavailable:
            if lines:
                lines.append("")
            lines.append(f"BOX HASHES UNAVAILABLE ({len(unavailable)}) — Box file found, but no SHA-1 was returned:")
            for m in unavailable:
                lines.append(f"  ✗ {m['filename']}")

        msg = QMessageBox(self)
        msg.setWindowTitle("Verify Box ↔ Local Hashes — Issues Found")
        msg.setIcon(QMessageBox.Critical)
        msg.setText(summary)
        msg.setDetailedText("\n".join(lines))
        msg.exec()

        for issue in issues:
            append_qc_report(
                self.current_deployment_folder,
                "file_hash_" + issue["type"],
                "",
                "error",
                issue["filename"],
            )

    def run_box_verify(self):
        """Compare Box deployment folder against local inventory."""
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Session", "Load a session first.")
            return
        if not self.file_inventory:
            QMessageBox.information(self, "No Inventory", "No file inventory in session.")
            return
        if not self.box_authenticated:
            QMessageBox.information(self, "Not Authenticated", "Authenticate with Box first.")
            return
        self.box_verify_btn.setEnabled(False)
        self.box_verify_btn.setText("Verifying…")
        self.log("Starting Box upload verification…")
        self.box_verify_thread = BoxVerifyThread(
            self.current_deployment_folder,
            self.file_inventory,
            self.metadata,
            box_config=self.box_config,
            valid_site_names=self._valid_site_names(),
        )
        self.box_verify_thread.finished.connect(self._on_box_verify_finished)
        self.box_verify_thread.start()

    def _on_box_verify_finished(self, ok: bool, summary: str, issues: list):
        self.box_verify_btn.setEnabled(self.box_authenticated)
        self.box_verify_btn.setText("Verify Box Upload")
        self.log(summary)

        # Aggregate result
        severity = "pass" if ok and not issues else ("warning" if ok else "error")
        append_qc_report(self.current_deployment_folder, "box_verify", "", severity, summary)

        if not issues:
            QMessageBox.information(self, "Box Verify Passed", summary)
            return

        missing = [i for i in issues if i["type"] == "missing_from_box"]
        extra = [i for i in issues if i["type"] == "extra_on_box"]
        lines = []
        if missing:
            lines.append(f"FILES IN INVENTORY BUT MISSING FROM BOX ({len(missing)}):")
            for m in missing:
                lines.append(f"  ✗ {m['filename']}")
        if extra:
            if lines:
                lines.append("")
            lines.append(f"FILES ON BOX BUT NOT IN LOCAL INVENTORY ({len(extra)}):")
            for e in extra:
                lines.append(f"  ⚠ {e['filename']}")

        msg = QMessageBox(self)
        msg.setWindowTitle("Box Verify — Issues Found" if missing else "Box Verify — Extras on Box")
        msg.setIcon(QMessageBox.Critical if missing else QMessageBox.Warning)
        msg.setText(summary)
        msg.setDetailedText("\n".join(lines))
        msg.exec()

    # -- deployment lifecycle ----------------------------------------------

    def open_different_deployment(self):
        """Pop the deployment selection dialog mid-session.
        If a session is active, run the pre-departure checklist first, then write
        a session summary, then load the new selection."""
        if self.current_deployment_folder:
            if not self.show_pre_departure_checklist():
                return
            try:
                generate_session_summary(
                    self.current_deployment_folder,
                    self.metadata,
                    self.file_inventory,
                    self.devices,
                )
            except Exception:
                pass

        sessions = self.find_all_sessions()
        if not sessions:
            scanned = "\n".join(str(r) for r in self.staging_roots())
            QMessageBox.information(
                self, "No Deployments Found",
                f"No deployments with session.json found under:\n{scanned}"
            )
            return

        # Reset transient state before loading another deployment
        self.fixity_check_run = False
        self.box_upload_complete = False
        self.offer_resume_session(sessions)

    def open_staging_folder(self):
        """Open staging folder in file explorer"""
        if platform.system() == 'Darwin':
            subprocess.run(['open', str(self.current_deployment_folder)])
        elif platform.system() == 'Windows':
            subprocess.run(['explorer', str(self.current_deployment_folder)])
        else:
            subprocess.run(['xdg-open', str(self.current_deployment_folder)])

    # ------------------------------------------------------------------
    # Wildlife SoundHub
    #
    # Two operator-initiated steps: transcode the bird audio into the local
    # tree that mirrors the S3 bucket, then push it. They are separate because
    # the transcode is long, restartable, and worth reviewing before anything
    # leaves the machine — and because the upload role cannot delete, so a
    # wrong key is permanent.
    # ------------------------------------------------------------------

    def _soundhub_staging_root(self) -> Path:
        return Path(load_soundhub_config()["staging_root"])

    def _soundhub_busy(self) -> bool:
        for thread in (self.soundhub_stage_thread, self.soundhub_upload_thread):
            if thread and thread.isRunning():
                return True
        return False

    def _set_soundhub_actions_enabled(self, enabled: bool):
        for action in (
            self.soundhub_stage_btn,
            self.soundhub_upload_btn,
            self.soundhub_verify_btn,
            self.new_btn,
            self.switch_deployment_btn,
        ):
            action.setEnabled(enabled)
        self.exit_btn.setEnabled(enabled)

    def stage_for_soundhub(self):
        """Transcode this deployment's bird audio to FLAC and build the CSVs."""
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Data", "No deployment data to prepare.")
            return
        if self._soundhub_busy():
            return
        if not flac_available():
            QMessageBox.warning(
                self, "FLAC Encoder Missing",
                "The 'flac' encoder is not installed.\n\n"
                "Install it with:  brew install flac",
            )
            return

        staging_root = self._soundhub_staging_root()
        confirm = QMessageBox.question(
            self, "Prepare Bird Audio for SoundHub",
            f"Convert this deployment's bird (BD) WAVs to FLAC?\n\n"
            f"Staging to: {staging_root}\n\n"
            "Source WAVs are not modified. Bat (BT) audio is not included. "
            "This can take several minutes per plot.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return

        self.soundhub_group.show()
        self.soundhub_progress_bar.setValue(0)
        self.soundhub_status_label.setStyleSheet("")
        self.soundhub_status_label.setText("Converting bird audio to FLAC…")
        self.cancel_soundhub_btn.setText("Cancel")
        self.cancel_soundhub_btn.setEnabled(True)
        self.cancel_soundhub_btn.show()
        self._set_soundhub_actions_enabled(False)

        self.soundhub_stage_thread = SoundHubStageThread(
            self.current_deployment_folder, staging_root
        )
        self.soundhub_stage_thread.progress.connect(self.on_soundhub_progress)
        self.soundhub_stage_thread.completed.connect(self.on_soundhub_stage_complete)
        self.soundhub_stage_thread.start()

    def upload_to_soundhub(self):
        """Push the staged SoundHub tree to the S3 bucket."""
        if self._soundhub_busy():
            return
        if not boto3_available():
            QMessageBox.warning(
                self, "AWS SDK Missing",
                "boto3 is not installed.\n\nInstall it with:  pip install boto3",
            )
            return

        staging_root = self._soundhub_staging_root()
        try:
            settings = load_soundhub_config()
            objects = staged_objects(staging_root, settings)
        except Exception as e:
            QMessageBox.warning(self, "Nothing Staged", str(e))
            return

        total_gb = sum(o["size"] for o in objects) / 1e9
        confirm = QMessageBox.question(
            self, "Upload Bird Data to SoundHub",
            f"Upload {len(objects)} object(s) ({total_gb:.2f} GB) to\n"
            f"s3://{settings['bucket']}/{project_prefix(settings)}/ ?\n\n"
            "The SoundHub account cannot delete objects, so anything sent must "
            "be removed by hand on their side. Objects already in the bucket "
            "are skipped.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.soundhub_group.show()
        self.soundhub_progress_bar.setValue(0)
        self.soundhub_status_label.setStyleSheet("")
        self.soundhub_status_label.setText("Uploading to SoundHub…")
        self.cancel_soundhub_btn.setText("Cancel")
        self.cancel_soundhub_btn.setEnabled(True)
        self.cancel_soundhub_btn.show()
        self._set_soundhub_actions_enabled(False)

        self.soundhub_upload_thread = SoundHubUploadThread(staging_root)
        self.soundhub_upload_thread.progress.connect(self.on_soundhub_progress)
        self.soundhub_upload_thread.completed.connect(self.on_soundhub_upload_complete)
        self.soundhub_upload_thread.start()

    def verify_soundhub(self):
        """Reconcile the staged SoundHub tree against the bucket."""
        if self._soundhub_busy():
            return
        try:
            check = verify_project(self._soundhub_staging_root())
        except Exception as e:
            QMessageBox.warning(self, "Verification Failed", str(e))
            return

        if check["ok"]:
            QMessageBox.information(
                self, "SoundHub Verified",
                f"All {check['checked']} staged object(s) are present in the bucket.",
            )
            return

        QMessageBox.warning(
            self, "SoundHub Verification",
            f"{check['present']}/{check['checked']} staged object(s) found in the "
            f"bucket.\n\n{len(check['missing'])} missing, "
            f"{len(check['mismatched'])} size mismatch(es).\n\n"
            "If SoundHub has already ingested this submission the landing zone "
            "is emptied and this result is expected — check the platform before "
            "re-uploading.",
        )

    def cancel_soundhub(self):
        """Cooperatively cancel the running SoundHub stage or upload."""
        for thread in (self.soundhub_stage_thread, self.soundhub_upload_thread):
            if thread and thread.isRunning():
                thread.cancel()
                self.cancel_soundhub_btn.setText("Cancelling…")
                self.cancel_soundhub_btn.setEnabled(False)
                self.soundhub_status_label.setText(
                    "Cancellation requested — finishing the current file…"
                )
                return

    def on_soundhub_progress(self, current, total, name):
        if total:
            self.soundhub_progress_bar.setValue(int(current / total * 100))
        self.soundhub_status_label.setText(f"{name} ({current}/{total})")

    def on_soundhub_stage_complete(self, success, cancelled, message, result):
        self.cancel_soundhub_btn.hide()
        self._set_soundhub_actions_enabled(True)
        self.log(message)

        if success:
            self.soundhub_progress_bar.setValue(100)
            self.soundhub_status_label.setText(f"✓ {message}")
            self.soundhub_status_label.setStyleSheet("color: green;")
            QMessageBox.information(
                self, "SoundHub Preparation Complete",
                message + "\n\nUse Uploads → Upload Bird Data to SoundHub when ready.",
            )
            return

        self.soundhub_status_label.setText(f"{'⚠' if cancelled else '✗'} {message}")
        self.soundhub_status_label.setStyleSheet(
            "color: orange;" if cancelled else "color: red; font-weight: bold;"
        )
        if not cancelled:
            QMessageBox.warning(self, "SoundHub Preparation Failed", message)

    def on_soundhub_upload_complete(self, success, cancelled, message, result):
        self.cancel_soundhub_btn.hide()
        self._set_soundhub_actions_enabled(True)
        self.log(message)

        if success:
            self.soundhub_progress_bar.setValue(100)
            self.soundhub_status_label.setText(f"✓ {message}")
            self.soundhub_status_label.setStyleSheet("color: green;")
            self._write_soundhub_provenance()
            QMessageBox.information(self, "SoundHub Upload Complete", message)
            return

        self.soundhub_status_label.setText(f"{'⚠' if cancelled else '✗'} {message}")
        self.soundhub_status_label.setStyleSheet(
            "color: orange;" if cancelled else "color: red; font-weight: bold;"
        )
        if not cancelled:
            QMessageBox.warning(self, "SoundHub Upload Failed", message)

    def _write_soundhub_provenance(self):
        """Stamp the SoundHub submission columns on this deployment's BD rows.

        This is the durable record that the submission happened. SoundHub drains
        its S3 landing zone once it ingests a batch, so the bucket cannot be
        asked after the fact — only these columns can answer it.
        """
        if not self.current_deployment_folder:
            return
        csv_path = self.current_deployment_folder / "audio_file_metadata.csv"
        if not csv_path.exists():
            return

        observer = self.metadata.get("observer", "")
        submitted_at = datetime.now(timezone.utc).isoformat()
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                if row.get("device_type") != SOUNDHUB_DEVICE_TYPE:
                    continue
                row["is_submitted_to_soundhub"] = True
                row["soundhub_submitter"] = observer
                row["soundhub_submission_datetime"] = submitted_at
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=AUDIO_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            self.log(f"Recorded SoundHub submission provenance in {csv_path.name}")
        except Exception as e:
            self.log(f"Warning: could not record SoundHub provenance: {e}")

    def open_soundhub_folder(self):
        """Reveal the local tree that mirrors the SoundHub bucket."""
        root = self._soundhub_staging_root()
        root.mkdir(parents=True, exist_ok=True)
        if platform.system() == 'Darwin':
            subprocess.run(['open', str(root)])
        elif platform.system() == 'Windows':
            subprocess.run(['explorer', str(root)])
        else:
            subprocess.run(['xdg-open', str(root)])

    def _build_checklist_items(self) -> list[dict]:
        """
        Evaluate pre-departure checklist. Returns list of
        {"label": str, "ok": bool, "note": str} dicts.
        """
        items = []

        # 1. All devices complete
        all_done = True
        pending_count = 0
        for i in range(self.device_tree.topLevelItemCount()):
            if self.device_tree.topLevelItem(i).text(2) == "Pending":
                all_done = False
                pending_count += 1
        items.append({
            "label": "All devices complete",
            "ok": all_done,
            "note": f"{pending_count} device(s) still pending" if not all_done else "",
        })

        # 2. Hash verification passed. Auto-satisfied by a successful automatic
        # post-upload hash verify: the automatic verify writes its result to
        # qc_report.json under "file_hash_verification_run", so a passing entry
        # there means a hash verification ran AND passed with no errors — no need
        # to also click the manual button. The in-session manual flag still counts.
        hash_verified = self.fixity_check_run
        if not hash_verified:
            hash_qc_path = qc_path_for(self.current_deployment_folder, "qc_report.json")
            if hash_qc_path.exists():
                try:
                    with open(hash_qc_path) as f:
                        hash_qc_data = json.load(f)
                    hash_verified = any(
                        c.get("check") == "file_hash_verification_run"
                        and c.get("severity") == "pass"
                        for c in hash_qc_data.get("current_state", [])
                    )
                except Exception:
                    pass
        items.append({
            "label": "Hash verification passed",
            "ok": hash_verified,
            "note": "Run an upload (auto-verifies) or click 'Verify Box ↔ Local Hashes'" if not hash_verified else "",
        })

        # 3. Box upload verified
        verification_file = qc_path_for(self.current_deployment_folder, "box_upload_verification.json")
        upload_manifest = qc_path_for(self.current_deployment_folder, "box_upload_manifest.json")
        box_ok = self.box_upload_complete or verification_file.exists() or upload_manifest.exists()
        items.append({
            "label": "Box upload completed",
            "ok": box_ok,
            "note": "No upload recorded for this session" if not box_ok else "",
        })

        # 4. No QC errors in qc_report.json
        qc_ok = True
        qc_error_count = 0
        qc_path = qc_path_for(self.current_deployment_folder, "qc_report.json")
        if qc_path.exists():
            try:
                with open(qc_path) as f:
                    qc_data = json.load(f)
                current = qc_data.get("current_state", [])
                errors = [c for c in current if c.get("severity") == "error"]
                qc_error_count = len(errors)
                qc_ok = qc_error_count == 0
            except Exception:
                pass
        items.append({
            "label": "No QC errors recorded",
            "ok": qc_ok,
            "note": f"{qc_error_count} error(s) in qc_report.json" if not qc_ok else "",
        })

        return items

    def show_pre_departure_checklist(self) -> bool:
        """
        Show the pre-departure checklist dialog. Returns True if the user
        acknowledges and wants to proceed, False to cancel.
        Only shown if a session is active.
        """
        if not self.current_deployment_folder:
            return True  # no active session — let action proceed unimpeded

        items = self._build_checklist_items()
        all_ok = all(it["ok"] for it in items)

        dlg = QDialog(self)
        dlg.setWindowTitle("Pre-Departure Checklist")
        dlg.setMinimumWidth(500)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel(
            "<b>Review the following before closing or resetting the session:</b>"
        ))

        list_widget = QListWidget()
        for it in items:
            icon = "✓" if it["ok"] else "⚠"
            text = f"{icon}  {it['label']}"
            if it["note"]:
                text += f"\n     {it['note']}"
            entry = QListWidgetItem(text)
            if not it["ok"]:
                entry.setForeground(QColor("#cc4400"))
            list_widget.addItem(entry)
        list_widget.setSelectionMode(QListWidget.NoSelection)
        layout.addWidget(list_widget)

        if not all_ok:
            ack_cb = QCheckBox("I acknowledge the issues above and want to proceed")
            layout.addWidget(ack_cb)
        else:
            layout.addWidget(QLabel("All checks passed."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Proceed")
        if not all_ok:
            buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            ack_cb.toggled.connect(buttons.button(QDialogButtonBox.Ok).setEnabled)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        accepted = dlg.exec() == QDialog.Accepted
        if accepted:
            failed = [it["label"] for it in items if not it["ok"]]
            if failed:
                append_qc_report(self.current_deployment_folder, "pre_departure", "", "warning",
                                 f"Acknowledged with {len(failed)} unmet item(s): {', '.join(failed)}")
            else:
                append_qc_report(self.current_deployment_folder, "pre_departure", "", "pass",
                                 "All pre-departure items satisfied")
        return accepted

    def closeEvent(self, event):
        """Override close to show pre-departure checklist and write summary when a session is active."""
        if self.wi_split_thread and self.wi_split_thread.isRunning():
            QMessageBox.information(
                self,
                "WI Preparation In Progress",
                "Cancel Wildlife Insights image preparation and wait for it to stop "
                "before closing the application.",
            )
            event.ignore()
            return
        if self.current_deployment_folder:
            if not self.show_pre_departure_checklist():
                event.ignore()
                return
            try:
                summary_path = generate_session_summary(
                    self.current_deployment_folder,
                    self.metadata,
                    self.file_inventory,
                    self.devices,
                )
                self.log(f"Session summary written to: {summary_path.name}")
            except Exception:
                pass
        event.accept()

    def start_new_deployment(self):
        """Reset for new deployment"""
        if self.current_deployment_folder and not self.show_pre_departure_checklist():
            return
        reply = QMessageBox.question(
            self, "Start New Deployment",
            "This will reset the wizard. Are you sure?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # Write session summary before clearing state
            if self.current_deployment_folder:
                try:
                    generate_session_summary(
                        self.current_deployment_folder,
                        self.metadata,
                        self.file_inventory,
                        self.devices,
                    )
                except Exception:
                    pass
            # Clean up session file before clearing state
            if self.current_deployment_folder:
                session_file = self.current_deployment_folder / "session.json"
                session_file.unlink(missing_ok=True)

            self.metadata = {}
            self.devices = []
            self.file_inventory = []
            self.seen_file_hashes = set()
            self.current_deployment_folder = None
            self.fixity_check_run = False
            self.box_upload_complete = False

            # Reset UI
            self.site_name_combo.setCurrentIndex(-1)
            self.site_short_name_edit.clear()
            self.site_code_edit.clear()
            self.deploy_start_date.setDate(QDate.currentDate())
            self.deploy_end_date.setDate(QDate.currentDate())
            self.observer_combo.setCurrentIndex(-1)
            self.clear_all_devices()
            self.device_tree.clear()
            self.log_text.clear()
            self.summary_text.clear()
            self.upload_group.hide()
            if hasattr(self, "hash_group"):
                self.hash_group.hide()

            self.tabs.setCurrentIndex(0)
