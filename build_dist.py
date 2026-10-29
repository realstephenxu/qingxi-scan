#!/usr/bin/python3
"""Build a DEB and source distribution from a strict source allowlist."""
import argparse
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

VERSION = '1.1.1'
RELEASE = '清晰扫描_v{}_UOS分发包'.format(VERSION)
DEB = 'qingxi-scan_{}_all.deb'.format(VERSION)
SOURCE = Path(__file__).resolve().parent
PROGRAM = ['app.py', 'core.py', 'icon.svg', 'chevron.svg', 'sane/dll.conf']
SOURCE_FILES = PROGRAM + ['test_app.py', '使用说明.md', 'build_dist.py', 'README.md', '.gitignore',
                          'docs/screenshots/scanning.png', 'docs/screenshots/id-card.png']

CONTROL = '''Package: qingxi-scan
Version: __VERSION__
Section: graphics
Priority: optional
Architecture: all
Maintainer: Qingxi Scan <noreply@localhost>
Depends: python3 (>= 3.7), python3-pyqt5 (>= 5.11), python3-pil (>= 5.4), python3-reportlab (>= 3.5), sane-utils
Recommends: fonts-noto-cjk
Description: Local document scanner for HP LaserJet M1005
 Native Chinese desktop interface for UOS with page preview, crop,
 rotation, ordering, local draft recovery, and PDF or image export.
 Includes ID front/back and household-page layouts on A4 sheets.
 Uses the system hpljm1005 SANE backend. No cloud service is used.
'''.replace('__VERSION__', VERSION)

DESKTOP = '''[Desktop Entry]
Version=1.0
Type=Application
Name=清晰扫描
GenericName=文档扫描
Comment=逐页扫描、整理和保存 PDF 或图片
Exec=/usr/bin/qingxi-scan
TryExec=/usr/bin/qingxi-scan
Icon=qingxi-scan
Terminal=false
Categories=Graphics;Scanning;
StartupNotify=false
StartupWMClass=清晰扫描
'''

LAUNCHER = '''#!/bin/sh
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 /usr/share/qingxi-scan/app.py "$@"
'''


def copy_file(relative, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(SOURCE / relative), str(destination))
    destination.chmod(0o644)


def write_file(destination, content, mode=0o644):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding='utf-8')
    destination.chmod(mode)


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output):
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    final = output / RELEASE
    archive = output / (RELEASE + '.tar.gz')
    if final.exists() or archive.exists():
        raise FileExistsError('同名分发包已存在，请选择一个新的输出文件夹。')
    with tempfile.TemporaryDirectory(prefix='qingxi-build-') as temporary:
        work = Path(temporary)
        release = work / RELEASE
        release.mkdir()
        package = work / 'deb-root'
        for relative in PROGRAM:
            copy_file(relative, package / 'usr/share/qingxi-scan' / relative)
        write_file(package / 'usr/bin/qingxi-scan', LAUNCHER, 0o755)
        write_file(package / 'usr/share/applications/qingxi-scan.desktop', DESKTOP)
        copy_file('icon.svg', package / 'usr/share/icons/hicolor/scalable/apps/qingxi-scan.svg')
        copy_file('使用说明.md', package / 'usr/share/doc/qingxi-scan/使用说明.md')
        size = sum(path.stat().st_size for path in package.rglob('*') if path.is_file())
        write_file(package / 'DEBIAN/control', CONTROL + 'Installed-Size: {}\n'.format((size + 1023) // 1024))
        md5sums = []
        for path in sorted((package / 'usr').rglob('*')):
            if path.is_file():
                md5sums.append('{}  {}'.format(hashlib.md5(path.read_bytes()).hexdigest(), path.relative_to(package).as_posix()))
        write_file(package / 'DEBIAN/md5sums', '\n'.join(md5sums) + '\n')
        # The live application uses a restrictive umask. Package directories
        # must be readable by all users on the receiving machine.
        for path in package.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(package), str(release / DEB)], check=True)
        (release / DEB).chmod(0o644)
        for relative in SOURCE_FILES:
            copy_file(relative, release / '源码' / relative)
        copy_file('使用说明.md', release / '使用说明.md')
        hashes = []
        for path in sorted(release.rglob('*')):
            if path.is_file():
                hashes.append('{}  {}'.format(checksum(path), path.relative_to(release).as_posix()))
        write_file(release / 'SHA256SUMS', '\n'.join(hashes) + '\n')
        for path in release.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)

        temporary_archive = work / (RELEASE + '.tar.gz')

        def portable_owner(info):
            info.uid, info.gid = 0, 0
            info.uname, info.gname = 'root', 'root'
            info.mode = 0o755 if info.isdir() else 0o644
            return info

        with tarfile.open(str(temporary_archive), 'w:gz') as stream:
            stream.add(str(release), arcname=RELEASE, filter=portable_owner)
        # Publish without overwriting any file that might appear while building.
        shutil.copytree(str(release), str(final))
        with open(str(archive), 'xb') as destination:
            with temporary_archive.open('rb') as source:
                shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        archive.chmod(0o644)
    print('分发文件夹：' + str(final))
    print('完整压缩包：' + str(archive))
    print('SHA256：' + checksum(archive))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='构建清晰扫描分发包（不包含用户扫描资料）')
    parser.add_argument('output', help='分发文件夹和 tar.gz 的输出位置')
    build(parser.parse_args().output)
