import hashlib
import hmac

from remediator.github.signature import verify_signature


def test_valid_signature() -> None:
    body = b'{"ok":true}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, f"sha256={digest}")
    assert not verify_signature("secret", body, "sha256=bad")


def test_missing_signature() -> None:
    assert not verify_signature("secret", b"body", None)


def test_wrong_signature_prefix() -> None:
    assert not verify_signature("secret", b"body", "sha1=bad")


def test_signature_without_prefix() -> None:
    body = b'{"ok":true}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert not verify_signature("secret", body, digest)
