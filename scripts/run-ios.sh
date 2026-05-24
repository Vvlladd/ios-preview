#!/bin/bash
# run-ios.sh — build, install, launch, and stream logs for any iOS app on the simulator.
#
# Reads all project configuration from IOS_* environment variables (written by detect.sh).
# Source the env file first, or set IOS_* directly; detect.sh is called automatically
# if any required variable is missing.
#
# Usage:
#   run-ios.sh [--no-stream] [--no-build] [--logs-only]
#
# Modes:
#   (default)     build + install + launch + stream logs (blocks)
#   --no-stream   build + install + launch then exit 0 (no log stream; used by /ios-preview:start)
#   --no-build    skip build; install + launch + stream logs
#   --logs-only   skip build/install/launch; attach to running app's log stream
#
# Required IOS_* variables (sourced from env file or detect.sh):
#   IOS_PROJECT_DIR        absolute project root
#   IOS_PROJECT            absolute path to .xcworkspace or .xcodeproj
#   IOS_PROJECT_KIND       workspace | project
#   IOS_SCHEME             Xcode scheme
#   IOS_PRODUCT_NAME       product/process name (validated)
#   IOS_BUNDLE_ID          bundle identifier
#   IOS_FULL_PRODUCT_NAME  .app bundle name
#   IOS_DERIVED_DATA       DerivedData path
#   IOS_SIM_UDID           simulator UDID (resolved once by detect.sh)
#
# Optional IOS_* variables:
#   IOS_CONFIG             build configuration (default: Debug)
#   IOS_LOG_SUBSYSTEM      NSPredicate subsystem filter (default: process-only)
#   IOS_LOG_LEVEL          log stream --level (default: debug)
#
# Override the full predicate with LOG_PREDICATE to bypass IOS_* predicate building.

set -euo pipefail

# ---------------------------------------------------------------------------
# Mode parsing
# ---------------------------------------------------------------------------
MODE="build"
for _arg in "$@"; do
    case "$_arg" in
        --no-stream)  MODE="no-stream" ;;
        --no-build)   MODE="no-build" ;;
        --logs-only)  MODE="logs-only" ;;
    esac
done

# ---------------------------------------------------------------------------
# Source env file + optional detect.sh fallback
# ---------------------------------------------------------------------------
_REQUIRED_VARS="IOS_PROJECT_DIR IOS_PROJECT IOS_PROJECT_KIND IOS_SCHEME IOS_PRODUCT_NAME IOS_BUNDLE_ID IOS_FULL_PRODUCT_NAME IOS_DERIVED_DATA IOS_SIM_UDID"

# Attempt to source the env file
ENV_FILE="${IOS_ENV_FILE:-${IOS_PROJECT_DIR:-$PWD}/.claude/ios-preview.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set +u; source "$ENV_FILE"; set -u
fi

# Check if all required vars are set; if not, run detect.sh
_all_set=1
for _v in $_REQUIRED_VARS; do
    eval "_val=\"\${${_v}:-}\""
    if [ -z "$_val" ]; then
        _all_set=0
        break
    fi
done

if [ "$_all_set" -eq 0 ]; then
    DETECT_SH="$(cd "$(dirname "$0")" && pwd)/detect.sh"
    if [ ! -f "$DETECT_SH" ]; then
        echo "run-ios.sh: detect.sh not found at $DETECT_SH and required IOS_* vars are not set." >&2
        exit 1
    fi
    echo "run-ios.sh: sourcing detect.sh to resolve missing IOS_* variables..." >&2
    eval "$(bash "$DETECT_SH" 2>/dev/null)" || {
        echo "run-ios.sh: detect.sh failed. Set IOS_* variables or fix the project." >&2
        exit 1
    }
fi

# ---------------------------------------------------------------------------
# Apply defaults for optional variables
# ---------------------------------------------------------------------------
IOS_CONFIG="${IOS_CONFIG:-Debug}"
IOS_LOG_LEVEL="${IOS_LOG_LEVEL:-${LOG_LEVEL:-debug}}"

# ---------------------------------------------------------------------------
# Move to project root (absolute path from IOS_PROJECT_DIR)
# ---------------------------------------------------------------------------
cd "$IOS_PROJECT_DIR"

# ---------------------------------------------------------------------------
# Simulator — use IOS_SIM_UDID directly (resolved once by detect.sh)
# ---------------------------------------------------------------------------
SIM="$IOS_SIM_UDID"

echo "run-ios.sh: sim=$SIM" >&2

# ---------------------------------------------------------------------------
# Boot simulator (actual boot happens here; detect.sh only picks the UDID)
# ---------------------------------------------------------------------------
echo "run-ios.sh: booting simulator $SIM" >&2
xcrun simctl boot "$SIM" 2>/dev/null || true
open -a Simulator 2>/dev/null || true

# ---------------------------------------------------------------------------
# Build flag for xcodebuild (-workspace or -project)
# ---------------------------------------------------------------------------
case "$IOS_PROJECT_KIND" in
    workspace) PROJECT_FLAG="-workspace" ;;
    project)   PROJECT_FLAG="-project" ;;
    *)
        echo "run-ios.sh: unknown IOS_PROJECT_KIND='$IOS_PROJECT_KIND' (expected workspace or project)." >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Build + install
# ---------------------------------------------------------------------------
if [ "$MODE" = "logs-only" ]; then
    echo "run-ios.sh: --logs-only, skipping build/install/launch" >&2
elif [ "$MODE" = "no-build" ]; then
    # Skip build; fall through to install below
    echo "run-ios.sh: --no-build, skipping build step" >&2
else
    # Full build (default mode and no-stream mode)
    echo "run-ios.sh: building $IOS_SCHEME ($IOS_CONFIG) for simulator" >&2
    xcodebuild \
        "$PROJECT_FLAG" "$IOS_PROJECT" \
        -scheme "$IOS_SCHEME" \
        -configuration "$IOS_CONFIG" \
        -skipMacroValidation \
        -destination "platform=iOS Simulator,id=$SIM" \
        -derivedDataPath "$IOS_DERIVED_DATA" \
        build

    APP="$IOS_DERIVED_DATA/Build/Products/${IOS_CONFIG}-iphonesimulator/$IOS_FULL_PRODUCT_NAME"
    if [ ! -d "$APP" ]; then
        echo "run-ios.sh: app not found at $APP" >&2
        exit 1
    fi
    if [ ! -f "$APP/Info.plist" ]; then
        echo "run-ios.sh: built .app is missing Info.plist — incomplete build." >&2
        exit 1
    fi

    echo "run-ios.sh: installing $IOS_BUNDLE_ID" >&2
    xcrun simctl install "$SIM" "$APP"
fi

# Install path for --no-build
if [ "$MODE" = "no-build" ]; then
    APP="$IOS_DERIVED_DATA/Build/Products/${IOS_CONFIG}-iphonesimulator/$IOS_FULL_PRODUCT_NAME"
    if [ ! -d "$APP" ]; then
        echo "run-ios.sh: app not found at $APP (did you build yet?)" >&2
        exit 1
    fi
    echo "run-ios.sh: installing $IOS_BUNDLE_ID" >&2
    xcrun simctl install "$SIM" "$APP"
fi

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
if [ "$MODE" != "logs-only" ]; then
    echo "run-ios.sh: launching $IOS_BUNDLE_ID" >&2
    xcrun simctl launch "$SIM" "$IOS_BUNDLE_ID"
fi

# ---------------------------------------------------------------------------
# --no-stream: exit here (used by /ios-preview:start Step 2)
# ---------------------------------------------------------------------------
if [ "$MODE" = "no-stream" ]; then
    echo "run-ios.sh: app launched. (--no-stream: exiting without log stream)" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# Build log predicate (S1: escape \ and " in product name)
# ---------------------------------------------------------------------------
if [ -z "${LOG_PREDICATE:-}" ]; then
    # Escape backslash first, then double-quote (S1)
    _escaped_product="$(echo "$IOS_PRODUCT_NAME" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    LOG_PREDICATE="process == \"${_escaped_product}\""

    if [ -n "${IOS_LOG_SUBSYSTEM:-}" ]; then
        _escaped_sub="$(echo "$IOS_LOG_SUBSYSTEM" | sed 's/\\/\\\\/g; s/"/\\"/g')"
        LOG_PREDICATE="${LOG_PREDICATE} AND subsystem == \"${_escaped_sub}\""
    else
        # Xcode-console-like default: drop the com.apple.* framework firehose
        # (network/boringssl, CFNetwork, securityd, xpc, ...). Set IOS_LOG_VERBOSE
        # to a truthy value to include those framework logs.
        case "${IOS_LOG_VERBOSE:-}" in
            1|true|TRUE|yes|YES) : ;;
            *) LOG_PREDICATE="${LOG_PREDICATE} AND NOT (subsystem BEGINSWITH \"com.apple\")" ;;
        esac
    fi
fi

# ---------------------------------------------------------------------------
# Stream logs (blocks until killed)
# ---------------------------------------------------------------------------
echo "run-ios.sh: streaming logs (predicate: $LOG_PREDICATE)" >&2
exec xcrun simctl spawn "$SIM" log stream \
    --style=compact \
    --level="$IOS_LOG_LEVEL" \
    --predicate "$LOG_PREDICATE"
