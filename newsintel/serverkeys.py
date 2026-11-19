"""Server-side keys the app can generate for itself, so the owner never has to handle them.

- Web Push (VAPID) key pair: used to sign push notifications.
- App secret: signs unsubscribe and email-confirmation links.

Environment variables win when set (VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY, UNSUBSCRIBE_SECRET).
Otherwise each value is generated once and kept in the database (the `kv` table), which lives
on the server's persistent disk and never in git or in web/.
"""
from __future__ import annotations

import os
import secrets as _secrets

_store = None


def init(store) -> None:
    """Called once at server start with the app's Store."""
    global _store
    _store = store


def _get_or_create(key: str, make) -> str | None:
    if _store is None:
        return None
    value = _store.get_kv(key)
    if not value:
        value = make()
        _store.set_kv(key, value)
    return value


def app_secret() -> str:
    env = os.environ.get("UNSUBSCRIBE_SECRET")
    if env and env != "change-me":
        return env
    return _get_or_create("app_secret", lambda: _secrets.token_urlsafe(32)) or "change-me"


def _new_vapid_pair() -> str:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02, b64urlencode

    v = Vapid02()
    v.generate_keys()
    private = b64urlencode(v.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public = b64urlencode(v.public_key.public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint))
    return f"{public} {private}"


def vapid_keys() -> tuple[str | None, str | None]:
    """(public, private) as base64url strings, or (None, None) if push is unavailable."""
    pub, priv = os.environ.get("VAPID_PUBLIC_KEY"), os.environ.get("VAPID_PRIVATE_KEY")
    if pub and priv:
        return pub, priv
    try:
        pair = _get_or_create("vapid", _new_vapid_pair)
    except ImportError:  # pywebpush / py_vapid not installed
        return None, None
    if not pair:
        return None, None
    pub, priv = pair.split(" ", 1)
    return pub, priv


def vapid_subject() -> str:
    """Contact for push services. Apple rejects placeholder addresses, so prefer the app's own URL."""
    return (os.environ.get("VAPID_SUBJECT")
            or os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
            or "mailto:alerts@example.com")
