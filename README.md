# Face Recognition Attendance System (v3)

Webcam -> dlib face detection -> 128-D embeddings -> blink liveness -> SQLite -> Flask admin dashboard.

## Setup (Python 3.12 on Windows)
```
py -3.12 -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install dlib-bin
pip install face_recognition --no-deps
pip install face_recognition_models Click Pillow numpy opencv-python flask reportlab "setuptools<81"
python app.py
```
Open http://127.0.0.1:5000 and sign in with **admin / admin123** (you must change it on first login).
Other OS: `pip install dlib` needs CMake + a C++ compiler; then `pip install -r requirements.txt face_recognition`.
**Upgrading from v2:** copy your old `attendance.db` and `snapshots/` folder into this folder. The database upgrades itself and keeps all data.
(Coming from v1? Delete the old database; v1 had no classes.)

## Deploying on Railway

Deploy this repository as a Dockerfile-based service. The included Dockerfile installs the Linux build dependencies for dlib and runs the Flask app with Gunicorn.

1. Use the repository root as the service's **Root Directory** (the default; change it from `/face_attendance` if previously configured).
2. Attach a Railway volume to the service at `/data`.
3. Set `DATA_DIR=/data` and `SESSION_SECRET` to a long, random secret in Railway's Variables settings.
4. Optionally set `TELEGRAM_BOT_TOKEN` if using Telegram.

SQLite data and snapshots are written under `DATA_DIR`; they are lost when the service is redeployed unless a volume is mounted. The app creates an initial `admin / admin123` account on a fresh database and requires a password change at first login.

**Railway does not provide a server-side webcam.** The dashboard can run there, but live camera attendance requires the app's camera to be attached to the server, so that feature will not work on Railway as-is. Keep `.secret_key` and face snapshots out of Git; they are ignored by the repository and Docker build. Use a fresh `SESSION_SECRET` in Railway instead of reusing the local key. If this repository was previously cloned or public, rotate the old key even after removing it from Git history.

## Workflow
1. **Classes** – add a class (e.g. "CSE-A Sem 7") and its subjects.
2. **Enroll** – fill ID/name/class, Start camera, Capture 30 photos (turn your head slightly).
   Duplicate faces are rejected. Stop any live session first so the browser can use the webcam.
3. **Live** – choose class + subject, Start session. People look at the camera and blink once.
   Only students of that class are marked; others show "(other class)"; strangers show "Unknown".
4. **Reports / Analytics** – filter by class, subject, person and dates; CSV and PDF export; charts
   (needs internet for Chart.js CDN); students under 75% are flagged (`ATTENDANCE_THRESHOLD`).
5. **Alerts** – snapshots of unknown faces seen during sessions.

## New in v3
- **Voice greeting** - on the Live page click "Enable voice greeting" (browsers need one click). It says "Good morning, Asha" as each person is marked.
- **Student login** - students sign in at `/student/login` with their ID and the temporary password shown after enrollment (or from Users -> Reset password). They see overall and per-subject attendance, absences, and can link Telegram.
- **Manual override** - Sessions page: open any session (or create a past one), mark a student present or absent with a note. Face vs manual is shown in reports. Every change goes to the **Audit** log.
- **Telegram bot** - see setup below.

## Telegram bot setup (optional, needs internet)
1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts, copy the token.
2. Paste it into `TELEGRAM_BOT_TOKEN` in `config.py` (or set the `TELEGRAM_BOT_TOKEN` environment variable). Restart `python app.py`; the terminal prints `bot @yourbot is running`.
3. A student logs in, clicks **Connect Telegram**, then sends `/link 123456` to the bot.
4. Bot commands: `/attendance`, `/today`, `/unlink`, `/help`. The bot also messages the student when they are marked present, and (if ticked when stopping a session) tells absent students.

## Features
Voice greeting · student portal · manual override + audit log · Telegram bot · Admin login (hashed passwords, CSRF protection, forced password change) · web enrollment ·
live MJPEG camera feed · classes/subjects/sessions · per-check-in photo proof (`snapshots/`) ·
unknown-face log · analytics charts · PDF/CSV reports · blink anti-spoofing · duplicate-face check.

## Files
`app.py` routes · `database.py` SQLite layer · `recognizer.py` camera thread + recognition ·
`telegram_bot.py` bot + alerts · `reports.py` PDF · `config.py` settings · `templates/`, `static/` UI.

## Tuning (config.py)
`TOLERANCE` (0.45 stricter / 0.55 looser) · `CAMERA_INDEX` · `PROCESS_EVERY_N` and `FRAME_SCALE` for speed ·
`REQUIRE_BLINK` · `ATTENDANCE_THRESHOLD`.

## Limits & privacy
Blink check stops photo spoofing, not video replays. Face data is biometric: get consent, back up and
protect `attendance.db` and `snapshots/`. Run on localhost or behind HTTPS/VPN; don't expose it directly to the internet.
