# Run via utils/build_macos.py; only explicitly selected assets enter the bundle.
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
profile = os.environ.get('CASSN_BUILD_PROFILE', 'unified')
assert profile in {'unified', 'uc', 'csu'}
title = 'CA-SSN Data Manager' if profile == 'unified' else profile.upper() + ' Field Data Manager'
stage = root / 'build' / (profile + '-tools')
a = Analysis(
    [str(root / 'packaging' / (profile + '_entry.py'))], pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'assets'), 'assets'), (str(stage), 'tools')]
          + collect_data_files('certifi') + collect_data_files('botocore')
          + copy_metadata('keyring') + copy_metadata('box-sdk-gen'),
    hiddenimports=['keyring.backends.macOS', 'PySide6.QtNetwork', 'zoneinfo'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'pytest', 'tkinter', 'IPython'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=title,
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, target_arch='arm64', codesign_identity=os.environ.get('CASSN_SIGNING_IDENTITY') or None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=title)
app = BUNDLE(coll, name=title + '.app',
             icon=str(root / 'assets' / 'ssn_data_manager_icon.icns'),
             bundle_identifier='org.cassn.' + profile + '.field-data-manager.prototype',
             info_plist={'CFBundleShortVersionString': '4.0', 'CFBundleVersion': '4013',
                         'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True})
