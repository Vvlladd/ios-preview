---
description: Build & live-preview an iOS app in the simulator (interactive MJPEG + logs). Auto-detects project/scheme/bundle/sim.
argument-hint: "[udid|auto|booted|<sim name>] [scheme]"
allowed-tools:
  - Bash
  - Read
  - mcp__Claude_Preview__preview_start
disable-model-invocation: true
---

Start an interactive iOS simulator preview for the current project. Run each step
with the Bash tool so you can react to failures; do not guess past an error.

## Step 1 — Detect project + resolve one simulator (also writes the env file)
Run (this prints `IOS_*=` lines AND atomically writes a 0600 env file the later
steps read):

  bash "${CLAUDE_PLUGIN_ROOT}/scripts/detect.sh" --write-env "${CLAUDE_PROJECT_DIR:-$PWD}/.claude/ios-preview.env" "${1:-auto}" "${2:-}"

If it exits non-zero, STOP and show the user the stderr verbatim — it names the
exact `IOS_*` override to set (e.g. IOS_PROJECT, IOS_SCHEME). Note the printed
`IOS_PROJECT_DIR` value; use it wherever you'd use the project dir below.

## Step 2 — Build, install, launch (no log streaming, so it returns)
Run with an extended timeout (cold builds exceed 2 min):

  bash "${CLAUDE_PLUGIN_ROOT}/scripts/run-ios.sh" --no-stream

(run-ios.sh sources the env file written in Step 1; it builds + installs +
launches the app on IOS_SIM_UDID and exits.) If the build fails, read the
xcodebuild error, summarize the cause, and stop — do not open the preview on a
failed build.

## Step 3 — Merge launch.json (absolute plugin paths baked in)
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/write-launch-json.py" \
    --plugin-root "${CLAUDE_PLUGIN_ROOT}" \
    --project-dir "<IOS_PROJECT_DIR from Step 1>" \
    --env-file "<IOS_PROJECT_DIR from Step 1>/.claude/ios-preview.env"

Tell the user in one line that you merged `ios-preview:` entries into their
`.claude/launch.json` (give the path) and that their own entries were untouched.

## Step 4 — Open the live pane
Call preview_start with name exactly: ios-preview: interactive simulator
Then tell the user: preview is live (clicks=tap, drags=swipe; logs in the side
panel). The pane has a **Run/Stop button** (Xcode-style): Run rebuilds + installs +
relaunches the app (build output shows in the log panel), Stop terminates it.
If preview_start reports the port is in use, advise rerunning with
PORT=<free> exported, or /ios-preview:stop first.
