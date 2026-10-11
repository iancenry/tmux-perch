#!/usr/bin/env bash
# perch.tmux — TPM / run-shell entry point.
#
# Registers the prefix bindings (toggle + menu, plus their Ctrl-variants) and
# nothing else. Always exits 0 so a config reload never surfaces a noisy
# "returned 1" message.

set -u

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERCH="$CURRENT_DIR/perch.py"

if ! command -v python3 >/dev/null 2>&1; then
    tmux display-message "perch: python3 not found in PATH"
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
    tmux bind-key "$key" run-shell "python3 '$PERCH' $action '#{client_tty}'"
    # Hold-Ctrl friendliness: pressing "prefix then p" with Ctrl held down
    # delivers Ctrl+P, which tmux-sensible binds to previous-window. Also
    # register the Ctrl-variant of single-character keys so the chord works
    # whether or not Ctrl is released between the two keys.
    if [ "$bind_ctrl" = "on" ]; then
        case "$key" in
            ?) tmux bind-key "C-$key" run-shell "python3 '$PERCH' $action '#{client_tty}'" ;;
        esac
    fi
}

bind_key "$toggle_key" toggle
bind_key "$menu_key" menu

# Per-session floats die with their base session: when any session closes,
# run the garbage collector immediately (it is a no-op unless
# @perch-per-session is on, and when a float is collected the extra pass it
# triggers finds nothing to do).
# Per-session floats die with their base session: when any session closes,
# run the garbage collector immediately (it is a no-op unless
# @perch-per-session is on, and when a float is collected the extra pass it
# triggers finds nothing to do).
#
# -b (background) is essential: a foreground run-shell job makes the server
# wait for it during shutdown, and the server's late cleanup then unlinks
# the socket path. If a new server has already rebound that path (fast
# kill-server + restart), the new server dies when its socket vanishes
# ("server exited unexpectedly"). Backgrounded jobs do not delay shutdown.
tmux set-hook -g session-closed "run-shell -b \"python3 '$PERCH' gc 2>/dev/null || true\""

exit 0
