# ios-preview

Build and live-preview any iOS app on the simulator inside Claude — interactive
MJPEG view, side-by-side `os.Logger` stream, in-pane **Run/Stop button**, and
**simulator picker**. Zero per-project setup.

## What's new in 0.5.0

- **In-pane Run/Stop button** — click Run to rebuild, install, and relaunch; build
  output streams into the log panel. Click Stop to terminate the app (and cancel
  an in-flight build). Works like Xcode's Run button, from inside the preview pane.
- **Simulator picker** — a dropdown in the run bar lists all available iOS simulators
  (booted ones first). Choosing one boots it if needed and re-points the entire
  preview — video, logs, taps, and the next Run — to that device in one click.
- **`IOS_PREVIEW_ALLOW_ORIGIN`** — new escape-hatch env var for non-loopback
  proxy setups (see §6 and Troubleshooting).

## Demo

https://github.com/user-attachments/assets/a2c5fcbd-618b-4d83-9ea9-38cf3c60b6c2

## 1. What it does

`/ios-preview:start` auto-detects your Xcode project and scheme, builds the app,
boots a simulator if none is running, and opens an interactive pane showing the
simulator screen. Clicks become taps, drags become swipes.

The run bar at the top of the pane has two controls:

- **Run/Stop button** — click **Run** to rebuild, install, and relaunch the app;
  build output streams into the log panel live. Click **Stop** to terminate the
  app (or cancel an in-flight build). Tapping and the log stream keep working
  throughout.
- **Simulator picker** — lists all available iOS simulators (booted ones first).
  Choosing one boots it if needed and re-points the entire preview — video, logs,
  taps, and the next Run — to that device in one click.

A log panel alongside shows your app's `os.Logger` output live. A toolbar
dropdown toggles between **App logs** (your app's output only, Xcode-like) and
**All logs** (the full system firehose) without restarting.

`/ios-preview:logs` attaches the log stream to an already-running app without
building or opening the video feed.

`/ios-preview:stop` tears down both the video and log panes.

All project detection is automatic: CocoaPods workspaces, single-scheme and
multi-scheme projects, and custom simulator choices are all supported.

## 2. Requirements

- macOS (Apple Silicon or Intel)
- Xcode + Command Line Tools (`xcode-select --install`)
- Node.js / `npx` (for `xcodebuildmcp` which supplies the `axe` binary)
- Python 3 (ships with macOS)
- Claude Code with plugin support

## 3. Install

```
/plugin marketplace add Vvlladd/ios-preview
/plugin install ios-preview@ios-preview
/reload-plugins
```

When prompted, trust the two bundled MCP servers (`xcodebuildmcp` and `sosumi`).

## 4. Usage

```
/ios-preview:start                          # auto-detect sim, build, preview
/ios-preview:start booted                   # use the already-booted simulator
/ios-preview:start "iPhone 15 Pro"          # pick sim by name
/ios-preview:start auto MyScheme            # specify scheme explicitly
/ios-preview:logs                           # log stream only (app already running)
/ios-preview:stop                           # tear down all ios-preview panes
```

All `IOS_*` variables can be set in the environment to override detection:

```sh
export IOS_PROJECT=/path/to/App.xcworkspace
export IOS_SCHEME=MyApp
/ios-preview:start
```

## 5. What it writes to your project

The plugin writes two files inside your project's `.claude/` directory:

| File | Purpose |
|------|---------|
| `.claude/ios-preview.env` | Detected `IOS_*` values (mode `0600`; gitignored automatically) |
| `.claude/launch.json` | Merged `ios-preview:` preview entries (your own entries are untouched) |

The `ios-preview.env` file is appended to `.claude/.gitignore` automatically.
The plugin never modifies your `xcodeproj`, `xcworkspace`, `Info.plist`, or any
other project file.

## 6. IOS_* environment variable reference

| Variable | Meaning | Default |
|----------|---------|---------|
| `IOS_PROJECT_DIR` | Absolute project root | Detected |
| `IOS_PROJECT` | Path to `.xcworkspace` or `.xcodeproj` | Detected |
| `IOS_PROJECT_KIND` | `workspace` or `project` | Detected |
| `IOS_SCHEME` | Xcode scheme | Detected (sole app scheme) |
| `IOS_CONFIG` | Build configuration | `Debug` |
| `IOS_PRODUCT_NAME` | Process name for log predicate | Detected from build settings |
| `IOS_BUNDLE_ID` | Bundle identifier | Detected from build settings |
| `IOS_FULL_PRODUCT_NAME` | `.app` bundle name | Detected from build settings |
| `IOS_DERIVED_DATA` | DerivedData path | Newest matching or `<base>-claude-preview` |
| `IOS_SIM_UDID` | Simulator UDID (resolved once) | Detected |
| `IOS_LOG_SUBSYSTEM` | Filter to one `subsystem ==` (most targeted) | Unset |
| `IOS_LOG_MODE` | Initial log filter — `app` (your app's logs, Xcode-like) or `all` (everything). Switchable live via the toolbar dropdown. | `app` |
| `IOS_LOG_VERBOSE` | Legacy alias for `IOS_LOG_MODE=all` | Unset |
| `IOS_LOG_LEVEL` | `simctl log stream --level` | `debug` |
| `PORT` | MJPEG server port | `8765` |
| `FPS` | Video frames per second | `12` |
| `QUALITY` | JPEG quality (1-100) | `55` |
| `SCALE` | Video scale `0.1`-`1.0` (lower = smaller frames = less lag) | `0.75` |
| `IOS_PREVIEW_ALLOW_ORIGIN` | Extra allowed POST `Origin` (exact, or `*`) when the pane is proxied under a non-loopback origin | Unset (loopback origins always allowed) |

## 7. Troubleshooting

**`axe` binary still downloading**
`xcodebuildmcp` installs `axe` on first use via `npx`. Run `/ios-preview:start`
once and wait; subsequent runs use the cached binary.

**Port 8765 already in use**
Export a free port before running:
```sh
export PORT=8766
/ios-preview:start
```
Or stop the existing preview first with `/ios-preview:stop`.

**Wrong scheme detected / multiple schemes**
Set `IOS_SCHEME` to the exact scheme name:
```sh
export IOS_SCHEME=MyApp
/ios-preview:start
```

**Wrong project detected / multiple projects**
Set `IOS_PROJECT` to the exact path:
```sh
export IOS_PROJECT=/path/to/App.xcworkspace
/ios-preview:start
```

**SwiftPM-only project (no .xcodeproj/.xcworkspace)**
Open the package in Xcode once (`File > Open`) so Xcode generates the derived
project files, then rerun `/ios-preview:start`.

**Build fails**
`/ios-preview:start` runs the build via Bash and reports the `xcodebuild` error
directly. Fix the underlying compile or signing error in Xcode, then retry.

**Logs too noisy (boringssl / CFNetwork / system spam)**
The pane defaults to **App logs** — it shows what your app's own code emits
(print, NSLog, Logger) and hides everything from system frameworks (anything
under `/System` or `/usr/lib`, plus `com.apple.*` subsystems), like the Xcode
console. Flip the **App logs / All logs** dropdown in the toolbar to switch live,
or set `IOS_LOG_SUBSYSTEM` to your app's `os.Logger` subsystem to narrow to just
those messages.

**Taps/clicks or Run/Stop not registering in the preview pane**
The server enforces an `Origin` check on POST requests (tap/swipe/key and
Run/Stop). It accepts loopback origins (`localhost`/`127.0.0.1`/`::1`, any port)
and requests with no `Origin` header. If your preview is proxied under a
non-loopback origin, requests are rejected with 403 — set
`IOS_PREVIEW_ALLOW_ORIGIN` to that origin (or `*`) and rerun:
```sh
export IOS_PREVIEW_ALLOW_ORIGIN='https://your-proxy-origin'
/ios-preview:start
```

**Preview feels laggy / video trails behind**
The pane is a proxied MJPEG stream, so video latency has a floor set by the
preview proxy itself — your input (tap/swipe) stays 1:1, but the picture trails
slightly. Shrink the frames (the biggest lever): lower `SCALE` and/or `QUALITY`.
Raising `FPS` improves smoothness but can *increase* latency on a constrained
link, so prefer smaller frames over more frames:
```sh
export SCALE=0.5 QUALITY=40
/ios-preview:start
```

## Known limitations

- **Origin/CSRF guard:** the `sim-mjpeg.py` HTTP server accepts POST requests
  (`/tap`,`/swipe`,`/key`,`/run`,`/stop`) only from loopback origins
  (`localhost`/`127.0.0.1`/`::1`, any port) or with no `Origin` header (e.g.
  direct `curl`). This guards against malicious-page CSRF on the loopback
  interface. If you access the preview through a proxy/tunnel that rewrites the
  origin to a non-loopback value, set `IOS_PREVIEW_ALLOW_ORIGIN` to that origin
  (or `*`) — or edit `_check_origin()` in `scripts/sim-mjpeg.py`.

- **Single booted simulator:** the plugin resolves exactly one simulator UDID at
  detection time and passes it to all downstream scripts. If you boot a second
  simulator after running `/ios-preview:start`, the existing pane continues
  streaming the original sim.

- **Cold builds on first run** can take several minutes. `/ios-preview:start`
  uses an extended Bash timeout; subsequent incremental builds are much faster.

## 8. Changelog

| Version | Highlights |
|---------|------------|
| **0.5.0** | In-pane Run/Stop button; simulator picker; `IOS_PREVIEW_ALLOW_ORIGIN` |
| **0.3.0** | Live App/All log-filter toolbar dropdown; system-framework noise filtering |
| **0.2.0** | `SCALE` + `QUALITY` tuning knobs; TCP_NODELAY; smoother stream defaults |
| **0.1.0** | Initial release: `/ios-preview:start`, `/logs`, `/stop`; auto-detection; MJPEG + log pane |

## 9. Security and privacy

**sosumi (Apple documentation MCP)**
The bundled `sosumi` server is a remote HTTP MCP at `https://sosumi.ai/mcp`.
When Claude queries Apple documentation, those queries are sent to the
third-party `sosumi.ai` service. Review their privacy policy before use.
The plugin does not control or log what sosumi receives.

**xcodebuildmcp**
Pinned to version `1.15.1` in `.mcp.json`. It runs locally via `npx` and
never contacts external services at runtime (only `npmjs.com` at install time).

To bump the pin:
1. Open `.mcp.json` in the plugin directory.
2. Change `"xcodebuildmcp@1.15.1"` to the desired version.
3. Run `/reload-plugins` and re-trust the MCP server.

**Env file**
`ios-preview.env` is written with mode `0600` (owner-read-only) and is
automatically added to `.claude/.gitignore` so it is never committed.

**No telemetry**
The plugin itself sends no data anywhere. MIT licensed — see `LICENSE`.
