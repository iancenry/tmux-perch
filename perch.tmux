#!/usr/bin/env bash
# perch.tmux — TPM / run-shell entry point.
#
# Registers the prefix bindings (toggle + menu, plus their Ctrl-variants) and
# the per-session garbage-collection hook. Always exits 0 so a config reload
# never surfaces a noisy "returned 1" message.
#
# Implementation resolution: the compiled Go binary (bin/perch) is preferred;
# python3 perch.py is the fallback. Force either with PERCH_IMPL=go|python
# (e.g. `run-shell "PERCH_IMPL=python $HOME/.tmux/plugins/tmux-perch/perch.tmux"`).

set -u

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PERCH_CMD=""
case "${PERCH_IMPL:-}" in
    python) PERCH_CMD="python3 '$CURRENT_DIR/perch.py'" ;;
    go | binary) PERCH_CMD="'$CURRENT_DIR/bin/perch'" ;;
esac

if [ -z "$PERCH_CMD" ]; then
    if [ -x "$CURRENT_DIR/bin/perch" ]; then
        PERCH_CMD="'$CURRENT_DIR/bin/perch'"
    elif command -v python3 >/dev/null 2>&1; then
        PERCH_CMD="python3 '$CURRENT_DIR/perch.py'"
    else
        tmux display-message "perch: neither bin/perch nor python3 is available"
        exit 0
    fi
fi

# tmux >= 3.3 is required (popups). Fail with a message on older versions
# instead of silently breaking on every keypress.
if ! tmux list-commands display-popup >/dev/null 2>&1; then
    tmux display-message "perch: tmux >= 3.3 is required"
    exit 0
fi

toggle_key="$(tmux show-option -gqv @perch-bind)"
if [ -z "$toggle_key" ]; then
    toggle_key="p"
fi

menu_key="$(tmux show-option -gqv @perch-bind-menu)"
if [ -z "$menu_key" ]; then
    menu_key="P"
fi

bind_ctrl="$(tmux show-option -gqv @perch-bind-ctrl)"
if [ -z "$bind_ctrl" ]; then
    bind_ctrl="on"
fi

bind_key() {
    local key="$1" action="$2"
    [ -n "$key" ] || return 0
    tmux bind-key "$key" run-shell "$PERCH_CMD $action '#{client_tty}'"
    # Hold-Ctrl friendliness: pressing "prefix then p" with Ctrl held down
    # delivers Ctrl+P, which tmux-sensible binds to previous-window. Also
    # register the Ctrl-variant of single-character keys so the chord works
    # whether or not Ctrl is released between the two keys.
    if [ "$bind_ctrl" = "on" ]; then
        case "$key" in
            ?) tmux bind-key "C-$key" run-shell "$PERCH_CMD $action '#{client_tty}'" ;;
        esac
    fi
}

bind_key "$toggle_key" toggle
bind_key "$menu_key" menu

# Per-session floats die with their base session: when any session closes,
# run the garbage collector immediately (a no-op unless @perch-per-session
# is on; when it collects a float, the extra pass it triggers finds nothing
# left to do). The indexed slot [99] cannot clobber (or be clobbered by)
# other session-closed hooks, and is replaced in place on config reloads.
#
# -b (background) is essential: a foreground run-shell job makes the server
# wait for it during shutdown, and the server's late cleanup then unlinks
# the socket path. If a new server has already rebound that path (fast
# kill-server + restart), the new server dies when its socket vanishes
# ("server exited unexpectedly"). Backgrounded jobs do not delay shutdown.
tmux set-hook -g 'session-closed[99]' "run-shell -b \"$PERCH_CMD gc 2>/dev/null || true\""

exit 0
