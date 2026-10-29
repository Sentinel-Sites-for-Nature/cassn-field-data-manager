"""Tests for concise application-startup reporting."""

from types import SimpleNamespace

from cassn.__main__ import _lookup_status_message


def test_lookup_status_reports_successful_box_sync() -> None:
    result = SimpleNamespace(
        source="box",
        synced_files=("sites.csv", "plots.csv"),
    )

    assert _lookup_status_message(result) == (
        "Lookup tables: downloaded 2 files from Box and validated successfully."
    )


def test_lookup_status_reports_validated_offline_cache() -> None:
    result = SimpleNamespace(source="offline-cache", synced_files=())

    assert _lookup_status_message(result) == (
        "Lookup tables: using the last validated offline cache."
    )


def test_first_start_connects_before_bootstrapping(monkeypatch, tmp_path):
    import pytest
    from unittest.mock import MagicMock
    from cassn import __main__ as main_module
    from cassn.box.auth import BoxConfig
    from cassn.gui import box_connect

    events = []
    config = BoxConfig("client", "secret", "data", "lookups")
    def missing():
        raise FileNotFoundError()
    monkeypatch.setattr(main_module, "load_box_config", missing)
    dialog = MagicMock()
    dialog.connected_config = config
    dialog.exec.side_effect = lambda: (events.append("login") or 1)
    monkeypatch.setattr(box_connect, "BoxConnectDialog", lambda *_: dialog)
    bootstrap = SimpleNamespace(source="box", synced_files=(), lookups=object(), warning="")
    def boot(actual_config, *_):
        assert actual_config == config
        events.append("bootstrap")
        return bootstrap
    monkeypatch.setattr(main_module, "bootstrap_lookup_tables", boot)
    app = MagicMock()
    app.exec.return_value = 0
    monkeypatch.setattr(main_module, "QApplication", lambda *_: app)
    monkeypatch.setattr(main_module, "ResourceLease", MagicMock())
    monkeypatch.setattr(main_module, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(main_module, "QLocalServer", MagicMock())
    monkeypatch.setattr(main_module, "ActivationServer", MagicMock())
    windows = MagicMock()
    monkeypatch.setattr(main_module, "DeploymentWindows", lambda **_: windows)
    with pytest.raises(SystemExit) as stopped:
        main_module.main()
    assert stopped.value.code == 0
    assert events == ["login", "bootstrap"]
    windows.new_window.assert_called_once_with(open_sessions=True)


def test_startup_allows_offline_cache_with_locked_keychain(monkeypatch, tmp_path):
    import pytest
    from unittest.mock import MagicMock
    from cassn import __main__ as main_module
    from cassn.box.auth import BoxConfig
    from cassn.gui import box_connect

    monkeypatch.setattr(main_module, "load_box_config", lambda: BoxConfig("client", None, "data", "lookups"))
    dialog = MagicMock()
    monkeypatch.setattr(box_connect, "BoxConnectDialog", dialog)
    bootstrap = SimpleNamespace(source="offline-cache", synced_files=(), lookups=object(), warning="")
    monkeypatch.setattr(main_module, "bootstrap_lookup_tables", lambda *_: bootstrap)
    app = MagicMock()
    app.exec.return_value = 0
    monkeypatch.setattr(main_module, "QApplication", lambda *_: app)
    monkeypatch.setattr(main_module, "ResourceLease", MagicMock())
    monkeypatch.setattr(main_module, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(main_module, "QLocalServer", MagicMock())
    monkeypatch.setattr(main_module, "ActivationServer", MagicMock())
    monkeypatch.setattr(main_module, "DeploymentWindows", MagicMock())
    with pytest.raises(SystemExit):
        main_module.main()
    dialog.assert_not_called()


def test_hosted_startup_uses_class_dialog_code_and_passes_connected_state(monkeypatch, tmp_path):
    import pytest
    from unittest.mock import MagicMock
    from PySide6.QtWidgets import QDialog
    from cassn import __main__ as main_module
    from cassn.box.auth import hosted_config
    from cassn.gui import hosted_setup
    config = hosted_config('uc')
    snapshot = SimpleNamespace(source='box', lookups=object(), synced_files=(), warning='')
    setup = SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted,
                            bootstrap=snapshot, connected=True)
    monkeypatch.setattr(hosted_setup, 'HostedSetupDialog', lambda *_: setup)
    monkeypatch.setattr(main_module, 'load_box_config', lambda: config)
    app = MagicMock()
    app.exec.return_value = 0
    monkeypatch.setattr(main_module, 'QApplication', lambda *_: app)
    monkeypatch.setattr(main_module, 'ResourceLease', MagicMock())
    monkeypatch.setattr(main_module, 'CONFIG_JSON', tmp_path / 'config.json')
    monkeypatch.setattr(main_module, 'QLocalServer', MagicMock())
    monkeypatch.setattr(main_module, 'ActivationServer', MagicMock())
    windows = MagicMock()
    monkeypatch.setattr(main_module, 'DeploymentWindows', lambda **_: windows)
    with pytest.raises(SystemExit) as stopped:
        main_module.main()
    assert stopped.value.code == 0
    assert windows.box_authenticated is True
    windows.new_window.assert_called_once_with(open_sessions=True)
