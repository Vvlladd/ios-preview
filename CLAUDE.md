# CLAUDE.md — ios-preview

A **public Claude Code plugin** that builds and live-previews any iOS app on the simulator from inside the Claude app: an interactive MJPEG view (click = tap, drag = swipe) with a side-by-side `os.Logger` stream. Goal: **zero per-project setup** — install once, works in any iOS project.

## How it works (runtime flow)
The slash command `/ios-preview:start` orchestrates four bundled pieces:
1. `scripts/detect.sh` — auto-detects project/scheme/bundle/product/sim and writes a `0600` `IOS_*` env file. **Single source of truth** for detection.
2. `scripts/run-ios.sh --no-stream` — builds + installs + launches the app on the resolved sim, then exits (Claude runs this so it can react to build errors).
3. `scripts/write-launch-json.py` — merges two `ios-preview:` entries into the project's `.claude/launch.json` with **absolute** plugin-script paths baked in.
4. `preview_start("ios-preview: interactive simulator")` → runs `scripts/sim-mjpeg.py`, an HTTP server the Claude pane proxies.

`commands/logs.md` = logs-only (no video); `commands/stop.md` = teardown. `.mcp.json` bundles `xcodebuildmcp` (provides the `axe` binary) + `sosumi` (Apple docs).

## The IOS_* env contract
Detection is decoupled from the scripts via env vars (see `detect.sh` header for the full list): `IOS_PROJECT`/`IOS_PROJECT_DIR` (absolute), `IOS_PROJECT_KIND`, `IOS_SCHEME`, `IOS_CONFIG`, `IOS_PRODUCT_NAME`, `IOS_BUNDLE_ID`, `IOS_FULL_PRODUCT_NAME`, `IOS_DERIVED_DATA`, `IOS_SIM_UDID`, optional `IOS_LOG_SUBSYSTEM`/`IOS_LOG_LEVEL`. Stream tuning: `PORT`/`FPS`/`QUALITY`/`SCALE`. Any preset value is honored as an override.

## Invariants — do NOT break these
- **Single-sim:** `detect.sh` resolves exactly ONE `IOS_SIM_UDID`; `run-ios.sh` and `sim-mjpeg.py` both read it and never re-pick. Keeps video + logs + build on the same sim.
- **stdout contract:** `detect.sh` prints ONLY `IOS_*=` lines to stdout; all progress/errors go to stderr (the command parses stdout).
- **Security (S1):** `detect.sh` validates `IOS_PRODUCT_NAME` (`^[A-Za-z0-9 ._+-]+$`) and `IOS_LOG_SUBSYSTEM`; `run-ios.sh` and `sim-mjpeg.py` escape `\` then `"` before building the `log stream --predicate`. Default predicate is the app process minus `com.apple.*` framework subsystems (Xcode-console-like); `IOS_LOG_SUBSYSTEM` narrows to one subsystem; `IOS_LOG_VERBOSE` (truthy) includes framework logs.
- **Security (S2):** `sim-mjpeg.py` binds `127.0.0.1` only, allowlists `/key` names, and enforces an `Origin` check on every POST (`/tap`,`/swipe`,`/key`) — reject non-loopback origins.
- **launch.json:** merge-not-clobber by the `ios-preview:` name prefix; never touch the user's own entries; tolerate comments/trailing commas, back up to `.bak` on unparseable.
- **`launch.json` does NOT expand `${CLAUDE_PLUGIN_ROOT}`** — the command must bake absolute script paths (that's why `write-launch-json.py` takes `--plugin-root`).
- **Portability:** `*.sh` target macOS **bash 3.2** (no associative arrays, no `${var,,}`, guard empty-array expansion under `set -u`). `*.py` are **Python 3 stdlib only** (no pip).
- **Publishability:** no `showpad`/`SHOWPAD_`, no machine `/Users/...` paths in shipped files; `xcodebuildmcp` is **version-pinned** (never `@latest`); `sosumi` is `{"type":"http"}`.

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
