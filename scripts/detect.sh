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
        for f in "${WORKSPACES[@]+"${WORKSPACES[@]}"}" "${PROJECTS[@]+"${PROJECTS[@]}"}"; do
            echo "  $f" >&2
        done
        echo "  export IOS_PROJECT=<path>" >&2
        exit 3
    fi
fi

# ---------------------------------------------------------------------------
# Step 4 — Scheme resolution (R6 heuristic filter)
# ---------------------------------------------------------------------------
if [ -n "${IOS_SCHEME:-}" ]; then
    emit "IOS_SCHEME" "$IOS_SCHEME"
elif [ -n "$SCHEME_OVERRIDE" ]; then
    IOS_SCHEME="$SCHEME_OVERRIDE"
    emit "IOS_SCHEME" "$IOS_SCHEME"
else
    echo "detect.sh: Listing schemes for $IOS_PROJECT ..." >&2

    SCHEMES_JSON="$(xcodebuild -list "-${IOS_PROJECT_KIND}" "$IOS_PROJECT" -json 2>/dev/null)" || {
        echo "detect.sh: xcodebuild -list failed for '$IOS_PROJECT'. Set IOS_SCHEME to override." >&2
        exit 6
    }

    ALL_SCHEMES="$(python3 - "$SCHEMES_JSON" <<'PYEOF'
import sys, json
data = json.loads(sys.argv[1])
for key in ("project", "workspace"):
    if key in data and "schemes" in data[key]:
        for s in data[key]["schemes"]:
            print(s)
        break
PYEOF
    )"

    # Heuristic filter: drop exact "Pods" and names ending in Tests/UITests
    FILTERED_SCHEMES=()
    while IFS= read -r scheme; do
        [ -z "$scheme" ] && continue
        [ "$scheme" = "Pods" ] && continue
        case "$scheme" in
            *Tests|*UITests) continue ;;
        esac
        FILTERED_SCHEMES+=("$scheme")
    done <<< "$ALL_SCHEMES"

    FILTERED_COUNT="${#FILTERED_SCHEMES[@]}"

    if [ "$FILTERED_COUNT" -eq 1 ]; then
        IOS_SCHEME="${FILTERED_SCHEMES[0]}"
        emit "IOS_SCHEME" "$IOS_SCHEME"
    elif [ "$FILTERED_COUNT" -eq 0 ]; then
        echo "detect.sh: No app scheme found after filtering. Set IOS_SCHEME to override." >&2
        exit 5
    else
        # Multiple survivors — probe each for PRODUCT_TYPE=application
        APP_SCHEMES=()
        for scheme in "${FILTERED_SCHEMES[@]}"; do
            PRODUCT_TYPE="$(xcodebuild -showBuildSettings "-${IOS_PROJECT_KIND}" "$IOS_PROJECT" -scheme "$scheme" 2>/dev/null \
                | grep -m1 'PRODUCT_TYPE' | awk '{print $NF}' || true)"
            if [ "$PRODUCT_TYPE" = "com.apple.product-type.application" ]; then
                APP_SCHEMES+=("$scheme")
            fi
        done

        APP_COUNT="${#APP_SCHEMES[@]}"

        if [ "$APP_COUNT" -eq 1 ]; then
            IOS_SCHEME="${APP_SCHEMES[0]}"
            emit "IOS_SCHEME" "$IOS_SCHEME"
        elif [ "$APP_COUNT" -eq 0 ]; then
            echo "detect.sh: No app scheme found (PRODUCT_TYPE=com.apple.product-type.application). Set IOS_SCHEME to override." >&2
            exit 5
        else
            echo "detect.sh: Multiple app schemes found. Set IOS_SCHEME to choose one:" >&2
            for s in "${APP_SCHEMES[@]}"; do
                echo "  $s" >&2
            done
            echo "  export IOS_SCHEME=<scheme>" >&2
            exit 4
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Step 5 — Build settings + S1 input validation
# ---------------------------------------------------------------------------
echo "detect.sh: Reading build settings for scheme '$IOS_SCHEME' ..." >&2

BUILD_SETTINGS="$(xcodebuild -showBuildSettings "-${IOS_PROJECT_KIND}" "$IOS_PROJECT" -scheme "$IOS_SCHEME" 2>/dev/null)" || {
    echo "detect.sh: xcodebuild -showBuildSettings failed. Verify IOS_SCHEME='$IOS_SCHEME' builds cleanly." >&2
    exit 6
}

_extract_setting() {
    echo "$BUILD_SETTINGS" | grep -m1 "^ *$1 " | sed 's/.*= *//'
}

if [ -z "${IOS_PRODUCT_NAME:-}" ]; then
    IOS_PRODUCT_NAME="$(_extract_setting PRODUCT_NAME)"
fi
if [ -z "${IOS_BUNDLE_ID:-}" ]; then
    IOS_BUNDLE_ID="$(_extract_setting PRODUCT_BUNDLE_IDENTIFIER)"
fi
if [ -z "${IOS_FULL_PRODUCT_NAME:-}" ]; then
    IOS_FULL_PRODUCT_NAME="$(_extract_setting FULL_PRODUCT_NAME)"
fi

if [ -z "$IOS_PRODUCT_NAME" ]; then
    echo "detect.sh: PRODUCT_NAME missing from build settings. Verify the scheme builds." >&2
    exit 6
fi
if [ -z "$IOS_BUNDLE_ID" ]; then
    echo "detect.sh: PRODUCT_BUNDLE_IDENTIFIER missing from build settings." >&2
    exit 6
fi
if [ -z "$IOS_FULL_PRODUCT_NAME" ]; then
    echo "detect.sh: FULL_PRODUCT_NAME missing from build settings." >&2
    exit 6
fi

# S1: validate product name charset
if ! echo "$IOS_PRODUCT_NAME" | grep -qE '^[A-Za-z0-9 ._+-]+$'; then
    echo "detect.sh: IOS_PRODUCT_NAME='$IOS_PRODUCT_NAME' contains unexpected characters; set IOS_PRODUCT_NAME/IOS_LOG_SUBSYSTEM to override." >&2
    exit 6
fi

# S1: validate subsystem charset if preset
if [ -n "${IOS_LOG_SUBSYSTEM:-}" ]; then
    if ! echo "$IOS_LOG_SUBSYSTEM" | grep -qE '^[A-Za-z0-9._-]+$'; then
        echo "detect.sh: IOS_LOG_SUBSYSTEM='$IOS_LOG_SUBSYSTEM' contains unexpected characters; set IOS_PRODUCT_NAME/IOS_LOG_SUBSYSTEM to override." >&2
        exit 6
    fi
fi

emit "IOS_PRODUCT_NAME" "$IOS_PRODUCT_NAME"
emit "IOS_BUNDLE_ID" "$IOS_BUNDLE_ID"
emit "IOS_FULL_PRODUCT_NAME" "$IOS_FULL_PRODUCT_NAME"

# ---------------------------------------------------------------------------
# Step 6 — DerivedData path
# ---------------------------------------------------------------------------
if [ -z "${IOS_DERIVED_DATA:-}" ]; then
    PROJECT_BASE="$(basename "$IOS_PROJECT")"
    PROJECT_BASE="${PROJECT_BASE%.xcworkspace}"
    PROJECT_BASE="${PROJECT_BASE%.xcodeproj}"

    DERIVED_DATA_BASE="$HOME/Library/Developer/Xcode/DerivedData"
    IOS_DERIVED_DATA=""

    if [ -d "$DERIVED_DATA_BASE" ]; then
        IOS_DERIVED_DATA="$(find "$DERIVED_DATA_BASE" -maxdepth 1 -name "${PROJECT_BASE}-*" -type d \
            2>/dev/null | sort | tail -1 || true)"
    fi

    if [ -z "$IOS_DERIVED_DATA" ]; then
        IOS_DERIVED_DATA="$DERIVED_DATA_BASE/${PROJECT_BASE}-claude-preview"
    fi
fi

emit "IOS_DERIVED_DATA" "$IOS_DERIVED_DATA"

# ---------------------------------------------------------------------------
# Step 7 — Single-UDID simulator resolution
# (CRITICAL: this is the only place a sim is chosen)
# ---------------------------------------------------------------------------
if [ -n "${IOS_SIM_UDID:-}" ]; then
    emit "IOS_SIM_UDID" "$IOS_SIM_UDID"
else
    RESOLVED_UDID=""

    case "$SIM_SELECTOR" in
        auto|booted|"")
            DEVICES_JSON="$(xcrun simctl list devices --json 2>/dev/null)" || DEVICES_JSON="{}"
            RESOLVED_UDID="$(python3 - "$DEVICES_JSON" <<'PYEOF'
import sys, json
data = json.loads(sys.argv[1])
for runtime, sims in data.get("devices", {}).items():
    for sim in sims:
        if sim.get("state") == "Booted":
            print(sim["udid"])
            sys.exit(0)
PYEOF
            )" || true
            ;;
        *)
            # 36-char UDID pattern (8-4-4-4-12)
            if echo "$SIM_SELECTOR" | grep -qE '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'; then
                RESOLVED_UDID="$SIM_SELECTOR"
            else
                # Name match — find first available simulator with that name
                DEVICES_JSON="$(xcrun simctl list devices --json 2>/dev/null)" || DEVICES_JSON="{}"
                RESOLVED_UDID="$(python3 - "$DEVICES_JSON" "$SIM_SELECTOR" <<'PYEOF'
import sys, json
data = json.loads(sys.argv[1])
name = sys.argv[2]
for runtime, sims in data.get("devices", {}).items():
    for sim in sims:
        if sim.get("name") == name and sim.get("isAvailable", False):
            print(sim["udid"])
            sys.exit(0)
PYEOF
                )" || true
            fi
            ;;
    esac

    # If still empty, pick best available iPhone (newest runtime first)
    if [ -z "$RESOLVED_UDID" ]; then
        DEVICES_JSON="$(xcrun simctl list devices --json 2>/dev/null)" || DEVICES_JSON="{}"
        RESOLVED_UDID="$(python3 - "$DEVICES_JSON" <<'PYEOF'
import sys, json
data = json.loads(sys.argv[1])
candidates = []
for runtime_key, sims in data.get("devices", {}).items():
    for sim in sims:
        if not sim.get("isAvailable", False):
            continue
        if "iPhone" not in sim.get("name", ""):
            continue
        candidates.append((runtime_key, sim["udid"], sim.get("name", "")))
if not candidates:
    sys.exit(0)
# Newest runtime first (sort descending by key), then stable by name
candidates.sort(key=lambda x: x[0], reverse=True)
print(candidates[0][1])
PYEOF
        )" || true
    fi

    if [ -z "$RESOLVED_UDID" ]; then
        echo "detect.sh: No simulator found for selector '$SIM_SELECTOR'. Boot a simulator or set IOS_SIM_UDID." >&2
        exit 7
    fi

    IOS_SIM_UDID="$RESOLVED_UDID"
    emit "IOS_SIM_UDID" "$IOS_SIM_UDID"
fi

# ---------------------------------------------------------------------------
# Step 8 — Emit IOS_LOG_SUBSYSTEM only if preset
# ---------------------------------------------------------------------------
if [ -n "${IOS_LOG_SUBSYSTEM:-}" ]; then
    emit "IOS_LOG_SUBSYSTEM" "$IOS_LOG_SUBSYSTEM"
fi

# ---------------------------------------------------------------------------
# Step 9 — Atomically write env file if --write-env was given (R5/S3/S7)
# ---------------------------------------------------------------------------
if [ -n "$WRITE_ENV_PATH" ]; then
    ENV_DIR="$(dirname "$WRITE_ENV_PATH")"

    # Ensure parent .claude directory exists with restricted permissions
    if [ ! -d "$ENV_DIR" ]; then
        mkdir -p "$ENV_DIR"
        chmod 0700 "$ENV_DIR"
    fi

    # Remove any prior file first
    rm -f "$WRITE_ENV_PATH"

    # Write to a temp file in the same directory (atomic mv)
    TEMP_ENV="$(mktemp "${ENV_DIR}/.ios-preview-env-XXXXXX")"
    chmod 0600 "$TEMP_ENV"

    {
        echo "IOS_PROJECT_DIR=$ROOT_DIR"
        echo "IOS_PROJECT=$IOS_PROJECT"
        echo "IOS_PROJECT_KIND=$IOS_PROJECT_KIND"
        echo "IOS_SCHEME=$IOS_SCHEME"
        echo "IOS_PRODUCT_NAME=$IOS_PRODUCT_NAME"
        echo "IOS_BUNDLE_ID=$IOS_BUNDLE_ID"
        echo "IOS_FULL_PRODUCT_NAME=$IOS_FULL_PRODUCT_NAME"
        echo "IOS_DERIVED_DATA=$IOS_DERIVED_DATA"
        echo "IOS_SIM_UDID=$IOS_SIM_UDID"
        if [ -n "${IOS_LOG_SUBSYSTEM:-}" ]; then
            echo "IOS_LOG_SUBSYSTEM=$IOS_LOG_SUBSYSTEM"
        fi
    } > "$TEMP_ENV"

    mv "$TEMP_ENV" "$WRITE_ENV_PATH"
    echo "detect.sh: Wrote env file to $WRITE_ENV_PATH (mode 0600)." >&2

    # Ensure .claude/.gitignore lists ios-preview.env (S7)
    GITIGNORE_PATH="${ENV_DIR}/.gitignore"
    if [ ! -f "$GITIGNORE_PATH" ]; then
        echo "ios-preview.env" > "$GITIGNORE_PATH"
    elif ! grep -qF "ios-preview.env" "$GITIGNORE_PATH"; then
        echo "ios-preview.env" >> "$GITIGNORE_PATH"
    fi
fi
