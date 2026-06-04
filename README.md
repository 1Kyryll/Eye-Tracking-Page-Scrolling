# Eye Scroll

Scroll any window with your eyes. A small webcam-based app that tracks your gaze
and scrolls the page when you look up or down. Runs quietly in the **system tray**
on Windows, macOS, and Linux.

> The mouse wheel goes to whatever window is **under the mouse pointer**, so hover
> the pointer over the page/editor/chat you want to scroll. You don't need to click it.

## How it works

- **Look down** past the reading zone → scroll down. **Look up** → scroll up.
- The middle **reading zone** is a calm band where nothing scrolls, so you can read
  freely. Bring content into the middle and it settles there (anchored).
- Gaze is measured from the iris position relative to your **eye corners** (a stable
  reference that doesn't move when your eyelids do), normalized for camera distance.

## Using the app

Launch it and a tray icon appears (green = active, grey = paused). On first launch it
**auto-calibrates** — look straight ahead for ~2 seconds when you see the notification.

Right-click the tray icon for:

| Menu item | What it does |
|-----------|--------------|
| **Pause / Resume scrolling** | Stops tracking and releases the camera (light off). |
| **Recalibrate** | Re-measures your straight-ahead gaze. Look straight, then click. |
| **Show camera preview** | Optional debug window showing the live `dev=` value. |
| **Quit** | Exit the app. |

Calibration is saved per-user, so it's remembered between runs.

## Per-OS permissions (important)

These are inherent to any app that uses the camera and controls input — not bugs:

- **Windows:** the unsigned `.exe` triggers a SmartScreen prompt the first time →
  *More info → Run anyway*. (Code-signing removes this.)
- **macOS:** grant **Camera** *and* **Accessibility** in
  *System Settings → Privacy & Security* (Accessibility is required for the app to
  send scroll events). An unsigned/un-notarized `.app` must be opened via
  *right-click → Open* the first time.
- **Linux:** requires an **X11** session (scrolling via `pyautogui` doesn't work on
  Wayland) plus `libGL`, `scrot`, and `python3-tk` installed.

## Run from source

```bash
pip install -r requirements.txt
python eyescroll_app.py
```

(`eye_scroll.py` is the original single-window version, kept for reference/tuning.)

## Building the apps

Builds are produced per-OS by **GitHub Actions** (`.github/workflows/build.yml`):

- Push this repo to GitHub.
- Run the **Build** workflow manually (Actions tab → Build → *Run workflow*) to get
  downloadable artifacts for all three OSes, **or**
- Push a version tag to also create a Release with the files attached:
  ```bash
  git tag v0.1.0 && git push origin v0.1.0
  ```

### Build locally instead

On the target OS:

```bash
pip install -r requirements.txt pyinstaller
# download the model next to the spec first:
python -c "import urllib.request; urllib.request.urlretrieve('https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task','face_landmarker.task')"
pyinstaller eyescroll.spec
# → dist/EyeScroll(.exe)  or  dist/EyeScroll.app
```

## Tuning

Edit `config.json` in your per-user app folder (created on first run):

- Windows: `%APPDATA%\EyeScroll\config.json`
- macOS: `~/Library/Application Support/EyeScroll/config.json`
- Linux: `~/.config/EyeScroll/config.json`

| Key | Effect |
|-----|--------|
| `reading_zone` | Bigger = larger calm band; smaller = scroll with less eye movement. |
| `gaze_range` | Smaller = reach max speed with less deflection (more sensitive). |
| `scroll_max_step` | Top scroll speed. |
| `ema_alpha_brake` | Higher = stops faster when you look back to center (less overshoot). |
