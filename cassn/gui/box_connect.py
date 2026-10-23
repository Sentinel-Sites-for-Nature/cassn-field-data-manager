"""Connect to Box without terminal commands or pasted redirect URLs."""
from PySide6.QtCore import QThread, QTimer, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from cassn.gui.dialogs import WindowDialog as QDialog
from cassn.gui.appearance import workspace_label, workspace_stylesheet
from cassn.gui.box_button import BoxConnectButton
from cassn.box.auth import BoxConfig, load_box_config
from cassn.box.connect import REDIRECT_URI, browser_login, save_connection
from cassn.box.secrets import backend


class LoginWorker(QThread):
    open_url = Signal(str)
    progress = Signal(str)

    def __init__(self, config, parent):
        super().__init__(parent)
        self.config = config
        self.result = None
        self.error = None

    def run(self):
        try:
            backend()  # Fail before opening the browser if secure storage is unavailable.
            if self.config.provider == "hosted":
                from cassn.box.hosted import manager_for
                self.result = manager_for(self.config).login(self.open_url.emit, self.isInterruptionRequested, self.progress.emit)
            else:
                self.result = browser_login(self.config, self.open_url.emit, self.isInterruptionRequested)
        except RuntimeError as exc:
            from cassn.box.hosted import HostedError
            self.error = str(exc) if isinstance(exc, HostedError) or self.config.provider != "hosted" else "Could not access secure storage. Check Keychain and retry."
        except OSError:
            self.error = "Could not start sign-in. Check your connection and that port 8765 is available."
        except Exception:
            # SDK exception text can contain request details. Keep it out of the UI/log.
            self.error = "Box could not complete sign-in. Check your Box app settings and try again."


class BoxConnectDialog(QDialog):
    def __init__(self, config=None, parent=None, *, auto_start=False):
        super().__init__(parent)
        self.setWindowTitle("Connect to Box")
        self.setObjectName("fieldWorkspace")
        self.setStyleSheet(workspace_stylesheet(QApplication.palette()))
        self.setMinimumWidth(520)
        self.resize(560, 430)
        self.worker = None
        self._cancel_pending = False
        self.connected_config = None
        self._browser_url = None
        if config is None:
            try:
                config = load_box_config()
            except Exception:
                config = BoxConfig()
        self.config = config
        self._auto_start = auto_start and config.is_complete and bool(config.app_config_folder_id)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(workspace_label(
            f"{config.profile.upper()} PROJECT" if config.provider == "hosted" else "CLOUD CONNECTION", "connected"))
        layout.addWidget(workspace_label("Connect to Box", "pageTitle"))
        note = workspace_label("Sign in securely in your browser. Your connection is saved in your system’s secure credential store (Keychain on Mac).", "muted", wrap=True)
        layout.addWidget(note)
        advanced = QPushButton("Box app settings…")
        layout.addWidget(advanced)
        self.settings = QWidget()
        form = QFormLayout(self.settings)
        self.fields = {}
        for key, label in (
            ("client_id", "Client ID"), ("client_secret", "Client secret"),
            ("field_data_folder_id", "Field data folder ID"),
            ("app_config_folder_id", "App config folder ID"),
        ):
            field = QLineEdit(getattr(config, key) or "")
            if key == "client_secret":
                field.setEchoMode(QLineEdit.Password)
            self.fields[key] = field
            form.addRow(label, field)
        callback = QLabel(f"Administrator setup: register this OAuth redirect URI in the Box Developer Console once:\n{REDIRECT_URI}")
        callback.setWordWrap(True)
        form.addRow(callback)
        layout.addWidget(self.settings)
        self.settings.setVisible(config.provider != "hosted" and (not config.is_complete or not config.app_config_folder_id))
        advanced.setVisible(config.provider != "hosted")
        if config.provider == "hosted":
            note.setText("Use the Box account approved to access your project folder. "
                         "If your browser is signed into a different Box account, sign out of Box there first.")
        advanced.clicked.connect(lambda: self.settings.setVisible(not self.settings.isVisible()))
        if config.provider == "hosted":
            layout.addWidget(workspace_label("Your browser will open for sign-in. This Mac remembers your connection securely in Keychain.", "muted", wrap=True))
        layout.addStretch()
        self.connect_button = BoxConnectButton()
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self.start_login)
        self.connect_button.setVisible(not self._auto_start)
        layout.addWidget(self.connect_button)
        self.status = workspace_label("", "muted", wrap=True)
        self.status.setMinimumHeight(self.status.fontMetrics().lineSpacing() * 3)
        layout.addWidget(self.status)
        self.reopen_browser = QPushButton("Open browser again")
        self.reopen_browser.setEnabled(False)
        self.reopen_browser.hide()
        self.reopen_browser.clicked.connect(lambda: self.open_browser(self._browser_url) if self._browser_url else None)
        layout.addWidget(self.reopen_browser)
        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)

    def exec(self):
        if self._auto_start:
            QTimer.singleShot(0, self.start_login)
        return super().exec()

    def start_login(self):
        if self.worker is not None:
            return
        config = self.config if self.config.provider == "hosted" else BoxConfig(**{key: field.text().strip() or None for key, field in self.fields.items()})
        if not config.is_complete or not config.app_config_folder_id:
            self.settings.show()
            self.status.setText("Enter the Box client ID, client secret, and both folder IDs supplied by your administrator.")
            return
        self.config = config
        self.settings.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.status.setText("Waiting for Box sign-in in your browser…")
        self.worker = LoginWorker(config, self)
        self.worker.open_url.connect(self.open_browser)
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self.login_finished)
        self.worker.start()

    def open_browser(self, url):
        self._browser_url = url
        self.reopen_browser.setVisible(self.worker is not None)
        self.reopen_browser.setEnabled(self.worker is not None and not self._cancel_pending)
        if not QDesktopServices.openUrl(QUrl(url)):
            self.status.setText("Could not open your browser. Check your default browser and try again.")
            self.worker.requestInterruption()

    def login_finished(self):
        worker = self.worker
        self.worker = None
        self._browser_url = None
        self.reopen_browser.setEnabled(False)
        self.reopen_browser.hide()
        if self._cancel_pending and not (self.config.provider == "hosted" and worker.result):
            worker.deleteLater()
            super().reject()
            return
        if worker.result:
            try:
                if self.config.provider != "hosted":
                    save_connection(self.config, worker.result[0])
            except Exception:
                self.status.setText("Could not save the connection securely. Your existing configuration was retained where possible. Check Keychain access and try again.")
            else:
                self.connected_config = self.config
                if self.parentWidget():
                    self.parentWidget().raise_()
                    self.parentWidget().activateWindow()
                self.accept()
                worker.deleteLater()
                return
        else:
            self.status.setText(worker.error or "Box sign-in cancelled.")
        worker.deleteLater()
        self.settings.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.connect_button.show()

    def reject(self):
        if self.worker is not None:
            self._cancel_pending = True
            self.reopen_browser.setEnabled(False)
            self.worker.requestInterruption()
            self.status.setText("Cancelling sign-in… Please wait.")
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
