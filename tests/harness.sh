#!/usr/bin/env bash
# perch end-to-end harness.
#
# Spins a throwaway tmux server on its own socket, drives a real client
# through a pty (utilising script(1)), and asserts on observable state via
# list-clients / list-sessions. Each phase gets a fresh server so phases
# cannot contaminate each other.
#
# Usage: tests/harness.sh [tmux-binary]

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMUX_BIN="${1:-${TMUX_BIN:-tmux}}"
SOCK="perch-harness-$$"
CONF="$(mktemp)"
LOG="$(mktemp)"

pass=0
fail=0
ok()  { printf 'PASS  %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf 'FAIL  %s\n' "$1"; fail=$((fail + 1)); }

cleanup() {
    "$TMUX_BIN" -L "$SOCK" kill-server 2>/dev/null
    rm -f "$CONF" "$LOG"
}
trap cleanup EXIT

command -v script >/dev/null 2>&1 || { echo "script(1) is required"; exit 1; }

cat > "$CONF" <<EOF
set -g prefix C-a
set -g @perch-bind p
run-shell "$HERE/perch.tmux"
EOF

clients()  { "$TMUX_BIN" -L "$SOCK" list-clients -F '#{client_session}' 2>/dev/null; }
sessions() { "$TMUX_BIN" -L "$SOCK" list-sessions -F '#{session_name}' 2>/dev/null; }

start_server() {
    local conf="${1:-$CONF}"
    "$TMUX_BIN" -L "$SOCK" kill-server 2>/dev/null
    # Wait for the old server to fully exit: it unlinks the socket on the way
    # out, and racing a fresh server against that stomps the new socket.
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        "$TMUX_BIN" -L "$SOCK" has-session 2>/dev/null || break
        sleep 0.2
    done
    "$TMUX_BIN" -L "$SOCK" -f "$conf" new-session -d -s main
}

# ── phase 1: toggle open, toggle close, session persists ──────────────────
start_server
( sleep 1; printf '\001p'; sleep 2.5; printf '\001p'; sleep 1.5 ) | \
    script -q /dev/null "$TMUX_BIN" -L "$SOCK" attach -t main >"$LOG" 2>&1 &

sleep 1.8
clients | grep -qx perch && ok "toggle opens the float" || bad "toggle opens the float"

sleep 2.5
clients | grep -qx perch && bad "toggle closes the float" || ok "toggle closes the float"
sessions | grep -qx perch && ok "session persists after close" || bad "session persists after close"

sleep 2

# ── phase 2: menu full-screen must re-open the popup (floax regression) ───
start_server
( sleep 1; printf '\001p'; sleep 2; printf '\001P'; sleep 1.2; printf 'f'; sleep 2.5; printf '\001p'; sleep 1.5 ) | \
    script -q /dev/null "$TMUX_BIN" -L "$SOCK" attach -t main >"$LOG" 2>&1 &

sleep 1.8
clients | grep -qx perch && ok "float open before menu" || bad "float open before menu"

sleep 3.8
clients | grep -qx perch && ok "menu full-screen re-opens the popup" || bad "menu full-screen re-opens the popup"

sleep 3.0
clients | grep -qx perch && bad "float closes after menu reopen" || ok "float closes after menu reopen"

# ── phase 3: held-Ctrl chord (C-a C-p) must toggle too ────────────────────
# Typing "prefix then p" with Ctrl held down delivers Ctrl+P; the shim
# registers that variant, so the float must close on either chord.
start_server
( sleep 1; printf '\001p'; sleep 2.2; printf '\001\020'; sleep 2 ) | \
    script -q /dev/null "$TMUX_BIN" -L "$SOCK" attach -t main >"$LOG" 2>&1 &

sleep 1.8
clients | grep -qx perch && ok "float opens (held-Ctrl phase)" || bad "float opens (held-Ctrl phase)"

sleep 2.8
clients | grep -qx perch && bad "held-Ctrl chord (C-a C-p) toggles the float" || ok "held-Ctrl chord (C-a C-p) toggles the float"

# ── phase 4: per-session floats are independent ───────────────────────────
CONF_PS="$(mktemp)"
{ cat "$CONF"; printf 'set -g @perch-per-session on\n'; } > "$CONF_PS"
start_server "$CONF_PS"
"$TMUX_BIN" -L "$SOCK" new-session -d -s beta -c /tmp
( sleep 1; printf '\001p'; sleep 2.5; printf '\001p'; sleep 2.5; printf '\001p'; sleep 2 ) | \
    script -q /dev/null "$TMUX_BIN" -L "$SOCK" attach -t main >"$LOG" 2>&1 &

sleep 2.5
clients | grep -qx perch-main && ok "per-session: 'main' gets its own float" || bad "per-session: 'main' gets its own float"

sleep 2.3
outer_tty="$("$TMUX_BIN" -L "$SOCK" list-clients -F '#{client_tty} #{client_session}' | awk '$2=="main"{print $1; exit}')"
if [ -n "$outer_tty" ]; then "$TMUX_BIN" -L "$SOCK" switch-client -c "$outer_tty" -t beta; fi

sleep 2.7
clients | grep -qx perch-beta && ok "per-session: 'beta' gets a separate float" || bad "per-session: 'beta' gets a separate float"
sessions | grep -qx 'perch-main' && ok "per-session: 'main' float persists alongside" || bad "per-session: 'main' float persists alongside"

sleep 2

# ── phase 5: orphaned per-session floats are garbage-collected ────────────
start_server "$CONF_PS"
"$TMUX_BIN" -L "$SOCK" new-session -d -s beta -c /tmp
( sleep 2; printf '\001p'; sleep 2; printf '\001p'; sleep 2.5; printf '\001p'; sleep 2 ) | \
    script -q /dev/null "$TMUX_BIN" -L "$SOCK" attach -t main >"$LOG" 2>&1 &

sleep 1.2
outer_tty="$("$TMUX_BIN" -L "$SOCK" list-clients -F '#{client_tty} #{client_session}' | awk '$2=="main"{print $1; exit}')"
if [ -n "$outer_tty" ]; then "$TMUX_BIN" -L "$SOCK" switch-client -c "$outer_tty" -t beta; fi

sleep 2.3
clients | grep -qx perch-beta && ok "gc: beta float is open" || bad "gc: beta float is open"

sleep 2.0
outer_tty="$("$TMUX_BIN" -L "$SOCK" list-clients -F '#{client_tty} #{client_session}' | awk '$2=="beta"{print $1; exit}')"
if [ -n "$outer_tty" ]; then "$TMUX_BIN" -L "$SOCK" switch-client -c "$outer_tty" -t main; fi
"$TMUX_BIN" -L "$SOCK" kill-session -t beta

sleep 3.2
clients | grep -qx perch-main && ok "gc: toggle from 'main' opens its float" || bad "gc: toggle from 'main' opens its float"
sessions | grep -qx 'perch-beta' && bad "gc: orphaned 'perch-beta' was removed" || ok "gc: orphaned 'perch-beta' was removed"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
