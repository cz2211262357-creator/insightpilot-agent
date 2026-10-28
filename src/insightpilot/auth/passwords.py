from functools import cache

from pwdlib import PasswordHash

PASSWORDS = PasswordHash.recommended()


@cache
def dummy_hash():
    return PASSWORDS.hash("non-account-timing-placeholder")


def hash_password(password):
    if len(password) < 16 or len(password) > 256:
        raise ValueError("password_length")
    return PASSWORDS.hash(password)


def verify_password(password, hashed):
    try:
        return PASSWORDS.verify(password, hashed)
    except Exception:
        return False
