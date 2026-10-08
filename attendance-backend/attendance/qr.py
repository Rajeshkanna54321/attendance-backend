"""Rotating QR tokens. Token = <session_id>.<slot>.<hmac>, slot = floor(server_time / 3s).
The server clock decides validity, so changing a phone's clock does not help."""
import hashlib
import hmac
import time

from django.conf import settings


def _now():
    return time.time()


def current_slot():
    return int(_now() // settings.QR_SLOT_SECONDS)


def _sig(secret, session_id, slot):
    return hmac.new(secret.encode(), f"{session_id}:{slot}".encode(), hashlib.sha256).hexdigest()[:20]


def make_token(session):
    slot = current_slot()
    return f"{session.id}.{slot}.{_sig(session.secret, session.id, slot)}"


def seconds_left_in_slot():
    s = settings.QR_SLOT_SECONDS
    return round(s - (_now() % s), 2)


def parse_session_id(token):
    try:
        return int(str(token).split(".")[0])
    except (ValueError, IndexError):
        return None


def validate_token(session, token):
    """Return None if valid, else a reason string."""
    try:
        sid, slot, sig = str(token).split(".")
        sid, slot = int(sid), int(slot)
    except ValueError:
        return "qr_malformed"
    if sid != session.id or not hmac.compare_digest(sig, _sig(session.secret, sid, slot)):
        return "qr_invalid"
    cur = current_slot()
    if not (cur - settings.QR_GRACE_SLOTS <= slot <= cur):
        return "qr_expired"
    return None
