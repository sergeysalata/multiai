"""Encryption for provider API keys stored in Postgres.

Keys are encrypted with Fernet (AES-128-CBC + HMAC) using ENCRYPTION_KEY.
A stolen database dump is useless without that key, so keep it out of the
repo and out of the database server's own backups.
"""

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class EncryptionNotConfigured(RuntimeError):
    pass


def _fernet():
    raw = current_app.config.get("ENCRYPTION_KEY") or ""
    if not raw:
        raise EncryptionNotConfigured(
            "ENCRYPTION_KEY is not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(raw.encode() if isinstance(raw, str) else raw)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Stored key could not be decrypted. ENCRYPTION_KEY has changed "
            "since this key was saved; re-add the key."
        ) from exc


def mask(plaintext: str) -> str:
    if len(plaintext) <= 8:
        return "•" * len(plaintext)
    return f"{plaintext[:4]}{'•' * 8}{plaintext[-4:]}"
