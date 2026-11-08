import pytest

from chirpo.security import (
    DecryptionError,
    Encryptor,
    InvalidKeyError,
    generate_key,
    parse_keys,
)


def test_encrypt_then_decrypt_returns_original() -> None:
    encryptor = Encryptor([generate_key()])
    token = encryptor.encrypt("ghp_secret_token")
    assert token != "ghp_secret_token"
    assert encryptor.decrypt(token) == "ghp_secret_token"


def test_same_plaintext_encrypts_differently_each_time() -> None:
    encryptor = Encryptor([generate_key()])
    assert encryptor.encrypt("x") != encryptor.encrypt("x")


def test_wrong_key_cannot_decrypt() -> None:
    token = Encryptor([generate_key()]).encrypt("secret")
    with pytest.raises(DecryptionError):
        Encryptor([generate_key()]).decrypt(token)


def test_tampered_value_is_rejected() -> None:
    encryptor = Encryptor([generate_key()])
    token = encryptor.encrypt("secret")
    tampered = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(DecryptionError):
        encryptor.decrypt(tampered)


def test_rotation_old_values_still_readable_and_can_be_reencrypted() -> None:
    old_key, new_key = generate_key(), generate_key()
    old_token = Encryptor([old_key]).encrypt("secret")

    rotating = Encryptor([new_key, old_key])
    assert rotating.decrypt(old_token) == "secret"

    rotated = rotating.rotate(old_token)
    assert Encryptor([new_key]).decrypt(rotated) == "secret"


def test_parse_keys_splits_and_strips() -> None:
    a, b = generate_key(), generate_key()
    assert parse_keys(f" {a} , {b} ,") == [a, b]


@pytest.mark.parametrize("raw", ["", " , ", "not-a-key", "short,keys"])
def test_parse_keys_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(InvalidKeyError):
        parse_keys(raw)


def test_invalid_key_error_does_not_leak_the_key() -> None:
    with pytest.raises(InvalidKeyError) as error:
        parse_keys("super-secret-but-invalid")
    assert "super-secret" not in str(error.value)
