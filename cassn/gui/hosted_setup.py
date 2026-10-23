"""Responsive hosted setup, including a recoverable empty-project state."""
from PySide6.QtCore import QThread, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QLayout, QLabel, QPushButton, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from cassn.gui.dialogs import WindowDialog as QDialog
from cassn.gui.box_button import BoxConnectButton
from cassn.box.hosted import HostedError, manager_for
from cassn.config import LOCAL_DATA_DIR
from cassn.gui.appearance import workspace_label, workspace_stylesheet
from cassn.lookup_sync import bootstrap_lookup_tables, LookupBootstrapError


class TaskWorker(QThread):
    def __init__(self, action, parent):
        super().__init__(parent)
        self.action = action
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.action()
        except HostedError as exc:
            self.error = str(exc)
        except Exception:
            self.error = "Could not complete this operation. Please retry."


class HostedDisconnectDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Disconnect from Box")
        self.setMinimumWidth(420)
        self.remote_removed = False
        self.disconnected = False
        layout = QVBoxLayout(self)
        self.status = QLabel("Disconnecting this Box connection…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.close_button)
        self.worker = TaskWorker(manager_for(config).disconnect, self)
        self.worker.finished.connect(self.finished_task)
        self.worker.start()

    def finished_task(self):
        worker, self.worker = self.worker, None
        if worker.error:
            self.status.setText(worker.error)
        else:
            self.disconnected = True
            self.remote_removed = worker.result
            self.status.setText("Disconnected from Box." if worker.result else
                "Disconnected on this Mac. The server could not confirm cleanup; the remote session expires within seven days.")
        self.close_button.setEnabled(True)
        worker.deleteLater()

    def reject(self):
        if self.worker is None:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)


class HostedSetupDialog(QDialog):
    def __init__(self, config, parent=None, *, cache_dir=LOCAL_DATA_DIR):
        super().__init__(parent)
        self.project_name = config.profile.upper()
        self.setWindowTitle("CA-SSN Data Manager")
        self.setObjectName("fieldWorkspace")
        self.setStyleSheet(workspace_stylesheet(QApplication.palette()))
        self.setMinimumSize(420, 360)
        self.resize(620, 600)
        self.config, self.cache_dir = config, cache_dir
        self.bootstrap = None
        self.connected = False
        self.worker = None
        self._closing = False
        self._signing_in = False
        self._login_cancel_pending = False
        self._browser_url = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(12)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 12, 12, 16)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setSpacing(16)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)
        layout.addWidget(workspace_label("CALIFORNIA SENTINEL SITE NETWORK", "muted"))
        self.title = workspace_label("Welcome to the CA-SSN Data Manager", "pageTitle", wrap=True)
        layout.addWidget(self.title)
        self.subtitle = workspace_label(f"{self.project_name} project · Connect to Box to load your project settings.", "subtitle", wrap=True)
        layout.addWidget(self.subtitle)

        card = QFrame()
        card.setObjectName("setupCard")
        content = QVBoxLayout(card)
        content.setContentsMargins(20, 20, 20, 20)
        content.setSpacing(14)
        self.intro_heading = workspace_label("Click Connect to Box below to begin.", "sectionTitle", wrap=True)
        content.addWidget(self.intro_heading)
        self.intro_note = workspace_label(
            "Sign in with the Box account approved to access your project folder. "
            "Your browser will open for secure sign-in. This Mac remembers your connection in Keychain.", "muted", wrap=True)
        content.addWidget(self.intro_note)
        self.connect_button = BoxConnectButton()
        self.connect_button.setDefault(True)
        content.addWidget(self.connect_button)
        self.status = workspace_label("Checking your saved connection…", "muted", wrap=True)
        content.addWidget(self.status)
        self.details = workspace_label("", "muted", wrap=True)
        self.details.hide()
        content.addWidget(self.details)
        self.retry_button = QPushButton("Reload project settings")
        self.retry_button.setProperty("role", "primary")
        self.retry_button.setToolTip("Reload project settings from Box using your saved connection. You do not need to sign in again.")
        self.retry_button.hide()
        content.addWidget(self.retry_button)
        self.reopen_browser = QPushButton("Open browser again")
        self.reopen_browser.hide()
        self.reopen_browser.clicked.connect(lambda: self.open_browser(self._browser_url) if self._browser_url else None)
        content.addWidget(self.reopen_browser)
        layout.addWidget(card)
        link_color = "#8ed0bd" if QApplication.palette().color(QPalette.Window).lightness() < 128 else "#216e62"
        help_label = QLabel('Need help with project access? Contact John at '
                            f'<a style="color: {link_color}" href="mailto:jtimperato@ucdavis.edu">jtimperato@ucdavis.edu</a>.')
        help_label.setWordWrap(True)
        help_label.setOpenExternalLinks(True)
        help_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        layout.addWidget(help_label)
        row = QHBoxLayout()
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.hide()
        self.close_button = QPushButton("Close")
        row.addWidget(self.disconnect_button)
        self.cancel_login_button = QPushButton("Cancel sign-in")
        self.cancel_login_button.hide()
        self.cancel_login_button.clicked.connect(self.cancel_login)
        row.addWidget(self.cancel_login_button)
        row.addStretch()
        row.addWidget(self.close_button)
        outer.addLayout(row)
        self.connect_button.clicked.connect(self.connect_box)
        self.retry_button.clicked.connect(self.load_project)
        self.disconnect_button.clicked.connect(self.disconnect_box)
        self.close_button.clicked.connect(self.reject)
        QTimer.singleShot(0, self.load_project)

    def _busy(self, busy):
        for button in (self.connect_button, self.retry_button, self.disconnect_button):
            button.setEnabled(not busy)

    def load_project(self):
        if self.worker:
            return
        self._busy(True)
        self.details.hide()
        self.status.setText(f"Loading {self.project_name} project configuration…")
        def load():
            manager = manager_for(self.config)
            connected = False
            auth_error = None
            try:
                manager.token()
                connected = True
            except HostedError as exc:
                auth_error = str(exc)
                if exc.kind in ("keychain_approval", "storage", "reconnect", "denied"):
                    # Keep recovery visible; do not retry credentials or silently
                    # enter offline mode after an authentication failure.
                    return None, False, auth_error
            try:
                snapshot = bootstrap_lookup_tables(self.config, self.cache_dir)
                return snapshot, connected and snapshot.source == "box", None
            except LookupBootstrapError as exc:
                return None, connected, exc if connected else auth_error
        self.worker = TaskWorker(load, self)
        self.worker.finished.connect(self.loaded)
        self.worker.start()

    def loaded(self):
        worker, self.worker = self.worker, None
        self._busy(False)
        if self._closing:
            worker.deleteLater()
            super().reject()
            return
        if worker.error:
            self.status.setText(worker.error)
        else:
            self.bootstrap, self.connected, error = worker.result
            if self.bootstrap is not None:
                worker.deleteLater()
                self.accept()
                return
            if isinstance(error, LookupBootstrapError):
                if error.reason == "missing_files":
                    self.status.setText(f"Box access confirmed. The {self.project_name} project configuration is incomplete.")
                    self.details.setText("Missing from app_config:\n" + "\n".join("• " + name for name in error.missing_files)
                        + "\n\nJohn needs to publish the project settings. Once they are ready, choose Reload project settings. You do not need to sign in again.")
                elif error.reason == "invalid_files":
                    self.status.setText(f"Box access confirmed. The {self.project_name} project settings need correction.")
                    self.details.setText("The project files downloaded but did not pass validation. Contact John to correct the configuration, then choose Reload project settings. You do not need to sign in again.")
                else:
                    self.status.setText("You’re signed in to Box, but project settings are not ready to load.")
                    self.details.setText("Check your internet connection and try again. If this continues, contact John to check the project configuration and folder access.")
                self.details.show()
            else:
                self.status.setText(error or "Connect to Box to set up this project.")
            has_session = manager_for(self.config).session is not None
            self.disconnect_button.setVisible(has_session)
            self.disconnect_button.setEnabled(has_session)
            self.retry_button.setVisible(self.connected or has_session)
            self.connect_button.setVisible(not self.connected)
            self.connect_button.setDefault(not self.connected)
            self.retry_button.setDefault(self.connected)
            self.intro_heading.setVisible(not self.connected)
            self.intro_note.setVisible(not self.connected)
            if self.connected:
                self.title.setText(f"{self.project_name} project setup")
                self.subtitle.setText("Your Box connection is ready. Load the project settings to continue.")
            self.connect_button.setText("Reconnect to Box" if self.connected else "Connect to Box")
        worker.deleteLater()

    def connect_box(self):
        from cassn.gui.box_connect import LoginWorker
        if self.worker is not None:
            return
        self._busy(True)
        self._signing_in = True
        self._login_cancel_pending = False
        self.details.hide()
        self.connect_button.hide()
        self.intro_heading.setText("Complete sign-in in your browser")
        self.status.setText("Opening Box sign-in in your browser…")
        self.cancel_login_button.setEnabled(True)
        self.cancel_login_button.show()
        self.worker = LoginWorker(self.config, self)
        self.worker.open_url.connect(self.open_browser)
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self.login_finished)
        self.worker.start()

    def open_browser(self, url):
        if not self._signing_in or self._login_cancel_pending:
            return
        self._browser_url = url
        self.reopen_browser.show()
        if not QDesktopServices.openUrl(QUrl(url)):
            self.status.setText("Could not open your browser. Check your default browser and try again.")
            self.worker.requestInterruption()

    def cancel_login(self):
        if self._signing_in and self.worker is not None:
            self._login_cancel_pending = True
            self._browser_url = None
            self.reopen_browser.hide()
            self.cancel_login_button.setEnabled(False)
            self.status.setText("Cancelling sign-in… Please wait.")
            self.worker.requestInterruption()

    def login_finished(self):
        worker, self.worker = self.worker, None
        self._signing_in = False
        self._browser_url = None
        self.reopen_browser.hide()
        self.cancel_login_button.hide()
        self._busy(False)
        worker.deleteLater()
        if self._closing:
            super().reject()
        elif worker.result:
            self.raise_()
            self.activateWindow()
            self.load_project()
        else:
            self.intro_heading.setText("Click Connect to Box below to begin.")
            self.status.setText(worker.error or "Box sign-in cancelled. Connect again when you are ready.")
            self.connect_button.show()

    def disconnect_box(self):
        dialog = HostedDisconnectDialog(self.config, self)
        dialog.exec()
        if dialog.disconnected:
            self.connected = False
            self.details.hide()
            self.status.setText("Disconnected. Connect to Box when you are ready.")
            self.connect_button.setText("Connect to Box")
            self.connect_button.show()
            self.connect_button.setDefault(True)
            self.retry_button.setDefault(False)
            self.intro_heading.show()
            self.intro_note.show()
            self.title.setText("Welcome to the CA-SSN Data Manager")
            self.subtitle.setText(f"{self.project_name} project · Connect to Box to load your project settings.")
            self.disconnect_button.setEnabled(False)
            self.disconnect_button.hide()
            self.retry_button.hide()

    def reject(self):
        if self.worker is not None:
            self._closing = True
            self._busy(True)
            if self._signing_in:
                self.cancel_login()
            else:
                self.status.setText("Finishing the current request before closing…")
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
