"""Dialogs belong to their deployment window, leaving other windows usable."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox


class WindowDialog(QDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowModality(Qt.WindowModal)


class WindowMessageBox(QMessageBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowModality(Qt.WindowModal)

    @classmethod
    def _message(cls, icon, parent, title, text, buttons, defaultButton):
        message = cls(icon, title, text, buttons, parent)
        if defaultButton != cls.NoButton:
            message.setDefaultButton(defaultButton)
        return cls.StandardButton(message.exec())

    @classmethod
    def information(cls, parent, title, text, buttons=QMessageBox.Ok, defaultButton=QMessageBox.NoButton):
        return cls._message(cls.Information, parent, title, text, buttons, defaultButton)

    @classmethod
    def warning(cls, parent, title, text, buttons=QMessageBox.Ok, defaultButton=QMessageBox.NoButton):
        return cls._message(cls.Warning, parent, title, text, buttons, defaultButton)

    @classmethod
    def critical(cls, parent, title, text, buttons=QMessageBox.Ok, defaultButton=QMessageBox.NoButton):
        return cls._message(cls.Critical, parent, title, text, buttons, defaultButton)

    @classmethod
    def question(cls, parent, title, text, buttons=QMessageBox.Yes | QMessageBox.No, defaultButton=QMessageBox.NoButton):
        return cls._message(cls.Question, parent, title, text, buttons, defaultButton)
