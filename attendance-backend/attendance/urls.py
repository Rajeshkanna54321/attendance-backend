from django.urls import path
from . import views

urlpatterns = [
    path("sessions/", views.SessionListStartView.as_view()),
    path("sessions/<int:pk>/qr/", views.QRView.as_view()),
    path("sessions/<int:pk>/live/", views.LiveView.as_view()),
    path("sessions/<int:pk>/end/", views.EndSessionView.as_view()),
    path("sessions/<int:pk>/export/", views.ExportView.as_view()),
    path("export/", views.ExportView.as_view()),
    path("attendance/scan/", views.ScanView.as_view()),
    path("attendance/my/", views.MyAttendanceView.as_view()),
]
