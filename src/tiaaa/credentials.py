"""Private, per-portal credentials for required employer application accounts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from contextlib import suppress
from urllib.parse import urlsplit

from tiaaa.config import AppPaths, ensure_dirs

_KEY_BYTES = 32
_PASSWORD_CONTEXT = b"tiaaa-employer-account-password-v1\0"


def _load_or_create_account_key(paths: AppPaths) -> bytes:
    """Load the install-local key, creating it atomically with private permissions."""

    paths = ensure_dirs(paths)
    key_path = paths.employer_account_key
    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        generated = secrets.token_bytes(_KEY_BYTES)
        try:
            descriptor = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            key = key_path.read_bytes()
        else:
            try:
                os.write(descriptor, generated)
            finally:
                os.close(descriptor)
            key = generated
    if len(key) != _KEY_BYTES:
        raise ValueError("The local employer-account key is invalid")
    with suppress(OSError):
        key_path.chmod(0o600)
    return key


def application_account_password(
    *,
    paths: AppPaths,
    application_url: str,
    email: str,
) -> str:
    """Derive a stable unique password without storing reusable plaintext credentials."""

    hostname = (urlsplit(application_url).hostname or "").casefold().rstrip(".")
    normalized_email = email.strip().casefold()
    if not hostname:
        raise ValueError("Application URL has no employer hostname")
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("A candidate email address is required for employer accounts")
    key = _load_or_create_account_key(paths)
    context = f"{hostname}\0{normalized_email}".encode()
    digest = hmac.new(key, _PASSWORD_CONTEXT + context, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    # The fixed prefix satisfies the common upper/lower/digit/symbol policy;
    # the HMAC suffix keeps accounts unique without password reuse or storage.
    return f"Tia1!{token[:15]}"
