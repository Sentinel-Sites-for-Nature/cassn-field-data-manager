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

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("unified", "uc", "csu"), default="unified")
    profile = parser.parse_args().profile
    title = "CA-SSN Data Manager" if profile == "unified" else profile.upper() + " Field Data Manager"
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('This build targets Apple Silicon macOS.')
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
                    '--distpath', str(ROOT / 'dist' / profile),
                    '--workpath', str(ROOT / 'build' / (profile + '-pyinstaller')),
                    str(ROOT / 'packaging' / 'hosted.spec')], cwd=ROOT, env=dict(os.environ, CASSN_BUILD_PROFILE=profile), check=True)
    app = ROOT / 'dist' / profile / (title + '.app')
    archive_name = "CA-SSN-Data-Manager-arm64-prototype.zip" if profile == "unified" else profile.upper() + "-Field-Data-Manager-arm64-prototype.zip"
    archive = ROOT / "dist" / profile / archive_name
    subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(archive)], check=True)
    print(f'Prototype: {app}\nArchive: {archive}\nPublic distribution still requires Developer ID signing and notarization.')


if __name__ == '__main__':
    main()
