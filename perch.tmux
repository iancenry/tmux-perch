#!/usr/bin/env bash
# perch.tmux — TPM / run-shell entry point.
#
# Registers the two prefix bindings (toggle + menu) and nothing else. Always
# exits 0 so a config reload never surfaces a noisy "returned 1" message.

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

tmux bind-key "$toggle_key" run-shell "python3 '$PERCH' toggle"
tmux bind-key "$menu_key" run-shell "python3 '$PERCH' menu"

exit 0
