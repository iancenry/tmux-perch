#!/usr/bin/env bash
# perch playground.
#
# Starts a throwaway tmux server (own socket, minimal config) with perch
# loaded, so you can try it out without touching your real tmux setup.
# Run this from a fresh terminal — not from inside your main tmux.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK="${1:-perchplay}"
CONF="$(mktemp)"

cat > "$CONF" <<EOF
set -g detach-on-destroy on
run-shell "$HERE/perch.tmux"
EOF

printf 'Starting the perch playground (prefix: Ctrl-b)...\n\n'

tmux -L "$SOCK" kill-server 2>/dev/null
tmux -L "$SOCK" -f "$CONF" new-session -s play "
printf '\n  perch playground — prefix is Ctrl-b\n\n'
printf '    Ctrl-b p    toggle the float\n'
printf '    Ctrl-b P    menu: size down/up, full screen, reset, close\n\n'
printf '  Detach (Ctrl-b d) or exit to tear it down.\n\n'
exec \"${SHELL:-/bin/zsh}\" -l
"
rc=$?

rm -f "$CONF"
tmux -L "$SOCK" kill-server 2>/dev/null
exit "$rc"
