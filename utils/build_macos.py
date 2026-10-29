"""Build an arm64 Mac prototype with Python, Qt, certificates and ExifTool.

Usage: .venv/bin/python utils/build_macos.py
Install packaging/requirements-build.txt first. ExifTool 13.55 must be available
on the build host; end users do not install it. Output is local, ad-hoc signed.
"""
from pathlib import Path
import os
import argparse
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def install_built_app(app: Path, install_dir: Path) -> Path:
    """Replace the installed bundle at a stable Dock path, preserving it on failure."""
    app = app.resolve(strict=True)
    install_dir = install_dir.expanduser().resolve()
    destination = install_dir / app.name
    if app == destination:
        raise RuntimeError('Build outside the installation directory before installing.')
    executable = destination / 'Contents' / 'MacOS' / app.stem
    processes = subprocess.check_output(['/bin/ps', '-axo', 'command='], text=True)
    if any(line.strip() == str(executable) or line.strip().startswith(str(executable) + ' ')
           for line in processes.splitlines()):
        raise RuntimeError(f'Quit {destination} before installing the new build. Built app: {app}')
    if destination.is_symlink():
        raise RuntimeError(f'Refusing to replace a symlink: {destination}')
    backup = install_dir / ('.' + app.name + '.previous')
    if backup.exists():
        raise RuntimeError(f'Previous installation backup needs review: {backup}')
    install_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.cassn-install-', dir=install_dir) as temporary:
        candidate = Path(temporary) / app.name
        shutil.copytree(app, candidate, symlinks=True)
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(candidate)], check=True)
        try:
            if destination.exists():
                destination.rename(backup)
            candidate.rename(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("unified", "uc", "csu"), default="unified")
    parser.add_argument("--signing-identity", default=os.environ.get("CASSN_SIGNING_IDENTITY"),
                        help="Installed Developer ID Application certificate name or SHA-1 fingerprint")
    parser.add_argument("--dist-dir", type=Path,
                        help="Artifact output directory; installation still occurs unless --no-install is set")
    parser.add_argument("--install-dir", type=Path, default=Path.home() / 'Applications',
                        help="Permanent application location (default: ~/Applications)")
    parser.add_argument("--no-install", action='store_true',
                        help="Build distribution artifacts without updating the installed app")
    args = parser.parse_args()
    profile = args.profile
    title = "CA-SSN Data Manager" if profile == "unified" else profile.upper() + " Field Data Manager"
    dist_dir = (args.dist_dir or ROOT / 'dist' / profile).resolve()
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('This build targets Apple Silicon macOS.')
    if args.signing_identity:
        import re
        identities = subprocess.check_output(['/usr/bin/security', 'find-identity', '-v', '-p', 'codesigning'], text=True)
        matches = re.findall(r'([0-9A-Fa-f]{40}) "(Developer ID Application:[^"]+)"', identities)
        selected = [fingerprint for fingerprint, name in matches
                    if args.signing_identity in (fingerprint, name)]
        if len(selected) != 1:
            raise SystemExit('Choose an installed, valid Developer ID Application certificate. No app was rebuilt.')
        signing_identity = selected[0]
    else:
        signing_identity = ''
    tool = shutil.which('exiftool')
    if not tool:
        raise SystemExit('Install ExifTool 13.55 on the build host first.')
    tool = Path(tool).resolve()
    version = subprocess.check_output([str(tool), '-ver'], text=True).strip()
    if version != '13.55':
        raise SystemExit('Use the validated ExifTool version 13.55 for this build.')
    # Homebrew relocates Perl modules. Include only architecture-independent
    # ExifTool modules, not optional third-party XS extensions from the build host.
    libs = tool.parent.parent / 'libexec' / 'lib' / 'perl5'
    if not (libs / 'Image' / 'ExifTool.pm').is_file():
        raise SystemExit('Expected the Homebrew ExifTool module layout.')
    stage = ROOT / 'build' / (profile + '-tools')
    if stage.exists():
        shutil.rmtree(stage)
    (stage / 'lib').mkdir(parents=True)
    for name in ('Image', 'File'):
        shutil.copytree(libs / name, stage / 'lib' / name)
    source = tool.read_text().replace('    use Config;', '    unshift @INC, $incDir;')
    lines = source.splitlines()
    lines[0] = '#!/usr/bin/perl'
    lines = [line for line in lines if not line.startswith('unshift @INC, "/usr/local/')]
    (stage / 'exiftool').write_text('\n'.join(lines) + '\n')
    (stage / 'exiftool').chmod(0o755)
    shutil.copy2(tool.parent.parent / 'README', stage / 'ExifTool-README.txt')
    # The distribution carries ExifTool's Perl licensing notice in its README.
    env = dict(os.environ, PATH=str(stage) + ':/usr/bin:/bin:/usr/sbin:/sbin')
    assert subprocess.check_output([str(stage / 'exiftool'), '-ver'], text=True, env=env).strip() == version
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath', str(dist_dir),
                    '--workpath', str(ROOT / 'build' / (profile + '-pyinstaller')),
                    str(ROOT / 'packaging' / 'hosted.spec')], cwd=ROOT,
                    env=dict(os.environ, CASSN_BUILD_PROFILE=profile, CASSN_SIGNING_IDENTITY=signing_identity), check=True)
    app = dist_dir / (title + '.app')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    archive_name = "CA-SSN-Data-Manager-arm64-prototype.zip" if profile == "unified" else profile.upper() + "-Field-Data-Manager-arm64-prototype.zip"
    archive = dist_dir / archive_name
    subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(archive)], check=True)
    print(f'App: {app}\nArchive: {archive}')
    if not args.no_install:
        print(f'Installed: {install_built_app(app, args.install_dir)}')
    print('Developer ID signed; public distribution still requires notarization.' if signing_identity else
          'Ad-hoc prototype: updates can require Keychain approval again. Public distribution requires Developer ID signing and notarization.')


if __name__ == '__main__':
    main()
