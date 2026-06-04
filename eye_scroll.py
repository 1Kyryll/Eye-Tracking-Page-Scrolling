"""
eye_scroll.py — gaze-controlled page scrolling (MediaPipe >= 0.10)
-------------------------------------------------------------------
Setup:
    pip install mediapipe opencv-python pyautogui numpy
 
    # Download the face landmarker model (~30 MB, one-time):
    python -c "
import urllib.request, pathlib
url = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task'
pathlib.Path('face_landmarker.task').exists() or urllib.request.urlretrieve(url, 'face_landmarker.task')
print('Model ready.')
"
 
Run:
    python eye_scroll.py
 
Controls:
    Q  — quit
    C  — recalibrate (look straight ahead, then press C)
"""
 
import cv2
import mediapipe as mp
import pyautogui
import numpy as np
import time
import urllib.request
import pathlib

# ── tunables ──────────────────────────────────────────────────────────────────
 
# Scrolling is applied EVERY frame (~30x/sec) in small proportional steps, so it
# reads as smooth continuous motion instead of discrete jumps.
SCROLL_MIN_STEP   = 4       # wheel clicks/frame just past the dead zone (gentle start)
SCROLL_MAX_STEP   = 90      # wheel clicks/frame at full gaze deflection (fast)

# READING ZONE: a neutral band centered on your straight-ahead gaze where NOTHING
# scrolls — so you can read freely. You only scroll by glancing to the top/bottom
# EDGES (beyond the band). As content rises into the band your eyes follow it and
# scrolling stops, so the section settles in the middle of the screen.
#   READING_ZONE bigger  = larger calm reading area, but you must look further to scroll
#   GAZE_RANGE   smaller = reach max speed with less eye movement (more sensitive)
READING_ZONE      = 0.035   # half-height of the no-scroll band (deviation units)
GAZE_RANGE        = 0.10    # deviation at the screen edge = max scroll speed

# Gaze smoothing. We brake faster than we accelerate: when your eyes return toward
# center the scroll stops almost immediately instead of coasting past your target,
# which is what lets a section "anchor" where you stop looking.
EMA_ALPHA         = 0.6     # response when looking further out (accelerating)
EMA_ALPHA_BRAKE   = 0.95    # response when returning toward center (decelerating)
CALIBRATION_SECS  = 2       # seconds to collect calibration samples
 
MODEL_PATH        = "face_landmarker.task"
MODEL_URL         = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
 
# Iris / eye landmark indices (MediaPipe FaceMesh / FaceLandmarker w/ iris).
# Vertical gaze is measured against the EYE CORNERS (canthi), which stay put when
# you move your eyes — unlike the eyelids, which droop when you look down and ruin
# the signal. Each iris is paired with the two corners of its own eye.
LEFT_IRIS      = 468   # left-eye iris center
RIGHT_IRIS     = 473   # right-eye iris center
LEFT_CORNER_A  = 33    # left-eye outer corner
LEFT_CORNER_B  = 133   # left-eye inner corner
RIGHT_CORNER_A = 362   # right-eye inner corner
RIGHT_CORNER_B = 263   # right-eye outer corner
 
# ── download model if missing ─────────────────────────────────────────────────
 
if not pathlib.Path(MODEL_PATH).exists():
    print("Downloading face landmarker model (~30 MB)…")
    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    print("Done.")
 
# ── build FaceLandmarker (VIDEO mode = synchronous, no callback needed) ───────
 
BaseOptions          = mp.tasks.BaseOptions
FaceLandmarker       = mp.tasks.vision.FaceLandmarker
FaceLandmarkerOptions = mp.tasks.vision.FaceLandmarkerOptions
VisionRunningMode    = mp.tasks.vision.RunningMode
 
options = FaceLandmarkerOptions(
    base_options=BaseOptions(model_asset_path=MODEL_PATH),
    running_mode=VisionRunningMode.VIDEO,
    num_faces=1,
    output_face_blendshapes=False,
    output_facial_transformation_matrixes=False,
)
landmarker = FaceLandmarker.create_from_options(options)

pyautogui.FAILSAFE = False

# ── monotonic timestamp source ─────────────────────────────────────────────────
# MediaPipe VIDEO mode requires every timestamp handed to a given landmarker to be
# strictly increasing across ALL calls (calibration + main loop). A single shared
# clock based on wall time guarantees this and tracks real frame timing.

_start_perf   = time.perf_counter()
_last_ts_ms   = -1

def next_timestamp_ms():
    """Return a strictly increasing millisecond timestamp."""
    global _last_ts_ms
    ts = int((time.perf_counter() - _start_perf) * 1000)
    if ts <= _last_ts_ms:
        ts = _last_ts_ms + 1
    _last_ts_ms = ts
    return ts

# ── helpers ───────────────────────────────────────────────────────────────────
 
def compute_gaze_ratio(landmarks):
    """
    Vertical gaze metric using the EYE CORNERS as a stable reference.
    Returns the iris's vertical offset from the corner line, normalized by eye
    width (so it's invariant to camera distance):
        ~0  looking straight ahead
        >0  looking DOWN  (iris sits below the corners)
        <0  looking UP    (iris sits above the corners)
    Averages both eyes for robustness. Calibration removes the resting offset.
    """
    def offset(iris_idx, corner_a, corner_b):
        corner_y = (landmarks[corner_a].y + landmarks[corner_b].y) * 0.5
        width    = abs(landmarks[corner_a].x - landmarks[corner_b].x)
        if width < 1e-4:         # eye not reliably detected
            return None
        return (landmarks[iris_idx].y - corner_y) / width

    left  = offset(LEFT_IRIS,  LEFT_CORNER_A,  LEFT_CORNER_B)
    right = offset(RIGHT_IRIS, RIGHT_CORNER_A, RIGHT_CORNER_B)
    valid = [v for v in (left, right) if v is not None]
    return float(np.mean(valid)) if valid else None
 
 
def calibrate(cap, landmarker, duration=CALIBRATION_SECS):
    """Collect gaze ratios while the user looks straight ahead."""
    samples  = []
    deadline = time.time() + duration

    while time.time() < deadline:
        ok, frame = cap.read()
        if not ok:
            continue
        frame = cv2.flip(frame, 1)
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result   = landmarker.detect_for_video(mp_image, next_timestamp_ms())
 
        remaining = int(deadline - time.time()) + 1
        cv2.putText(frame, f"Calibrating… look straight ahead ({remaining}s)",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 200, 255), 2)
        cv2.imshow("Eye Scroll", frame)
        cv2.waitKey(1)
 
        if result.face_landmarks:
            r = compute_gaze_ratio(result.face_landmarks[0])
            if r is not None:
                samples.append(r)
 
    return float(np.mean(samples)) if samples else 0.5
 
# ── main ──────────────────────────────────────────────────────────────────────
 
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)   # CAP_DSHOW = faster camera open on Windows
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

if not cap.isOpened():
    raise RuntimeError("Could not open webcam (index 0). Is another app using it?")
 
smoothed_ratio    = None    # EMA state for the gaze ratio
calibrated_center = 0.5
 
print("Eye Scroll started.  Look down to scroll down, look up to scroll up.")
print("IMPORTANT: keep your MOUSE POINTER hovering over the window you want to")
print("scroll (Chrome, VS Code, a chat panel...). The wheel goes to whatever is")
print("under the cursor — you do NOT need to click it.")
print("Press Q to quit, C to recalibrate (the preview window must be focused).\n")

# Keep the small preview always-on-top so it doesn't disappear behind your app.
cv2.namedWindow("Eye Scroll", cv2.WINDOW_NORMAL)
cv2.resizeWindow("Eye Scroll", 320, 240)
try:
    cv2.setWindowProperty("Eye Scroll", cv2.WND_PROP_TOPMOST, 1)
except Exception:
    pass   # WND_PROP_TOPMOST unavailable on some OpenCV builds

calibrated_center = calibrate(cap, landmarker)
print(f"Calibration done. Center baseline = {calibrated_center:.3f}")
 
try:
    while True:
        ok, frame = cap.read()
        if not ok:
            break

        frame      = cv2.flip(frame, 1)
        h, w       = frame.shape[:2]
        rgb        = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result   = landmarker.detect_for_video(mp_image, next_timestamp_ms())

        gaze_ratio = None
        smoothed   = smoothed_ratio
        direction  = "·"

        if result.face_landmarks:
            gaze_ratio = compute_gaze_ratio(result.face_landmarks[0])

            if gaze_ratio is not None:
                # Asymmetric EMA: accelerate gently, BRAKE fast. If the new reading
                # is closer to center than the current smoothed value, the eyes are
                # returning to the reading zone — respond quickly so scrolling stops
                # without coasting past the section you want to anchor.
                if smoothed_ratio is None:
                    smoothed_ratio = gaze_ratio
                else:
                    returning = abs(gaze_ratio - calibrated_center) < abs(smoothed_ratio - calibrated_center)
                    alpha     = EMA_ALPHA_BRAKE if returning else EMA_ALPHA
                    smoothed_ratio = alpha * gaze_ratio + (1.0 - alpha) * smoothed_ratio
                smoothed   = smoothed_ratio
                deviation  = smoothed - calibrated_center

                if abs(deviation) > READING_ZONE:
                    # Outside the reading band: ramp speed from the band edge
                    # (READING_ZONE) up to full speed at the screen edge (GAZE_RANGE).
                    magnitude = (abs(deviation) - READING_ZONE) / max(1e-3, GAZE_RANGE - READING_ZONE)
                    magnitude = float(np.clip(magnitude, 0.0, 1.0))
                    step      = int(SCROLL_MIN_STEP + magnitude * (SCROLL_MAX_STEP - SCROLL_MIN_STEP))

                    if deviation > 0:          # looking below the band → scroll down
                        pyautogui.scroll(-step)
                        direction = "DOWN"
                    else:                       # looking above the band → scroll up
                        pyautogui.scroll(step)
                        direction = "UP"
                else:
                    direction = "READING"      # inside the reading zone → anchored

        # ── HUD ──────────────────────────────────────────────────────────────
        # Center-zero meter: middle = neutral, fills right when looking DOWN,
        # left when looking UP. The dim box in the middle is the READING ZONE.

        bar_x, bar_y, bar_w, bar_h = 20, h - 60, 240, 16
        mid_x = bar_x + bar_w // 2
        cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h),
                      (50, 50, 50), -1)

        # reading-zone band (no scroll here)
        rz_px = int(np.clip(READING_ZONE / GAZE_RANGE, 0, 1) * (bar_w // 2))
        cv2.rectangle(frame, (mid_x - rz_px, bar_y), (mid_x + rz_px, bar_y + bar_h),
                      (90, 90, 90), -1)
        cv2.line(frame, (mid_x, bar_y - 2), (mid_x, bar_y + bar_h + 2), (200, 200, 200), 1)

        dev_txt = "--"
        if smoothed is not None:
            deviation = smoothed - calibrated_center
            dev_txt   = f"{deviation:+.3f}"
            # fill from center proportional to deviation (clamped to ±GAZE_RANGE)
            frac = float(np.clip(deviation / GAZE_RANGE, -1.0, 1.0))
            end  = mid_x + int(frac * (bar_w // 2))
            x0, x1 = sorted((mid_x, end))
            cv2.rectangle(frame, (x0, bar_y), (x1, bar_y + bar_h), (0, 200, 120), -1)

        cv2.putText(frame, f"Gaze: {direction}   dev={dev_txt}",
                    (20, h - 70), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
        cv2.putText(frame, "Q=quit  C=recalibrate", (20, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

        cv2.imshow("Eye Scroll", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        if key == ord('c'):
            smoothed_ratio = None
            calibrated_center = calibrate(cap, landmarker)
            print(f"Recalibrated. New center = {calibrated_center:.3f}")
finally:
    cap.release()
    landmarker.close()
    cv2.destroyAllWindows()