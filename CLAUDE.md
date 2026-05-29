# CLAUDE.md — ios-preview

A **public Claude Code plugin** that builds and live-previews any iOS app on the simulator from inside the Claude app: an interactive MJPEG view (click = tap, drag = swipe) with a side-by-side `os.Logger` stream. Goal: **zero per-project setup** — install once, works in any iOS project.

## How it works (runtime flow)
The slash command `/ios-preview:start` orchestrates four bundled pieces:
1. `scripts/detect.sh` — auto-detects project/scheme/bundle/product/sim and writes a `0600` `IOS_*` env file. **Single source of truth** for detection.
2. `scripts/run-ios.sh --no-stream` — builds + installs + launches the app on the resolved sim, then exits (Claude runs this so it can react to build errors).
3. `scripts/write-launch-json.py` — merges two `ios-preview:` entries into the project's `.claude/launch.json` with **absolute** plugin-script paths baked in.
4. `preview_start("ios-preview: interactive simulator")` → runs `scripts/sim-mjpeg.py`, an HTTP server the Claude pane proxies.

The interactive pane has an in-pane **Run/Stop button** (Xcode-style). `sim-mjpeg.py` exposes `POST /run` (full rebuild+install+launch via `run-ios.sh --no-stream` on a background thread), `POST /stop` (`xcrun simctl terminate "$SIM" "$IOS_BUNDLE_ID"`, also kills an in-flight build), `GET /status` (run-state JSON), and `GET /build/stream` (SSE of build output + state changes). A thread-safe `RunController` tracks state (`idle`→`building`→`running`/`failed`); build output streams to the pane **and** is mirrored to the server's stdout. Because of this, `write-launch-json.py` bakes the **full `IOS_*` set** (incl. `IOS_BUNDLE_ID` + build vars) into the *interactive* entry, not just UDID + product name.

The run bar also has a **simulator picker**: `GET /sims` lists available iOS sims (booted first) and `POST /sim {udid}` calls `switch_sim()` — it boots the target and re-points the streamed `SIM`, screen dims (`W_PTS`/`H_PTS`), and the stdout log mirror; the client then reloads `/stream` + `/logs/stream`. So video, logs, taps, and the next Run/Stop all follow the picked sim.

`commands/logs.md` = logs-only (no video); `commands/stop.md` = teardown. `.mcp.json` bundles `xcodebuildmcp` (provides the `axe` binary) + `sosumi` (Apple docs).

## The IOS_* env contract
Detection is decoupled from the scripts via env vars (see `detect.sh` header for the full list): `IOS_PROJECT`/`IOS_PROJECT_DIR` (absolute), `IOS_PROJECT_KIND`, `IOS_SCHEME`, `IOS_CONFIG`, `IOS_PRODUCT_NAME`, `IOS_BUNDLE_ID`, `IOS_FULL_PRODUCT_NAME`, `IOS_DERIVED_DATA`, `IOS_SIM_UDID`, optional `IOS_LOG_SUBSYSTEM`/`IOS_LOG_LEVEL`. Stream tuning: `PORT`/`FPS`/`QUALITY`/`SCALE`. Any preset value is honored as an override.

## Invariants — do NOT break these
- **Single-sim (one at a time):** `detect.sh` resolves ONE `IOS_SIM_UDID` at startup. The pane's **sim picker** can change the active sim at runtime via `switch_sim()`, but it re-points **everything together** (video, logs, taps, the build target `IOS_SIM_UDID`, and the stdout mirror) so video + logs + build always stay on the *same* sim — never split across two. `run-ios.sh` and `sim-mjpeg.py` never *independently* re-pick.
- **stdout contract:** `detect.sh` prints ONLY `IOS_*=` lines to stdout; all progress/errors go to stderr (the command parses stdout).
- **Security (S1):** `detect.sh` validates `IOS_PRODUCT_NAME` (`^[A-Za-z0-9 ._+-]+$`) and `IOS_LOG_SUBSYSTEM`; `run-ios.sh` and `sim-mjpeg.py` escape `\` then `"` before building the `log stream --predicate`. Default (`app` mode) predicate = the app process minus `com.apple.*` subsystems AND system-framework senders (`senderImagePath` under `/System` or `/usr/lib`) — Xcode-console-like. The pane's **App/All** dropdown hits `/logs/stream?mode=app|all`; `IOS_LOG_MODE`/`IOS_LOG_VERBOSE` set the initial mode; `IOS_LOG_SUBSYSTEM` narrows to one subsystem; `LOG_PREDICATE` overrides everything.
- **Security (S2):** `sim-mjpeg.py` binds `127.0.0.1` only, allowlists `/key` names, and enforces an `Origin` check on every POST (`/tap`,`/swipe`,`/key`,`/run`,`/stop`) — allow a missing `Origin` or any **loopback** host (`localhost`/`127.0.0.1`/`::1`, any port), reject everything else. `IOS_PREVIEW_ALLOW_ORIGIN` (exact origin, or `*`) is the escape hatch when the pane is proxied under a non-loopback origin.
- **Build paths (R2):** `/ios-preview:start` still runs the build via Bash so Claude reacts to errors. The in-pane **Run** button is a user-driven path that runs the *same* `run-ios.sh --no-stream` from inside `sim-mjpeg.py`, streaming build output to `/build/stream` (the pane) and mirroring it to stdout so failures stay visible to the assistant. It spawns `run-ios.sh` with `IOS_ENV_FILE=/dev/null` so the launch.json-baked `IOS_*` (the sim this pane streams) stays authoritative — otherwise a stale `<project>/.claude/ios-preview.env` could re-point the build/launch at a different simulator and the app would launch off-screen.
- **launch.json:** merge-not-clobber by the `ios-preview:` name prefix; never touch the user's own entries; tolerate comments/trailing commas, back up to `.bak` on unparseable.
- **`launch.json` does NOT expand `${CLAUDE_PLUGIN_ROOT}`** — the command must bake absolute script paths (that's why `write-launch-json.py` takes `--plugin-root`).
- **Portability:** `*.sh` target macOS **bash 3.2** (no associative arrays, no `${var,,}`, guard empty-array expansion under `set -u`). `*.py` are **Python 3 stdlib only** (no pip).
- **Publishability:** no `showpad`/`SHOWPAD_`, no machine `/Users/...` paths in shipped files; `xcodebuildmcp` is **version-pinned** (never `@latest`); `sosumi` is `{"type":"http"}`.
- **Releasing:** bump `version` in `.claude-plugin/plugin.json` (and `marketplace.json` metadata) on **every** shipped change. Claude Code's updater only re-pulls when the version string changes — pushing new commits under the same version requires a manual cache sync. (A versionless git plugin would fall back to the commit SHA, auto-pulling every push.)

## Commands
```sh
# Syntax / compile
bash -n scripts/*.sh
python3 -m py_compile scripts/*.py
# Tests (each prints PASS/FAIL, exits non-zero on failure)
bash    tests/test_detect.sh
python3 tests/test_sim_mjpeg.py
python3 tests/test_write_launch_json.py
# Static gate (must be clean before shipping)
grep -rniI showpad . --exclude-dir=.git        # expect none
grep -rnI  SHOWPAD_ . --exclude-dir=.git        # expect none
grep -rnI  "/Users/" . --exclude-dir=.git --include='*.sh' --include='*.py' --include='*.md' --include='*.json'  # expect none
```

## Layout
```
.claude-plugin/{plugin.json, marketplace.json}   # manifest + self-marketplace (source "./")
.mcp.json                                         # bundled MCP servers (pinned)
commands/{start,logs,stop}.md                     # slash commands (disable-model-invocation: true)
scripts/{detect.sh, run-ios.sh, sim-mjpeg.py, write-launch-json.py}
tests/{test_detect.sh, test_sim_mjpeg.py, test_write_launch_json.py}
examples/settings.json · README.md · LICENSE (MIT)
```

## Gotchas
- **Video latency is proxy-bound.** The pane is a proxied MJPEG stream; lag has a floor that `FPS`/`QUALITY`/`SCALE` tuning can't beat (lower `SCALE` = smaller frames = less lag, but the floor remains). Input (tap/swipe) is 1:1. Real fix would be a WebRTC/H.264 transport — not currently worth it.
- **Logs are live-from-connect.** A quiet/idle app shows the "Waiting for logs…" placeholder; navigate the app to populate it. Apps using a custom subsystem or `print`/NSLog may need `IOS_LOG_SUBSYSTEM` (or won't show).
- **Ambiguity → clear error.** Multiple projects/schemes make `detect.sh` exit non-zero with the exact `IOS_PROJECT`/`IOS_SCHEME` override to set.
- **`axe`** comes from the bundled `xcodebuildmcp` (npx cache); only the interactive view needs it — build + logs work without it.
