from django.contrib import admin
from .models import Device, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "roll_number", "role", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("email", "name", "roll_number")


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("device_id", "user", "last_login_at", "last_logout_at")
    search_fields = ("user__email", "user__roll_number", "device_id")
    actions = ["reset_device"]

    @admin.action(description="Reset device (unlink student, e.g. lost/changed phone)")
    def reset_device(self, request, queryset):
        queryset.update(user=None, last_logout_at=None, last_login_at=None)
