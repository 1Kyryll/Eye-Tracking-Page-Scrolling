"""
eyescroll_app.py — gaze-controlled scrolling as a background tray app.

Runs the eye tracker on a worker thread and exposes a system-tray icon
(Windows / macOS / Linux) with: Pause/Resume, Recalibrate, Show/Hide preview, Quit.

Run from source:
    pip install -r requirements.txt
    python eyescroll_app.py

Packaged builds (see eyescroll.spec / .github/workflows/build.yml) bundle the
MediaPipe model so the app works offline on first launch.

Usage reminder: the mouse wheel goes to whatever window is UNDER the mouse
pointer, so hover the pointer over the page/editor/chat you want to scroll.
Look down past the reading zone to scroll down, up to scroll up.
"""

import os
import sys
import json
import time
import threading
import urllib.request

import cv2
import numpy as np
import mediapipe as mp
import pyautogui
import pystray
from PIL import Image, ImageDraw

# ── constants ───────────────────────────────────────────────────────────────

APP_NAME   = "EyeScroll"
MODEL_NAME = "face_landmarker.task"
MODEL_URL  = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)

# Iris / eye-corner landmark indices (MediaPipe FaceLandmarker with iris).
# Vertical gaze is measured against the EYE CORNERS, which stay put when the
# eyes move — unlike the eyelids, which droop when looking down and invert the
# signal. Each iris is paired with the two corners of its own eye.
LEFT_IRIS, LEFT_CORNER_A, LEFT_CORNER_B    = 468, 33, 133
RIGHT_IRIS, RIGHT_CORNER_A, RIGHT_CORNER_B = 473, 362, 263

DEFAULT_CONFIG = {
    "scroll_min_step":  4,      # wheel clicks/frame just past the reading zone
    "scroll_max_step":  95,     # wheel clicks/frame at full gaze deflection
    "reading_zone":     0.02,  # half-height of the no-scroll band (deviation units)
    "gaze_range":       0.10,   # deviation at the screen edge = max scroll speed
    "ema_alpha":        0.6,    # smoothing when looking further out (accelerating)
    "ema_alpha_brake":  0.95,   # smoothing when returning toward center (braking)
    "calibration_secs": 2,      # seconds of samples collected per calibration
    "calibrated_center": 0.0,   # resting gaze offset (set by calibration)
}


# ── per-user paths ──────────────────────────────────────────────────────────

def app_data_dir():
    """Writable per-user folder for config + a downloaded model fallback."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def resource_path(rel):
    """Path to a bundled resource (works both from source and from PyInstaller)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def ensure_model():
    """Return a path to the model: bundled → cached → downloaded."""
    bundled = resource_path(MODEL_NAME)
    if os.path.exists(bundled):
        return bundled
    cached = os.path.join(app_data_dir(), MODEL_NAME)
    if os.path.exists(cached):
        return cached
    urllib.request.urlretrieve(MODEL_URL, cached)
    return cached


def config_path():
    return os.path.join(app_data_dir(), "config.json")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# ── gaze metric ─────────────────────────────────────────────────────────────

def compute_gaze_ratio(landmarks):
    """
    Iris vertical offset from the eye-corner line, normalized by eye width
    (distance-invariant). ~0 = straight, >0 = looking down, <0 = looking up.
    Averages both eyes. Calibration removes the resting offset.
    """
    def offset(iris_idx, corner_a, corner_b):
        corner_y = (landmarks[corner_a].y + landmarks[corner_b].y) * 0.5
        width    = abs(landmarks[corner_a].x - landmarks[corner_b].x)
        if width < 1e-4:
            return None
        return (landmarks[iris_idx].y - corner_y) / width

    left  = offset(LEFT_IRIS,  LEFT_CORNER_A,  LEFT_CORNER_B)
    right = offset(RIGHT_IRIS, RIGHT_CORNER_A, RIGHT_CORNER_B)
    valid = [v for v in (left, right) if v is not None]
    return float(np.mean(valid)) if valid else None


# ── tracker thread ──────────────────────────────────────────────────────────

class GazeScroller(threading.Thread):
    def __init__(self, cfg, notify):
        super().__init__(daemon=True)
        self.cfg    = cfg
        self.notify = notify

        self._stop        = threading.Event()
        self._paused      = threading.Event()
        self._recalibrate = threading.Event()
        self.show_preview = False

        self.calibrated_center = float(cfg.get("calibrated_center", 0.0))
        self._smoothed         = None
        self._start_perf       = time.perf_counter()
        self._last_ts          = -1

    # public controls -------------------------------------------------------
    def is_paused(self):  return self._paused.is_set()
    def stop(self):       self._stop.set()

    def toggle_pause(self):
        (self._paused.clear if self._paused.is_set() else self._paused.set)()

    def request_recalibration(self):
        self._recalibrate.set()

    # internals -------------------------------------------------------------
    def _next_ts(self):
        ts = int((time.perf_counter() - self._start_perf) * 1000)
        if ts <= self._last_ts:
            ts = self._last_ts + 1
        self._last_ts = ts
        return ts

    def _open_camera(self):
        backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else 0
        cap = cv2.VideoCapture(0, backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        return cap if cap.isOpened() else None

    def run(self):
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=ensure_model()),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_faces=1,
        )
        landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)
        pyautogui.FAILSAFE = False

        cap = None
        calibrating   = False
        calib_samples = []
        calib_deadline = 0.0
        # Calibrate once on launch so it works out of the box.
        self._recalibrate.set()

        try:
            while not self._stop.is_set():
                if self._paused.is_set():
                    if cap is not None:
                        cap.release()
                        cap = None
                        self._destroy_preview()
                    time.sleep(0.1)
                    continue

                if cap is None:
                    cap = self._open_camera()
                    if cap is None:
                        self.notify("Could not open the webcam — is another app using it?")
                        self._paused.set()
                        continue

                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.01)
                    continue

                frame = cv2.flip(frame, 1)
                rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = landmarker.detect_for_video(mp_img, self._next_ts())

                ratio = None
                if result.face_landmarks:
                    ratio = compute_gaze_ratio(result.face_landmarks[0])

                # ----- calibration mode -----
                if self._recalibrate.is_set():
                    if not calibrating:
                        calibrating    = True
                        calib_samples  = []
                        calib_deadline = time.time() + self.cfg["calibration_secs"]
                        self._smoothed = None
                        self.notify("Calibrating — look straight ahead…")
                    if ratio is not None:
                        calib_samples.append(ratio)
                    if time.time() >= calib_deadline:
                        if calib_samples:
                            self.calibrated_center = float(np.mean(calib_samples))
                            self.cfg["calibrated_center"] = self.calibrated_center
                            save_config(self.cfg)
                            self.notify("Calibration done — ready to scroll.")
                        else:
                            self.notify("Calibration failed — no face detected.")
                        calibrating = False
                        self._recalibrate.clear()
                    self._draw_preview(frame, ratio, calibrating=True)
                    continue

                # ----- scroll mode -----
                if ratio is not None:
                    self._step_scroll(ratio)
                self._draw_preview(frame, ratio, calibrating=False)
        finally:
            if cap is not None:
                cap.release()
            landmarker.close()
            self._destroy_preview()

    def _step_scroll(self, ratio):
        c = self.cfg
        # asymmetric EMA: gentle when accelerating, fast brake when returning
        if self._smoothed is None:
            self._smoothed = ratio
        else:
            returning = abs(ratio - self.calibrated_center) < abs(self._smoothed - self.calibrated_center)
            alpha = c["ema_alpha_brake"] if returning else c["ema_alpha"]
            self._smoothed = alpha * ratio + (1.0 - alpha) * self._smoothed

        deviation = self._smoothed - self.calibrated_center
        rz, gr = c["reading_zone"], c["gaze_range"]
        if abs(deviation) <= rz:
            return  # inside the reading zone — anchored, no scroll

        magnitude = (abs(deviation) - rz) / max(1e-3, gr - rz)
        magnitude = float(np.clip(magnitude, 0.0, 1.0))
        step = int(c["scroll_min_step"] + magnitude * (c["scroll_max_step"] - c["scroll_min_step"]))
        pyautogui.scroll(-step if deviation > 0 else step)

    # ----- optional preview window -----
    def _draw_preview(self, frame, ratio, calibrating):
        if not self.show_preview:
            return
        try:
            h = frame.shape[0]
            if calibrating:
                cv2.putText(frame, "Calibrating… look straight ahead", (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
            dev = (ratio - self.calibrated_center) if ratio is not None else None
            txt = f"{dev:+.3f}" if dev is not None else "--"
            cv2.putText(frame, f"dev={txt}", (20, h - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
            cv2.imshow("Eye Scroll — preview", frame)
            cv2.waitKey(1)
        except cv2.error:
            self.show_preview = False  # GUI not available on this platform/thread

    def _destroy_preview(self):
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass


# ── tray icon ───────────────────────────────────────────────────────────────

def make_icon(active=True):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (0, 200, 120, 255) if active else (130, 130, 130, 255)
    d.ellipse([6, 18, 58, 46], outline=color, width=4)   # eye outline
    d.ellipse([26, 26, 38, 38], fill=color)              # pupil
    return img


def main():
    cfg = load_config()
    icon = pystray.Icon(APP_NAME, make_icon(True), "Eye Scroll")

    def notify(msg, title="Eye Scroll"):
        try:
            icon.notify(msg, title)
        except Exception:
            pass

    scroller = GazeScroller(cfg, notify)

    def refresh():
        active = not scroller.is_paused()
        icon.icon  = make_icon(active)
        icon.title = "Eye Scroll — " + ("Active" if active else "Paused")
        icon.update_menu()

    def on_pause(_i, _item):    scroller.toggle_pause(); refresh()
    def on_recal(_i, _item):    scroller.request_recalibration()
    def on_preview(_i, _item):  scroller.show_preview = not scroller.show_preview
    def on_quit(_i, _item):     scroller.stop(); icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda i: "Resume scrolling" if scroller.is_paused()
                                   else "Pause scrolling", on_pause),
        pystray.MenuItem("Recalibrate (look straight ahead)", on_recal),
        pystray.MenuItem("Show camera preview", on_preview,
                         checked=lambda i: scroller.show_preview),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    scroller.start()
    icon.run()  # blocks on the main thread until Quit


if __name__ == "__main__":
    main()
