"""Responsive hosted setup, including a recoverable empty-project state."""
from PySide6.QtCore import QSize, Signal, QThread, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QLayout, QLabel, QPushButton, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from cassn.gui.dialogs import WindowDialog as QDialog
from cassn.gui.box_button import BoxConnectButton
from cassn.box.hosted import HostedError, manager_for
from cassn.config import LOCAL_DATA_DIR
from cassn.gui.appearance import style_cancel_button, ARTWORK, platform_logo, workspace_label, workspace_stylesheet
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
    loading_settings = Signal(bool)

    def __init__(self, config, parent=None, *, cache_dir=LOCAL_DATA_DIR):
        super().__init__(parent)
        self.project_name = config.profile.upper()
        self.setWindowTitle("CA-SSN Data Manager")
        self.setObjectName("fieldWorkspace")
        self.setStyleSheet(workspace_stylesheet(QApplication.palette()))
        self.setMinimumSize(420, 360)
        self.resize(540, 380)
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
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)
        self.title = workspace_label("Opening your project…", "pageTitle", wrap=True)
        layout.addWidget(self.title)

        organization = QFrame()
        organization.setObjectName("sidebarOrganization")
        brand = QVBoxLayout(organization)
        brand.setContentsMargins(16, 10, 16, 14)
        brand.setSpacing(6)
        brand.addWidget(workspace_label("Organization", "organizationCaption"))
        if config.profile == "csu":
            name, artwork = "California State University", "csu-wordmark-stacked.svg"
            size = QSize(205, 70)
        else:
            name, artwork = "University of California Nature", "uc-nature-lockup-hori-black.png"
            size = QSize(190, 46)
        self.organization_logo = QLabel(name)
        self.organization_logo.setAccessibleName(name)
        logo = QIcon(str(ARTWORK / artwork)).pixmap(size, self.devicePixelRatioF())
        if not logo.isNull():
            self.organization_logo.setPixmap(logo)
        brand.addWidget(self.organization_logo, alignment=Qt.AlignLeft)
        layout.addWidget(organization)
        self.intro_note = workspace_label("", "muted", wrap=True)
        self.intro_note.hide()
        layout.addWidget(self.intro_note)
        self.connection_status = QWidget()
        self.connection_status.setAccessibleName("Checking your Box connection")
        checking = QHBoxLayout(self.connection_status)
        checking.setContentsMargins(0, 0, 0, 0)
        checking.setSpacing(4)
        self.connection_check = workspace_label("✓", "connected")
        self.connection_check.hide()
        checking.addWidget(self.connection_check)
        self.connection_lead = workspace_label("Checking your", "contextTitle")
        checking.addWidget(self.connection_lead)
        checking.addWidget(platform_logo("box", height=18))
        self.connection_tail = workspace_label("connection…", "contextTitle")
        checking.addWidget(self.connection_tail)
        checking.addStretch()
        layout.addWidget(self.connection_status)
        self.loading_settings.connect(self._show_loading_settings)
        self.status = workspace_label("", "muted", wrap=True)
        self.status.hide()
        settings_row = QHBoxLayout()
        self.settings_check = workspace_label("✓", "connected")
        self.settings_check.hide()
        settings_row.addWidget(self.settings_check, alignment=Qt.AlignTop)
        settings_row.addWidget(self.status, 1)
        layout.addLayout(settings_row)
        self.open_timer = QTimer(self)
        self.open_timer.setSingleShot(True)
        self.open_timer.timeout.connect(self.accept)
        self.details = workspace_label("", "muted", wrap=True)
        self.details.hide()
        layout.addWidget(self.details)
        self.connect_button = BoxConnectButton()
        self.connect_button.setAccessibleDescription("Sign in to Box to load your project settings.")
        self.connect_button.setDefault(True)
        self.connect_button.hide()
        layout.addWidget(self.connect_button)
        self.retry_button = QPushButton("Reload project settings")
        self.retry_button.setProperty("role", "primary")
        self.retry_button.setToolTip("Reload project settings from Box using your saved connection. You do not need to sign in again.")
        self.retry_button.hide()
        layout.addWidget(self.retry_button)
        self.reopen_browser = QPushButton("Open browser again")
        self.reopen_browser.hide()
        self.reopen_browser.clicked.connect(lambda: self.open_browser(self._browser_url) if self._browser_url else None)
        layout.addWidget(self.reopen_browser)
        link_color = "#8ed0bd" if QApplication.palette().color(QPalette.Window).lightness() < 128 else "#216e62"
        help_label = QLabel('Having trouble connecting to Box or loading your project settings? Contact John at '
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
        self.cancel_login_button = style_cancel_button(QPushButton("Cancel sign-in"))
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

    def _show_loading_settings(self, connected):
        if self._closing:
            return
        self.connection_check.setVisible(connected)
        self.connection_lead.setVisible(False)
        state = "connected" if connected else "connection unavailable"
        self.connection_tail.setText(state)
        self.connection_status.setAccessibleName(f"Box {state}")
        self.status.setText("Loading your project settings…")
        self.status.show()

    def _show_sign_in(self):
        self.connection_status.hide()
        self.title.setText("Connect your Box account")
        self.intro_note.setText(
            "Sign in with the account that has access to your project folder. "
            "The app will load your settings. You can upload staged files to Box later."
        )
        self.intro_note.show()
        self.connect_button.show()
        self.connect_button.setDefault(True)

    def _busy(self, busy):
        for button in (self.connect_button, self.retry_button, self.disconnect_button):
            button.setEnabled(not busy)

    def load_project(self, *, require_live=False):
        if self.worker:
            return
        # Retries after browser sign-in must also require a fresh Box snapshot.
        self._require_live = getattr(self, "_require_live", False) or require_live
        require_live = self._require_live
        self._busy(True)
        self.details.hide()
        self.title.setText("Opening your project…")
        self.intro_note.hide()
        self.connect_button.hide()
        self.retry_button.hide()
        self.status.hide()
        self.settings_check.hide()
        self.connection_check.hide()
        self.connection_lead.show()
        self.connection_tail.setText("connection…")
        self.connection_status.setAccessibleName("Checking your Box connection")
        self.connection_status.show()
        def load():
            manager = manager_for(self.config)
            connected = False
            auth_error = None
            try:
                manager.token()
                connected = True
            except HostedError as exc:
                auth_error = None if exc.kind == "reconnect" and manager.session is None else str(exc)
                if exc.kind in ("keychain_approval", "storage", "reconnect", "denied"):
                    # Keep recovery visible; do not retry credentials or silently
                    # enter offline mode after an authentication failure.
                    return None, False, auth_error
            self.loading_settings.emit(connected)
            try:
                snapshot = bootstrap_lookup_tables(self.config, self.cache_dir)
                if (require_live or connected) and snapshot.source != "box":
                    # A completed browser sign-in is not a completed project
                    # download. Keep the operator here to reload settings,
                    # retaining the successful connection for that retry.
                    return None, connected, (LookupBootstrapError(
                        "Box sign-in succeeded, but project settings could not be refreshed.")
                        if connected else auth_error)
                return snapshot, connected, None
            except LookupBootstrapError as exc:
                return None, connected, exc if connected else auth_error
        self.worker = TaskWorker(load, self)
        self.worker.finished.connect(self.loaded)
        self.worker.start()

    def loaded(self):
        self.status.show()
        worker, self.worker = self.worker, None
        self._busy(False)
        if self._closing:
            worker.deleteLater()
            super().reject()
            return
        if worker.error:
            if self.connected:
                self.title.setText("Project settings need attention")
                self.retry_button.show()
            else:
                self._show_sign_in()
            self.status.setText(worker.error)
        else:
            self.bootstrap, self.connected, error = worker.result
            if self.bootstrap is not None:
                worker.deleteLater()
                self.status.setText("Project settings loaded" if self.connected else "Saved project settings loaded")
                self.settings_check.show()
                self._busy(True)
                self.open_timer.start(450)
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
                self.status.setText(error or "")
                self.status.setVisible(bool(error))
            has_session = manager_for(self.config).session is not None
            self.disconnect_button.setVisible(has_session)
            self.disconnect_button.setEnabled(has_session)
            self.retry_button.setVisible(self.connected or has_session)
            self.connect_button.setVisible(not self.connected)
            self.connect_button.setDefault(not self.connected)
            self.retry_button.setDefault(self.connected)
            if self.connected:
                self.title.setText("Project settings need attention")
                self.intro_note.hide()
            else:
                self._show_sign_in()
        worker.deleteLater()

    def connect_box(self):
        from cassn.gui.box_connect import LoginWorker
        if self.worker is not None:
            return
        self._busy(True)
        self.connection_status.hide()
        self._signing_in = True
        self._login_cancel_pending = False
        self.details.hide()
        self.connect_button.hide()
        self.title.setText("Sign in to Box in your browser")
        self.intro_note.setText("Return here after sign-in. Your project will open when the settings are ready.")
        self.intro_note.show()
        self.status.show()
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
            self.load_project(require_live=True)
        else:
            self._show_sign_in()
            self.status.setText(worker.error or "Sign-in cancelled. Choose Connect to Box to try again.")

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
            self._show_sign_in()
            self.disconnect_button.setEnabled(False)
            self.disconnect_button.hide()
            self.retry_button.hide()

    def reject(self):
        self.open_timer.stop()
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
