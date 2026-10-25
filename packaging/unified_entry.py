"""One Mac app; organization selection precedes all project imports."""
import os
from pathlib import Path
import sys

if getattr(sys, "frozen", False):
    os.environ["PATH"] = str(Path(sys._MEIPASS) / "tools") + ":/usr/bin:/bin:/usr/sbin:/sbin"

if "--self-check" in sys.argv:
    import json
    import platform
    import subprocess
    import certifi
    from cassn.launcher import OrganizationDialog, select_profile
    # An explicit diagnostic option exercises each project in a fresh process.
    profile = sys.argv[sys.argv.index("--profile") + 1] if "--profile" in sys.argv else "uc"
    select_profile(profile)
    from cassn.config import APP_TITLE, BUNDLE_DIR, CONFIG_DIR, DEFAULT_STAGING_ROOT
    from cassn.box.auth import load_box_config
    from cassn.box.hosted import Transport
    from cassn.gui.hosted_setup import HostedSetupDialog
    from keyring.backends.macOS import Keyring
    config = load_box_config()
    Transport(config.service_url)
    assert config.is_complete and config.client_secret is None
    assert (BUNDLE_DIR / "assets" / "workspace" / "box.webp").is_file()
    assert (BUNDLE_DIR / "assets" / "ssn-california-map-transparent.png").is_file()
    assert (BUNDLE_DIR / "assets" / "ssn_data_manager_icon.png").is_file()
    assert Path(certifi.where()).is_file()
    exif = subprocess.check_output(["exiftool", "-ver"], text=True).strip()
    from cassn.soundhub.staging import flac_executable
    flac = flac_executable()
    flac_checked = "--check-flac" in sys.argv
    if flac_checked:
        import tempfile
        import wave
        from cassn.soundhub.staging import convert_wav_to_flac
        assert flac is not None, "FLAC encoder could not be found"
        with tempfile.TemporaryDirectory(prefix="cassn-flac-check-") as temporary:
            source = Path(temporary) / "test.wav"
            encoded = Path(temporary) / "test.flac"
            with wave.open(str(source), "wb") as recording:
                recording.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                recording.writeframes(b"\x00\x00" * 480)
            convert_wav_to_flac(source, encoded)
            subprocess.run([flac, "--test", "--silent", str(encoded)], check=True)
    keychain_checked = "--check-keychain" in sys.argv
    if keychain_checked:
        import uuid
        from cassn.box.secrets import _read_macos_without_prompt
        # Exercise the packaged Security.framework bridge without accessing any
        # real saved connection or presenting an authorization dialog.
        assert _read_macos_without_prompt("diagnostic-missing-" + str(uuid.uuid4())) is None
    print(json.dumps({"app": APP_TITLE, "frozen": bool(getattr(sys, "frozen", False)),
        "architecture": platform.machine(), "profile": config.profile,
        "config_directory": str(CONFIG_DIR), "staging_directory": str(DEFAULT_STAGING_ROOT),
        "data_folder": config.field_data_folder_id, "config_folder": config.app_config_folder_id,
        "service": config.service_url, "exiftool": exif, "flac": flac,
        "flac_conversion_checked": flac_checked, "gui_imports": True,
        "certificate_bundle": True, "keychain_no_prompt": keychain_checked}))
else:
    from cassn.launcher import main
    main()
