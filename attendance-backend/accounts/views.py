from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from . import services
from .models import Device, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "name", "roll_number", "role"]


def tokens_for(user, device_id):
    refresh = RefreshToken.for_user(user)
    refresh["device_id"] = device_id or ""
    return {"access": str(refresh.access_token), "refresh": str(refresh), "user": UserSerializer(user).data}


def complete_login(email, device_id, name=None, roll_number=None):
    """Log in an existing user or sign up a new student, then bind the phone. All-or-nothing."""
    email = email.strip().lower()
    with transaction.atomic():
        user = User.objects.filter(email=email).first()
        if user is None:
            roll_number = (roll_number or "").strip().upper()
            if not (name or "").strip() or not roll_number:
                raise ValidationError({"detail": "name and roll_number are required to sign up.",
                                       "code": "signup_details_required"})
            if User.objects.filter(roll_number=roll_number).exists():
                raise ValidationError({"detail": "This roll number is already registered.", "code": "roll_taken"})
            user = User.objects.create_user(email=email, name=name.strip(), roll_number=roll_number, role="student")
        if not user.is_active:
            raise PermissionDenied("Account disabled.")
        services.bind_device(user, device_id)
    return tokens_for(user, device_id)


class OTPRequestView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def post(self, request):
        email = serializers.EmailField().run_validation(request.data.get("email", "")).strip().lower()
        services.send_otp(email)
        return Response({"detail": "OTP sent.", "is_new_user": not User.objects.filter(email=email).exists()})


class OTPVerifyView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        d = request.data
        email = serializers.EmailField().run_validation(d.get("email", "")).strip().lower()
        if not services.check_otp(email, str(d.get("otp", ""))):
            raise ValidationError({"detail": "Invalid or expired OTP.", "code": "bad_otp"})
        return Response(complete_login(email, d.get("device_id"), d.get("name"), d.get("roll_number")))


class GoogleLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        d = request.data
        try:
            info = services.verify_google_token(d.get("id_token", ""))
        except Exception:
            raise ValidationError({"detail": "Invalid Google token.", "code": "bad_google_token"})
        name = d.get("name") or info.get("name")
        return Response(complete_login(info["email"], d.get("device_id"), name, d.get("roll_number")))


class LogoutView(APIView):
    def post(self, request):
        try:
            RefreshToken(request.data.get("refresh", "")).blacklist()
        except TokenError:
            pass
        Device.objects.filter(user=request.user).update(last_logout_at=timezone.now())
        return Response({"detail": "Logged out."})


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)
