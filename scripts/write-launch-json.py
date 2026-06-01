#!/usr/bin/env python3
"""
write-launch-json.py — merge ios-preview entries into <project>/.claude/launch.json.

Bakes absolute plugin-root paths into runtimeArgs so launch.json works without
${CLAUDE_PLUGIN_ROOT} expansion (which preview_start does not support).

Usage:
    python3 write-launch-json.py \\
        --plugin-root  <abs-path-to-plugin>  \\
        --project-dir  <abs-path-to-project> \\
        --env-file     <abs-path-to-env-file>

Security (S6): all three paths are realpath'd; env-file must resolve UNDER
project-dir or the script exits non-zero.

Merge strategy (R3/R7):
  - Read existing launch.json; if it has comments or trailing commas, strip and
    retry; if still unparseable, back it up to .bak and start fresh.
  - Drop any entry whose "name" starts with "ios-preview:".
  - Append exactly two new entries with absolute runtimeArgs paths.
  - Write with 2-space indent + trailing newline. Idempotent.
"""

import argparse
import json
import os
import re
import shutil
import sys


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Merge ios-preview entries into .claude/launch.json.")
    p.add_argument("--plugin-root",  required=True, help="Absolute path to plugin root directory.")
    p.add_argument("--project-dir",  required=True, help="Absolute path to iOS project root directory.")
    p.add_argument("--env-file",     required=True, help="Absolute path to ios-preview.env file.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# S6: path validation
# ---------------------------------------------------------------------------

def validate_paths(plugin_root_raw: str, project_dir_raw: str, env_file_raw: str):
    plugin_root = os.path.realpath(plugin_root_raw)
    project_dir = os.path.realpath(project_dir_raw)
    env_file    = os.path.realpath(env_file_raw)

    if not os.path.isdir(project_dir):
        sys.exit(f"write-launch-json.py: --project-dir '{project_dir}' is not an existing directory.")

    # env_file must resolve under project_dir (prevents path traversal)
    if env_file != project_dir and not env_file.startswith(project_dir + os.sep):
        sys.exit(
            f"write-launch-json.py: --env-file '{env_file}' does not resolve under "
            f"--project-dir '{project_dir}'. Refusing to proceed."
        )

    return plugin_root, project_dir, env_file


# ---------------------------------------------------------------------------
# Env file reader
# ---------------------------------------------------------------------------

def read_env_file(path: str) -> dict:
    """Parse KEY=VALUE lines from the env file into a dict.

    Strips surrounding single or double quotes from values.
    Lines starting with # and blank lines are ignored.
    """
    env = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                # Strip surrounding quotes
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                if key:
                    env[key] = val
    except FileNotFoundError:
        sys.exit(f"write-launch-json.py: env file not found: '{path}'")
    return env


# ---------------------------------------------------------------------------
# Lenient JSON parser (R7)
# ---------------------------------------------------------------------------

def _strip_comments_and_trailing_commas(text: str) -> str:
    """Remove // line-comments, /* */ block-comments, and trailing commas."""
    # Remove /* */ block comments
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    # Remove // line comments (not inside strings — good-enough heuristic)
    text = re.sub(r'//[^\n]*', '', text)
    # Remove trailing commas before ] or }
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def load_launch_json(path: str):
    """Load launch.json with fallback strategies.

    Returns (data, bak_path):
      - data is the parsed dict (may be a fresh skeleton if all parsing failed)
      - bak_path is set if a .bak was created, else None
    """
    fresh = {"version": "0.0.1", "configurations": []}

    if not os.path.exists(path):
        return fresh, None

    with open(path) as fh:
        raw = fh.read()

    # 1. Strict parse
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        pass

    # 2. Lenient parse: strip comments + trailing commas
    try:
        cleaned = _strip_comments_and_trailing_commas(raw)
        return json.loads(cleaned), None
    except json.JSONDecodeError:
        pass

    # 3. Total failure: back up and start fresh
    bak_path = path + ".bak"
    shutil.copy2(path, bak_path)
    print(
        f"write-launch-json.py: WARNING — could not parse '{path}'; "
        f"backed up to '{bak_path}' and starting fresh.",
        file=sys.stderr,
    )
    return fresh, bak_path


# ---------------------------------------------------------------------------
# Entry builders
# ---------------------------------------------------------------------------

def build_interactive_entry(plugin_root: str, env: dict) -> dict:
    """Build the 'ios-preview: interactive simulator' launch.json entry.

    Bakes the FULL IOS_* set (not just UDID + product name) so the in-pane
    Run/Stop button in sim-mjpeg.py can drive run-ios.sh (build/install/launch)
    and `xcrun simctl terminate` (stop). This mirrors build_logs_entry's keys.
    """
    # Full IOS_* set: IOS_BUNDLE_ID is needed to launch/terminate the app and the
    # build vars are needed by run-ios.sh --no-stream invoked from the server.
    ios_keys = [
        "IOS_PROJECT_DIR", "IOS_PROJECT", "IOS_PROJECT_KIND",
        "IOS_SCHEME", "IOS_CONFIG",
        "IOS_PRODUCT_NAME", "IOS_BUNDLE_ID", "IOS_FULL_PRODUCT_NAME",
        "IOS_DERIVED_DATA", "IOS_SIM_UDID",
    ]
    entry_env = {}
    for key in ios_keys:
        if key in env:
            entry_env[key] = env[key]

    # IOS_LOG_LEVEL defaults to "debug" (matches build_logs_entry)
    entry_env["IOS_LOG_LEVEL"] = env.get("IOS_LOG_LEVEL", "debug")

    # Optional — only include when present in env file
    for key in ("IOS_LOG_SUBSYSTEM", "PORT", "FPS", "QUALITY"):
        if key in env:
            entry_env[key] = env[key]

    return {
        "name": "ios-preview: interactive simulator",
        "runtimeExecutable": "python3",
        "runtimeArgs": [os.path.join(plugin_root, "scripts", "sim-mjpeg.py")],
        "port": 8765,
        "env": entry_env,
    }


def build_logs_entry(plugin_root: str, env: dict) -> dict:
    """Build the 'ios-preview: app logs' launch.json entry."""
    # All IOS_* keys from env dict
    ios_keys = [
        "IOS_PROJECT_DIR", "IOS_PROJECT", "IOS_PROJECT_KIND",
        "IOS_SCHEME", "IOS_CONFIG",
        "IOS_PRODUCT_NAME", "IOS_BUNDLE_ID", "IOS_FULL_PRODUCT_NAME",
        "IOS_DERIVED_DATA", "IOS_SIM_UDID",
    ]
    entry_env = {}
    for key in ios_keys:
        if key in env:
            entry_env[key] = env[key]

    # IOS_LOG_LEVEL defaults to "debug"
    entry_env["IOS_LOG_LEVEL"] = env.get("IOS_LOG_LEVEL", "debug")

    # Optional
    if "IOS_LOG_SUBSYSTEM" in env:
        entry_env["IOS_LOG_SUBSYSTEM"] = env["IOS_LOG_SUBSYSTEM"]

    return {
        "name": "ios-preview: app logs",
        "runtimeExecutable": "bash",
        "runtimeArgs": [
            os.path.join(plugin_root, "scripts", "run-ios.sh"),
            "--logs-only",
        ],
        "port": 0,
        "env": entry_env,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    plugin_root, project_dir, env_file = validate_paths(
        args.plugin_root, args.project_dir, args.env_file
    )

    # Read and chmod the env file
    env = read_env_file(env_file)
    try:
        os.chmod(env_file, 0o600)
    except OSError as exc:
        print(f"write-launch-json.py: could not chmod env file: {exc}", file=sys.stderr)

    # Ensure .claude directory exists
    claude_dir = os.path.join(project_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    launch_path = os.path.join(claude_dir, "launch.json")

    # Load existing (with fallback strategies)
    data, bak_path = load_launch_json(launch_path)

    # Ensure the structure is sane
    if not isinstance(data, dict):
        data = {"version": "0.0.1", "configurations": []}
    if "configurations" not in data or not isinstance(data["configurations"], list):
        data["configurations"] = []
    if "version" not in data:
        data["version"] = "0.0.1"

    # Merge-not-clobber: drop stale ios-preview: entries, keep everything else
    preserved = [
        entry for entry in data["configurations"]
        if not (isinstance(entry, dict) and
                isinstance(entry.get("name", ""), str) and
                entry["name"].startswith("ios-preview:"))
    ]

    # Append our two entries
    preserved.append(build_interactive_entry(plugin_root, env))
    preserved.append(build_logs_entry(plugin_root, env))

    data["configurations"] = preserved

    # Write with 2-space indent + trailing newline
    output = json.dumps(data, indent=2) + "\n"
    with open(launch_path, "w") as fh:
        fh.write(output)

    print(
        f"write-launch-json.py: merged ios-preview: entries into '{launch_path}' "
        f"(existing entries untouched)."
    )


if __name__ == "__main__":
    main()
