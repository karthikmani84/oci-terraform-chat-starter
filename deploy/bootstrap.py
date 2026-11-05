"""Inline-source bootstrap for one OCI Container Instance.

The archive contains source only. Credentials and Terraform state stay in OCI.
"""
from __future__ import annotations
import base64
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path('/opt/oci-terraform-chat')


def main():
    if len(sys.argv) != 2:
        raise ValueError('The inline source archive is required.')
    expected = os.environ.get('APP_SHA256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('A source archive SHA256 is required.')
    raw = base64.b64decode(sys.argv[1], validate=True)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Source archive checksum mismatch.')
    ROOT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            target = (ROOT / item.filename).resolve()
            if not target.is_relative_to(ROOT) or item.filename.startswith('/'):
                raise ValueError('Source archive entry escapes the application directory.')
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', str(ROOT / 'requirements.txt')], check=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/install_terraform.py'),
                    '--output', '/usr/local/bin/terraform'], check=True)
    os.environ.setdefault('PORT', '8080')
    os.chdir(ROOT)
    print('TERRAFORM_CHAT_BOOTSTRAP_READY', flush=True)
    os.execv(sys.executable, [sys.executable, '-m', 'app.server'])


if __name__ == '__main__':
    main()
