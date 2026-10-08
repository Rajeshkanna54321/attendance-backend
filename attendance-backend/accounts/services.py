import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException

from .models import Device, EmailOTP


class DeviceError(APIException):
    status_code = 403

    def __init__(self, message, code, retry_after=None):
        self.detail = {"detail": message, "code": code, "retry_after": retry_after}


# ---------- Email OTP ----------
def _hash(email, code):
    return hmac.new(settings.SECRET_KEY.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def send_otp(email):
    EmailOTP.objects.filter(email=email).delete()
    code = f"{secrets.randbelow(10**6):06d}"
    EmailOTP.objects.create(email=email, code_hash=_hash(email, code))
    send_mail(
        "Your attendance app OTP",
        f"Your OTP is {code}. It is valid for {settings.OTP_TTL_MINUTES} minutes.",
        None, [email],
    )


def check_otp(email, code):
    otp = EmailOTP.objects.filter(email=email).order_by("-created_at").first()
    if not otp or otp.attempts >= 5:
        return False
    if timezone.now() - otp.created_at > timedelta(minutes=settings.OTP_TTL_MINUTES):
        return False
    otp.attempts += 1
    otp.save(update_fields=["attempts"])
    if hmac.compare_digest(otp.code_hash, _hash(email, code)):
        otp.delete()
        return True
    return False


# ---------- Google ----------
def verify_google_token(token):
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token
    info = id_token.verify_oauth2_token(token, g_requests.Request(), settings.GOOGLE_CLIENT_ID)
    if not info.get("email_verified"):
        raise ValueError("Google email not verified")
    return info


# ---------- Device binding ----------
def bind_device(user, device_id):
    """Enforce: one phone = one student account, 30-min wait when a phone switches accounts."""
    if user.role != "student":
        return None
    if not device_id:
        raise DeviceError("device_id is required for students.", "device_id_required")
    now = timezone.now()
    cooldown = timedelta(minutes=settings.DEVICE_COOLDOWN_MINUTES)
    with transaction.atomic():
        device, _ = Device.objects.select_for_update().get_or_create(device_id=device_id)
        if device.user_id == user.id:
            device.last_login_at = now
            device.save(update_fields=["last_login_at"])
            return device
        if Device.objects.filter(user=user).exclude(pk=device.pk).exists():
            raise DeviceError("This account is linked to another phone. Ask your teacher/admin to reset it.",
                              "account_bound_elsewhere")
        if device.user_id is not None:
            if device.is_in_use():
                raise DeviceError("This phone is still signed in to another account.", "device_in_use")
            remaining = (device.last_logout_at + cooldown - now).total_seconds()
            if remaining > 0:
                raise DeviceError(f"This phone was recently used by another account. Try again in {int(remaining // 60) + 1} min.",
                                  "device_cooldown", retry_after=int(remaining))
        device.user = user
        device.last_login_at = now
        device.save()
        return device
