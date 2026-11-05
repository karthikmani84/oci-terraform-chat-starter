"""Hash an operator-supplied, verified OIDC subject locally; no network or JWTs."""
from __future__ import annotations

import getpass
import hashlib


def subject_hash(subject: str) -> str:
    if not isinstance(subject, str) or not 1 <= len(subject) <= 512 or any(c in subject for c in '\r\n\x00'):
        raise ValueError('Enter the exact non-empty OIDC sub value, not a token or JSON document.')
    if subject.count('.') == 2 and subject.startswith('eyJ'):
        raise ValueError('Enter only the verified sub value, not an ID token.')
    if subject.lstrip().startswith(('{', '[')):
        raise ValueError('Enter only the verified sub value, not a JSON document.')
    return hashlib.sha256(subject.encode('utf-8')).hexdigest()


def main():
    try:
        value = getpass.getpass('Exact verified OIDC sub (input hidden): ')
        print(subject_hash(value))
    except (ValueError, EOFError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
