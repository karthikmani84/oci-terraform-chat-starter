"""Install the pinned official Terraform archive after SHA256 verification."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def install(metadata, output, *, system=None, machine=None, download=None):
    metadata = Path(metadata)
    output = Path(output)
    release = json.loads(metadata.read_text())
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    architecture = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    name = f"terraform_{release['version']}_{system}_{architecture}.zip"
    entry = release['archives'].get(name)
    if not entry:
        raise ValueError('No pinned Terraform archive for this platform. Build the OCI container with --platform linux/amd64.')
    prefix = f"https://releases.hashicorp.com/terraform/{release['version']}/"
    if not entry['url'].startswith(prefix) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
        raise ValueError('Terraform release metadata is invalid.')
    if download is None:
        def download(url):
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.read()
    raw = download(entry['url'])
    if hashlib.sha256(raw).hexdigest() != entry['sha256']:
        raise ValueError('Terraform archive checksum mismatch.')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        binary = archive.read('terraform')
        license_text = archive.read('LICENSE.txt')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(binary)
        temporary.chmod(0o755)
        os.replace(temporary, output)
        license_path = output.parent / 'terraform-LICENSE.txt'
        license_path.write_bytes(license_text)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return {'version': release['version'], 'archive': name, 'sha256': entry['sha256'],
            'binary': str(output.resolve()), 'license': str(license_path.resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', type=Path, default=ROOT / 'deploy/terraform-release.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(install(args.metadata, args.output)))


if __name__ == '__main__':
    main()
