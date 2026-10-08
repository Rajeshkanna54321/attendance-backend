from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import qr
from .excel import build_workbook
from .geo import haversine_m
from .models import Attendance, ClassSession, ScanLog


class IsTeacher(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user.is_authenticated and request.user.role in ("teacher", "admin"))


class IsStudent(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user.is_authenticated and request.user.role == "student")


def session_dict(s):
    present_count = getattr(s, "present_count", None)
    if present_count is None:
        present_count = s.records.count()
    return {"id": s.id, "subject": s.subject, "started_at": s.started_at, "ends_at": s.ends_at,
            "is_open": s.is_open, "radius_m": s.radius_m,
            "present_count": present_count}


def own_session(request, pk):
    qs = ClassSession.objects.all() if request.user.role == "admin" else ClassSession.objects.filter(teacher=request.user)
    return get_object_or_404(qs, pk=pk)


# ---------------- Teacher ----------------
class StartSessionSerializer(serializers.Serializer):
    subject = serializers.CharField(max_length=100)
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    radius_m = serializers.IntegerField(min_value=10, max_value=500, default=settings.DEFAULT_RADIUS_M)
    duration_minutes = serializers.IntegerField(min_value=1, max_value=300, default=60)


class SessionListStartView(APIView):
    permission_classes = [IsTeacher]

    def get(self, request):
        qs = ClassSession.objects.filter(teacher=request.user).annotate(present_count=Count("records"))
        if request.query_params.get("date"):
            try:
                qs = qs.filter(started_at__date=date.fromisoformat(request.query_params["date"]))
            except ValueError:
                raise ValidationError({"detail": "date must be YYYY-MM-DD"})
        return Response([session_dict(s) for s in qs.order_by("-started_at")[:200]])

    def post(self, request):
        s = StartSessionSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        session = ClassSession.objects.create(
            teacher=request.user, subject=d["subject"], latitude=d["latitude"], longitude=d["longitude"],
            radius_m=d["radius_m"], ends_at=timezone.now() + timedelta(minutes=d["duration_minutes"]))
        return Response(session_dict(session), status=201)


class QRView(APIView):
    """Teacher app polls this every ~3 s (or 1 s) and renders `token` as a QR code."""
    permission_classes = [IsTeacher]

    def get(self, request, pk):
        session = own_session(request, pk)
        if not session.is_open:
            raise ValidationError({"detail": "Session is closed.", "code": "session_closed"})
        return Response({"token": qr.make_token(session), "refresh_in": qr.seconds_left_in_slot()})


class LiveView(APIView):
    permission_classes = [IsTeacher]

    def get(self, request, pk):
        session = own_session(request, pk)
        rows = session.records.select_related("student").order_by("-marked_at")
        return Response({**session_dict(session), "students": [
            {"roll_number": r.student.roll_number, "name": r.student.name, "marked_at": r.marked_at} for r in rows]})


class EndSessionView(APIView):
    permission_classes = [IsTeacher]

    def post(self, request, pk):
        session = own_session(request, pk)
        session.is_active = False
        session.save(update_fields=["is_active"])
        return Response(session_dict(session))


class ExportView(APIView):
    """GET /api/sessions/<id>/export/  or  /api/export/?date=YYYY-MM-DD  -> .xlsx"""
    permission_classes = [IsTeacher]

    def get(self, request, pk=None):
        if pk:
            sessions, name = [own_session(request, pk)], f"attendance_session_{pk}"
        else:
            try:
                day = date.fromisoformat(request.query_params.get("date", ""))
            except ValueError:
                raise ValidationError({"detail": "date=YYYY-MM-DD is required"})
            sessions = list(ClassSession.objects.filter(teacher=request.user, started_at__date=day))
            name = f"attendance_{day}"
        resp = HttpResponse(build_workbook(sessions),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
        return resp


# ---------------- Student ----------------
class ScanSerializer(serializers.Serializer):
    token = serializers.CharField()
    device_id = serializers.CharField()
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    accuracy = serializers.FloatField(min_value=0)
    is_mock = serializers.BooleanField(default=False)


class ScanView(APIView):
    permission_classes = [IsStudent]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "scan"

    def post(self, request):
        s = ScanSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d, user = s.validated_data, request.user
        session, dist = None, None

        def reject(reason, msg):
            ScanLog.objects.create(session=session, student=user, success=False, reason=reason,
                                   distance_m=dist, accuracy_m=d["accuracy"])
            return Response({"success": False, "reason": reason, "detail": msg}, status=400)

        device = getattr(user, "device", None)
        if device is None or device.device_id != d["device_id"]:
            return reject("device_mismatch", "This is not your registered phone.")
        if d["is_mock"]:
            return reject("mock_location", "Mock location detected.")
        if d["accuracy"] > settings.MAX_GPS_ACCURACY_M:
            return reject("poor_gps", "GPS signal too weak. Move near a window/open area and retry.")

        session = ClassSession.objects.filter(pk=qr.parse_session_id(d["token"])).first()
        if session is None:
            return reject("qr_invalid", "Invalid QR code.")
        if not session.is_open:
            return reject("session_closed", "This class session is closed.")
        reason = qr.validate_token(session, d["token"])
        if reason:
            return reject(reason, "QR code expired or invalid. Scan the live QR on the teacher's screen.")

        dist = haversine_m(d["latitude"], d["longitude"], session.latitude, session.longitude)
        if dist > session.radius_m:
            return reject("too_far", f"You are {int(dist)} m away; must be within {session.radius_m} m.")

        try:
            with transaction.atomic():
                Attendance.objects.create(session=session, student=user, distance_m=dist)
        except IntegrityError:
            ScanLog.objects.create(session=session, student=user, success=True, reason="already_marked",
                                   distance_m=dist, accuracy_m=d["accuracy"])
            return Response({"success": True, "already_marked": True, "detail": "Already marked present."})
        ScanLog.objects.create(session=session, student=user, success=True, reason="ok",
                               distance_m=dist, accuracy_m=d["accuracy"])
        return Response({"success": True, "already_marked": False, "subject": session.subject,
                         "detail": "Attendance marked."})


class MyAttendanceView(APIView):
    permission_classes = [IsStudent]

    def get(self, request):
        rows = Attendance.objects.filter(student=request.user).select_related("session").order_by("-marked_at")[:200]
        return Response([{"subject": r.session.subject, "marked_at": r.marked_at} for r in rows])
