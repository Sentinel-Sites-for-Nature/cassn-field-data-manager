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
    print(json.dumps({"app": APP_TITLE, "frozen": bool(getattr(sys, "frozen", False)),
        "architecture": platform.machine(), "profile": config.profile,
        "config_directory": str(CONFIG_DIR), "staging_directory": str(DEFAULT_STAGING_ROOT),
        "data_folder": config.field_data_folder_id, "config_folder": config.app_config_folder_id,
        "service": config.service_url, "exiftool": exif, "gui_imports": True,
        "certificate_bundle": True}))
else:
    from cassn.launcher import main
    main()
