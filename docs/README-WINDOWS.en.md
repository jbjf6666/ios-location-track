# Windows Guide — iOS Virtual Location & Route Simulator

[中文](README-WINDOWS.md) | [**English**](README-WINDOWS.en.md)

> This document is for Windows users and maintainers.
> macOS instructions live in [README.md](../README.md); the porting rationale and
> lessons learned are in [WINDOWS_PORT.md](WINDOWS_PORT.md).

---

## 1. Quick start (3 minutes)

1. Run `iOSLocationTrack-1.1.0_win-setup.exe`, or unzip the portable build and
   double-click `iOSLocationTrack.exe`.
2. **The app requests administrator rights** — click *Yes* on the UAC prompt.
   This is only needed so it can auto-install the Apple driver on first run.
3. An **Environment Check** window appears, listing every required component:
   - Ready items show a green ✓ and their button reads *已就绪* (disabled)
   - Missing items show a red ✗ with a clickable **Install / Download** button
   - **The "进入程序" (Enter) button only becomes clickable once every required item is ready**
4. Click **进入程序** — the UI opens in a Chrome/Edge app window (no address bar).
5. Connect your iPhone by cable → unlock it → tap *Trust* → click **连接设备** in the UI.

> Running from source: `.\scripts\run.ps1` (creates the venv, installs dependencies
> and fetches the disk-image assets automatically).

---

## 2. What the Environment Check verifies

| Item | Required | How it is checked | What "Install" does |
| --- | --- | --- | --- |
| Python runtime | ✅ | version string | — (bundled in the exe) |
| pymobiledevice3 | ✅ | import + version | — (bundled) |
| **Apple Mobile Device Support** | ✅ | service state + a protocol round-trip on port 27015 | downloads and installs it (~40–200 MB) |
| RSD tunnel deps | ✅ | imports `pytun_pmd3` / `pmd_pytcp` etc. | — (bundled) |
| Developer disk image | ✅ | three files under `resources/ddi/personalized/` | downloads the personalized image (~15 MB) |
| Administrator rights | ✅ | `IsUserAnAdmin` | right-click → *Run as administrator* |
| iPhone connection | ➖ optional | usbmuxd `ListDevices` | — (just plug the phone in) |
| Network reachability | ➖ optional | TCP to `gs.apple.com:443` | — (Apple signs the image online) |

**The only authoritative signal that usbmuxd actually works is a protocol round-trip
on `127.0.0.1:27015`** — not merely that the port accepts a connection.
`ReadBUID` needs no device attached, so the service can be validated with no phone present.

### Text-mode self check

```powershell
# portable build
.\dist\iOSLocationTrack\iOSLocationTrack.exe --check-driver
# from source
.venv\Scripts\python.exe main.py --check-driver
```

Sample output:

```
平台：Windows（管理员：是）
服务：RUNNING（Apple Mobile Device Service）
版本：Apple Mobile Device Support 20.0.0.35
usbmuxd(127.0.0.1:27015)：可通信
设备：1 台
  · 00008030-001551840A02402E（USB）
结论：Apple Mobile Device Service 正在运行，usbmuxd 可通信
```

---

## 3. Automatic Apple Mobile Device Support installation

Click **Install** on the Apple row in the Environment Check window, or run:

```powershell
main.py --install-driver                  # from source
iOSLocationTrack.exe --install-driver     # portable build
```

Strategies are tried in order and fall through automatically (`core/apple_driver.py`):

| # | Strategy | Notes |
| --- | --- | --- |
| 0 | Start the existing service | If it is installed but stopped, `sc start` is all that is needed (cheapest, most common fix) |
| 1 | Standalone AMDS MSI | `AppleMobileDeviceSupport64.msi` from Apple's CDN (~38 MB) |
| 2 | Full iTunes bundle | `https://www.apple.com/itunes/download/win64` (~193 MB) |
| 3 | winget | `Apple.AppleMobileDeviceSupport` / `Apple.iTunes` |

Success exit codes: `0`, `3010`, `1641`, `1638` (MSI semantics; 3010/1641 mean a reboot is needed).

### Traps you must know about (all verified on real hardware)

1. **The service name contains spaces: `Apple Mobile Device Service`.**
   `AppleMobileDeviceService` is only the exe basename — `sc query AppleMobileDeviceService`
   fails with error 1060.
2. **There is no `\\.\pipe\usbmuxd` on Windows.** usbmuxd is exposed purely as TCP
   `127.0.0.1:27015`.
3. **Do not detect the driver by looking for `usbaapl64`.** The AMDS MSI contains
   **zero `.inf`/`.sys`/`.cat` files** — it does **not** ship the USB driver. On a modern
   machine the iPhone actually binds to `appleusb.inf` (service `WINUSB` / `usbccgp`).
   A `usbaapl64` check produces false "missing driver" reports on perfectly healthy systems.
4. **The AMDS ProductCode changes with every version.** The stable identifier is the
   UpgradeCode `{5CF0656E-F3B8-11DB-BA81-804E56D89593}` (shared by x86 and x64).
5. **Apple publishes no stable "latest" direct link** — the `iTunes64Setup.exe` URL embeds
   a build number. This project therefore pins a SHA256 where one is available and fails
   loudly rather than installing something unverifiable.
6. **Microsoft Store iTunes and the classic installer are mutually exclusive**: the classic
   installer's `LaunchCondition` hard-codes `NOT ITUNES_STORE_APP_INSTALLED`. Separately,
   the Store daemon **cannot discover devices over Wi-Fi**. pymobiledevice3's own docs
   recommend the classic installer for exactly this reason.

---

## 4. How Windows differs from macOS (**important**)

The single biggest porting trap: **the two platforms build the RSD tunnel in fundamentally
different ways**, so `core/tunnel.py` has two entirely separate code paths.

| | macOS | Windows |
| --- | --- | --- |
| Tunnel | `remote start-tunnel --native` (piggybacks Apple's `remoted`) | `UserspaceRsdTunnel` (pure-Python PyTCP userspace stack) |
| Needs root/admin | No | **No** |
| Needs a third-party driver | No | **No** (Wintun not required) |
| RSD address reachable from | any process (kernel-routable) | **this process only** (userspace stack) |
| Implementation | subprocess + parse `HOST PORT` from stdout | build and hold the RSD object on the app's event loop |
| Measured time | — | **~0.3 s** |

### Why Windows cannot use the "tunnel helper subprocess" design

On macOS the tunnel runs in a child process that prints `HOST PORT`, and the main process
connects over ordinary TCP. That does not work on Windows:

- The userspace tunnel's device address **only exists inside the protocol stack of the
  process that created it**;
- pymobiledevice3 injects a relay dialer into the `RemoteServiceDiscoveryService` object it
  returns (`open_connection=dial_plane.dial`);
- Building a *new* RSD from `(host, port)` falls back to the stdlib dialer, which goes to the
  OS network stack and fails with `OSError [WinError 1231] The network location cannot be
  reached` (reproduced during this port).

**The correct approach**: inject the **rsd object itself** into the DVT layer.
`RealLocationProvider` already had an `rsd=` injection seam (`core/location_provider.py`),
and `core/server.py:provider_for()` now uses it on Windows.

### About Wintun

`docs/WINDOWS_PORT.md` originally said to install Wintun first. **Measurement says it is not
needed:**

- The userspace tunnel runs a pure-Python PyTCP stack and creates no kernel adapter;
- Moreover the `pytun_pmd3` dependency **already bundles** `wintun.dll` for every
  architecture (`pytun_pmd3/wintun/bin/amd64/wintun.dll`), so there was never anything to
  download.

### Verified environment

| Item | Value |
| --- | --- |
| OS | Windows 11 Home (Chinese), 10.0.26200, x64 |
| Python | 3.12.14 |
| pymobiledevice3 | 11.31.0 |
| Device | iPhone 11 (`iPhone12,1`), **iOS 26.6.1** |
| Apple Mobile Device Support | 20.0.0.35 |
| iTunes | 12.13.11.1 (classic) |

Measured connect time: **0.8 s** (0.3 s tunnel + image/DVT session).

---

## 5. Where data and logs live

| Content | Path |
| --- | --- |
| Config / tile cache / logs | `%LOCALAPPDATA%\iOSLocationTrack\` |
| Application log | `%LOCALAPPDATA%\iOSLocationTrack\logs\app.log` |
| MSI install logs | `%LOCALAPPDATA%\iOSLocationTrack\logs\msi-*.log` |
| Downloaded installers | `%LOCALAPPDATA%\iOSLocationTrack\installers\` |
| Developer image cache | `%USERPROFILE%\.pymobiledevice3\Xcode_iOS_DDI_Personalized\` |

`app.log` is UTF-8, tab-separated: `timestamp \t level \t message`.
Open it in Notepad or use `Get-Content -Encoding UTF8` (Windows PowerShell 5.1 defaults to
ANSI and will show mojibake).

---

## 6. Building

```powershell
# One shot: icon → image assets → tests → PyInstaller → Inno Setup
.\scripts\build.ps1

# exe only
.\scripts\build.ps1 -NoInstaller

# Fast rebuild, skip tests
.\scripts\build.ps1 -SkipTests -SkipIcon -SkipDdi
```

Outputs:

| File | Notes |
| --- | --- |
| `dist\iOSLocationTrack\iOSLocationTrack.exe` | Portable onedir build — copy the whole folder |
| `dist\installer\iOSLocationTrack-1.1.0_win-setup.exe` | Installer (Inno Setup 6) |

**Prerequisites:**

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ pymobiledevice3 pillow pyinstaller
winget install -e --id JRSoftware.InnoSetup     # optional, installer only
```

### Packaging traps (all fixed — do not reintroduce them)

1. **Do not exclude `distutils` / `setuptools` / `wheel` / `pip`.** PyInstaller and
   setuptools hooks force-import or alias them, and the build dies immediately:
   ```
   ValueError: Target module "distutils" already imported as ExcludedModule('distutils',)
   ValueError: Target module "wheel"     already imported as ExcludedModule('wheel',)
   ```
2. **Do not exclude `tkinter`** — the Environment Check window is built with it.
3. **`copy_metadata` must include** `apple_compress` and friends, otherwise mounting fails
   with `No package metadata was found for apple_compress`.
4. **Do not exclude `questionary` / `prompt_toolkit` / `pygments`** — pymobiledevice3 imports
   them at runtime; excluding them silently breaks real-device support (device discovery
   degrades to the libimobiledevice fallback).
5. **With `--windowed`, `sys.stdout` is `None`** — check before printing.
6. **`.ps1` files must be UTF-8 *with BOM*.** Windows PowerShell 5.1 only parses a script as
   UTF-8 when it sees a BOM; otherwise it uses the system ANSI code page (GBK on Chinese
   Windows), which turns every Chinese character into mojibake and **breaks the script with a
   syntax error** (`The string is missing the terminator`).
   Both `scripts\run.ps1` and `scripts\build.ps1` are stored as UTF-8 with BOM, and
   `build.ps1` re-adds a missing BOM automatically as step 0.5.

### Requesting administrator rights at launch

The Windows branch of `Location.spec` sets `uac_admin=True`, which makes PyInstaller write
`requestedExecutionLevel level="requireAdministrator"` into the exe manifest.
You can verify it by searching the exe for `requireAdministrator`.

> Note: the userspace tunnel itself does **not** need administrator rights. Elevation exists
> purely so the first run can auto-install Apple Mobile Device Support without asking the user
> to right-click and elevate manually.

---

## 7. Acceptance tests

```powershell
# No device needed (136 checks)
.venv\Scripts\python.exe tests\selftest.py        # 83: protocol/geometry/playback/config/tiles/e2e
.venv\Scripts\python.exe tests\drift_sim.py       # 40: realism model
.venv\Scripts\python.exe tests\offline_tiles.py   # 13: offline tile cache

# Platform feasibility (real device, 7 checks)
.venv\Scripts\python.exe tests\win_e2e.py --confirm

# Application layer (real device, 9 checks; uses App's real wiring)
.venv\Scripts\python.exe tests\win_app_e2e.py --confirm

# UI without a device
.venv\Scripts\python.exe main.py --mock
```

Real-device acceptance, the 6 steps from §7 of `WINDOWS_PORT.md`:

1. `pymobiledevice3 usbmux list` sees the device ✅
2. Developer disk image mounts ✅
3. "Connect device" → tunnel up → DVT session ready ✅
4. Play a route → the phone's location actually moves ✅
5. "Stop" → the phone returns to its real location ✅
6. App layer 9/9 and platform layer 7/7 ✅

---

## 8. Known limitations

| Limitation | Detail |
| --- | --- |
| **One userspace tunnel per process** | PyTCP's stack is a process-global singleton. Do not run two instances. |
| **Only one tool may hold the RSD connection** | 爱思助手 / i4Tools / Xcode will evict each other. Close them first. |
| **iOS 16 and earlier unsupported** | Classic images must be signed by Apple per build; third-party images are rejected by the device. This project targets iOS 17+. |
| **DVT can only change latitude/longitude** | `simulateLocationWithLatitude:longitude:` takes only two arguments; altitude, accuracy and sensor data are synthesized by iOS and cannot be injected. |
| **Mounting the image needs network access** | The personalized image is signed online by Apple TSS per device. |
| **The tunnel can drop silently** | The Windows userspace stack occasionally disconnects, after which the device quietly reverts to its real location while the UI still looks fine. A **keepalive** re-sends the last point every 15 s when a location is set but playback is stopped (`core/location_provider.py:KEEPALIVE_INTERVAL`). |
| **DDI mounting may disturb DVT on iOS 26.4.x** | One peer project reports that uploading the image on 26.4.1 kills the tunnel, poisoning later DVT calls with `InvalidService`. Verified fine on 26.6.1 here. If you hit it, mount the image in another tool first. |
| **Unsigned exe triggers SmartScreen** | There is no code-signing certificate. Click *More info → Run anyway*. |
| **Windows 10 1809 or later** | The installer sets `MinVersion=10.0`. |

---

## 9. File map (Windows-related changes)

| File | Notes |
| --- | --- |
| `core/platform.py` | **New.** Platform abstraction: data dir, browser probing, process management, elevation, signals |
| `core/apple_driver.py` | **New.** Apple Mobile Device Support detection/install + developer image download |
| `core/setup_window.py` | **New.** The Environment Check window (tkinter) |
| `core/tunnel.py` | **Rewritten.** Windows uses an in-process userspace tunnel; macOS keeps the native subprocess tunnel |
| `core/server.py` | Injects the runner into TunnelManager; `provider_for()` injects the rsd object; file logging; resources dir split from data dir |
| `core/location_provider.py` | Platform-aware error messages; **keepalive added** (`_keepalive_loop`) |
| `main.py` | Environment Check window, background Apple driver check, `--check-driver` / `--install-driver`, cross-platform browser and signals |
| `Location.spec` | Platform branches; Windows uses `.ico` + `uac_admin=True` + `version_info.txt`; **one spec, no fork** |
| `tools/make_ico.py` | **New.** Builds a multi-size `.ico` from the existing PNG iconset |
| `tools/probe_tunnel_win.py` | **New.** Tunnel feasibility diagnostic |
| `scripts/run.ps1` / `build.ps1` | **New.** Windows run/build scripts |
| `installer/iosloc.iss` | **New.** Inno Setup installer script |
| `installer/version_info.txt` | **New.** exe version resource |
| `installer/languages/ChineseSimplified.isl` | **New.** Inno Setup 6.7 no longer bundles Chinese, so it ships with the repo |
| `tests/win_e2e.py` / `win_app_e2e.py` | **New.** Real-device acceptance (platform layer / application layer) |

The macOS build chain (`tools/make_dmg.py`, `scripts/run.sh`, `build_app.sh`, and the
`BUNDLE` branch of `Location.spec`) is **fully retained** — a single codebase builds on both
platforms.
