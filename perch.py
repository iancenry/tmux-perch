#!/usr/bin/env python3
"""perch — a floating scratch terminal for tmux.

One key toggles a popup attached to a persistent scratch session. The popup
is only a view; closing it leaves the session (and anything running in it)
untouched.

Design notes, each paid for in production incidents by predecessor plugins
(tmux-floax, tmux-floatpane — see README):

* Single file, subcommand dispatch. There is no shared shell function
  namespace, so the bug class where one script shadowed another's functions
  (floax's embed.sh `pop()` recursion) cannot happen here.
* Runtime state lives in tmux user options (@perch-*), never in the global
  environment.
* The attach command run inside the popup strips TMUX and passes the socket
  explicitly (`env -u TMUX tmux -S <socket> attach`). On tmux master
  (next-3.9) popups are floating panes and a plain `tmux attach` inside one
  trips the nested-session guard; this keeps both old and new tmux working.
* Resizing detaches the popup and re-opens it on a *captured* client tty
  (`popup -c <tty>`), because after `detach-client` the popup's own client
  is gone and a blind re-open silently fails (upstream floax PR #70's bug).
* Path-following only types `cd` when the scratch pane's foreground is a
  shell; it never injects keystrokes into a running TUI.

Requires python3 and tmux >= 3.3.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import traceback

SELF = os.path.realpath(__file__)

SHELLS = ("zsh", "bash", "sh", "fish", "nu")


# ── tmux plumbing ──────────────────────────────────────────────────────────

def tmux_run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def tmux(*args: str) -> str:
    return tmux_run(*args).stdout.strip()


def get_opt(name: str, default: str) -> str:
    value = tmux("show-option", "-gqv", name)
    return value if value else default


def set_opt(name: str, value: str) -> None:
    tmux_run("set-option", "-gq", name, value)


def unset_opt(name: str) -> None:
    tmux_run("set-option", "-guq", name)


SESSION = get_opt("@perch-session", "perch")
WIDTH = get_opt("@perch-width", "80%")
HEIGHT = get_opt("@perch-height", "80%")
BORDER = get_opt("@perch-border-color", "magenta")
TEXT = get_opt("@perch-text-color", "default")
FOLLOW = get_opt("@perch-change-path", "on")
HOTKEYS = get_opt("@perch-hotkeys", "off")

HOTKEY_ACTIONS = {
    "C-M-s": ("zoom", "in"),
    "C-M-b": ("zoom", "out"),
    "C-M-f": ("zoom", "full"),
    "C-M-r": ("zoom", "reset"),
}


def script_cmd(*args: str) -> str:
    """A shell command that re-invokes this script with the given arguments."""
    return "python3 " + " ".join(shlex.quote(a) for a in (SELF, *args))


def notify(message: str) -> None:
    tmux_run("display-message", f"perch: {message}")


def current_session() -> str:
    return tmux("display", "-p", "#{session_name}")


def session_exists() -> bool:
    return tmux_run("has-session", "-t", SESSION).returncode == 0


def outer_client() -> str | None:
    """The attached client that is NOT showing the perch session."""
    listing = tmux("list-clients", "-F", "#{client_tty} #{client_session}")
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] != SESSION:
            return parts[0]
    return None


# ── session lifecycle ──────────────────────────────────────────────────────

def ensure_session(path: str) -> None:
    if not session_exists():
        tmux_run("new-session", "-d", "-s", SESSION, "-c",
                 path or os.path.expanduser("~"))
    tmux_run("set-option", "-t", SESSION, "status", "off")
    # If the scratch session is destroyed (its shell exited), detach instead
    # of letting the client jump into another session.
    tmux_run("set-option", "-t", SESSION, "detach-on-destroy", "on")


def follow_path(path: str) -> None:
    if FOLLOW != "on" or not path:
        return
    if tmux("display", "-t", SESSION, "-p", "#{pane_current_path}") == path:
        return
    foreground = tmux("display", "-t", SESSION, "-p", "#{pane_current_command}")
    if foreground not in SHELLS:
        return  # a TUI is in front; do not type into it
    tmux_run("send-keys", "-t", SESSION, "-R", f' cd "{path}"', "C-m")


# ── popup ──────────────────────────────────────────────────────────────────

def default_title() -> str:
    prefix = tmux("show-options", "-gv", "prefix")
    menu_key = get_opt("@perch-bind-menu", "P")
    return f" perch │ {prefix} {menu_key}: menu "


def open_popup(target_client: str | None) -> int:
    """Open the popup. Blocks while it is open; returns the exit status."""
    width = get_opt("@perch-cur-width", WIDTH)
    height = get_opt("@perch-cur-height", HEIGHT)
    title = tmux("show-option", "-gqv", "@perch-title")
    if not title:
        title = default_title()
    socket_path = tmux("display", "-p", "#{socket_path}")

    attach = "env -u TMUX tmux -S {} attach-session -t {}".format(
        shlex.quote(socket_path), shlex.quote(SESSION)
    )

    args = [
        "popup",
        "-S", f"fg={BORDER}",
        "-s", f"fg={TEXT}",
        "-T", title,
        "-w", width,
        "-h", height,
        "-b", "rounded",
        "-E",
    ]
    if target_client:
        args += ["-c", target_client]
    args.append(attach)
    return tmux_run(*args).returncode


def open_popup_with_retry(target_client: str | None) -> None:
    started = time.monotonic()
    code = open_popup(target_client)
    failed_fast = time.monotonic() - started < 1.5
    if code == 0 or not failed_fast or not session_exists():
        return
    # The popup could not open (typically a stale runtime size). Reset the
    # runtime size and try once more.
    unset_opt("@perch-cur-width")
    unset_opt("@perch-cur-height")
    open_popup(target_client)


# ── hotkeys (opt-in via @perch-hotkeys on) ─────────────────────────────────

def bind_hotkeys() -> None:
    if HOTKEYS != "on":
        return
    for key, args in HOTKEY_ACTIONS.items():
        tmux_run("bind-key", "-n", key, "run-shell", script_cmd(*args))


def unbind_hotkeys() -> None:
    for key in (*HOTKEY_ACTIONS, "C-M-u"):
        tmux_run("unbind-key", "-q", "-n", key)


# ── commands ───────────────────────────────────────────────────────────────

def toggle() -> None:
    if current_session() == SESSION:
        unbind_hotkeys()
        tmux_run("detach-client")
        return

    origin = current_session()
    path = tmux("display", "-p", "#{pane_current_path}")
    set_opt("@perch-origin", origin)
    ensure_session(path)
    follow_path(path)
    bind_hotkeys()
    open_popup_with_retry(outer_client())


def close() -> None:
    if current_session() == SESSION:
        unbind_hotkeys()
        tmux_run("detach-client")


def step_percent(value: str, delta: int) -> str:
    number = value[:-1] if value.endswith("%") else value
    if not number.isdigit():
        base = WIDTH[:-1] if WIDTH.endswith("%") else ""
        number = base if base.isdigit() else "80"
    n = max(20, min(100, int(number) + delta))
    return f"{n}%"


def zoom(direction: str) -> None:
    if direction not in ("in", "out", "full", "reset"):
        notify(f"unknown zoom direction: {direction}")
        return
    if current_session() != SESSION:
        notify("open the float first")
        return

    # Capture the outer client BEFORE detaching: the popup's own client is
    # gone afterwards, and a re-open without an explicit target fails.
    target = outer_client()

    if direction == "full":
        width, height = "100%", "100%"
    elif direction == "reset":
        width, height = WIDTH, HEIGHT
    else:  # "in" shrinks (menu: size down), "out" grows (size up)
        delta = -5 if direction == "in" else 5
        width = step_percent(get_opt("@perch-cur-width", WIDTH), delta)
        height = step_percent(get_opt("@perch-cur-height", HEIGHT), delta)

    set_opt("@perch-cur-width", width)
    set_opt("@perch-cur-height", height)
    tmux_run("detach-client")
    open_popup_with_retry(target)


def menu() -> None:
    if current_session() == SESSION:
        items = [
            "size down", "-", f'run-shell "{script_cmd("zoom", "in")}"',
            "size up", "+", f'run-shell "{script_cmd("zoom", "out")}"',
            "full screen", "f", f'run-shell "{script_cmd("zoom", "full")}"',
            "reset size", "r", f'run-shell "{script_cmd("zoom", "reset")}"',
            "close float", "q", f'run-shell "{script_cmd("close")}"',
        ]
    else:
        items = [
            "open float", "o", f'run-shell "{script_cmd("toggle")}"',
        ]
    tmux_run("menu", "-T", " perch ", *items)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "toggle"
    try:
        if command == "toggle":
            toggle()
        elif command == "menu":
            menu()
        elif command == "zoom":
            zoom(argv[1] if len(argv) > 1 else "reset")
        elif command == "close":
            close()
        else:
            name = os.path.basename(SELF)
            print(f"usage: {name} {{toggle|menu|zoom {{in|out|full|reset}}|close}}",
                  file=sys.stderr)
            return 2
    except Exception as exc:
        traceback.print_exc()
        notify(f"error: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
