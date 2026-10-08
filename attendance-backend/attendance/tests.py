import re
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Device, User
from attendance import qr
from attendance.models import ScanLog

LAT, LON = 22.7196, 75.8577          # classroom
NEAR = (LAT + 0.0001, LON)           # ~11 m away
FAR = (LAT + 0.001, LON)             # ~111 m away
T0 = 1_800_000_000.0                 # fixed fake "server time"


def otp_signup(client, email, roll, device, name="Stu"):
    client.post("/api/auth/otp/request/", {"email": email}, format="json")
    code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
    return client.post("/api/auth/otp/verify/", {"email": email, "otp": code, "name": name,
                                                  "roll_number": roll, "device_id": device}, format="json")


class AttendanceFlow(TestCase):
    def setUp(self):
        cache.clear()
        self.teacher = User.objects.create_user("t@x.com", name="Teacher", role="teacher")
        self.tc = APIClient()
        self.tc.force_authenticate(self.teacher)
        r = self.tc.post("/api/sessions/", {"subject": "DBMS", "latitude": LAT, "longitude": LON}, format="json")
        self.sid = r.data["id"]
        self.session = self.teacher.class_sessions.get(pk=self.sid)

    def student(self, n=1, device=None):
        c = APIClient()
        r = otp_signup(c, f"s{n}@x.com", f"R{n}", device or f"dev{n}")
        self.assertEqual(r.status_code, 200, r.data)
        c.credentials(HTTP_AUTHORIZATION="Bearer " + r.data["access"])
        return c, device or f"dev{n}", r.data["refresh"]

    def scan(self, c, device, loc=NEAR, token=None, acc=8, mock=False):
        token = token or qr.make_token(self.session)
        return c.post("/api/attendance/scan/", {"token": token, "device_id": device, "latitude": loc[0],
                      "longitude": loc[1], "accuracy": acc, "is_mock": mock}, format="json")

    # ---- QR rules ----
    def test_valid_scan_marks_present_and_duplicate_is_idempotent(self):
        c, dev, _ = self.student()
        with patch("attendance.qr._now", return_value=T0):
            r = self.scan(c, dev)
            self.assertTrue(r.data["success"])
            self.assertTrue(self.scan(c, dev).data["already_marked"])
        self.assertEqual(self.session.records.count(), 1)
        self.assertEqual(
            list(ScanLog.objects.order_by("id").values_list("success", "reason")),
            [(True, "ok"), (True, "already_marked")],
        )

    def test_qr_valid_in_grace_but_dead_after_6_seconds(self):
        c, dev, _ = self.student()
        with patch("attendance.qr._now", return_value=T0):
            token = qr.make_token(self.session)
        with patch("attendance.qr._now", return_value=T0 + 4):      # 1 slot later -> grace
            self.assertEqual(qr.validate_token(self.session, token), None)
        with patch("attendance.qr._now", return_value=T0 + 20):     # WhatsApp-forwarded
            r = self.scan(c, dev, token=token)
        self.assertEqual(r.data["reason"], "qr_expired")
        self.assertEqual(self.session.records.count(), 0)

    def test_forged_token_rejected(self):
        c, dev, _ = self.student()
        with patch("attendance.qr._now", return_value=T0):
            slot = qr.current_slot()
            r = self.scan(c, dev, token=f"{self.sid}.{slot}.deadbeefdeadbeefdead")
        self.assertEqual(r.data["reason"], "qr_invalid")

    # ---- Location rules ----
    def test_too_far_rejected(self):
        c, dev, _ = self.student()
        with patch("attendance.qr._now", return_value=T0):
            self.assertEqual(self.scan(c, dev, loc=FAR).data["reason"], "too_far")

    def test_poor_gps_and_mock_location_rejected(self):
        c, dev, _ = self.student()
        with patch("attendance.qr._now", return_value=T0):
            self.assertEqual(self.scan(c, dev, acc=120).data["reason"], "poor_gps")
            self.assertEqual(self.scan(c, dev, mock=True).data["reason"], "mock_location")

    # ---- Device rules ----
    def test_scan_from_unregistered_phone_rejected(self):
        c, dev, _ = self.student()
        with patch("attendance.qr._now", return_value=T0):
            self.assertEqual(self.scan(c, "some-other-phone").data["reason"], "device_mismatch")

    def test_phone_cooldown_30_min_after_logout(self):
        c, dev, refresh = self.student(1, "shared-phone")
        c.post("/api/auth/logout/", {"refresh": refresh}, format="json")
        r = otp_signup(APIClient(), "friend@x.com", "R99", "shared-phone")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data["code"], "device_cooldown")
        self.assertFalse(User.objects.filter(email="friend@x.com").exists())   # signup rolled back
        past = timezone.now() - timedelta(minutes=31)
        Device.objects.filter(device_id="shared-phone").update(last_login_at=past - timedelta(minutes=5), last_logout_at=past)
        self.assertEqual(otp_signup(APIClient(), "friend@x.com", "R99", "shared-phone").status_code, 200)

    def test_phone_still_signed_in_cannot_be_used_by_another(self):
        self.student(1, "shared-phone")
        r = otp_signup(APIClient(), "friend@x.com", "R99", "shared-phone")
        self.assertEqual(r.data["code"], "device_in_use")

    def test_account_cannot_move_to_second_phone(self):
        self.student(1, "phone-A")
        c = APIClient()
        c.post("/api/auth/otp/request/", {"email": "s1@x.com"}, format="json")
        code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
        r = c.post("/api/auth/otp/verify/", {"email": "s1@x.com", "otp": code, "device_id": "phone-B"}, format="json")
        self.assertEqual(r.data["code"], "account_bound_elsewhere")

    # ---- Auth + teacher features ----
    def test_wrong_otp_and_missing_signup_fields(self):
        c = APIClient()
        c.post("/api/auth/otp/request/", {"email": "n@x.com"}, format="json")
        r = c.post("/api/auth/otp/verify/", {"email": "n@x.com", "otp": "000000", "device_id": "d"}, format="json")
        self.assertEqual(r.data["code"], "bad_otp")

    def test_google_login_creates_student(self):
        info = {"email": "g@x.com", "name": "Gee", "email_verified": True}
        with patch("accounts.services.verify_google_token", return_value=info):
            r = APIClient().post("/api/auth/google/", {"id_token": "t", "device_id": "gdev", "roll_number": "G1"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["user"]["roll_number"], "G1")

    def test_student_cannot_use_teacher_endpoints(self):
        c, _, _ = self.student()
        self.assertEqual(c.get(f"/api/sessions/{self.sid}/qr/").status_code, 403)

    def test_live_list_and_excel_export(self):
        c, dev, _ = self.student()
        with patch("attendance.qr._now", return_value=T0):
            self.scan(c, dev)
        live = self.tc.get(f"/api/sessions/{self.sid}/live/").data
        self.assertEqual(live["present_count"], 1)
        r = self.tc.get(f"/api/sessions/{self.sid}/export/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content[:2], b"PK")      # valid xlsx (zip)
        day = timezone.localdate().isoformat()
        self.assertEqual(self.tc.get(f"/api/export/?date={day}").status_code, 200)

    def test_closed_session_rejects_scan_and_qr(self):
        c, dev, _ = self.student()
        self.tc.post(f"/api/sessions/{self.sid}/end/")
        with patch("attendance.qr._now", return_value=T0):
            self.assertEqual(self.scan(c, dev).data["reason"], "session_closed")
        self.assertEqual(self.tc.get(f"/api/sessions/{self.sid}/qr/").status_code, 400)
