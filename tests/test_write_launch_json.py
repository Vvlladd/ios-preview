#!/usr/bin/env python3
"""
Tests for write-launch-json.py.

Each case uses a tempfile.TemporaryDirectory and invokes the script via
subprocess.run so the real argparse / path validation / file I/O runs.

8 cases:
  1. no existing file  -> created with 2 ios-preview: entries, runtimeArgs[0] absolute
  2. user entry preserved after merge
  3. stale ios-preview: entry replaced, no duplicate
  4. commented + trailing-comma file -> user entry preserved (R7)
  5. totally unparseable -> .bak created + fresh file written
  6. --env-file OUTSIDE project-dir -> non-zero exit (S6)
  7. idempotent: content identical on second run
  8. IOS_SIM_UDID appears only in entries' env dict, never in runtimeArgs (AC-8)
"""

import json
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "write-launch-json.py")
)
PLUGIN_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

PASS = 0
FAIL = 0


def ok(msg: str):
    print(f"  [ok] {msg}")


def fail(msg: str):
    global FAIL
    print(f"  [FAIL] {msg}")
    FAIL += 1


def pass_case(label: str):
    global PASS
    print(f"PASS: {label}")
    PASS += 1


def fail_case(label: str):
    print(f"FAIL: {label}")


def make_env_file(directory: str, extras: dict = None) -> str:
    """Write a minimal ios-preview.env into directory/.claude/ and return its path."""
    claude_dir = os.path.join(directory, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    env_path = os.path.join(claude_dir, "ios-preview.env")
    env_vars = {
        "IOS_PROJECT_DIR": directory,
        "IOS_PROJECT": os.path.join(directory, "App.xcodeproj"),
        "IOS_PROJECT_KIND": "project",
        "IOS_SCHEME": "App",
        "IOS_PRODUCT_NAME": "App",
        "IOS_BUNDLE_ID": "com.example.app",
        "IOS_FULL_PRODUCT_NAME": "App.app",
        "IOS_DERIVED_DATA": os.path.join(directory, "DerivedData"),
        "IOS_SIM_UDID": "AAAAAAAA-1111-2222-3333-444444444444",
    }
    if extras:
        env_vars.update(extras)
    with open(env_path, "w") as fh:
        for k, v in env_vars.items():
            fh.write(f"{k}={v}\n")
    return env_path


def run_writer(project_dir: str, env_file: str, plugin_root: str = None) -> subprocess.CompletedProcess:
    if plugin_root is None:
        plugin_root = PLUGIN_ROOT
    return subprocess.run(
        [
            sys.executable, SCRIPT,
            "--plugin-root", plugin_root,
            "--project-dir", project_dir,
            "--env-file",    env_file,
        ],
        capture_output=True,
        text=True,
    )


def load_launch(project_dir: str) -> dict:
    path = os.path.join(project_dir, ".claude", "launch.json")
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Case 1: no existing file -> created with 2 ios-preview: entries
# ---------------------------------------------------------------------------
print("\n=== Case 1: no existing file -> created with 2 entries ===")
with tempfile.TemporaryDirectory() as tmpdir:
    env_file = make_env_file(tmpdir)
    result = run_writer(tmpdir, env_file)
    c1_ok = True

    if result.returncode == 0:
        ok("exit code 0")
    else:
        fail(f"non-zero exit: {result.returncode}\nstderr: {result.stderr}")
        c1_ok = False

    launch_path = os.path.join(tmpdir, ".claude", "launch.json")
    if os.path.isfile(launch_path):
        ok("launch.json created")
        data = load_launch(tmpdir)
        configs = data.get("configurations", [])
        ios_entries = [e for e in configs if e.get("name", "").startswith("ios-preview:")]
        if len(ios_entries) == 2:
            ok("exactly 2 ios-preview: entries")
        else:
            fail(f"expected 2 ios-preview: entries, got {len(ios_entries)}")
            c1_ok = False

        # runtimeArgs[0] must be absolute and end with sim-mjpeg.py
        for entry in ios_entries:
            if entry["name"] == "ios-preview: interactive simulator":
                ra = entry.get("runtimeArgs", [])
                if ra and os.path.isabs(ra[0]) and ra[0].endswith("sim-mjpeg.py"):
                    ok(f"interactive runtimeArgs[0] absolute: {ra[0]}")
                else:
                    fail(f"interactive runtimeArgs[0] not absolute or wrong: {ra}")
                    c1_ok = False
    else:
        fail("launch.json not created")
        c1_ok = False

if c1_ok:
    pass_case("1: no existing file -> 2 entries, runtimeArgs[0] absolute")
else:
    fail_case("1: no existing file -> 2 entries, runtimeArgs[0] absolute")

# ---------------------------------------------------------------------------
# Case 2: user entry preserved after merge
# ---------------------------------------------------------------------------
print("\n=== Case 2: user entry preserved ===")
with tempfile.TemporaryDirectory() as tmpdir:
    claude_dir = os.path.join(tmpdir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    user_entry = {"name": "My App Debug", "runtimeExecutable": "lldb", "port": 1234}
    existing = {"version": "0.0.1", "configurations": [user_entry]}
    with open(os.path.join(claude_dir, "launch.json"), "w") as fh:
        json.dump(existing, fh, indent=2)

    env_file = make_env_file(tmpdir)
    result = run_writer(tmpdir, env_file)
    c2_ok = True

    if result.returncode == 0:
        ok("exit code 0")
    else:
        fail(f"non-zero exit: {result.returncode}\nstderr: {result.stderr}")
        c2_ok = False

    data = load_launch(tmpdir)
    configs = data.get("configurations", [])
    preserved = [e for e in configs if e.get("name") == "My App Debug"]
    ios_entries = [e for e in configs if e.get("name", "").startswith("ios-preview:")]

    if len(preserved) == 1:
        ok("user entry 'My App Debug' preserved")
    else:
        fail(f"user entry missing or duplicated: {preserved}")
        c2_ok = False

    if len(ios_entries) == 2:
        ok("2 ios-preview: entries present")
    else:
        fail(f"expected 2 ios-preview: entries, got {len(ios_entries)}")
        c2_ok = False

if c2_ok:
    pass_case("2: user entry preserved")
else:
    fail_case("2: user entry preserved")

# ---------------------------------------------------------------------------
# Case 3: stale ios-preview: entry replaced, no duplicate
# ---------------------------------------------------------------------------
print("\n=== Case 3: stale ios-preview: entry replaced, no duplicate ===")
with tempfile.TemporaryDirectory() as tmpdir:
    claude_dir = os.path.join(tmpdir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    stale = [
        {"name": "ios-preview: interactive simulator", "runtimeExecutable": "python3",
         "runtimeArgs": ["/old/path/sim-mjpeg.py"], "port": 8765, "env": {}},
        {"name": "ios-preview: app logs", "runtimeExecutable": "bash",
         "runtimeArgs": ["/old/path/run-ios.sh", "--logs-only"], "port": 0, "env": {}},
        {"name": "Keep Me", "runtimeExecutable": "node", "port": 9999},
    ]
    existing = {"version": "0.0.1", "configurations": stale}
    with open(os.path.join(claude_dir, "launch.json"), "w") as fh:
        json.dump(existing, fh, indent=2)

    env_file = make_env_file(tmpdir)
    result = run_writer(tmpdir, env_file)
    c3_ok = True

    if result.returncode == 0:
        ok("exit code 0")
    else:
        fail(f"non-zero exit: {result.returncode}\nstderr: {result.stderr}")
        c3_ok = False

    data = load_launch(tmpdir)
    configs = data.get("configurations", [])
    ios_entries = [e for e in configs if e.get("name", "").startswith("ios-preview:")]
    keep_entries = [e for e in configs if e.get("name") == "Keep Me"]

    if len(ios_entries) == 2:
        ok("exactly 2 ios-preview: entries (no duplicates)")
    else:
        fail(f"expected 2 ios-preview: entries, got {len(ios_entries)}: {[e['name'] for e in ios_entries]}")
        c3_ok = False

    # Verify old path was replaced
    for e in ios_entries:
        for ra in e.get("runtimeArgs", []):
            if "/old/path" in ra:
                fail(f"stale runtimeArgs still present: {ra}")
                c3_ok = False
                break
        else:
            continue
        break
    else:
        ok("stale /old/path runtimeArgs replaced")

    if len(keep_entries) == 1:
        ok("'Keep Me' entry preserved")
    else:
        fail(f"'Keep Me' entry lost: {keep_entries}")
        c3_ok = False

if c3_ok:
    pass_case("3: stale ios-preview: replaced, no dup, user entry kept")
else:
    fail_case("3: stale ios-preview: replaced, no dup, user entry kept")

# ---------------------------------------------------------------------------
# Case 4: commented + trailing-comma file -> user entries preserved (R7)
# ---------------------------------------------------------------------------
print("\n=== Case 4: commented + trailing-comma file -> user entries preserved (R7) ===")
COMMENTED_JSON = """{
  // This is a comment
  "version": "0.0.1",
  "configurations": [
    /* block comment */
    {
      "name": "User Entry",
      "runtimeExecutable": "bash",
      "port": 5678,
    },
  ]
}"""

with tempfile.TemporaryDirectory() as tmpdir:
    claude_dir = os.path.join(tmpdir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    with open(os.path.join(claude_dir, "launch.json"), "w") as fh:
        fh.write(COMMENTED_JSON)

    env_file = make_env_file(tmpdir)
    result = run_writer(tmpdir, env_file)
    c4_ok = True

    if result.returncode == 0:
        ok("exit code 0 (lenient parse succeeded)")
    else:
        fail(f"non-zero exit: {result.returncode}\nstderr: {result.stderr}")
        c4_ok = False

    data = load_launch(tmpdir)
    configs = data.get("configurations", [])
    user_entries = [e for e in configs if e.get("name") == "User Entry"]
    ios_entries  = [e for e in configs if e.get("name", "").startswith("ios-preview:")]

    if len(user_entries) == 1:
        ok("'User Entry' preserved after comment/trailing-comma strip")
    else:
        fail(f"'User Entry' not preserved: found {len(user_entries)}")
        c4_ok = False

    if len(ios_entries) == 2:
        ok("2 ios-preview: entries appended")
    else:
        fail(f"expected 2 ios-preview: entries, got {len(ios_entries)}")
        c4_ok = False

    # .bak should NOT exist (lenient parse succeeded; .bak is only for total failure)
    bak_path = os.path.join(claude_dir, "launch.json.bak")
    if not os.path.exists(bak_path):
        ok("no .bak created (lenient parse did not fall through to .bak)")
    else:
        fail(".bak created unexpectedly on a comment-only file")
        c4_ok = False

if c4_ok:
    pass_case("4: commented+trailing-comma file -> user entries preserved")
else:
    fail_case("4: commented+trailing-comma file -> user entries preserved")

# ---------------------------------------------------------------------------
# Case 5: totally unparseable -> .bak created + fresh file written
# ---------------------------------------------------------------------------
print("\n=== Case 5: totally unparseable -> .bak created + fresh written ===")
GARBAGE = "this is not json at all {{{["

with tempfile.TemporaryDirectory() as tmpdir:
    claude_dir = os.path.join(tmpdir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    launch_path = os.path.join(claude_dir, "launch.json")
    with open(launch_path, "w") as fh:
        fh.write(GARBAGE)

    env_file = make_env_file(tmpdir)
    result = run_writer(tmpdir, env_file)
    c5_ok = True

    if result.returncode == 0:
        ok("exit code 0 (recovered from unparseable file)")
    else:
        fail(f"non-zero exit: {result.returncode}\nstderr: {result.stderr}")
        c5_ok = False

    bak_path = launch_path + ".bak"
    if os.path.isfile(bak_path):
        ok(f".bak created: {os.path.basename(bak_path)}")
        with open(bak_path) as fh:
            bak_content = fh.read()
        if bak_content == GARBAGE:
            ok("original garbage content preserved in .bak")
        else:
            fail("bak content does not match original garbage")
            c5_ok = False
    else:
        fail(".bak not created for unparseable file")
        c5_ok = False

    # Fresh file must be valid JSON with 2 ios-preview: entries
    try:
        data = load_launch(tmpdir)
        ios_entries = [e for e in data.get("configurations", []) if e.get("name", "").startswith("ios-preview:")]
        if len(ios_entries) == 2:
            ok("fresh launch.json has 2 ios-preview: entries")
        else:
            fail(f"expected 2 ios-preview: entries in fresh file, got {len(ios_entries)}")
            c5_ok = False
    except Exception as exc:
        fail(f"fresh launch.json not valid JSON: {exc}")
        c5_ok = False

if c5_ok:
    pass_case("5: unparseable -> .bak created + fresh written")
else:
    fail_case("5: unparseable -> .bak created + fresh written")

# ---------------------------------------------------------------------------
# Case 6: --env-file OUTSIDE project-dir -> non-zero exit (S6)
# ---------------------------------------------------------------------------
print("\n=== Case 6: --env-file outside project-dir -> non-zero exit (S6) ===")
with tempfile.TemporaryDirectory() as tmpdir:
    with tempfile.TemporaryDirectory() as other_dir:
        # Create env file in a completely different directory
        outside_env = os.path.join(other_dir, "ios-preview.env")
        with open(outside_env, "w") as fh:
            fh.write("IOS_PRODUCT_NAME=App\n")

        result = run_writer(tmpdir, outside_env)
        c6_ok = True

        if result.returncode != 0:
            ok(f"non-zero exit ({result.returncode}) for env-file outside project-dir")
        else:
            fail("expected non-zero exit for env-file outside project-dir, got 0")
            c6_ok = False

        # launch.json must NOT have been created
        launch_path = os.path.join(tmpdir, ".claude", "launch.json")
        if not os.path.isfile(launch_path):
            ok("launch.json not created (rejected before writing)")
        else:
            fail("launch.json was created despite S6 violation")
            c6_ok = False

if c6_ok:
    pass_case("6: env-file outside project-dir -> non-zero exit (S6)")
else:
    fail_case("6: env-file outside project-dir -> non-zero exit (S6)")

# ---------------------------------------------------------------------------
# Case 7: idempotent — content identical on second run
# ---------------------------------------------------------------------------
print("\n=== Case 7: idempotent (2nd run produces identical file) ===")
with tempfile.TemporaryDirectory() as tmpdir:
    env_file = make_env_file(tmpdir)

    result1 = run_writer(tmpdir, env_file)
    c7_ok = True

    if result1.returncode == 0:
        ok("first run exit 0")
    else:
        fail(f"first run failed: {result1.returncode}")
        c7_ok = False

    launch_path = os.path.join(tmpdir, ".claude", "launch.json")
    with open(launch_path) as fh:
        content_first = fh.read()

    result2 = run_writer(tmpdir, env_file)
    if result2.returncode == 0:
        ok("second run exit 0")
    else:
        fail(f"second run failed: {result2.returncode}")
        c7_ok = False

    with open(launch_path) as fh:
        content_second = fh.read()

    if content_first == content_second:
        ok("file content identical after second run (idempotent)")
    else:
        fail("file content changed on second run (NOT idempotent)")
        # Show diff for debugging
        first_lines  = content_first.splitlines()
        second_lines = content_second.splitlines()
        for i, (a, b) in enumerate(zip(first_lines, second_lines)):
            if a != b:
                fail(f"  diff at line {i+1}: {a!r} -> {b!r}")
        c7_ok = False

if c7_ok:
    pass_case("7: idempotent")
else:
    fail_case("7: idempotent")

# ---------------------------------------------------------------------------
# Case 8: IOS_SIM_UDID appears only in entries' env dict, never in runtimeArgs (AC-8)
# ---------------------------------------------------------------------------
print("\n=== Case 8: IOS_SIM_UDID only in env dict, not in runtimeArgs (AC-8) ===")
UDID = "BBBBBBBB-2222-3333-4444-555555555555"

with tempfile.TemporaryDirectory() as tmpdir:
    env_file = make_env_file(tmpdir, {"IOS_SIM_UDID": UDID})
    result = run_writer(tmpdir, env_file)
    c8_ok = True

    if result.returncode == 0:
        ok("exit code 0")
    else:
        fail(f"non-zero exit: {result.returncode}")
        c8_ok = False

    data = load_launch(tmpdir)
    configs = data.get("configurations", [])

    for entry in configs:
        # UDID must NOT appear in runtimeArgs
        for ra in entry.get("runtimeArgs", []):
            if UDID in ra:
                fail(f"UDID found in runtimeArgs of '{entry.get('name')}': {ra}")
                c8_ok = False

        # UDID MUST appear in env dict for ios-preview: entries that use the sim
        if entry.get("name", "").startswith("ios-preview:"):
            entry_env = entry.get("env", {})
            if "IOS_SIM_UDID" in entry_env and entry_env["IOS_SIM_UDID"] == UDID:
                ok(f"IOS_SIM_UDID correctly in env of '{entry['name']}'")
            else:
                fail(f"IOS_SIM_UDID missing or wrong in env of '{entry['name']}': {entry_env.get('IOS_SIM_UDID')!r}")
                c8_ok = False

    if c8_ok:
        ok("IOS_SIM_UDID not found in any runtimeArgs (AC-8)")

if c8_ok:
    pass_case("8: IOS_SIM_UDID in env dict only, not in runtimeArgs (AC-8)")
else:
    fail_case("8: IOS_SIM_UDID in env dict only, not in runtimeArgs (AC-8)")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n================================")
print(f"Results: {PASS} passed, {FAIL} failed")
print(f"================================")

sys.exit(0 if FAIL == 0 else 1)
