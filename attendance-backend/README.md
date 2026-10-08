# QR Attendance - Django backend

## Run
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py createsuperuser      # becomes admin
    python manage.py runserver
    python manage.py test                 # 14 tests
For MySQL, install `requirements-mysql.txt` instead of `requirements.txt`.
Create teachers in /admin (role = teacher). Students self-register in the app.
Set env vars from `.env.example` (set DB_NAME to use MySQL). For production, set `DEBUG=0`
and replace `DJANGO_SECRET_KEY` with a unique random value of at least 32 characters.

## API
| Endpoint | Who | Purpose |
|---|---|---|
| POST /api/auth/otp/request/ `{email}` | all | Email OTP; returns `is_new_user` |
| POST /api/auth/otp/verify/ `{email, otp, device_id, name?, roll_number?}` | all | Login, or sign up if new (name + roll_number needed) |
| POST /api/auth/google/ `{id_token, device_id, roll_number?}` | all | Google sign-in |
| POST /api/auth/logout/ `{refresh}` | all | Logout; starts the 30-min phone cooldown |
| POST /api/auth/token/refresh/ | all | Refresh JWT |
| POST /api/sessions/ `{subject, latitude, longitude, radius_m?, duration_minutes?}` | teacher | Start class (teacher's GPS = class centre) |
| GET /api/sessions/?date=YYYY-MM-DD | teacher | Date-wise list |
| GET /api/sessions/{id}/qr/ | teacher | `{token, refresh_in}` - poll every ~3 s, render as QR |
| GET /api/sessions/{id}/live/ | teacher | Live present list |
| POST /api/sessions/{id}/end/ | teacher | Close session |
| GET /api/sessions/{id}/export/ , /api/export/?date= | teacher | Excel (.xlsx) |
| POST /api/attendance/scan/ `{token, device_id, latitude, longitude, accuracy, is_mock}` | student | Mark attendance |
| GET /api/attendance/my/ | student | Own history |

## Scan checks (in order)
registered phone -> not mock GPS -> GPS accuracy <= 50 m -> QR signature -> QR slot (current or previous 3 s slot, server clock) -> session open -> within radius -> one record per student per session. Every attempt is stored in ScanLog.

## Device rules
- `device_id` = Android ID / app-generated ID (real MAC addresses are not available on Android 10+).
- One phone = one student account; one account = one phone.
- Logout starts a 30-min lock: another account cannot sign in on that phone until it ends.
- Lost/changed phone: Admin -> Devices -> action "Reset device".
