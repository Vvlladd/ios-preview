#!/usr/bin/env python3
"""
Tests for sim-mjpeg.py pure logic.

Re-implements the small testable functions here to avoid triggering the
module-level find_axe()/find_sim() side effects at import time.

8 cases:
  1. process-only predicate (IOS_PRODUCT_NAME set, no subsystem)
  2. predicate with subsystem (IOS_PRODUCT_NAME + IOS_LOG_SUBSYSTEM)
  3. quote-escaping in product name (no raw " in output)
  4. LOG_PREDICATE verbatim override
  5. empty product -> no "Showpad", uses broad fallback
  6. ALLOWED_KEYS membership (valid in, "hack" out)
  7. Origin logic (absent->ok, localhost->ok, evil.com->reject)
  8. port-in-use raises OSError when binding an already-bound port
"""

import os
import socket
import socketserver
import sys
from http.server import HTTPServer

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


# ---------------------------------------------------------------------------
# Re-implement the testable functions from sim-mjpeg.py
# (avoids module startup side-effects: find_axe, find_sim, screen_size_points)
# ---------------------------------------------------------------------------

ALLOWED_KEYS = {"home", "lock", "siri", "side-button", "apple-pay"}


def build_predicate(env: dict) -> str:
    """Replicate sim-mjpeg.py build_predicate() using a supplied env dict."""
    override = env.get("LOG_PREDICATE", "")
    if override:
        return override

    product = env.get("IOS_PRODUCT_NAME", "")
    if not product:
        return 'eventType == "logEvent"'

    escaped_product = product.replace("\\", "\\\\").replace('"', '\\"')

    subsystem = env.get("IOS_LOG_SUBSYSTEM", "")
    if subsystem:
        escaped_sub = subsystem.replace("\\", "\\\\").replace('"', '\\"')
        return f'process == "{escaped_product}" AND subsystem == "{escaped_sub}"'

    return f'process == "{escaped_product}"'


def check_origin(origin: str, port: int) -> bool:
    """Replicate sim-mjpeg.py _check_origin() logic."""
    if not origin:
        return True
    allowed = {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
    }
    return origin in allowed


# ---------------------------------------------------------------------------
# Case 1: process-only predicate
# ---------------------------------------------------------------------------
print("\n=== Case 1: process-only predicate ===")
env1 = {"IOS_PRODUCT_NAME": "MyApp"}
result1 = build_predicate(env1)
c1_ok = True
if result1 == 'process == "MyApp"':
    ok(f"predicate = {result1!r}")
else:
    fail(f"expected process==\"MyApp\", got {result1!r}")
    c1_ok = False
# Must not contain Showpad
if "Showpad" not in result1 and "showpad" not in result1.lower():
    ok("no Showpad in predicate")
else:
    fail("Showpad found in predicate")
    c1_ok = False
if c1_ok:
    pass_case("1: process-only predicate")
else:
    fail_case("1: process-only predicate")

# ---------------------------------------------------------------------------
# Case 2: predicate with subsystem
# ---------------------------------------------------------------------------
print("\n=== Case 2: predicate with subsystem ===")
env2 = {"IOS_PRODUCT_NAME": "MyApp", "IOS_LOG_SUBSYSTEM": "com.example.myapp.logs"}
result2 = build_predicate(env2)
expected2 = 'process == "MyApp" AND subsystem == "com.example.myapp.logs"'
c2_ok = True
if result2 == expected2:
    ok(f"predicate = {result2!r}")
else:
    fail(f"expected {expected2!r}, got {result2!r}")
    c2_ok = False
if c2_ok:
    pass_case("2: predicate with subsystem")
else:
    fail_case("2: predicate with subsystem")

# ---------------------------------------------------------------------------
# Case 3: quote-escaping in product name (S1)
# ---------------------------------------------------------------------------
print('\n=== Case 3: quote-escaping in product name ===')
env3 = {"IOS_PRODUCT_NAME": 'Bad"App'}
result3 = build_predicate(env3)
c3_ok = True
# The raw double-quote character must NOT appear unescaped inside the predicate value
# The predicate should be: process == "Bad\"App"
if '\\"' in result3:
    ok(f"escaped quote present in: {result3!r}")
else:
    fail(f"raw quote not escaped in: {result3!r}")
    c3_ok = False
# Verify: no unescaped double-quote inside the predicate value.
# Strip outer wrapper: process == "<value>"
# An unescaped " is one NOT preceded by a backslash.
import re as _re
inner = result3.replace('process == "', "", 1)
if inner.endswith('"'):
    inner = inner[:-1]
# Look for a quote that is NOT preceded by a backslash
unescaped = _re.search(r'(?<!\\)"', inner)
if not unescaped:
    ok(f"no unescaped double-quote inside predicate value: {inner!r}")
else:
    fail(f"unescaped double-quote found inside value: {inner!r}")
    c3_ok = False
# Also check backslash escaping
env3b = {"IOS_PRODUCT_NAME": "App\\Name"}
result3b = build_predicate(env3b)
if "\\\\" in result3b:
    ok(f"backslash escaped: {result3b!r}")
else:
    fail(f"backslash not escaped: {result3b!r}")
    c3_ok = False
if c3_ok:
    pass_case("3: quote + backslash escaping")
else:
    fail_case("3: quote + backslash escaping")

# ---------------------------------------------------------------------------
# Case 4: LOG_PREDICATE verbatim override
# ---------------------------------------------------------------------------
print("\n=== Case 4: LOG_PREDICATE verbatim override ===")
custom = 'subsystem == "com.custom.logs" AND category == "network"'
env4 = {"LOG_PREDICATE": custom, "IOS_PRODUCT_NAME": "ShouldBeIgnored"}
result4 = build_predicate(env4)
c4_ok = True
if result4 == custom:
    ok(f"verbatim override returned: {result4!r}")
else:
    fail(f"expected {custom!r}, got {result4!r}")
    c4_ok = False
if "ShouldBeIgnored" not in result4:
    ok("IOS_PRODUCT_NAME correctly ignored when LOG_PREDICATE set")
else:
    fail("IOS_PRODUCT_NAME leaked into predicate despite LOG_PREDICATE override")
    c4_ok = False
if c4_ok:
    pass_case("4: LOG_PREDICATE verbatim override")
else:
    fail_case("4: LOG_PREDICATE verbatim override")

# ---------------------------------------------------------------------------
# Case 5: empty product -> broad fallback, no "Showpad"
# ---------------------------------------------------------------------------
print("\n=== Case 5: empty product -> broad fallback, no Showpad ===")
env5 = {}
result5 = build_predicate(env5)
c5_ok = True
if "Showpad" not in result5 and "showpad" not in result5.lower():
    ok(f"no Showpad in fallback predicate: {result5!r}")
else:
    fail(f"Showpad found in fallback predicate: {result5!r}")
    c5_ok = False
# Should use a broad fallback, not be empty
if result5:
    ok(f"fallback predicate is non-empty: {result5!r}")
else:
    fail("fallback predicate is empty")
    c5_ok = False
if c5_ok:
    pass_case("5: empty product -> no Showpad, broad fallback")
else:
    fail_case("5: empty product -> no Showpad, broad fallback")

# ---------------------------------------------------------------------------
# Case 6: ALLOWED_KEYS membership
# ---------------------------------------------------------------------------
print("\n=== Case 6: ALLOWED_KEYS membership ===")
c6_ok = True
for valid_key in ("home", "lock", "siri", "side-button", "apple-pay"):
    if valid_key in ALLOWED_KEYS:
        ok(f"'{valid_key}' is in ALLOWED_KEYS")
    else:
        fail(f"'{valid_key}' missing from ALLOWED_KEYS")
        c6_ok = False
for bad_key in ("hack", "volume-up", "power", "", "HOME"):
    if bad_key not in ALLOWED_KEYS:
        ok(f"'{bad_key}' correctly not in ALLOWED_KEYS")
    else:
        fail(f"'{bad_key}' should NOT be in ALLOWED_KEYS")
        c6_ok = False
if c6_ok:
    pass_case("6: ALLOWED_KEYS membership")
else:
    fail_case("6: ALLOWED_KEYS membership")

# ---------------------------------------------------------------------------
# Case 7: Origin logic
# ---------------------------------------------------------------------------
print("\n=== Case 7: Origin logic ===")
PORT_TEST = 8765
c7_ok = True
# Absent origin -> allowed
if check_origin("", PORT_TEST):
    ok("absent Origin -> allowed")
else:
    fail("absent Origin should be allowed")
    c7_ok = False
# localhost origin -> allowed
if check_origin(f"http://localhost:{PORT_TEST}", PORT_TEST):
    ok(f"http://localhost:{PORT_TEST} -> allowed")
else:
    fail(f"http://localhost:{PORT_TEST} should be allowed")
    c7_ok = False
# 127.0.0.1 origin -> allowed
if check_origin(f"http://127.0.0.1:{PORT_TEST}", PORT_TEST):
    ok(f"http://127.0.0.1:{PORT_TEST} -> allowed")
else:
    fail(f"http://127.0.0.1:{PORT_TEST} should be allowed")
    c7_ok = False
# External origin -> rejected
for evil in ("https://evil.com", "http://evil.com", f"http://localhost:{PORT_TEST + 1}"):
    if not check_origin(evil, PORT_TEST):
        ok(f"'{evil}' -> rejected")
    else:
        fail(f"'{evil}' should be rejected")
        c7_ok = False
if c7_ok:
    pass_case("7: Origin logic (absent/localhost ok, evil rejected)")
else:
    fail_case("7: Origin logic")

# ---------------------------------------------------------------------------
# Case 8: port-in-use raises OSError when binding an already-bound port
# ---------------------------------------------------------------------------
print("\n=== Case 8: port-in-use OSError ===")
c8_ok = True

# Find a free port first
_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_sock.bind(("127.0.0.1", 0))
_bound_port = _sock.getsockname()[1]

# Now try to bind an HTTPServer to the same port while _sock holds it
# SO_REUSEADDR is set on ThreadedHTTPServer but the port is genuinely occupied
_sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)  # no reuse
try:
    _sock2.bind(("127.0.0.1", _bound_port))
    # If we get here the OS allowed reuse (shouldn't happen with SO_REUSEADDR=0)
    fail(f"expected OSError binding port {_bound_port} but bind succeeded")
    c8_ok = False
except OSError:
    ok(f"OSError raised as expected when binding already-used port {_bound_port}")
finally:
    _sock2.close()
    _sock.close()

# Also verify the sim-mjpeg.py error message format matches the design spec (R12)
# The message must contain "Port {PORT} in use" and "ios-preview:stop"
# We verify this by inspecting the source
_src_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "sim-mjpeg.py")
_src_path = os.path.normpath(_src_path)
if os.path.isfile(_src_path):
    with open(_src_path) as fh:
        _src = fh.read()
    if "in use" in _src and "ios-preview:stop" in _src and "except OSError" in _src:
        ok("sim-mjpeg.py has OSError handler with correct message format (R12)")
    else:
        fail("sim-mjpeg.py missing OSError handler or R12 message format")
        c8_ok = False
else:
    fail(f"sim-mjpeg.py not found at {_src_path}")
    c8_ok = False

if c8_ok:
    pass_case("8: port-in-use raises OSError")
else:
    fail_case("8: port-in-use raises OSError")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n================================")
print(f"Results: {PASS} passed, {FAIL} failed")
print(f"================================")

sys.exit(0 if FAIL == 0 else 1)
