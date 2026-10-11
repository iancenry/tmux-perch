# perch

A floating scratch terminal for tmux. Press one key and a popup opens over
your work, attached to a persistent session; press it again and the popup
disappears with everything inside still running.

Built after running [tmux-floax](https://github.com/omerxx/tmux-floax)
through a debugging gauntlet — the design notes below are the takeaways.
[operonlab/tmux-floatpane](https://github.com/operonlab/tmux-floatpane) was a
useful reference for what a hardened fork looks like.

## Requirements

- tmux >= 3.3
- python3 (stdlib only)

## Install

With TPM (once hosted on a git remote):

```tmux
set -g @plugin 'you/tmux-perch'
```

Manual, from this checkout:

```tmux
run-shell '/Users/iancenry/Desktop/projects/personal/tmux-perch/perch.tmux'
```

Then reload (`prefix + r`) or restart tmux.

## Usage

- `prefix + p` — toggle the float
- `prefix + P` — menu: size down/up, full screen, reset size, close
- inside the float it is a normal session: `prefix %` / `prefix "` split panes,
  and everything in it persists across hiding/showing

## Options

| Option | Default | Meaning |
| ------ | ------- | ------- |
| `@perch-bind` | `p` | prefix key that toggles the float |
| `@perch-bind-menu` | `P` | prefix key that opens the menu |
| `@perch-bind-ctrl` | `on` | also bind the Ctrl-variant (e.g. `C-p`) of single-char keys — holding Ctrl through the chord still toggles. Takes `C-p` from `previous-window` (tmux-sensible); set `off` to keep the stock binding. |
| `@perch-session` | `perch` | name of the persistent scratch session |
| `@perch-width` / `@perch-height` | `80%` | default float size (percent or cells) |
| `@perch-border-color` | `magenta` | popup border colour |
| `@perch-text-color` | `default` | popup text colour |
| `@perch-change-path` | `on` | follow the current pane's directory (shells only) |
| `@perch-hotkeys` | `off` | opt-in `C-M-s/b/f/r` root-table resize keys (needs a terminal with option-as-meta) |
| `@perch-title` | auto | custom popup title |

## Design notes (scars included)

- **Single file, subcommand dispatch.** No shell function namespace to
  collide with — floax's `embed.sh` recursion bug cannot happen here.
- **State lives in tmux options** (`@perch-cur-*`), never in the global
  environment.
- **Future-proof attach.** The in-popup command strips `TMUX` and passes the
  socket explicitly (`env -u TMUX tmux -S <socket> attach`). When tmux turns
  popups into floating panes (master / next-3.9), a plain `tmux attach`
  inside one trips the nested-session guard; this keeps old and new tmux
  working.
- **Resize re-opens on a captured client.** After `detach-client` the
  popup's own client is gone; re-opening with `popup -c <tty>` targets the
  outer client explicitly (upstream floax PR #70 fixes the same bug).
- **Safe path following.** `cd` is only typed when the scratch pane's
  foreground is a shell — never into a running TUI.
- **Hold-Ctrl chords work.** Toggle/menu keys are also registered on their
  Ctrl-variant (`C-p`), because holding Ctrl through the chord delivers
  Ctrl+P — which tmux-sensible maps to `previous-window` ("No previous
  window" if the session has one window).
- **Quiet on success.** The load shim always exits 0; successful runs print
  nothing.

## Tests

```sh
tests/harness.sh
```

Spins a throwaway tmux server, drives a real client through a pty with
`script(1)`, and asserts: toggle opens/closes the float, the session
persists, and the menu full-screen re-opens the popup (the floax
regression).

For hands-on testing, run the playground from a fresh terminal (not inside
tmux) — it starts a throwaway server with perch loaded:

```sh
tests/play.sh
```

Then `Ctrl-b p` toggles the float and `Ctrl-b P` opens the menu.

## License

Not set yet — add one before publishing.
