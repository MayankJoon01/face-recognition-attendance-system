import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

import numpy as np
from werkzeug.security import check_password_hash, generate_password_hash

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, must_change INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    UNIQUE(name, class_id));
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL, created_at TEXT NOT NULL,
    password_hash TEXT, must_change INTEGER NOT NULL DEFAULT 1,
    telegram_chat_id TEXT, link_code TEXT, link_expires TEXT);
CREATE TABLE IF NOT EXISTS encodings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, encoding BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    date TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    UNIQUE(class_id, subject_id, date));
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    time TEXT NOT NULL, snapshot TEXT,
    source TEXT NOT NULL DEFAULT 'face', note TEXT,
    UNIQUE(session_id, user_id));
CREATE TABLE IF NOT EXISTS unknown_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    time TEXT NOT NULL, snapshot TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT);
"""

# Columns added after v2: applied to existing databases so old data is kept.
MIGRATIONS = {
    "users": [("password_hash", "TEXT"), ("must_change", "INTEGER NOT NULL DEFAULT 1"),
              ("telegram_chat_id", "TEXT"), ("link_code", "TEXT"), ("link_expires", "TEXT")],
    "attendance": [("source", "TEXT NOT NULL DEFAULT 'face'"), ("note", "TEXT")],
    "sessions": [("notified", "INTEGER NOT NULL DEFAULT 0")],
}


def _migrate(c):
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _rows(c, q, args=()):
    return [dict(r) for r in c.execute(q, args).fetchall()]


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)
        if not c.execute("SELECT 1 FROM admins").fetchone():
            c.execute("INSERT INTO admins VALUES (?, ?, 1)",
                      ("admin", generate_password_hash("admin123")))


# ---------- admins ----------
def verify_admin(username, password):
    with conn() as c:
        r = c.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
    if r and check_password_hash(r["password_hash"], password):
        return dict(r)
    return None


def set_password(username, password):
    with conn() as c:
        c.execute("UPDATE admins SET password_hash = ?, must_change = 0 WHERE username = ?",
                  (generate_password_hash(password), username))


# ---------- classes & subjects ----------
def add_class(name):
    with conn() as c:
        c.execute("INSERT INTO classes (name) VALUES (?)", (name,))


def list_classes():
    with conn() as c:
        return _rows(c, """SELECT c.id, c.name,
            (SELECT COUNT(*) FROM users u WHERE u.class_id = c.id) AS students
            FROM classes c ORDER BY c.name""")


def delete_class(cid):
    with conn() as c:
        c.execute("DELETE FROM classes WHERE id = ?", (cid,))


def add_subject(name, class_id):
    with conn() as c:
        c.execute("INSERT INTO subjects (name, class_id) VALUES (?, ?)", (name, class_id))


def list_subjects(class_id=None):
    q = """SELECT s.id, s.name, s.class_id, c.name AS class_name FROM subjects s
           JOIN classes c ON c.id = s.class_id"""
    args = []
    if class_id:
        q += " WHERE s.class_id = ?"
        args.append(class_id)
    with conn() as c:
        return _rows(c, q + " ORDER BY c.name, s.name", args)


def delete_subject(sid):
    with conn() as c:
        c.execute("DELETE FROM subjects WHERE id = ?", (sid,))


# ---------- users ----------
def add_user(uid, name, class_id, encodings, password_hash=None):
    """Create or update a user and replace their face encodings. True if newly created."""
    with conn() as c:
        exists = c.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone()
        if exists:
            c.execute("UPDATE users SET name = ?, class_id = ? WHERE id = ?", (name, class_id, uid))
            c.execute("DELETE FROM encodings WHERE user_id = ?", (uid,))
        else:
            c.execute("INSERT INTO users (id, name, class_id, created_at, password_hash, must_change) "
                      "VALUES (?, ?, ?, ?, ?, 1)",
                      (uid, name, class_id, datetime.now().isoformat(timespec="seconds"), password_hash))
        c.executemany("INSERT INTO encodings (user_id, encoding) VALUES (?, ?)",
                      [(uid, np.asarray(e, dtype=np.float64).tobytes()) for e in encodings])
        return not exists


def load_known():
    """(ids, names, class_ids, encodings) with one entry per stored encoding."""
    with conn() as c:
        rows = c.execute("""SELECT e.user_id, u.name, u.class_id, e.encoding FROM encodings e
                            JOIN users u ON u.id = e.user_id""").fetchall()
    ids = [r["user_id"] for r in rows]
    names = [r["name"] for r in rows]
    classes = [r["class_id"] for r in rows]
    encs = np.array([np.frombuffer(r["encoding"], dtype=np.float64) for r in rows])
    return ids, names, classes, encs


def list_users(class_id=None):
    q = """SELECT u.id, u.name, u.class_id, c.name AS class_name, u.created_at,
           (SELECT COUNT(*) FROM encodings e WHERE e.user_id = u.id) AS samples
           FROM users u LEFT JOIN classes c ON c.id = u.class_id"""
    args = []
    if class_id:
        q += " WHERE u.class_id = ?"
        args.append(class_id)
    with conn() as c:
        return _rows(c, q + " ORDER BY u.name", args)


def delete_user(uid):
    with conn() as c:
        c.execute("DELETE FROM users WHERE id = ?", (uid,))


# ---------- sessions & attendance ----------
def get_or_create_session(class_id, subject_id, day=None, reopen=True):
    """One session per class + subject + date. day=None means today."""
    day = day or date.today().isoformat()
    now = datetime.now().strftime("%H:%M:%S")
    with conn() as c:
        r = c.execute("SELECT id FROM sessions WHERE class_id=? AND subject_id=? AND date=?",
                      (class_id, subject_id, day)).fetchone()
        if r:
            if reopen:
                c.execute("UPDATE sessions SET ended_at = NULL WHERE id = ?", (r["id"],))
            return r["id"]
        manual = not reopen
        return c.execute("INSERT INTO sessions (class_id, subject_id, date, started_at, ended_at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (class_id, subject_id, day, "00:00:00" if manual else now,
                          "00:00:00" if manual else None)).lastrowid


def get_session(sid):
    with conn() as c:
        r = c.execute("""SELECT s.*, c.name AS class_name, sub.name AS subject_name
            FROM sessions s JOIN classes c ON c.id = s.class_id
            JOIN subjects sub ON sub.id = s.subject_id WHERE s.id = ?""", (sid,)).fetchone()
    return dict(r) if r else None


def end_session(sid):
    with conn() as c:
        c.execute("UPDATE sessions SET ended_at = ? WHERE id = ?",
                  (datetime.now().strftime("%H:%M:%S"), sid))


def mark_attendance(session_id, uid, snapshot=None, source="face", note=None):
    with conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO attendance (session_id, user_id, time, snapshot, source, note) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, uid, datetime.now().strftime("%H:%M:%S"), snapshot, source, note))
        return cur.rowcount > 0


def unmark_attendance(session_id, uid):
    with conn() as c:
        return c.execute("DELETE FROM attendance WHERE session_id = ? AND user_id = ?",
                         (session_id, uid)).rowcount > 0


def marked_user_ids(session_id):
    with conn() as c:
        return {r["user_id"] for r in
                c.execute("SELECT user_id FROM attendance WHERE session_id = ?", (session_id,))}


def session_marks(session_id):
    with conn() as c:
        return _rows(c, """SELECT a.time, a.snapshot, a.source, u.id AS user_id, u.name FROM attendance a
            JOIN users u ON u.id = a.user_id WHERE a.session_id = ? ORDER BY a.time DESC""",
                     (session_id,))


def log_unknown(session_id, snapshot):
    with conn() as c:
        c.execute("INSERT INTO unknown_log (session_id, time, snapshot) VALUES (?, ?, ?)",
                  (session_id, datetime.now().isoformat(timespec="seconds"), snapshot))


def list_unknown(limit=120):
    with conn() as c:
        return _rows(c, """SELECT l.id, l.time, l.snapshot, c.name AS class_name, sub.name AS subject_name
            FROM unknown_log l LEFT JOIN sessions s ON s.id = l.session_id
            LEFT JOIN classes c ON c.id = s.class_id LEFT JOIN subjects sub ON sub.id = s.subject_id
            ORDER BY l.id DESC LIMIT ?""", (limit,))


def clear_unknown():
    with conn() as c:
        c.execute("DELETE FROM unknown_log")


# ---------- reports ----------
def get_records(start, end, class_id=None, subject_id=None, user_id=None):
    q = """SELECT s.date, a.time, u.id AS user_id, u.name, c.name AS class_name,
           sub.name AS subject_name, a.snapshot, a.source, a.note
           FROM attendance a JOIN sessions s ON s.id = a.session_id
           JOIN users u ON u.id = a.user_id
           JOIN classes c ON c.id = s.class_id JOIN subjects sub ON sub.id = s.subject_id
           WHERE s.date BETWEEN ? AND ?"""
    args = [start, end]
    for col, val in (("s.class_id", class_id), ("s.subject_id", subject_id), ("u.id", user_id)):
        if val:
            q += f" AND {col} = ?"
            args.append(val)
    with conn() as c:
        return _rows(c, q + " ORDER BY s.date DESC, a.time DESC", args)


def get_stats(class_id, subject_id, start, end):
    q = "SELECT id, date FROM sessions WHERE class_id = ? AND date BETWEEN ? AND ?"
    args = [class_id, start, end]
    if subject_id:
        q += " AND subject_id = ?"
        args.append(subject_id)
    with conn() as c:
        sessions = _rows(c, q, args)
        students = _rows(c, "SELECT id, name FROM users WHERE class_id = ? ORDER BY name", (class_id,))
        sids = [s["id"] for s in sessions]
        marks = []
        if sids:
            marks = _rows(c, f"SELECT session_id, user_id FROM attendance WHERE session_id IN "
                             f"({','.join('?' * len(sids))})", sids)
    total = len(sessions)
    present = {}
    for m in marks:
        present[m["user_id"]] = present.get(m["user_id"], 0) + 1
    rows = []
    for s in students:
        p = present.get(s["id"], 0)
        rows.append({"id": s["id"], "name": s["name"], "present": p, "total": total,
                     "pct": round(100 * p / total, 1) if total else 0.0})
    date_of = {s["id"]: s["date"] for s in sessions}
    per_day = {}
    for s in sessions:
        per_day.setdefault(s["date"], [0, 0])[0] += 1
    for m in marks:
        per_day[date_of[m["session_id"]]][1] += 1
    n = len(students)
    daily = [{"date": d, "sessions": v[0], "present": v[1],
              "pct": round(100 * v[1] / (v[0] * n), 1) if n else 0.0}
             for d, v in sorted(per_day.items())]
    avg = round(sum(r["pct"] for r in rows) / len(rows), 1) if rows else 0.0
    return {"total_sessions": total, "students": rows, "daily": daily, "avg": avg}


def dashboard_stats():
    today = date.today().isoformat()
    with conn() as c:
        one = lambda q, a=(): c.execute(q, a).fetchone()[0]
        return {
            "today": today,
            "enrolled": one("SELECT COUNT(*) FROM users"),
            "sessions": one("SELECT COUNT(*) FROM sessions WHERE date = ?", (today,)),
            "checkins": one("""SELECT COUNT(*) FROM attendance a JOIN sessions s ON s.id = a.session_id
                               WHERE s.date = ?""", (today,)),
            "unknown": one("SELECT COUNT(*) FROM unknown_log WHERE time LIKE ?", (today + "%",)),
            "recent": _rows(c, """SELECT a.time, a.snapshot, u.name, c.name AS class_name,
                sub.name AS subject_name FROM attendance a JOIN sessions s ON s.id = a.session_id
                JOIN users u ON u.id = a.user_id JOIN classes c ON c.id = s.class_id
                JOIN subjects sub ON sub.id = s.subject_id WHERE s.date = ?
                ORDER BY a.time DESC LIMIT 8""", (today,)),
        }


# ---------- audit ----------
def audit(actor, action, detail=""):
    with conn() as c:
        c.execute("INSERT INTO audit_log (time, actor, action, detail) VALUES (?, ?, ?, ?)",
                  (datetime.now().isoformat(timespec="seconds"), actor, action, detail))


def list_audit(limit=200):
    with conn() as c:
        return _rows(c, "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))


# ---------- sessions list / roster / manual override ----------
def list_sessions(limit=100):
    with conn() as c:
        return _rows(c, """SELECT s.id, s.date, s.ended_at, c.name AS class_name, sub.name AS subject_name,
            (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS present,
            (SELECT COUNT(*) FROM users u WHERE u.class_id = s.class_id) AS total
            FROM sessions s JOIN classes c ON c.id = s.class_id JOIN subjects sub ON sub.id = s.subject_id
            ORDER BY s.date DESC, s.id DESC LIMIT ?""", (limit,))


def session_roster(sid):
    """Every student of the session's class with their status for that session."""
    sess = get_session(sid)
    if not sess:
        return None, []
    with conn() as c:
        rows = _rows(c, """SELECT u.id, u.name, a.time, a.source, a.note, a.snapshot FROM users u
            LEFT JOIN attendance a ON a.user_id = u.id AND a.session_id = ?
            WHERE u.class_id = ? ORDER BY u.name""", (sid, sess["class_id"]))
    return sess, rows


def absentees(sid):
    sess, rows = session_roster(sid)
    return sess, [r for r in rows if not r["time"]]


def session_notified(sid):
    with conn() as c:
        r = c.execute("SELECT notified FROM sessions WHERE id = ?", (sid,)).fetchone()
        return bool(r and r["notified"])


def set_session_notified(sid):
    with conn() as c:
        c.execute("UPDATE sessions SET notified = 1 WHERE id = ?", (sid,))


# ---------- student accounts ----------
def get_user(uid):
    with conn() as c:
        r = c.execute("""SELECT u.*, c.name AS class_name FROM users u
                         LEFT JOIN classes c ON c.id = u.class_id WHERE u.id = ?""", (uid,)).fetchone()
    return dict(r) if r else None


def verify_student(uid, password):
    u = get_user(uid)
    if u and u["password_hash"] and check_password_hash(u["password_hash"], password):
        return u
    return None


def set_student_password(uid, password, must_change=False):
    with conn() as c:
        c.execute("UPDATE users SET password_hash = ?, must_change = ? WHERE id = ?",
                  (generate_password_hash(password), int(must_change), uid))


def student_summary(uid):
    """Overall + per-subject attendance, absences and recent records for one student."""
    u = get_user(uid)
    if not u:
        return None
    sessions, present = [], {}
    with conn() as c:
        if u["class_id"]:
            sessions = _rows(c, """SELECT s.id, s.date, s.subject_id, sub.name AS subject_name
                FROM sessions s JOIN subjects sub ON sub.id = s.subject_id
                WHERE s.class_id = ? ORDER BY s.date DESC, s.id DESC""", (u["class_id"],))
        for r in c.execute("SELECT session_id, time, source FROM attendance WHERE user_id = ?", (uid,)):
            present[r["session_id"]] = dict(r)
    per, absent, records = {}, [], []
    for s_ in sessions:
        d = per.setdefault(s_["subject_name"], {"subject": s_["subject_name"], "present": 0, "total": 0})
        d["total"] += 1
        if s_["id"] in present:
            d["present"] += 1
            records.append({"date": s_["date"], "subject": s_["subject_name"],
                            "time": present[s_["id"]]["time"], "source": present[s_["id"]]["source"]})
        else:
            absent.append({"date": s_["date"], "subject": s_["subject_name"]})
    subjects = sorted(per.values(), key=lambda x: x["subject"])
    for d in subjects:
        d["pct"] = round(100 * d["present"] / d["total"], 1)
    total = len(sessions)
    got = sum(1 for s_ in sessions if s_["id"] in present)
    code_ok = bool(u["link_code"] and u["link_expires"] and u["link_expires"] > datetime.now().isoformat())
    return {"user": u, "subjects": subjects, "total": total, "present": got,
            "pct": round(100 * got / total, 1) if total else 0.0,
            "absent": absent[:30], "records": records[:30],
            "link_code": u["link_code"] if code_ok else None, "linked": bool(u["telegram_chat_id"])}


# ---------- telegram linking ----------
def new_link_code(uid):
    import secrets
    from datetime import timedelta
    code = f"{secrets.randbelow(1000000):06d}"
    exp = (datetime.now() + timedelta(minutes=15)).isoformat()
    with conn() as c:
        c.execute("UPDATE users SET link_code = ?, link_expires = ? WHERE id = ?", (code, exp, uid))
    return code


def link_telegram(code, chat_id):
    """Attach a Telegram chat to the user holding this (unexpired) code."""
    if not code:
        return None
    with conn() as c:
        r = c.execute("SELECT id FROM users WHERE link_code = ? AND link_expires > ?",
                      (code, datetime.now().isoformat())).fetchone()
        if not r:
            return None
        c.execute("UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = ?", (chat_id,))
        c.execute("UPDATE users SET telegram_chat_id = ?, link_code = NULL, link_expires = NULL "
                  "WHERE id = ?", (chat_id, r["id"]))
    return get_user(r["id"])


def unlink_telegram(uid):
    with conn() as c:
        c.execute("UPDATE users SET telegram_chat_id = NULL WHERE id = ?", (uid,))


def user_by_chat(chat_id):
    with conn() as c:
        r = c.execute("SELECT id FROM users WHERE telegram_chat_id = ?", (str(chat_id),)).fetchone()
    return get_user(r["id"]) if r else None


def today_sessions_for(uid):
    u = get_user(uid)
    if not u or not u["class_id"]:
        return []
    with conn() as c:
        return _rows(c, """SELECT sub.name AS subject_name, a.time FROM sessions s
            JOIN subjects sub ON sub.id = s.subject_id
            LEFT JOIN attendance a ON a.session_id = s.id AND a.user_id = ?
            WHERE s.class_id = ? AND s.date = ? ORDER BY s.id""",
                     (uid, u["class_id"], date.today().isoformat()))
