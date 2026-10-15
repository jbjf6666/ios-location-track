# iOS Virtual Location & Route Simulator (macOS)

[中文](README.md) | [**English**](README.en.md)

A desktop tool for **virtual location** and **route simulation** on iPhone / iPad from a Mac: draw a route on the map, add waypoints, and drive the device's location along it at realistic speeds.

Supports **iOS 17 – iOS 27.2** (RSD tunnel + DVT `SimulateLocation`). The developer disk image assets are bundled inside the app, so **no Python installation is required**.

The UI is a local web app opened in a Chrome / Edge app window (no address bar — looks like a native app). You can also just open `http://127.0.0.1:8787` in a browser. The map, route engine, playback engine, and device communication all run locally; only map tiles and optional route planning need network access.

---

## 1. Quick Start

### Option A — Download the installer (recommended)

Grab `iOSLocationTrack-1.0.0.dmg` from [Releases](https://github.com/jbjf6666/ios-location-track/releases):

1. Open the DMG and drag `虚拟定位轨迹.app` into `Applications`
2. If macOS says *"unidentified developer"* on first launch → **right-click → Open → Open**

**No Python, no dependencies** — the Python runtime and every dependency are bundled inside the app (~80 MB).
Config and tile cache are written to `~/Library/Application Support/iOSLocationTrack/`.

> **Apple Silicon (M-series) only.** For Intel Macs, use the source option below, or run `./scripts/build_app.sh` on an Intel machine (the script is architecture-agnostic).

### Option B — Run from source

```bash
git clone https://github.com/jbjf6666/ios-location-track.git
cd ios-location-track

# First run creates a virtualenv and installs dependencies automatically
./scripts/run.sh
```

The UI opens automatically. Equivalent CLI usage:

```bash
./.venv/bin/python main.py                 # normal launch (opens the UI)
./.venv/bin/python main.py --mock           # no-device mock mode (demo the route/playback)
./.venv/bin/python main.py --port 8899 --no-browser
./.venv/bin/python main.py --window browser # open in the default browser instead
```

### Three ways to run it

| Form | Path | Environment needed | Use case |
| --- | --- | --- | --- |
| **DMG installer** | Release asset (45 MB) | **None** | Distribution / moving to another Mac |
| **Standalone app** | `dist/虚拟定位轨迹.app` (~80 MB extracted) | **None** — Python, deps, web UI, icon, disk image assets are all inside | Double-click and go |
| Source | `./scripts/run.sh` | Python 3.11+ (deps installed on first run) | Hacking on the code |

```bash
# Rebuild everything in one shot: icon → bundle → ad-hoc sign → DMG → verify
./scripts/build_app.sh
```

The DMG contains the app, an `Applications` shortcut (drag to install), and an install note.
To customize the DMG appearance, edit the constants at the top of `tools/make_dmg.py`.

> On first launch macOS will ask for permission to control Chrome/Edge (used to open the app window).

---

## 2. Supported iOS Versions

| Version | Supported | Notes |
| --- | --- | --- |
| **iOS 17 – 27.2** | ✅ | RSD tunnel + DVT. The app bundles the shared personalized disk image assets |
| iOS 16 and earlier | ❌ | Their developer disk images must be signed by Apple per OS build; third-party images are rejected by the device (verified: `Failed to verify the disk image signature`, and libimobiledevice fails identically) |

**Why one image covers everything:** starting with iOS 17, Apple switched to *personalized* developer disk images — a single base image (`Image.dmg`) plus `BuildManifest.plist`, **shared across all OS versions**. The actual signature is issued online by Apple's TSS server per device at mount time, so per-version assets are unnecessary. This tool bundles those assets (~16 MB); clicking **"Mount Developer Image"** works offline (only the signing step needs network).

---

## 3. Device Preparation (important)

| Requirement | Details |
| --- | --- |
| Cable connection | USB recommended; Wi-Fi drops easily when transferring large files |
| Unlock and **Trust** | Tap **Trust** on the *"Trust This Computer?"* prompt and enter your passcode |
| Enable Developer Mode | Settings → Privacy & Security → **Developer Mode** → on → **restart the phone** → confirm after reboot |
| Mount developer image | Click **"Mount Developer Image"** in the UI (assets are bundled), or let Xcode do it once |
| Close competing tools | i4Tools, 爱思助手, Xcode, etc. fight over the same RSD connection — **only one at a time** |

### Mounting the developer image

Click **"Mount Developer Image"** in the UI; progress streams into the log panel on the right. CLI equivalent:

```bash
# Check whether it's already mounted ( [] means not mounted )
./.venv/bin/python -m pymobiledevice3 mounter list

# Mount automatically
./.venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <YOUR_UDID>
```

Or just use Xcode: connect the device → Window → Devices and Simulators → wait for *"Preparing device for development"*.

> With Xcode installed, the system's `CoreDeviceService` usually mounts a local image
> (`/Library/Developer/DeveloperDiskImages/`) automatically when the device is plugged in.

> The packaged `.app` performs mounting through pymobiledevice3's **Python API** — no command-line tools required.

### About the iOS 17+ tunnel (RSD)

Since iOS 17, location simulation no longer goes over usbmux; an **RSD tunnel** must be established first. Three options:

1. **Rootless (default, recommended)**: runs `pymobiledevice3 remote start-tunnel --native --script-mode`, borrowing Apple's own `remoted` tunnel. The packaged app does this by **re-invoking itself** (`<app> tunnel-helper`), so **no external Python is needed**.
2. **Admin mode**: click *"Rebuild tunnel as administrator"* in the UI (macOS password prompt), or run `sudo ./scripts/run.sh`.
3. **Manual RSD**: the *"Manual RSD"* button — enter `HOST PORT` obtained elsewhere (e.g. `tunneld`).

> `remote tunneld`, `lockdown start-tunnel`, and the classic `remote start-tunnel` all **require root**; this tool never silently escalates privileges.

---

## 4. UI Overview

### Top bar: device
- Name / model / iOS version / connection type (USB or network)
- **Tunnel** status, **virtual location** status, **Developer Mode** status
- *Refresh*, *Connect*, *Disconnect*

### Left panel: route settings
- **Travel mode**: driving / cycling / walking / flying (sets base speed and whether real roads are used)
- **Playback speed**: 0.1x – 50x (affects route progression only, not the send rate)
- **Loop**: return to start at the end
- **Realism simulation** (toggleable anytime, even mid-playback):
  - **Location drift**: slow wandering that mimics GPS error, radius 0–50 m (default 4 m).
    Implemented with multi-octave smoothed noise — continuous along the route with inertia, so it **never produces random jumps**.
  - **Speed jitter**: instantaneous speed fluctuates smoothly around the setpoint, 0–60% (default ±8%).
  - **Send-timing jitter** (on by default): a real phone's GPS does not fire exactly once per second — it jitters 0.85–1.15 s with occasional dropped samples (~2%). Perfectly uniform intervals are one of the easiest things to spot in the data.
  - **Derive speed from displacement** (on by default): reported speed = actual displacement ÷ actual interval, matching how iOS derives `CLLocation.speed` from the coordinate stream, so **speed, displacement and timestamps stay mutually consistent**.
  - **Inject gross errors** (off by default): probabilistic bad fixes (multipath / urban canyon), for testing your own app's `horizontalAccuracy` filtering and outlier rejection.
  - All toggles and magnitudes persist across restarts.
- **Jump to coordinate**: type a lat/lng directly; save favourite places
- **Route summary**: total distance, estimated duration, path point count, waypoint count, route engine (OSRM real roads / straight line)
- **Waypoint list**: drag to reorder, double-click to edit coordinates, delete, centre on point
- **Saved routes**: save / load / delete (browser local storage)
- **Base map**: AMap vector (GCJ-02, Chinese labels) / AMap satellite / global satellite (Esri) / OpenStreetMap
- Actions: *Load OSRM route*, *Clear*, *Export GPX*, *Import GPX*, *Restore real location*
- Tile cache: shows directory and size, plus *Clear tile cache*

### Map toolbar
- **Select**: pan the map, drag waypoints, click to edit
- **Add waypoint**: click repeatedly on the map to add points (the most common way to draw)
- **Draw polyline**: click to draw, double-click or Esc to finish
- **Undo**, **Delete selected point**, **Fit to route**

### Bottom bar: playback
- Play / Pause / Resume / Stop
- Scrubbable progress bar (seek)
- Elapsed time, remaining time, sample index
- Live speed, current coordinates, playback rate

### Right panel: log
Live log of device connection, tunnel setup, image mounting, and every location push — makes troubleshooting much easier.

---

## 5. Typical Workflows

**A. Run a route along real roads**

1. Connect the device → *Connect* (iOS 17+ builds the tunnel automatically, up to ~1 minute)
2. Click *Add waypoint* and place start / intermediate / end points (at least 2)
3. Click *Load OSRM route* → real road-network polyline (falls back to a straight line offline)
4. Set *Playback speed* (e.g. 20x completes 10 km in a few seconds)
5. Click *Play* → the phone's location moves along the route; map marker and progress update live
6. Click *Stop* → the phone immediately returns to its real location

**B. Pin to a single location**

With the *Select* tool, double-click anywhere on the map, or use *Jump to coordinate*. Click *Restore real location* when done.

**C. Import an existing track**

Click *Import GPX* and pick a `.gpx` file → the track appears on the map → play it. Exported GPX files also import into Google Earth, Strava, etc.

**D. Preview without a device**

```bash
./.venv/bin/python main.py --mock
```
The UI, route drawing, playback engine and progress bar all work; locations just aren't pushed to a phone (the top bar shows *"mock mode"*).

---

## 6. CLI Tools and Self-Tests

```bash
# List devices + Developer Mode + DVT availability
./.venv/bin/python tools/probe_device.py
./.venv/bin/python tools/probe_device.py --probe      # actually open a DVT session

# One-shot virtual location from the CLI
./.venv/bin/python tools/probe_location.py --lat 39.9088 --lon 116.3975
./.venv/bin/python tools/probe_location.py --clear     # restore real location

# Offline self-test: 83 unit + end-to-end checks (no device needed)
./.venv/bin/python tests/selftest.py

# Realism model (GPS drift + speed jitter + timing jitter, 40 checks)
./.venv/bin/python tests/drift_sim.py

# Tile disk cache (13 checks) / browser-level offline tiles (9 checks)
./.venv/bin/python tests/offline_tiles.py
./.venv/bin/python tests/offline_browser.py

# UI acceptance (20 checks, drives a real browser)
./.venv/bin/python tests/ui_acceptance.py

# Verify the packaged standalone app: clean-env launch + all endpoints (22 checks)
./.venv/bin/python tools/verify_bundle.py

# Real-device end-to-end: actually moves the phone's location, restores afterwards
./.venv/bin/python tests/real_device_e2e.py --confirm --duration 5 --speed 20

# Screenshot the UI (visual acceptance)
./.venv/bin/python tools/screenshot.py http://127.0.0.1:8787/ /tmp/ui.png --wait 9
```

Packaging helpers:

```bash
./.venv/bin/python tools/make_icon.py     # generate the app icon (drawn programmatically)
./.venv/bin/python tools/fetch_ddi.py     # fetch developer disk image assets
./.venv/bin/python tools/make_dmg.py      # build the DMG
```

---

## 7. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| UI says "local service not connected" | Server didn't start or the port is taken. Check the terminal; try `--port 8899` |
| No device detected | Cable + unlocked + tap **Trust** |
| Prompted for Developer Mode | Settings → Privacy & Security → Developer Mode → on, then restart the phone |
| "Mount Developer Image" fails with a signature error | The device runs iOS 16 or earlier (unsupported); or the network is blocking Apple TSS. Check connectivity and retry |
| Tunnel-related errors when connecting | ① Close i4Tools / 爱思助手 (they steal the RSD connection); ② use *Rebuild tunnel as administrator*; ③ confirm the developer image is mounted |
| Location doesn't change on the phone | Make sure the UI shows "simulating"; on iOS 17+ the tunnel must be healthy |
| Blank map | Tile source blocked — switch base map (AMap satellite / Esri). **Previously viewed areas work offline** (tiles cached in `~/.ios-location-tiles`, 1 GB cap, LRU eviction) |
| Location snaps back right after the tunnel is up | Another program (i4Tools / Xcode) is fighting for the same RSD connection — close it |

---

## 8. Implementation Notes

- **Transport**: Python stdlib `http.server` plus a hand-written WebSocket (RFC 6455) — no web framework. Device side uses `pymobiledevice3`.
- **Virtual location**: DVT service `com.apple.instruments.dtservicehub` (iOS 17+, over the RSD tunnel), calling `LocationSimulation.set(lat, lon)` / `clear()`.
  **The connection must stay open** — once the DVT session drops, simulated location stops working. That's why the playback engine can't reconnect per point.
- **Playback engine**: advances by arc-length integration; the speed profile is generated from turn angles (slower in sharp corners) and then smoothed, so acceleration feels natural. A dedicated thread with a single in-flight request guarantees strictly ordered delivery.
- **Realism model**: drift uses **multi-octave value noise** (120 m slow + 30 m medium + 15 m fast correlation lengths, approximating the power-law spectrum of real GPS error), normalized to the configured radius and clipped at 1.8x; consecutive fixes transition smoothly, so there are no random-jump spikes.
- **Coordinates**: everything internal and backend-side is **WGS84**; AMap tiles are GCJ-02, converted in the frontend both for tile requests and marker placement — otherwise the marker and the actual location would be ~500 m apart.
- **Tiles**: server-side proxy plus disk cache (`~/.ios-location-tiles`, 1 GB cap, LRU). Fully transparent "empty tiles" returned by providers are detected and discarded.
- **Routing**: prefers OSRM real roads (`router.project-osrm.org`); falls back to a straight line offline and labels the engine type on the map.
- **Standalone packaging**: PyInstaller onedir + ad-hoc signature. Tunnel establishment uses **self-invocation** (`<app binary> tunnel-helper`), so the packaged app needs no Python on the machine.
- **Protocol**: see [docs/PROTOCOL.md](docs/PROTOCOL.md).
- **Windows port**: see [docs/WINDOWS_PORT.md](docs/WINDOWS_PORT.md) (portability audit, tunnel
  replacement, packaging changes, acceptance baseline). Roughly 90% of the core logic is reusable;
  only tunnel establishment and the distribution pipeline need rewriting. (Written in Chinese.)

---

## 9. Known Limitations

- **DVT can only set latitude/longitude**: `simulateLocationWithLatitude:longitude:` takes exactly two arguments. `horizontalAccuracy`, `verticalAccuracy`, `altitude`, `floor`, `sourceInformation.isSimulatedBySoftware`, and all accelerometer / gyroscope / pedometer data are either synthesized by iOS or come from hardware — **they cannot be injected over the DVT channel**. This is a shared boundary of every tool in this category, including Xcode's own location simulation.
- **Some apps are unaffected**: apps using a separate location channel, or performing server-side kinematic cross-validation, may either ignore the simulated location or accept it and discard the data.
- **Only one tool can hold the tunnel at a time**: an iOS 17+ device exposes a single RSD connection, so i4Tools / Xcode and this tool will kick each other off.
- The image-signing step requires reachable Apple TSS servers.
- Map tiles come from public services and are intended for personal testing only — respect their terms of service.

---

## 10. Acceptable Use

This tool is intended for **software testing, development debugging, and regression verification of location-dependent features**, for example:

- Testing how your app renders and behaves in different cities or countries
- Verifying edge cases in route replay, geofencing, and check-in style features
- Demos and teaching

Do not use it to deceive others, falsify travel records, evade attendance systems, or for any unlawful purpose. You are responsible for any consequences of using this tool.

In particular: this tool **does not** provide any capability to bypass third-party anti-cheat or risk-control systems. If you need to test features that depend on altitude, barometer, or pedometer data, the only reliable approach is to add a location-injection hook to a Debug build of your own app and construct a complete `CLLocation` yourself.
