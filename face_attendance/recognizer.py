"""Background camera thread: live recognition, blink liveness, snapshots, DB logging."""
import base64
import os
import threading
import time
from datetime import datetime

import cv2
import face_recognition
import numpy as np

import config
import database
import telegram_bot

GREEN, ORANGE, RED = (0, 200, 0), (0, 165, 255), (0, 0, 255)


def ear(eye):
    p = np.array(eye, dtype=float)
    return (np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4])) / (
        2.0 * np.linalg.norm(p[0] - p[3]))


def save_snapshot(frame, box, prefix):
    """Crop the face (with margin), save as JPEG, return path relative to SNAP_DIR."""
    t, r, b, l = box
    h, w = frame.shape[:2]
    m = int((b - t) * 0.4)
    crop = frame[max(t - m, 0):min(b + m, h), max(l - m, 0):min(r + m, w)]
    if crop.size == 0:
        return None
    crop = cv2.resize(crop, (160, max(1, int(160 * crop.shape[0] / crop.shape[1]))))
    now = datetime.now()
    name = f"{now:%Y-%m-%d}/{prefix}_{now:%H%M%S}.jpg"
    path = os.path.join(config.SNAP_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, crop)
    return name


def encode_frames(data_urls):
    """Turn browser-captured JPEG data URLs into face encodings (one face per frame)."""
    encs = []
    for url in data_urls:
        try:
            raw = base64.b64decode(url.split(",", 1)[1])
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            small = cv2.resize(img, (0, 0), fx=0.5, fy=0.5)
            locs = face_recognition.face_locations(cv2.cvtColor(small, cv2.COLOR_BGR2RGB), model="hog")
            if len(locs) != 1:
                continue
            t, r, b, l = [v * 2 for v in locs[0]]
            e = face_recognition.face_encodings(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), [(t, r, b, l)])
            if e:
                encs.append(e[0])
        except Exception:
            continue
    return encs


class Recognizer:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self.session = None
        self.frame = None      # latest annotated JPEG bytes
        self.error = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, session_id):
        if self.running:
            raise RuntimeError("A session is already running. Stop it first.")
        sess = database.get_session(session_id)
        ids, names, classes, known = database.load_known()
        if len(known) == 0:
            raise RuntimeError("No enrolled users yet. Enroll people first.")
        cam = cv2.VideoCapture(config.CAMERA_INDEX)
        if not cam.isOpened():
            raise RuntimeError("Cannot open the camera. Close other apps using it or change CAMERA_INDEX.")
        self.session, self.error, self.frame = sess, None, None
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(cam, sess, ids, names, classes, known),
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self.session, self.frame = None, None

    def _loop(self, cam, sess, ids, names, classes, known):
        sid, cls = sess["id"], sess["class_id"]
        marked = database.marked_user_ids(sid)
        blink, results, frame_no, last_unknown = {}, [], 0, 0.0
        inv = 1 / config.FRAME_SCALE
        try:
            while not self._stop.is_set():
                ok, frame = cam.read()
                if not ok:
                    self.error = "Camera stopped sending frames."
                    break
                frame_no += 1
                if frame_no % config.PROCESS_EVERY_N == 0:
                    small = cv2.resize(frame, (0, 0), fx=config.FRAME_SCALE, fy=config.FRAME_SCALE)
                    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                    locs = face_recognition.face_locations(rgb, model="hog")
                    encs = face_recognition.face_encodings(rgb, locs)
                    lms = (face_recognition.face_landmarks(rgb, locs)
                           if config.REQUIRE_BLINK else [None] * len(locs))
                    results = []
                    for loc, enc, lm in zip(locs, encs, lms):
                        box = tuple(int(v * inv) for v in loc)  # top, right, bottom, left
                        t, r, b, l = box
                        rect = (l, t, r, b)
                        dist = face_recognition.face_distance(known, enc)
                        i = int(np.argmin(dist))
                        if dist[i] > config.TOLERANCE:
                            results.append((rect, "Unknown", RED))
                            if time.time() - last_unknown > config.UNKNOWN_LOG_INTERVAL:
                                database.log_unknown(sid, save_snapshot(frame, box, "unknown"))
                                last_unknown = time.time()
                            continue
                        uid, name = ids[i], names[i]
                        if classes[i] != cls:
                            results.append((rect, f"{name} (other class)", ORANGE))
                            continue
                        if uid in marked:
                            results.append((rect, f"{name} (marked)", GREEN))
                            continue
                        live = True
                        if config.REQUIRE_BLINK and lm:
                            st = blink.setdefault(uid, {"closed": False, "until": 0.0})
                            e = (ear(lm["left_eye"]) + ear(lm["right_eye"])) / 2
                            if e < config.EAR_THRESHOLD:
                                st["closed"] = True
                            elif st["closed"]:
                                st["closed"] = False
                                st["until"] = time.time() + config.BLINK_VALID_SECONDS
                            live = time.time() < st["until"]
                        if live:
                            if database.mark_attendance(sid, uid, save_snapshot(frame, box, uid)):
                                telegram_bot.notify_marked(uid, sid)
                            marked.add(uid)
                            results.append((rect, f"{name} (marked)", GREEN))
                        else:
                            results.append((rect, f"{name} - blink to confirm", ORANGE))

                for (l, t, r, b), label, color in results:
                    cv2.rectangle(frame, (l, t), (r, b), color, 2)
                    cv2.putText(frame, label, (l, max(t - 8, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    self.frame = buf.tobytes()
        except Exception as ex:  # keep the web app alive
            self.error = f"Recognition error: {ex}"
        finally:
            cam.release()
            database.end_session(sid)


recognizer = Recognizer()
