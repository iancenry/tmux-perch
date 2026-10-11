#!/usr/bin/env python3
"""perch — a floating scratch terminal for tmux.

One key toggles a popup attached to a persistent scratch session. The popup
is only a view; closing it leaves the session (and anything running in it)
untouched.

Two modes:
  * shared (default): one "perch" session serves the whole tmux server.
  * per-session (@perch-per-session on): every base session gets its own
    independent float, named "perch-<session>", marked with the
    @perch-float-base option. Orphaned floats (base session gone) are
    garbage-collected on the next toggle.

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

FLOAT_MARKER = "@perch-float-base"


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


def session_option(session: str, name: str) -> str:
    return tmux("show-option", "-t", session, "-qv", name)


def project_label(path: str, fallback: str) -> str:
    """A human label for the float: the git repo root's name when the path
    is inside a repo, else the directory name, else the fallback."""
    home = os.path.expanduser("~")
    probe = os.path.realpath(path) if path else ""
    base = probe
    while probe and probe != "/":
        if os.path.exists(os.path.join(probe, ".git")):
            base = probe
            break
        probe = os.path.dirname(probe)
    name = os.path.basename(base.rstrip("/")) if base else ""
    if not name or base == home:
        return fallback
    return name


FLOAT_NAME = get_opt("@perch-session", "perch")
PER_SESSION = get_opt("@perch-per-session", "off")
WIDTH = get_opt("@perch-width", "80%")
HEIGHT = get_opt("@perch-height", "80%")
BORDER = get_opt("@perch-border-color", "magenta")
TEXT = get_opt("@perch-text-color", "default")
FOLLOW = get_opt("@perch-change-path", "on")
HOTKEYS = get_opt("@perch-hotkeys", "off")
ICON = get_opt("@perch-icon", "🐦")

HOTKEY_ACTIONS = {
    "C-M-s": ("zoom", "in"),
    "C-M-b": ("zoom", "out"),
    "C-M-f": ("zoom", "full"),
    "C-M-r": ("zoom", "reset"),
}


def script_cmd(*args: str) -> str:
    """A shell command that re-invokes this script with the given arguments."""
    return "python3 " + " ".join(shlex.quote(a) for a in (SELF, *args))


def script_cmd_tty(*args: str) -> str:
    """Like script_cmd, but appends '#{client_tty}': tmux expands it when the
    command runs, so the action knows which client triggered it."""
    return script_cmd(*args) + " '#{client_tty}'"


def notify(message: str) -> None:
    tmux_run("display-message", f"perch: {message}")


def display_for(tty: str | None, fmt: str) -> str:
    """Evaluate a format in a specific client's context when possible."""
    if tty:
        out = tmux("display-message", "-c", tty, "-p", fmt)
        if out:
            return out
    return tmux("display", "-p", fmt)


def current_session(tty: str | None = None) -> str:
    return display_for(tty, "#{session_name}")


def session_exists(name: str) -> bool:
    return tmux_run("has-session", "-t", name).returncode == 0


def outer_client(float_name: str) -> str | None:
    """The attached client that is NOT showing the given float."""
    listing = tmux("list-clients", "-F", "#{client_tty} #{client_session}")
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] != float_name:
            return parts[0]
    return None


def outer_of_float(float_name: str) -> str | None:
    """The client displaying the base session this float belongs to (falls
    back to any client not on the float)."""
    base = session_option(float_name, FLOAT_MARKER)
    listing = tmux("list-clients", "-F", "#{client_tty} #{client_session}")
    pairs = [line.split() for line in listing.splitlines() if line.strip()]
    if base:
        for tty, sess in pairs:
            if sess == base:
                return tty
    for tty, sess in pairs:
        if sess != float_name:
            return tty
    return None


# ── float resolution (shared vs per-session) ───────────────────────────────

def float_for(base: str) -> str:
    return f"{FLOAT_NAME}-{base}"


def float_context(tty: str | None = None) -> tuple[str, str | None, bool]:
    """(float session name, base session, already inside the float)."""
    cur = current_session(tty)
    if PER_SESSION != "on":
        return FLOAT_NAME, None, cur == FLOAT_NAME
    base = session_option(cur, FLOAT_MARKER)
    if base:
        return cur, base, True
    return float_for(cur), cur, False


def gc_orphans() -> None:
    """Kill per-session floats whose base session no longer exists."""
    if PER_SESSION != "on":
        return
    names = tmux("list-sessions", "-F", "#{session_name}").splitlines()
    for name in names:
        base = session_option(name, FLOAT_MARKER)
        if base and base not in names:
            tmux_run("kill-session", "-t", name)


# ── session lifecycle ──────────────────────────────────────────────────────

def ensure_session(name: str, path: str, base: str | None,
                   label: str) -> None:
    if not session_exists(name):
        tmux_run("new-session", "-d", "-s", name, "-c",
                 path or os.path.expanduser("~"))
    tmux_run("set-option", "-t", name, "status", "off")
    # If the float session is destroyed (its shell exited), detach instead of
    # letting the client jump into another session.
    tmux_run("set-option", "-t", name, "detach-on-destroy", "on")
    tmux_run("set-option", "-t", name, "@perch-float-label", label)
    if base is not None:
        tmux_run("set-option", "-t", name, FLOAT_MARKER, base)


def follow_path(name: str, path: str) -> None:
    if FOLLOW != "on" or not path:
        return
    if tmux("display", "-t", name, "-p", "#{pane_current_path}") == path:
        return
    foreground = tmux("display", "-t", name, "-p", "#{pane_current_command}")
    if foreground not in SHELLS:
        return  # a TUI is in front; do not type into it
    tmux_run("send-keys", "-t", name, "-R", f' cd "{path}"', "C-m")


# ── popup ──────────────────────────────────────────────────────────────────

def default_title(label: str) -> str:
    prefix = tmux("show-options", "-gv", "prefix")
    menu_key = get_opt("@perch-bind-menu", "P")
    icon = f"{ICON} " if ICON else ""
    return f" {icon}{label} │ {prefix} {menu_key}: menu "


def open_popup(name: str, label: str, target_client: str | None) -> int:
    """Open the popup. Blocks while it is open; returns the exit status."""
    width = get_opt("@perch-cur-width", WIDTH)
    height = get_opt("@perch-cur-height", HEIGHT)
    title = tmux("show-option", "-gqv", "@perch-title")
    if not title:
        title = default_title(label)
    socket_path = tmux("display", "-p", "#{socket_path}")

    attach = "env -u TMUX tmux -S {} attach-session -t {}".format(
        shlex.quote(socket_path), shlex.quote(name)
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


def open_popup_with_retry(name: str, label: str,
                          target_client: str | None) -> None:
    started = time.monotonic()
    code = open_popup(name, label, target_client)
    failed_fast = time.monotonic() - started < 1.5
    if code == 0 or not failed_fast or not session_exists(name):
        return
    # The popup could not open (typically a stale runtime size). Reset the
    # runtime size and try once more.
    unset_opt("@perch-cur-width")
    unset_opt("@perch-cur-height")
    open_popup(name, label, target_client)


# ── hotkeys (opt-in via @perch-hotkeys on) ─────────────────────────────────

def bind_hotkeys() -> None:
    if HOTKEYS != "on":
        return
    for key, args in HOTKEY_ACTIONS.items():
        tmux_run("bind-key", "-n", key, "run-shell", script_cmd_tty(*args))


def unbind_hotkeys() -> None:
    for key in (*HOTKEY_ACTIONS, "C-M-u"):
        tmux_run("unbind-key", "-q", "-n", key)


# ── commands ───────────────────────────────────────────────────────────────

def detach_current(tty: str | None) -> None:
    if tty:
        tmux_run("detach-client", "-t", tty)
    else:
        tmux_run("detach-client")


def toggle(tty: str | None = None) -> None:
    cur = current_session(tty)
    name, base, inside = float_context(tty)
    if inside:
        unbind_hotkeys()
        detach_current(tty)
        return

    path = display_for(tty, "#{pane_current_path}")
    label = project_label(path, cur)
    set_opt("@perch-origin", cur)
    gc_orphans()
    ensure_session(name, path, base, label)
    follow_path(name, path)
    bind_hotkeys()
    open_popup_with_retry(name, label, tty or outer_client(name))


def close(tty: str | None = None) -> None:
    _, _, inside = float_context(tty)
    if inside:
        unbind_hotkeys()
        detach_current(tty)


def step_percent(value: str, delta: int) -> str:
    number = value[:-1] if value.endswith("%") else value
    if not number.isdigit():
        base = WIDTH[:-1] if WIDTH.endswith("%") else ""
        number = base if base.isdigit() else "80"
    n = max(20, min(100, int(number) + delta))
    return f"{n}%"


def zoom(direction: str, tty: str | None = None) -> None:
    if direction not in ("in", "out", "full", "reset"):
        notify(f"unknown zoom direction: {direction}")
        return
    name, base, inside = float_context(tty)
    if not inside:
        notify("open the float first")
        return

    # Capture the outer client BEFORE detaching: the popup's own client is
    # gone afterwards, and a re-open without an explicit target fails.
    target = outer_of_float(name)
    label = session_option(name, "@perch-float-label") or base or "perch"

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
    detach_current(tty)
    open_popup_with_retry(name, label, target)


def menu(tty: str | None = None) -> None:
    _, _, inside = float_context(tty)
    if inside:
        items = [
            "size down", "-", f'run-shell "{script_cmd_tty("zoom", "in")}"',
            "size up", "+", f'run-shell "{script_cmd_tty("zoom", "out")}"',
            "full screen", "f", f'run-shell "{script_cmd_tty("zoom", "full")}"',
            "reset size", "r", f'run-shell "{script_cmd_tty("zoom", "reset")}"',
            "close float", "q", f'run-shell "{script_cmd_tty("close")}"',
        ]
    else:
        items = [
            "open float", "o", f'run-shell "{script_cmd_tty("toggle")}"',
        ]
    tmux_run("menu", "-T", " perch ", *items)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "toggle"
    rest = argv[1:]
    tty = next((a for a in rest if a.startswith("/")), None)
    direction = next((a for a in rest if a in ("in", "out", "full", "reset")),
                     "reset")
    try:
        if command == "toggle":
            toggle(tty)
        elif command == "menu":
            menu(tty)
        elif command == "zoom":
            zoom(direction, tty)
        elif command == "close":
            close(tty)
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
