---
description: Stop the running iOS simulator preview and log streams.
allowed-tools:
  - mcp__Claude_Preview__preview_stop
disable-model-invocation: true
---

Stop all active ios-preview panes.

Call preview_stop for each of the following names (call both regardless of which
is active — stopping a pane that is not running is a no-op):

  - ios-preview: interactive simulator
  - ios-preview: app logs

Report to the user which panes were stopped and which were already inactive.

Note to builder: confirm the exact `preview_stop` tool name and signature off
`/mcp` before shipping. If `preview_stop` stops all preview panes by default
(i.e. takes no name argument), document that behavior here and call it once
instead of twice. The tool name shown above (`mcp__Claude_Preview__preview_stop`)
matches the pattern confirmed for `preview_start`; verify it is correct.
