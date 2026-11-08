"""Encryption of secrets at rest with rotatable Fernet keys.

The first key encrypts; every key decrypts. To rotate: put the new key first, re-encrypt stored
values with `Encryptor.rotate`, then drop the old key.
"""

from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class InvalidKeyError(ValueError):
    pass


class DecryptionError(Exception):
    """Wrong key, or the stored value was tampered with."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


def parse_keys(raw: str) -> list[str]:
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    if not keys:
        raise InvalidKeyError("no encryption key given")
    for position, key in enumerate(keys, start=1):
        try:
            Fernet(key)
        except ValueError:
            # The key itself must never appear in the message: it would end up in logs.
            raise InvalidKeyError(f"encryption key #{position} is not a valid Fernet key") from None
    return keys


class Encryptor:
    def __init__(self, keys: Sequence[str]) -> None:
        if not keys:
            raise InvalidKeyError("no encryption key given")
        self._fernet = MultiFernet([Fernet(key) for key in keys])

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise DecryptionError("value could not be decrypted with the configured keys") from None

    def rotate(self, token: str) -> str:
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken:
            raise DecryptionError("value could not be decrypted with the configured keys") from None
