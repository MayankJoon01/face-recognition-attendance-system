"""Dashboard + live attendance:  python app.py  ->  http://127.0.0.1:5000"""
import csv
import io
import os
import secrets
import sqlite3
import time
from datetime import date, timedelta
from functools import wraps

import numpy as np
from werkzeug.security import generate_password_hash
from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

import config
import database
import reports
import telegram_bot
from recognizer import encode_frames, recognizer


def _secret():
    if not os.path.exists(config.SECRET_FILE):
        with open(config.SECRET_FILE, "w") as f:
            f.write(secrets.token_hex(32))
    with open(config.SECRET_FILE) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = config.SESSION_SECRET or _secret()
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
os.makedirs(config.DATA_DIR, exist_ok=True)
os.makedirs(config.SNAP_DIR, exist_ok=True)
database.init_db()
telegram_bot.start()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["threshold"] = config.ATTENDANCE_THRESHOLD
app.jinja_env.globals["telegram_on"] = telegram_bot.enabled


@app.before_request
def csrf_protect():
    if request.method == "POST":
        tok = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not tok or tok != session.get("csrf"):
            abort(400, "Bad or missing CSRF token. Reload the page and try again.")


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "admin" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("must_change") and request.endpoint not in ("password", "logout"):
            return redirect(url_for("password"))
        return f(*a, **kw)
    return wrapper


def student_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "student" not in session:
            return redirect(url_for("student_login"))
        if session.get("s_must_change") and request.endpoint not in ("student_password", "logout"):
            return redirect(url_for("student_password"))
        return f(*a, **kw)
    return wrapper


_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def temp_password():
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))


def _audit(action, detail=""):
    database.audit(session.get("admin", "?"), action, detail)


# ---------------- auth ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        admin = database.verify_admin(request.form.get("username", "").strip(),
                                      request.form.get("password", ""))
        if admin:
            session.clear()
            session["admin"] = admin["username"]
            session["must_change"] = bool(admin["must_change"])
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        time.sleep(1)  # slow down guessing
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def password():
    if request.method == "POST":
        p1, p2 = request.form.get("p1", ""), request.form.get("p2", "")
        if len(p1) < 8:
            flash("Use at least 8 characters.", "error")
        elif p1 != p2:
            flash("Passwords do not match.", "error")
        elif p1 == "admin123":
            flash("Choose something other than the default password.", "error")
        else:
            database.set_password(session["admin"], p1)
            session["must_change"] = False
            flash("Password updated.")
            return redirect(url_for("index"))
    return render_template("password.html")


# ---------------- dashboard ----------------
@app.route("/")
@login_required
def index():
    return render_template("index.html", s=database.dashboard_stats(),
                           live=recognizer.running, sess=recognizer.session)


# ---------------- live attendance ----------------
@app.route("/live")
@login_required
def live():
    return render_template("live.html", classes=database.list_classes(),
                           subjects=database.list_subjects(), running=recognizer.running,
                           sess=recognizer.session, error=recognizer.error)


@app.post("/live/start")
@login_required
def live_start():
    try:
        cid, sub = int(request.form["class_id"]), int(request.form["subject_id"])
        sid = database.get_or_create_session(cid, sub)
        recognizer.start(sid)
    except (KeyError, ValueError):
        flash("Pick a class and a subject first.", "error")
    except RuntimeError as e:
        flash(str(e), "error")
    return redirect(url_for("live"))


@app.post("/live/stop")
@login_required
def live_stop():
    sid = recognizer.session["id"] if recognizer.session else None
    recognizer.stop()
    msg = "Session stopped."
    if sid and request.form.get("alerts") and telegram_bot.enabled():
        msg += f" Absence alerts sent to {telegram_bot.notify_absentees(sid)} linked student(s)."
    flash(msg)
    return redirect(url_for("live"))


@app.route("/video_feed")
@login_required
def video_feed():
    def gen():
        while recognizer.running:
            f = recognizer.frame
            if f:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + f + b"\r\n"
            time.sleep(0.05)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
@login_required
def api_status():
    if not recognizer.running or not recognizer.session:
        return jsonify(running=False, error=recognizer.error)
    s = recognizer.session
    marks = database.session_marks(s["id"])
    return jsonify(running=True, error=recognizer.error, label=f'{s["class_name"]} · {s["subject_name"]}',
                   present=len(marks), total=len(database.list_users(s["class_id"])), marks=marks[:30])


# ---------------- enrollment ----------------
@app.route("/enroll")
@login_required
def enroll():
    return render_template("enroll.html", classes=database.list_classes(), live=recognizer.running)


@app.post("/api/enroll")
@login_required
def api_enroll():
    d = request.get_json(silent=True) or {}
    uid, name = str(d.get("id", "")).strip(), str(d.get("name", "")).strip()
    class_id = int(d["class_id"]) if str(d.get("class_id", "")).isdigit() else None
    frames = d.get("frames") or []
    if not uid or not name or not class_id:
        return jsonify(ok=False, error="ID, name and class are required."), 400
    encs = encode_frames(frames)
    if len(encs) < config.ENROLL_MIN_SAMPLES:
        return jsonify(ok=False, error=f"Only {len(encs)} usable frames (need {config.ENROLL_MIN_SAMPLES}). "
                       "Keep one face in view, with good light, and try again."), 400
    ids, names, _, known = database.load_known()
    if len(known):
        others = [i for i, x in enumerate(ids) if x != uid]
        if others:
            best = [float(np.min(np.linalg.norm(known[others] - e, axis=1))) for e in encs]
            if float(np.median(best)) < config.DUPLICATE_DISTANCE:
                j = others[int(np.argmin(np.linalg.norm(known[others] - encs[0], axis=1)))]
                return jsonify(ok=False, error=f"This face is already enrolled as {names[j]} ({ids[j]})."), 409
    temp = temp_password()
    created = database.add_user(uid, name, class_id, encs, generate_password_hash(temp))
    _audit("enroll" if created else "re-enroll", f"{uid} {name}")
    return jsonify(ok=True, samples=len(encs), temp_password=temp if created else None)


# ---------------- users / classes ----------------
@app.route("/users")
@login_required
def users():
    cid = request.args.get("class_id", type=int)
    return render_template("users.html", users=database.list_users(cid),
                           classes=database.list_classes(), cid=cid)


@app.post("/users/<uid>/delete")
@login_required
def delete_user(uid):
    database.delete_user(uid)
    _audit("delete_user", uid)
    flash(f"Deleted {uid} and all their face data.")
    return redirect(url_for("users"))


@app.route("/classes")
@login_required
def classes():
    return render_template("classes.html", classes=database.list_classes(),
                           subjects=database.list_subjects())


@app.post("/classes/add")
@login_required
def class_add():
    name = request.form.get("name", "").strip()
    try:
        if name:
            database.add_class(name)
    except sqlite3.IntegrityError:
        flash("That class already exists.", "error")
    return redirect(url_for("classes"))


@app.post("/classes/<int:cid>/delete")
@login_required
def class_delete(cid):
    database.delete_class(cid)
    flash("Class deleted with its subjects and sessions.")
    return redirect(url_for("classes"))


@app.post("/subjects/add")
@login_required
def subject_add():
    name, cid = request.form.get("name", "").strip(), request.form.get("class_id", type=int)
    try:
        if name and cid:
            database.add_subject(name, cid)
    except sqlite3.IntegrityError:
        flash("That subject already exists in the class.", "error")
    return redirect(url_for("classes"))


@app.post("/subjects/<int:sid>/delete")
@login_required
def subject_delete(sid):
    database.delete_subject(sid)
    return redirect(url_for("classes"))


# ---------------- reports ----------------
def _range():
    today = date.today()
    period = request.args.get("period", "")
    if period == "week":
        s, e = today - timedelta(days=6), today
    elif period == "month":
        s, e = today.replace(day=1), today
    else:
        s = request.args.get("start") or today.replace(day=1).isoformat()
        e = request.args.get("end") or today.isoformat()
    return str(s), str(e)


def _filters():
    start, end = _range()
    return dict(start=start, end=end, class_id=request.args.get("class_id", type=int),
                subject_id=request.args.get("subject_id", type=int),
                user_id=request.args.get("user") or None)


@app.route("/report")
@login_required
def report():
    f = _filters()
    return render_template("report.html", f=f, rows=database.get_records(**f),
                           classes=database.list_classes(), subjects=database.list_subjects(),
                           users=database.list_users())


@app.route("/export")
@login_required
def export():
    f = _filters()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Time", "ID", "Name", "Class", "Subject", "Source"])
    for r in database.get_records(**f):
        w.writerow([r["date"], r["time"], r["user_id"], r["name"], r["class_name"], r["subject_name"], r["source"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=attendance_{f['start']}_to_{f['end']}.csv"})


@app.route("/analytics")
@login_required
def analytics():
    f = _filters()
    classes_ = database.list_classes()
    if not f["class_id"] and classes_:
        f["class_id"] = classes_[0]["id"]
    stats = (database.get_stats(f["class_id"], f["subject_id"], f["start"], f["end"])
             if f["class_id"] else None)
    return render_template("analytics.html", f=f, stats=stats, classes=classes_,
                           subjects=database.list_subjects())


@app.route("/report/pdf")
@login_required
def report_pdf():
    f = _filters()
    classes_ = {c["id"]: c["name"] for c in database.list_classes()}
    if not f["class_id"]:
        flash("Choose a class to generate the PDF report.", "error")
        return redirect(url_for("analytics"))
    stats = database.get_stats(f["class_id"], f["subject_id"], f["start"], f["end"])
    subj = next((s["name"] for s in database.list_subjects() if s["id"] == f["subject_id"]), "All subjects")
    summary = [[r["id"], r["name"], f'{r["present"]}/{r["total"]}', f'{r["pct"]}%',
                "LOW" if r["pct"] < config.ATTENDANCE_THRESHOLD else "OK"] for r in stats["students"]]
    low = [i for i, r in enumerate(stats["students"]) if r["pct"] < config.ATTENDANCE_THRESHOLD]
    log = [[r["date"], r["time"], r["user_id"], r["name"], r["subject_name"]]
           for r in database.get_records(**{**f, "user_id": None})]
    pdf = reports.build_pdf(
        "Attendance Report",
        [f"Class: {classes_.get(f['class_id'], '-')}", f"Subject: {subj}",
         f"Period: {f['start']} to {f['end']}",
         f"Sessions held: {stats['total_sessions']} &nbsp; | &nbsp; Class average: {stats['avg']}% "
         f"&nbsp; | &nbsp; Below {config.ATTENDANCE_THRESHOLD}%: {len(low)}"],
        [("Student summary (red = below threshold)", ["ID", "Name", "Present", "Percent", "Status"], summary, low),
         ("Detailed check-in log", ["Date", "Time", "ID", "Name", "Subject"], log, [])])
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=attendance_report_{f['start']}_to_{f['end']}.pdf"})



# ---------------- manual override ----------------
@app.route("/sessions")
@login_required
def sessions_page():
    return render_template("sessions.html", rows=database.list_sessions(),
                           classes=database.list_classes(), subjects=database.list_subjects(),
                           today=date.today().isoformat())


@app.post("/sessions/new")
@login_required
def session_new():
    try:
        cid, sub = int(request.form["class_id"]), int(request.form["subject_id"])
        day = request.form.get("date") or date.today().isoformat()
        date.fromisoformat(day)
    except (KeyError, ValueError):
        flash("Pick a class, subject and a valid date.", "error")
        return redirect(url_for("sessions_page"))
    sid = database.get_or_create_session(cid, sub, day, reopen=False)
    return redirect(url_for("session_detail", sid=sid))


@app.route("/sessions/<int:sid>")
@login_required
def session_detail(sid):
    sess, roster = database.session_roster(sid)
    if not sess:
        abort(404)
    return render_template("session_detail.html", sess=sess, roster=roster,
                           live=recognizer.running and recognizer.session and recognizer.session["id"] == sid)


@app.post("/sessions/<int:sid>/mark")
@login_required
def session_mark(sid):
    uid, action = request.form.get("uid", ""), request.form.get("action", "")
    note = request.form.get("note", "").strip()[:200]
    sess = database.get_session(sid)
    if not sess or not database.get_user(uid):
        abort(404)
    label = f'{uid} in {sess["class_name"]} / {sess["subject_name"]} on {sess["date"]}'
    if action == "present":
        if database.mark_attendance(sid, uid, None, "manual", note or None):
            _audit("manual_present", f"{label}. Note: {note or '-'}")
            telegram_bot.notify_marked(uid, sid)
            flash(f"Marked {uid} present.")
    elif action == "absent":
        if database.unmark_attendance(sid, uid):
            _audit("manual_absent", f"{label}. Reason: {note or '-'}")
            flash(f"Marked {uid} absent.")
    return redirect(url_for("session_detail", sid=sid))


@app.route("/audit")
@login_required
def audit_page():
    return render_template("audit.html", rows=database.list_audit())


@app.post("/users/<uid>/reset-password")
@login_required
def reset_password(uid):
    if not database.get_user(uid):
        abort(404)
    temp = temp_password()
    database.set_student_password(uid, temp, must_change=True)
    _audit("reset_password", uid)
    flash(f"Temporary password for {uid}: {temp}  (shown once; they must change it at first login)")
    return redirect(url_for("users"))


# ---------------- student portal ----------------
@app.route("/student/login", methods=["GET", "POST"])
def student_login():
    if request.method == "POST":
        u = database.verify_student(request.form.get("uid", "").strip(), request.form.get("password", ""))
        if u:
            session.clear()
            session["student"] = u["id"]
            session["s_must_change"] = bool(u["must_change"])
            return redirect(url_for("me"))
        time.sleep(1)
        flash("Wrong ID or password. Ask your teacher to reset it.", "error")
    return render_template("student_login.html")


@app.route("/me")
@student_required
def me():
    return render_template("student.html", d=database.student_summary(session["student"]),
                           bot=telegram_bot.username, bot_on=telegram_bot.enabled())


@app.route("/me/password", methods=["GET", "POST"])
@student_required
def student_password():
    if request.method == "POST":
        p1, p2 = request.form.get("p1", ""), request.form.get("p2", "")
        if len(p1) < 8:
            flash("Use at least 8 characters.", "error")
        elif p1 != p2:
            flash("Passwords do not match.", "error")
        elif p1 == session["student"]:
            flash("Your password can't be your ID.", "error")
        else:
            database.set_student_password(session["student"], p1)
            session["s_must_change"] = False
            flash("Password updated.")
            return redirect(url_for("me"))
    return render_template("password.html")


@app.post("/me/telegram/code")
@student_required
def student_link_code():
    database.new_link_code(session["student"])
    return redirect(url_for("me"))


@app.post("/me/telegram/unlink")
@student_required
def student_unlink():
    database.unlink_telegram(session["student"])
    flash("Telegram disconnected.")
    return redirect(url_for("me"))


# ---------------- alerts / snapshots ----------------
@app.route("/alerts")
@login_required
def alerts():
    return render_template("alerts.html", rows=database.list_unknown())


@app.post("/alerts/clear")
@login_required
def alerts_clear():
    database.clear_unknown()
    return redirect(url_for("alerts"))


@app.route("/snapshot/<path:name>")
@login_required
def snapshot(name):
    return send_from_directory(config.SNAP_DIR, name)


if __name__ == "__main__":
    print("Dashboard: http://%s:%s   (first login: admin / admin123, you must change it)"
          % (config.FLASK_HOST, config.FLASK_PORT))
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False, threaded=True)
