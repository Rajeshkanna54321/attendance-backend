from django.contrib import admin
from .models import Attendance, ClassSession, ScanLog

admin.site.register(ClassSession)
admin.site.register(Attendance)


@admin.register(ScanLog)
class ScanLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "student", "session", "success", "reason", "distance_m")
    list_filter = ("success", "reason")
