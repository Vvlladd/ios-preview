#!/bin/bash
# test_detect.sh — PATH-shimmed fixtures for detect.sh
#
# Each test case sets up a TMPDIR fixture, installs fake xcodebuild/xcrun shims
# on PATH, then runs detect.sh and asserts on exit code / stdout / filesystem.
#
# Prints PASS/FAIL per case; exits 1 if any case fails.

set -uo pipefail

DETECT_SH="$(cd "$(dirname "$0")/.." && pwd)/scripts/detect.sh"

if [ ! -f "$DETECT_SH" ]; then
    echo "FATAL: detect.sh not found at $DETECT_SH" >&2
    exit 1
fi

PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

FIXTURE=""
SHIM_DIR=""

make_fixture() {
    FIXTURE="$(mktemp -d)"
    SHIM_DIR="$(mktemp -d)"
}

cleanup_fixture() {
    rm -rf "$FIXTURE" "$SHIM_DIR"
    FIXTURE=""
    SHIM_DIR=""
}

# Run detect.sh with shims on PATH, env var set for project dir
# Captures stdout to file; captures exit code to RC
run_detect() {
    STDOUT_FILE="$(mktemp)"
    PATH="$SHIM_DIR:$PATH" IOS_PROJECT_DIR="$FIXTURE" bash "$DETECT_SH" "$@" >"$STDOUT_FILE" 2>/dev/null
    DETECT_RC=$?
    STDOUT="$(cat "$STDOUT_FILE")"
    rm -f "$STDOUT_FILE"
}

assert_exit() {
    local actual="$1"
    local expected="$2"
    local label="${3:-}"
    if [ "$actual" -eq "$expected" ]; then
        echo "  [ok] exit code = $expected"
        return 0
    else
        echo "  [FAIL] exit code: got $actual, expected $expected ($label)"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

assert_stdout_contains() {
    local pattern="$1"
    if echo "$STDOUT" | grep -qF "$pattern"; then
        echo "  [ok] stdout contains: $pattern"
        return 0
    else
        echo "  [FAIL] stdout missing: $pattern"
        echo "  actual stdout: $STDOUT"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

assert_stdout_not_contains() {
    local pattern="$1"
    if echo "$STDOUT" | grep -qF "$pattern"; then
        echo "  [FAIL] stdout should NOT contain: $pattern"
        echo "  actual stdout: $STDOUT"
        FAIL=$((FAIL + 1))
        return 1
    else
        echo "  [ok] stdout does not contain: $pattern"
        return 0
    fi
}

assert_file_mode() {
    local file="$1"
    local expected_mode="$2"
    local actual_mode
    actual_mode="$(stat -f '%A' "$file" 2>/dev/null || stat -c '%a' "$file" 2>/dev/null)"
    if [ "$actual_mode" = "$expected_mode" ]; then
        echo "  [ok] file mode = $expected_mode: $(basename "$file")"
        return 0
    else
        echo "  [FAIL] file mode: got $actual_mode, expected $expected_mode: $file"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

assert_file_contains() {
    local file="$1"
    local pattern="$2"
    if grep -qF "$pattern" "$file"; then
        echo "  [ok] file contains '$pattern': $(basename "$file")"
        return 0
    else
        echo "  [FAIL] file missing '$pattern': $file"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

pass_case() {
    echo "PASS: $1"
    PASS=$((PASS + 1))
}

fail_case() {
    echo "FAIL: $1"
}

# ---------------------------------------------------------------------------
# Case 1 — Empty directory → exit 2 (no project found)
# ---------------------------------------------------------------------------
echo ""
echo "=== Case 1: empty dir → exit 2 ==="
make_fixture

cat > "$SHIM_DIR/xcrun" << 'EOF'
#!/bin/bash
echo '{"devices":{}}'
EOF
chmod +x "$SHIM_DIR/xcrun"

cat > "$SHIM_DIR/xcodebuild" << 'EOF'
#!/bin/bash
exit 1
EOF
chmod +x "$SHIM_DIR/xcodebuild"

run_detect
CASE1_OK=1
assert_exit "$DETECT_RC" 2 "no project" || CASE1_OK=0

if [ "$CASE1_OK" -eq 1 ]; then
    pass_case "1: empty dir → exit 2"
else
    fail_case "1: empty dir → exit 2"
fi
cleanup_fixture

# ---------------------------------------------------------------------------
# Case 2 — Two .xcodeproj files → exit 3 (ambiguous project)
# ---------------------------------------------------------------------------
echo ""
echo "=== Case 2: two .xcodeproj → exit 3 ==="
make_fixture
mkdir -p "$FIXTURE/Alpha.xcodeproj" "$FIXTURE/Beta.xcodeproj"

cat > "$SHIM_DIR/xcrun" << 'EOF'
#!/bin/bash
echo '{"devices":{}}'
EOF
chmod +x "$SHIM_DIR/xcrun"

cat > "$SHIM_DIR/xcodebuild" << 'EOF'
#!/bin/bash
exit 1
EOF
chmod +x "$SHIM_DIR/xcodebuild"

run_detect
CASE2_OK=1
assert_exit "$DETECT_RC" 3 "ambiguous project" || CASE2_OK=0

if [ "$CASE2_OK" -eq 1 ]; then
    pass_case "2: two .xcodeproj → exit 3"
else
    fail_case "2: two .xcodeproj → exit 3"
fi
cleanup_fixture

# ---------------------------------------------------------------------------
# Case 3 — Package.swift only (no xcodeproj/xcworkspace) → exit 8
# ---------------------------------------------------------------------------
echo ""
echo "=== Case 3: SwiftPM-only → exit 8 ==="
make_fixture
touch "$FIXTURE/Package.swift"

cat > "$SHIM_DIR/xcrun" << 'EOF'
#!/bin/bash
echo '{"devices":{}}'
EOF
chmod +x "$SHIM_DIR/xcrun"

cat > "$SHIM_DIR/xcodebuild" << 'EOF'
#!/bin/bash
exit 1
EOF
chmod +x "$SHIM_DIR/xcodebuild"

run_detect
CASE3_OK=1
assert_exit "$DETECT_RC" 8 "SwiftPM-only" || CASE3_OK=0

if [ "$CASE3_OK" -eq 1 ]; then
    pass_case "3: SwiftPM-only → exit 8"
else
    fail_case "3: SwiftPM-only → exit 8"
fi
cleanup_fixture

# ---------------------------------------------------------------------------
# Case 4 — Happy path: one .xcodeproj, single scheme, valid settings, booted sim
#          → exit 0 with absolute IOS_PROJECT_DIR and IOS_SIM_UDID
# ---------------------------------------------------------------------------
echo ""
echo "=== Case 4: happy path → exit 0 with abs IOS_PROJECT_DIR + IOS_SIM_UDID ==="
make_fixture
mkdir -p "$FIXTURE/MyApp.xcodeproj"

# Write shims with the UDID baked in (no interpolation issues)
cat > "$SHIM_DIR/xcrun" << 'EOF'
#!/bin/bash
if [ "${1:-}" = "simctl" ] && [ "${2:-}" = "list" ]; then
    echo '{"devices":{"com.apple.CoreSimulator.SimRuntime.iOS-17-0":[{"udid":"AABBCCDD-1111-2222-3333-444455556666","name":"iPhone 15","state":"Booted","isAvailable":true}]}}'
fi
EOF
chmod +x "$SHIM_DIR/xcrun"

cat > "$SHIM_DIR/xcodebuild" << 'EOF'
#!/bin/bash
case "$1" in
  -list)
    echo '{"project":{"schemes":["MyApp"]}}'
    ;;
  -showBuildSettings)
    echo "    PRODUCT_NAME = MyApp"
    echo "    PRODUCT_BUNDLE_IDENTIFIER = com.example.myapp"
    echo "    FULL_PRODUCT_NAME = MyApp.app"
    echo "    PRODUCT_TYPE = com.apple.product-type.application"
    ;;
esac
EOF
chmod +x "$SHIM_DIR/xcodebuild"

run_detect
CASE4_OK=1
assert_exit "$DETECT_RC" 0 "happy path" || CASE4_OK=0
assert_stdout_contains "IOS_PROJECT_DIR=$FIXTURE" || CASE4_OK=0
assert_stdout_contains "IOS_SIM_UDID=AABBCCDD-1111-2222-3333-444455556666" || CASE4_OK=0
assert_stdout_contains "IOS_PRODUCT_NAME=MyApp" || CASE4_OK=0

# Verify IOS_PROJECT_DIR starts with /
if echo "$STDOUT" | grep -E '^IOS_PROJECT_DIR=/' > /dev/null 2>&1; then
    echo "  [ok] IOS_PROJECT_DIR is absolute"
else
    echo "  [FAIL] IOS_PROJECT_DIR is not absolute"
    CASE4_OK=0
    FAIL=$((FAIL + 1))
fi

if [ "$CASE4_OK" -eq 1 ]; then
    pass_case "4: happy path → exit 0 with abs IOS_PROJECT_DIR + IOS_SIM_UDID"
else
    fail_case "4: happy path → exit 0 with abs IOS_PROJECT_DIR + IOS_SIM_UDID"
fi
cleanup_fixture

# ---------------------------------------------------------------------------
# Case 5 — Pods + test schemes filtered, single app scheme survives → exit 0
# ---------------------------------------------------------------------------
echo ""
echo "=== Case 5: Pods+test schemes filtered → exit 0 ==="
make_fixture
mkdir -p "$FIXTURE/Shop.xcworkspace"

cat > "$SHIM_DIR/xcrun" << 'EOF'
#!/bin/bash
if [ "${1:-}" = "simctl" ] && [ "${2:-}" = "list" ]; then
    echo '{"devices":{"com.apple.CoreSimulator.SimRuntime.iOS-17-0":[{"udid":"BBBBBBBB-2222-3333-4444-555566667777","name":"iPhone 15","state":"Booted","isAvailable":true}]}}'
fi
EOF
chmod +x "$SHIM_DIR/xcrun"

cat > "$SHIM_DIR/xcodebuild" << 'EOF'
#!/bin/bash
case "$1" in
  -list)
    echo '{"workspace":{"schemes":["Pods","ShopTests","ShopUITests","Shop"]}}'
    ;;
  -showBuildSettings)
    echo "    PRODUCT_NAME = Shop"
    echo "    PRODUCT_BUNDLE_IDENTIFIER = com.example.shop"
    echo "    FULL_PRODUCT_NAME = Shop.app"
    echo "    PRODUCT_TYPE = com.apple.product-type.application"
    ;;
esac
EOF
chmod +x "$SHIM_DIR/xcodebuild"

run_detect
CASE5_OK=1
assert_exit "$DETECT_RC" 0 "pods filtered" || CASE5_OK=0
assert_stdout_contains "IOS_SCHEME=Shop" || CASE5_OK=0
assert_stdout_not_contains "IOS_SCHEME=Pods" || CASE5_OK=0

if [ "$CASE5_OK" -eq 1 ]; then
    pass_case "5: Pods/Tests filtered → single app scheme → exit 0"
else
    fail_case "5: Pods/Tests filtered → single app scheme → exit 0"
fi
cleanup_fixture

# ---------------------------------------------------------------------------
# Case 6 — PRODUCT_NAME with quote character → exit 6 (S1 validation)
# ---------------------------------------------------------------------------
echo ""
echo "=== Case 6: product name with quote char → exit 6 (S1) ==="
make_fixture
mkdir -p "$FIXTURE/BadApp.xcodeproj"

cat > "$SHIM_DIR/xcrun" << 'EOF'
#!/bin/bash
if [ "${1:-}" = "simctl" ] && [ "${2:-}" = "list" ]; then
    echo '{"devices":{"r":[{"udid":"CCCCCCCC-3333-4444-5555-666677778888","name":"iPhone 15","state":"Booted","isAvailable":true}]}}'
fi
EOF
chmod +x "$SHIM_DIR/xcrun"

# Write xcodebuild shim with a literal double-quote in PRODUCT_NAME (triggers S1)
# Use printf with octal \042 to embed " without shell quoting issues
{
    echo '#!/bin/bash'
    echo 'case "$1" in'
    echo '  -list)'
    printf "    echo '%s'\n" '{"project":{"schemes":["BadApp"]}}'
    echo '    ;;'
    echo '  -showBuildSettings)'
    printf '    printf "    PRODUCT_NAME = Bad\\042App\\n"\n'
    echo '    echo "    PRODUCT_BUNDLE_IDENTIFIER = com.example.bad"'
    echo '    echo "    FULL_PRODUCT_NAME = BadApp.app"'
    echo '    echo "    PRODUCT_TYPE = com.apple.product-type.application"'
    echo '    ;;'
    echo 'esac'
} > "$SHIM_DIR/xcodebuild"
chmod +x "$SHIM_DIR/xcodebuild"

run_detect
CASE6_OK=1
assert_exit "$DETECT_RC" 6 "S1 validation" || CASE6_OK=0

if [ "$CASE6_OK" -eq 1 ]; then
    pass_case "6: quoted product name → exit 6 (S1)"
else
    fail_case "6: quoted product name → exit 6 (S1)"
fi
cleanup_fixture

# ---------------------------------------------------------------------------
# Case 7 — --write-env creates 0600 file and .gitignore entry
# ---------------------------------------------------------------------------
echo ""
echo "=== Case 7: --write-env → 0600 file + gitignore entry ==="
make_fixture
mkdir -p "$FIXTURE/GoodApp.xcodeproj"
ENV_FILE="$FIXTURE/.claude/ios-preview.env"

cat > "$SHIM_DIR/xcrun" << 'EOF'
#!/bin/bash
if [ "${1:-}" = "simctl" ] && [ "${2:-}" = "list" ]; then
    echo '{"devices":{"com.apple.CoreSimulator.SimRuntime.iOS-17-0":[{"udid":"DDDDDDDD-4444-5555-6666-777788889999","name":"iPhone 15","state":"Booted","isAvailable":true}]}}'
fi
EOF
chmod +x "$SHIM_DIR/xcrun"

cat > "$SHIM_DIR/xcodebuild" << 'EOF'
#!/bin/bash
case "$1" in
  -list)
    echo '{"project":{"schemes":["GoodApp"]}}'
    ;;
  -showBuildSettings)
    echo "    PRODUCT_NAME = GoodApp"
    echo "    PRODUCT_BUNDLE_IDENTIFIER = com.example.good"
    echo "    FULL_PRODUCT_NAME = GoodApp.app"
    echo "    PRODUCT_TYPE = com.apple.product-type.application"
    ;;
esac
EOF
chmod +x "$SHIM_DIR/xcodebuild"

# run_detect but pass --write-env (need to avoid IOS_PROJECT_DIR conflict in function)
STDOUT_FILE="$(mktemp)"
PATH="$SHIM_DIR:$PATH" IOS_PROJECT_DIR="$FIXTURE" bash "$DETECT_SH" --write-env "$ENV_FILE" >"$STDOUT_FILE" 2>/dev/null
DETECT_RC=$?
STDOUT="$(cat "$STDOUT_FILE")"
rm -f "$STDOUT_FILE"

CASE7_OK=1
assert_exit "$DETECT_RC" 0 "write-env" || CASE7_OK=0

if [ -f "$ENV_FILE" ]; then
    assert_file_mode "$ENV_FILE" "600" || CASE7_OK=0
    assert_file_contains "$ENV_FILE" "IOS_SIM_UDID=" || CASE7_OK=0
    assert_file_contains "$ENV_FILE" "IOS_PRODUCT_NAME=GoodApp" || CASE7_OK=0
else
    echo "  [FAIL] env file not created: $ENV_FILE"
    CASE7_OK=0
    FAIL=$((FAIL + 1))
fi

GITIGNORE_FILE="$FIXTURE/.claude/.gitignore"
if [ -f "$GITIGNORE_FILE" ]; then
    assert_file_contains "$GITIGNORE_FILE" "ios-preview.env" || CASE7_OK=0
else
    echo "  [FAIL] .gitignore not created: $GITIGNORE_FILE"
    CASE7_OK=0
    FAIL=$((FAIL + 1))
fi

if [ "$CASE7_OK" -eq 1 ]; then
    pass_case "7: --write-env → 0600 file + gitignore entry"
else
    fail_case "7: --write-env → 0600 file + gitignore entry"
fi
cleanup_fixture

# ---------------------------------------------------------------------------
# Case 8 — No simulator available → exit 7
# ---------------------------------------------------------------------------
echo ""
echo "=== Case 8: no simulator → exit 7 ==="
make_fixture
mkdir -p "$FIXTURE/NoSimApp.xcodeproj"

cat > "$SHIM_DIR/xcrun" << 'EOF'
#!/bin/bash
if [ "${1:-}" = "simctl" ] && [ "${2:-}" = "list" ]; then
    echo '{"devices":{}}'
fi
EOF
chmod +x "$SHIM_DIR/xcrun"

cat > "$SHIM_DIR/xcodebuild" << 'EOF'
#!/bin/bash
case "$1" in
  -list)
    echo '{"project":{"schemes":["NoSimApp"]}}'
    ;;
  -showBuildSettings)
    echo "    PRODUCT_NAME = NoSimApp"
    echo "    PRODUCT_BUNDLE_IDENTIFIER = com.example.nosim"
    echo "    FULL_PRODUCT_NAME = NoSimApp.app"
    echo "    PRODUCT_TYPE = com.apple.product-type.application"
    ;;
esac
EOF
chmod +x "$SHIM_DIR/xcodebuild"

run_detect
CASE8_OK=1
assert_exit "$DETECT_RC" 7 "no sim" || CASE8_OK=0

if [ "$CASE8_OK" -eq 1 ]; then
    pass_case "8: no simulator → exit 7"
else
    fail_case "8: no simulator → exit 7"
fi
cleanup_fixture

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
