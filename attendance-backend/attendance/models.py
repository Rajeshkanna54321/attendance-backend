import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def new_secret():
    return secrets.token_hex(32)


class ClassSession(models.Model):
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="class_sessions")
    subject = models.CharField(max_length=100)
    latitude = models.FloatField()
    longitude = models.FloatField()
    radius_m = models.PositiveIntegerField(default=30)
    secret = models.CharField(max_length=64, default=new_secret, editable=False)
    started_at = models.DateTimeField(auto_now_add=True)
    ends_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    @property
    def is_open(self):
        return self.is_active and timezone.now() < self.ends_at

    def __str__(self):
        return f"{self.subject} ({self.started_at:%Y-%m-%d %H:%M})"


class Attendance(models.Model):
    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name="records")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attendances")
    marked_at = models.DateTimeField(auto_now_add=True)
    distance_m = models.FloatField()

    class Meta:
        unique_together = ("session", "student")


class ScanLog(models.Model):
    """Every scan attempt (accepted or rejected) for audit / disputes."""
    session = models.ForeignKey(ClassSession, null=True, blank=True, on_delete=models.SET_NULL, related_name="scan_logs")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField()
    reason = models.CharField(max_length=40)
    distance_m = models.FloatField(null=True, blank=True)
    accuracy_m = models.FloatField(null=True, blank=True)
