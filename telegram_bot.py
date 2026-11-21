"""Telegram bot: students link their account, ask for attendance, and get instant alerts.
Uses only the standard library (urllib), so no extra packages are needed."""
import json
import threading
import time
import urllib.parse
import urllib.request

import config
import database

username = None   # filled from getMe when the bot starts
_started = False

HELP = ("Attendance bot commands:\n"
        "/link CODE - connect your account (get the code on your student page)\n"
        "/attendance - your overall and per-subject attendance\n"
        "/today - today's classes and whether you were marked\n"
        "/unlink - disconnect this chat")


def enabled():
    return bool(config.TELEGRAM_BOT_TOKEN)


def _call(method, params=None, timeout=35):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params or {}).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def send(chat_id, text):
    """Fire-and-forget so the camera loop is never blocked by the network."""
    if not enabled() or not chat_id:
        return

    def run():
        try:
            _call("sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)
        except Exception as e:
            print("[telegram] send failed:", e)
    threading.Thread(target=run, daemon=True).start()


# ---------- message handling (pure logic, easy to test) ----------
def handle_message(chat_id, text):
    chat_id = str(chat_id)
    cmd, _, arg = (text or "").strip().partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    if cmd in ("/start", "/help"):
        return "Hi! I report your attendance.\n\n" + HELP
    if cmd == "/link":
        u = database.link_telegram(arg, chat_id)
        if not u:
            return "That code is invalid or expired. Open your student page and generate a new one."
        return f"Linked to {u['name']} ({u['id']}). You'll now get attendance alerts here. Try /attendance"
    u = database.user_by_chat(chat_id)
    if not u:
        return "This chat isn't linked yet. Log in to the student page, copy your 6-digit code and send /link CODE here."
    if cmd == "/unlink":
        database.unlink_telegram(u["id"])
        return "Disconnected. You will no longer get alerts."
    if cmd == "/attendance":
        s = database.student_summary(u["id"])
        if not s["total"]:
            return "No sessions recorded for your class yet."
        lines = [f"{u['name']} - {u['class_name'] or 'no class'}",
                 f"Overall: {s['pct']}% ({s['present']}/{s['total']})"]
        lines += [f"- {d['subject']}: {d['pct']}% ({d['present']}/{d['total']})" for d in s["subjects"]]
        if s["pct"] < config.ATTENDANCE_THRESHOLD:
            lines.append(f"Warning: below the {config.ATTENDANCE_THRESHOLD}% requirement.")
        return "\n".join(lines)
    if cmd == "/today":
        rows = database.today_sessions_for(u["id"])
        if not rows:
            return "No classes recorded for you today."
        return "\n".join((f"Present - {r['subject_name']} at {r['time']}" if r["time"]
                          else f"Absent - {r['subject_name']}") for r in rows)
    return "I didn't understand that.\n\n" + HELP


# ---------- outgoing notifications ----------
def notify_marked(uid, sid):
    u, s = database.get_user(uid), database.get_session(sid)
    if u and u["telegram_chat_id"] and s:
        send(u["telegram_chat_id"],
             f"Attendance marked: {s['subject_name']} ({s['class_name']}) at {time.strftime('%H:%M')}.")


def notify_absentees(sid):
    """Tell every linked student who was not marked. Returns how many alerts were queued."""
    if not enabled() or database.session_notified(sid):
        return 0
    sess, missing = database.absentees(sid)
    n = 0
    for r in missing:
        u = database.get_user(r["id"])
        if u and u["telegram_chat_id"]:
            send(u["telegram_chat_id"],
                 f"You were marked ABSENT for {sess['subject_name']} on {sess['date']}. "
                 "If this is a mistake, please contact your teacher.")
            n += 1
    database.set_session_notified(sid)
    return n


# ---------- polling loop ----------
def _poll():
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset:
                params["offset"] = offset
            for up in _call("getUpdates", params).get("result", []):
                offset = up["update_id"] + 1
                msg = up.get("message") or {}
                chat = msg.get("chat", {})
                if msg.get("text") and chat.get("type") == "private":
                    reply = handle_message(chat["id"], msg["text"])
                    if reply:
                        send(chat["id"], reply)
        except Exception as e:
            print("[telegram] polling error:", e)
            time.sleep(5)


def start():
    global _started, username
    if _started or not enabled():
        return
    try:
        username = _call("getMe", timeout=10)["result"]["username"]
        print(f"[telegram] bot @{username} is running")
    except Exception as e:
        print("[telegram] could not start (check token / internet):", e)
        return
    _started = True
    threading.Thread(target=_poll, daemon=True).start()
