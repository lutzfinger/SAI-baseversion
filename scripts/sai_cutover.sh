#!/bin/sh
# Phase 3F cutover helper.
#
# Builds the merged runtime tree from public + private, verifies the
# manifest, and (optionally) reloads launchd to start running from
# the merged tree. Inverse: --rollback reverts to running from the
# private repo directly.
#
# Usage:
#   scripts/sai_cutover.sh --build           # merge + verify, no launchd change
#   scripts/sai_cutover.sh --switch          # build + flip launchd to merged tree
#   scripts/sai_cutover.sh --rollback        # flip launchd back to private repo
#   scripts/sai_cutover.sh --status          # show what's loaded right now
#
# Override paths via env if you don't follow the conventional layout:
#   SAI_PUBLIC=/path/to/SAI-baseversion
#   SAI_PRIVATE=/path/to/SAI
#   SAI_RUNTIME=/path/to/.sai-runtime

set -u  # unbound vars are errors; not -e — we want explicit exit handling

PUBLIC="${SAI_PUBLIC:-$HOME/sai-public}"
PRIVATE="${SAI_PRIVATE:-$HOME/sai-private}"
RUNTIME="${SAI_RUNTIME:-$HOME/.sai-runtime}"
LAUNCHD_LABEL="com.sai.tag-new-inbox"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
say() { printf '[%s] %s\n' "$(ts)" "$*"; }
fail() { say "FAIL: $*" >&2; exit 1; }

build_runtime() {
    say "merging $PUBLIC + $PRIVATE → $RUNTIME"
    [ -d "$PUBLIC" ] || fail "public path missing: $PUBLIC"
    [ -d "$PRIVATE" ] || fail "private path missing: $PRIVATE"

    SAI_OVERLAY="$PUBLIC/.venv/bin/sai-overlay"
    [ -x "$SAI_OVERLAY" ] || fail "sai-overlay not found at $SAI_OVERLAY (run 'make install' in $PUBLIC)"

    "$SAI_OVERLAY" merge \
        --public "$PUBLIC" \
        --private "$PRIVATE" \
        --out "$RUNTIME" \
        --clean || fail "merge failed"

    say "verifying $RUNTIME"
    "$SAI_OVERLAY" verify --runtime "$RUNTIME" || fail "verify failed (manifest mismatch)"
    say "build ok: $(find "$RUNTIME" -type f | wc -l | tr -d ' ') files, $(du -sh "$RUNTIME" | cut -f1)"
}

unload_launchd() {
    if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
        say "unloading launchd $LAUNCHD_LABEL"
        launchctl unload "$LAUNCHD_PLIST" 2>&1 | grep -v "Input/output error" || true
    else
        say "launchd $LAUNCHD_LABEL already unloaded"
    fi
}

load_launchd() {
    say "loading launchd $LAUNCHD_LABEL"
    launchctl load "$LAUNCHD_PLIST" 2>&1 || fail "load failed"
    sleep 1
    if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
        say "launchd $LAUNCHD_LABEL is loaded"
    else
        fail "launchd $LAUNCHD_LABEL did not load"
    fi
}

status() {
    say "PUBLIC   = $PUBLIC"
    say "PRIVATE  = $PRIVATE"
    say "RUNTIME  = $RUNTIME"
    say "PLIST    = $LAUNCHD_PLIST"
    if [ -d "$RUNTIME" ]; then
        say "runtime exists: $(find "$RUNTIME" -type f | wc -l | tr -d ' ') files"
    else
        say "runtime does not exist yet"
    fi
    if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
        # Read which directory the plist is currently invoking
        say "launchd $LAUNCHD_LABEL is loaded"
        grep -A1 "WorkingDirectory" "$LAUNCHD_PLIST" 2>/dev/null | grep "string>" | sed 's/.*<string>//;s/<\/string>//' | while read -r d; do
            say "  WorkingDirectory: $d"
        done
    else
        say "launchd $LAUNCHD_LABEL is not loaded"
    fi
}

case "${1:-}" in
    --build)
        build_runtime
        ;;
    --switch)
        unload_launchd
        build_runtime
        say "EDIT $LAUNCHD_PLIST and change WorkingDirectory + ProgramArguments"
        say "  to point at $RUNTIME, then re-run: scripts/sai_cutover.sh --reload"
        ;;
    --reload)
        load_launchd
        ;;
    --rollback)
        unload_launchd
        say "EDIT $LAUNCHD_PLIST and change WorkingDirectory + ProgramArguments"
        say "  back to point at $PRIVATE, then re-run: scripts/sai_cutover.sh --reload"
        ;;
    --status|"")
        status
        ;;
    *)
        echo "unknown flag: $1" >&2
        echo "usage: $0 [--build|--switch|--reload|--rollback|--status]" >&2
        exit 2
        ;;
esac
