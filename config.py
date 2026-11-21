import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", BASE_DIR))
DB_PATH = os.path.join(DATA_DIR, "attendance.db")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
SECRET_FILE = os.path.join(DATA_DIR, ".secret_key")
SESSION_SECRET = os.environ.get("SESSION_SECRET")

# --- Camera / recognition ---
CAMERA_INDEX = 0          # server webcam used for live attendance
TOLERANCE = 0.50          # lower = stricter matching
FRAME_SCALE = 0.5         # downscale for faster detection
PROCESS_EVERY_N = 2       # run recognition on every Nth frame

# --- Anti-spoofing: person must blink once before being marked ---
REQUIRE_BLINK = True
EAR_THRESHOLD = 0.21
BLINK_VALID_SECONDS = 6

# --- Enrollment ---
ENROLL_MIN_SAMPLES = 10
DUPLICATE_DISTANCE = 0.40  # reject enrollment if face already belongs to someone else

# --- Alerts / analytics ---
UNKNOWN_LOG_INTERVAL = 15  # seconds between unknown-face snapshots
ATTENDANCE_THRESHOLD = 75  # % below which a student is flagged

FLASK_HOST = "127.0.0.1"
FLASK_PORT = int(os.environ.get("PORT", "5000"))

# --- Telegram bot (optional) ---
# 1) In Telegram, talk to @BotFather -> /newbot -> copy the token.
# 2) Paste it below OR set the TELEGRAM_BOT_TOKEN environment variable. Leave empty to disable.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
