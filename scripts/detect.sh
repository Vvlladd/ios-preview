#!/bin/bash
# detect.sh — single source of truth for iOS project detection.
#
# Usage:
#   detect.sh [--write-env <path>] [<sim-selector>] [<scheme-override>]
#
# sim-selector: auto | booted | <36-char-UDID> | <simulator name> (default: auto)
# scheme-override: exact scheme name (overrides detection)
#
# Stdout: IOS_*=<value> lines only — nothing else.
# Stderr: all progress messages, warnings, and errors.
#
# Exit codes:
#   0  success
#   2  no .xcworkspace/.xcodeproj found
#   3  ambiguous project (>1 found); set IOS_PROJECT to override
#   4  ambiguous scheme (>1 app scheme); set IOS_SCHEME to override
#   5  no app scheme found
#   6  build-settings unreadable OR product/subsystem contains unexpected characters
#   7  no simulator found
#   8  SwiftPM-only project (Package.swift, no Xcode project)
#
# Emitted IOS_* variables:
#   IOS_PROJECT_DIR        absolute path to project root directory
#   IOS_PROJECT_KIND       workspace | project
#   IOS_PROJECT            absolute path to .xcworkspace or .xcodeproj
#   IOS_SCHEME             build scheme
#   IOS_PRODUCT_NAME       product name (validated: ^[A-Za-z0-9 ._+-]+$)
#   IOS_BUNDLE_ID          bundle identifier
#   IOS_FULL_PRODUCT_NAME  .app bundle name
#   IOS_DERIVED_DATA       DerivedData path for this project
#   IOS_SIM_UDID           single resolved simulator UDID
#   IOS_LOG_SUBSYSTEM      (only emitted if preset before invocation)
#
# Honors any pre-set IOS_* as an override — never re-detects what is already set.
# Deps: xcodebuild, xcrun simctl, python3 (bash 3.2-compatible)

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
WRITE_ENV_PATH=""
SIM_SELECTOR="auto"
SCHEME_OVERRIDE=""

_args=("$@")
_i=0
_pos=()
while [ "$_i" -lt "${#_args[@]}" ]; do
    _arg="${_args[$_i]}"
    if [ "$_arg" = "--write-env" ]; then
        _i=$((_i + 1))
        WRITE_ENV_PATH="${_args[$_i]:?'--write-env requires a path argument'}"
    else
        _pos+=("$_arg")
    fi
    _i=$((_i + 1))
done

if [ "${#_pos[@]}" -ge 1 ]; then SIM_SELECTOR="${_pos[0]}"; fi
if [ "${#_pos[@]}" -ge 2 ]; then SCHEME_OVERRIDE="${_pos[1]}"; fi

# ---------------------------------------------------------------------------
# Helper: emit a single IOS_*=value line to stdout
# ---------------------------------------------------------------------------
emit() {
    printf '%s=%s\n' "$1" "$2"
}

# ---------------------------------------------------------------------------
# Step 1 — Resolve project root directory
# ---------------------------------------------------------------------------
if [ -n "${IOS_PROJECT_DIR:-}" ]; then
    ROOT_DIR="$(cd "$IOS_PROJECT_DIR" && pwd)"
else
    ROOT_DIR="$(cd "$PWD" && pwd)"
fi
emit "IOS_PROJECT_DIR" "$ROOT_DIR"

# ---------------------------------------------------------------------------
# Step 2 — SwiftPM guard
# ---------------------------------------------------------------------------
XCWORKSPACE_COUNT=0
XCODEPROJ_COUNT=0
for f in "$ROOT_DIR"/*.xcworkspace; do
    [ -e "$f" ] && XCWORKSPACE_COUNT=$((XCWORKSPACE_COUNT + 1))
done
for f in "$ROOT_DIR"/*.xcodeproj; do
    [ -e "$f" ] && XCODEPROJ_COUNT=$((XCODEPROJ_COUNT + 1))
done

if [ -f "$ROOT_DIR/Package.swift" ] && [ "$XCWORKSPACE_COUNT" -eq 0 ] && [ "$XCODEPROJ_COUNT" -eq 0 ]; then
    echo "detect.sh: SwiftPM-only project detected (Package.swift found, no .xcworkspace/.xcodeproj). Open in Xcode once to generate the project." >&2
    exit 8
fi

# ---------------------------------------------------------------------------
# Step 3 — Resolve Xcode project / workspace
# ---------------------------------------------------------------------------
if [ -n "${IOS_PROJECT:-}" ]; then
    # Preset wins — resolve to absolute path using parent dir
    IOS_PROJECT="$(cd "$(dirname "$IOS_PROJECT")" && pwd)/$(basename "$IOS_PROJECT")"
    case "$IOS_PROJECT" in
        *.xcworkspace) IOS_PROJECT_KIND="workspace" ;;
        *.xcodeproj)   IOS_PROJECT_KIND="project" ;;
        *)
            echo "detect.sh: IOS_PROJECT='$IOS_PROJECT' has unrecognized extension (expected .xcworkspace or .xcodeproj)." >&2
            exit 2
            ;;
    esac
    emit "IOS_PROJECT" "$IOS_PROJECT"
    emit "IOS_PROJECT_KIND" "$IOS_PROJECT_KIND"
else
    # Collect workspaces — prefer them (CocoaPods/SPM integration)
    WORKSPACES=()
    for f in "$ROOT_DIR"/*.xcworkspace; do
        [ -e "$f" ] && WORKSPACES+=("$f")
    done

    PROJECTS=()
    for f in "$ROOT_DIR"/*.xcodeproj; do
        [ -e "$f" ] && PROJECTS+=("$f")
    done

    TOTAL_WS="${#WORKSPACES[@]}"
    TOTAL_PROJ="${#PROJECTS[@]}"

    if [ "$TOTAL_WS" -eq 1 ]; then
        # .xcworkspace is a directory bundle — use parent dir + basename
        _ws="${WORKSPACES[0]}"
        IOS_PROJECT="$(cd "$(dirname "$_ws")" && pwd)/$(basename "$_ws")"
        IOS_PROJECT_KIND="workspace"
        emit "IOS_PROJECT" "$IOS_PROJECT"
        emit "IOS_PROJECT_KIND" "$IOS_PROJECT_KIND"
    elif [ "$TOTAL_WS" -eq 0 ] && [ "$TOTAL_PROJ" -eq 1 ]; then
        _proj="${PROJECTS[0]}"
        IOS_PROJECT="$(cd "$(dirname "$_proj")" && pwd)/$(basename "$_proj")"
        IOS_PROJECT_KIND="project"
        emit "IOS_PROJECT" "$IOS_PROJECT"
        emit "IOS_PROJECT_KIND" "$IOS_PROJECT_KIND"
    elif [ "$((TOTAL_WS + TOTAL_PROJ))" -eq 0 ]; then
        echo "detect.sh: No .xcworkspace or .xcodeproj found in '$ROOT_DIR'. Set IOS_PROJECT to the project path." >&2
        exit 2
    else
        echo "detect.sh: Multiple Xcode projects found in '$ROOT_DIR'. Set IOS_PROJECT to choose one:" >&2
        for f in "${WORKSPACES[@]}" "${PROJECTS[@]}"; do
            echo "  $f" >&2
        done
        echo "  export IOS_PROJECT=<path>" >&2
        exit 3
    fi
fi

