---
description: Stream the running iOS app's os.Logger output in Claude (logs only, no video).
argument-hint: "[udid|auto|booted|<sim name>] [scheme]"
allowed-tools:
  - Bash
  - Read
  - mcp__Claude_Preview__preview_start
disable-model-invocation: true
---

Attach to the os.Logger stream of an already-running iOS app. No build step —
the app must already be running on the simulator. Run each step with the Bash
tool so you can react to failures; do not guess past an error.

## Step 1 — Detect project + resolve one simulator (also writes the env file)
Run (this prints `IOS_*=` lines AND atomically writes a 0600 env file the later
steps read):

  bash "${CLAUDE_PLUGIN_ROOT}/scripts/detect.sh" --write-env "${CLAUDE_PROJECT_DIR:-$PWD}/.claude/ios-preview.env" "${1:-auto}" "${2:-}"

If it exits non-zero, STOP and show the user the stderr verbatim — it names the
exact `IOS_*` override to set (e.g. IOS_PROJECT, IOS_SCHEME). Note the printed
`IOS_PROJECT_DIR` value; use it wherever you'd use the project dir below.

## Step 2 — Merge launch.json (absolute plugin paths baked in)
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/write-launch-json.py" \
    --plugin-root "${CLAUDE_PLUGIN_ROOT}" \
    --project-dir "<IOS_PROJECT_DIR from Step 1>" \
    --env-file "<IOS_PROJECT_DIR from Step 1>/.claude/ios-preview.env"

Tell the user in one line that you merged `ios-preview:` entries into their
`.claude/launch.json` (give the path) and that their own entries were untouched.

## Step 3 — Open the log pane
Call preview_start with name exactly: ios-preview: app logs
Then tell the user: log stream is live (os.Logger output from the running app;
no video feed in this mode — use /ios-preview:start for the interactive preview).
If the app is not already running, the log stream will be silent until it launches.
