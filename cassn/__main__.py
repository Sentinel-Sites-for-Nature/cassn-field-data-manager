"""
Application entry point — ``python -m cassn``.

This is the one place the layers are wired together. It loads the Box
credentials and the lookup tables once, then creates independent deployment
windows through :class:`~cassn.gui.windows.DeploymentWindows` and starts the Qt event
loop. The data, export, and Box-storage layers stay free of module globals and
import-time side effects; this module supplies their inputs explicitly.

Mirrors the original ``main()``: it warns (but does not abort) when the
optional EXIF or Box dependencies are missing, applies the Fusion style, and
exits with the Qt application's return code.
"""

from __future__ import annotations

import sys

from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from cassn.box.auth import BOX_AVAILABLE, load_box_config
from cassn.config import CONFIG_JSON, LOCAL_DATA_DIR, LOCK_DIR
from cassn.profile import HOSTED, PROFILE
from cassn.core.image_metadata import EXIF_AVAILABLE
from cassn.gui.windows import DeploymentWindows, ResourceBusy, ResourceLease
from cassn.gui.activation import ActivationServer, activate_running_instance, activation_name
from cassn.lookup_sync import (
    LookupBootstrapError,
    LookupBootstrapResult,
    bootstrap_lookup_tables,
)


def _lookup_status_message(result: LookupBootstrapResult) -> str:
    """Return one concise terminal summary for the startup lookup snapshot."""
    if result.source == "box":
        count = len(result.synced_files)
        noun = "file" if count == 1 else "files"
        return (
            f"Lookup tables: downloaded {count} {noun} from Box "
            "and validated successfully."
        )
    return "Lookup tables: using the last validated offline cache."


def main(*, application=None) -> None:
    if not EXIF_AVAILABLE:
        print("Warning: PIL/piexif not available. EXIF extraction will be disabled.")
        print("Install with: pip install pillow piexif")

    if not BOX_AVAILABLE:
        print("Warning: box-sdk-gen not available. Box upload will be disabled.")
        print("Install with: pip install box-sdk-gen")
        print("Install dependencies, then use Connect to Box in the app.")

    app = application if application is not None else QApplication(sys.argv)
    app.setStyle("Fusion")

    lock_directory = LOCK_DIR if HOSTED else CONFIG_JSON.parent / "window-locks"
    server_name = activation_name(lock_directory / PROFILE if HOSTED else lock_directory)
    try:
        application_lease = ResourceLease(lock_directory, ["application:" + PROFILE if HOSTED else "application"])
    except ResourceBusy:
        print("CA-SSN is already running; requesting its existing window.", flush=True)
        if activate_running_instance(server_name):
            print("Existing CA-SSN window activated.", flush=True)
            return
        print("Could not contact window activation. The existing instance remains running; no new instance started.", flush=True)
        QMessageBox.information(
            None, "CA-SSN Is Already Running",
            "CA-SSN is still running. Return to its Python window using the Dock or Mission Control.\n\n"
            "Use File → New Window there for another deployment. Wait for any active jobs before closing that app.",
        )
        return
    except OSError as exc:
        QMessageBox.critical(None, "Cannot Start Application", str(exc))
        return

    # Box is the canonical distribution point. Synchronize the complete
    # configuration before strict loading; a validated local snapshot is the
    # only permitted offline fallback.
    try:
        box_config = load_box_config()
    except Exception:
        box_config = None
    hosted_connected = None
    if box_config is not None and box_config.provider == "hosted":
        from cassn.gui.hosted_setup import HostedSetupDialog
        dialog = HostedSetupDialog(box_config)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            application_lease.release()
            return
        bootstrap = dialog.bootstrap
        lookups = bootstrap.lookups
        hosted_connected = dialog.connected
    else:
        if box_config is None:
            from cassn.gui.box_connect import BoxConnectDialog
            dialog = BoxConnectDialog(box_config)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                application_lease.release()
                return
            box_config = dialog.connected_config
        try:
            bootstrap = bootstrap_lookup_tables(box_config, LOCAL_DATA_DIR)
            lookups = bootstrap.lookups
        except LookupBootstrapError as exc:
            from cassn.gui.box_connect import BoxConnectDialog
            QMessageBox.information(None, "Box Connection Required", str(exc))
            dialog = BoxConnectDialog(box_config)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                application_lease.release()
                return
            box_config = dialog.connected_config
            try:
                bootstrap = bootstrap_lookup_tables(box_config, LOCAL_DATA_DIR)
                lookups = bootstrap.lookups
            except LookupBootstrapError as retry_exc:
                QMessageBox.critical(None, "Lookup Configuration Unavailable", str(retry_exc))
                application_lease.release()
                return
    print(_lookup_status_message(bootstrap), flush=True)
    if bootstrap.warning:
        QMessageBox.warning(None, "Using Offline Lookup Cache", bootstrap.warning)

    windows = DeploymentWindows(lookups=lookups, box_config=box_config, lock_directory=lock_directory)
    if hosted_connected is not None:
        windows.box_authenticated = hosted_connected
    windows.new_window(open_sessions=True)
    # The application lease proves no other supported instance owns this name.
    QLocalServer.removeServer(server_name)
    activation_server = None
    try:
        activation_server = ActivationServer(server_name, windows.activate_window, app)
    except OSError as exc:
        print(f"Window reactivation unavailable: {exc}", flush=True)
    try:
        exit_code = app.exec()
    finally:
        if activation_server:
            activation_server.close()
        application_lease.release()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
