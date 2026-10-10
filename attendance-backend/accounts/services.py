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

    import logging
    import threading
    logger = logging.getLogger(__name__)

    result = {"success": False}

    def _send():
        brevo_key = getattr(settings, "BREVO_API_KEY", "") or os.getenv("BREVO_API_KEY", "")
        if brevo_key:
            try:
                import requests
                sender_email = getattr(settings, "DEFAULT_FROM_EMAIL", "") or getattr(settings, "EMAIL_HOST_USER", "") or "noreply@attendance.local"
                payload = {
                    "sender": {"name": "QR Attendance App", "email": sender_email},
                    "to": [{"email": email}],
                    "subject": "Your Attendance App OTP Code",
                    "htmlContent": (
                        f"<div style='font-family:Arial,sans-serif;max-width:500px;margin:0 auto;padding:20px;border:1px solid #e2e8f0;border-radius:10px;'>"
                        f"<h2 style='color:#1e293b;margin-bottom:10px;'>QR Attendance Verification</h2>"
                        f"<p style='color:#475569;font-size:16px;'>Use the code below to complete your sign in:</p>"
                        f"<div style='background-color:#f1f5f9;padding:15px;text-align:center;font-size:32px;font-weight:bold;letter-spacing:6px;color:#2563eb;border-radius:8px;margin:20px 0;'>{code}</div>"
                        f"<p style='color:#64748b;font-size:14px;'>This OTP is valid for <strong>{settings.OTP_TTL_MINUTES} minutes</strong>. Please do not share this code with anyone.</p>"
                        f"</div>"
                    ),
                    "textContent": f"Your attendance app OTP is {code}. It is valid for {settings.OTP_TTL_MINUTES} minutes.",
                }
                resp = requests.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers={
                        "api-key": brevo_key,
                        "accept": "application/json",
                        "content-type": "application/json",
                    },
                    json=payload,
                    timeout=8,
                )
                if resp.status_code in (200, 201, 202):
                    result["success"] = True
                    logger.info("OTP sent to %s via Brevo HTTPS API", email)
                    return
                else:
                    logger.warning("Brevo API failed (%s): %s", resp.status_code, resp.text)
            except Exception as b_exc:
                logger.warning("Brevo request error: %s", b_exc)

        # Fallback to standard Django send_mail (SMTP or Console)
        try:
            send_mail(
                "Your attendance app OTP",
                f"Your OTP is {code}. It is valid for {settings.OTP_TTL_MINUTES} minutes.",
                None, [email],
            )
            result["success"] = True
        except Exception as exc:
            logger.warning("Email send failed: %s", exc)

    t = threading.Thread(target=_send, daemon=True)
    t.start()
    t.join(timeout=8)  # wait at most 8 seconds for email to send

    if not result["success"]:
        # Fallback: print to console so the OTP is still readable during testing
        logger.warning("⚠️  OTP email not sent. Code for %s: %s", email, code)
        print(f"\n{'='*50}\n⚠️  OTP for {email}: {code}\n{'='*50}\n")



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
