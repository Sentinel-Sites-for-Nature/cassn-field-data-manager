"""Standalone UC entry point; select profile before importing application code."""
import os
import sys
from pathlib import Path

os.environ["CASSN_PROFILE"] = "uc"
# Use bundled ExifTool, system Perl and macOS utilities; never rely on Homebrew.
if getattr(sys, "frozen", False):
    os.environ["PATH"] = str(Path(sys._MEIPASS) / "tools") + ":/usr/bin:/bin:/usr/sbin:/sbin"

if "--check-service" in sys.argv:
    # Safe administrator diagnostic: no OAuth consent or saved credentials.
    import base64, hashlib, json, secrets
    from cassn.box.auth import load_box_config
    from cassn.box.hosted import Transport, _authorization_url
    config = load_box_config()
    request = Transport(config.service_url)
    proof = secrets.token_urlsafe(32)
    status, data = request("/v1/sessions", payload={"code_challenge":
        base64.urlsafe_b64encode(hashlib.sha256(proof.encode()).digest()).decode().rstrip("=")})
    report = {"profile": config.profile, "status": status, "fields": sorted(data)}
    if status == 201:
        try:
            _authorization_url(data, config.profile)
            report["authorization_url_valid"] = True
        finally:
            report["cleanup_status"] = request("/v1/sessions/" + data["session_id"], "DELETE", proof=proof)[0]
    print(json.dumps(report))
elif "--self-check" in sys.argv:
    import json
    import platform
    import subprocess
    import certifi
    from cassn.config import BUNDLE_DIR, CONFIG_DIR, APP_TITLE
    from cassn.box.auth import hosted_config
    from cassn.box.hosted import Transport
    from cassn.gui.hosted_setup import HostedSetupDialog
    from keyring.backends.macOS import Keyring
    Transport()  # Validate packaged certificate data without contacting a server.
    exif = subprocess.run(["exiftool", "-ver"], capture_output=True, text=True, check=True).stdout.strip()
    assert (BUNDLE_DIR / "assets" / "cassn_icon.png").is_file()
    assert Path(certifi.where()).is_file()
    assert hosted_config("uc").client_secret is None
    print(json.dumps({"app": APP_TITLE, "frozen": bool(getattr(sys, "frozen", False)),
        "architecture": platform.machine(), "python": platform.python_version(),
        "exiftool": exif, "profile": "uc", "config_directory": str(CONFIG_DIR),
        "gui_imports": True, "certificate_bundle": True}))
else:
    from cassn.__main__ import main
    main()
