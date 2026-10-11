// perch — a floating scratch terminal for tmux. Go port of perch.py.
//
// Behavior contract, option names and subcommands are identical to the
// Python version; the tests/ harness exercises this binary through tmux
// only, so it is the acceptance suite for both runtimes.
//
// Parity notes (each mirrors a lesson in the Python source and README):
//   - Every tmux CLI call has a 10s timeout except `popup` and `menu`,
//     which block by design until the float is dismissed / an item chosen.
//   - Runtime state lives in tmux user options (@perch-*), never env.
//   - The in-popup attach strips TMUX and passes the socket explicitly,
//     so tmux master (next-3.9) where popups are floating panes works too.
//   - Resize re-opens the popup on a captured client tty (`-c <tty>`).
//   - Path-following only types `cd` at a shell prompt, shell-quoted.
//   - The session-closed hook runs the GC backgrounded (`-b`): a foreground
//     run-shell job delays server shutdown and its late socket cleanup can
//     kill a fast-restarted server.
package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const floatMarker = "@perch-float-base"

var shells = map[string]bool{
	"zsh": true, "bash": true, "sh": true, "fish": true, "nu": true,
}

var (
	optCache = map[string]string{}
	selfPath string
)

// ── tmux plumbing ──────────────────────────────────────────────────────────

func tmuxRun(ctx context.Context, args ...string) string {
	out, _ := exec.CommandContext(ctx, "tmux", args...).Output()
	return strings.TrimSpace(string(out))
}

// tmuxStr runs a tmux command with the standard timeout.
func tmuxStr(args ...string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return tmuxRun(ctx, args...)
}

func opt(name, def string) string {
	if v, ok := optCache[name]; ok {
		return v
	}
	v := tmuxStr("show-option", "-gqv", name)
	if v == "" {
		v = def
	}
	optCache[name] = v
	return v
}

func setOpt(name, value string) {
	tmuxStr("set-option", "-gq", name, value)
	optCache[name] = value
}

func unsetOpt(name string) {
	tmuxStr("set-option", "-guq", name)
	delete(optCache, name)
}

func sessionOption(session, name string) string {
	return tmuxStr("show-option", "-t", session, "-qv", name)
}

// shQuote makes s safe to embed in a POSIX shell command line
// (the Go equivalent of Python's shlex.quote).
func shQuote(s string) string {
	if s == "" {
		return "''"
	}
	const safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./-"
	if strings.Trim(s, safe) == "" {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

func projectLabel(path, fallback string) string {
	home, _ := os.UserHomeDir()
	probe := ""
	if path != "" {
		if resolved, err := filepath.EvalSymlinks(path); err == nil {
			probe = resolved
		} else {
			probe = path
		}
	}
	base := probe
	for probe != "" && probe != "/" {
		if _, err := os.Stat(filepath.Join(probe, ".git")); err == nil {
			base = probe
			break
		}
		probe = filepath.Dir(probe)
	}
	name := ""
	if base != "" && base != "/" {
		name = filepath.Base(base)
	}
	if name == "" || name == "/" || base == home {
		return fallback
	}
	return name
}

func scriptCmd(args ...string) string {
	parts := make([]string, 0, len(args)+1)
	parts = append(parts, shQuote(selfPath))
	for _, a := range args {
		parts = append(parts, shQuote(a))
	}
	return strings.Join(parts, " ")
}

// scriptCmdTTY appends '#{client_tty}', which tmux expands when the command
// runs so the action knows which client triggered it.
func scriptCmdTTY(args ...string) string {
	return scriptCmd(args...) + " '#{client_tty}'"
}

func notify(message string) {
	tmuxStr("display-message", "perch: "+message)
}

func displayFor(tty, format string) string {
	if tty != "" {
		if out := tmuxStr("display-message", "-c", tty, "-p", format); out != "" {
			return out
		}
	}
	return tmuxStr("display", "-p", format)
}

func currentSession(tty string) string {
	return displayFor(tty, "#{session_name}")
}

func sessionExists(name string) bool {
	// A dedicated call: the exit code is the answer.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "tmux", "has-session", "-t", name).Run() == nil
}

func clientPairs() [][2]string {
	out := tmuxStr("list-clients", "-F", "#{client_tty} #{client_session}")
	var pairs [][2]string
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) == 2 {
			pairs = append(pairs, [2]string{f[0], f[1]})
		}
	}
	return pairs
}

func outerClient(floatName string) string {
	for _, p := range clientPairs() {
		if p[1] != floatName {
			return p[0]
		}
	}
	return ""
}

func outerOfFloat(floatName string) string {
	base := sessionOption(floatName, floatMarker)
	pairs := clientPairs()
	if base != "" {
		for _, p := range pairs {
			if p[1] == base {
				return p[0]
			}
		}
	}
	for _, p := range pairs {
		if p[1] != floatName {
			return p[0]
		}
	}
	return ""
}

// ── float resolution (shared vs per-session) ───────────────────────────────

func floatFor(base string) string {
	return opt("@perch-session", "perch") + "-" + base
}

// floatContext returns (float session name, base, already inside the float).
// base is "" in shared mode. cur may be pre-supplied to save a call.
func floatContext(tty, cur string) (string, string, bool) {
	if cur == "" {
		cur = currentSession(tty)
	}
	if opt("@perch-per-session", "off") != "on" {
		name := opt("@perch-session", "perch")
		return name, "", cur == name
	}
	if base := sessionOption(cur, floatMarker); base != "" {
		return cur, base, true
	}
	return floatFor(cur), cur, false
}

func gcOrphans() {
	if opt("@perch-per-session", "off") != "on" {
		return
	}
	// One call: session name + marker for every session. Both must be
	// wrapped as #{...} — a bare marker string would be parsed as a session
	// name and the GC would "collect" every session it sees.
	listing := tmuxStr("list-sessions", "-F",
		fmt.Sprintf("#{session_name}|#{%s}", floatMarker))
	type entry struct{ name, base string }
	var entries []entry
	names := map[string]bool{}
	for _, line := range strings.Split(listing, "\n") {
		if line == "" {
			continue
		}
		name, base, _ := strings.Cut(line, "|")
		names[name] = true
		entries = append(entries, entry{name, base})
	}
	for _, e := range entries {
		if e.base != "" && !names[e.base] && !sessionExists(e.base) {
			// Re-check the base immediately before killing: it may have
			// been recreated between the listing and now.
			tmuxStr("kill-session", "-t", e.name)
		}
	}
}

// ── session lifecycle ──────────────────────────────────────────────────────

func ensureSession(name, path, base, label string) {
	if !sessionExists(name) {
		if path == "" {
			path, _ = os.UserHomeDir()
		}
		tmuxStr("new-session", "-d", "-s", name, "-c", path)
	}
	// One tmux invocation covers all of the session setup.
	setup := []string{
		"set-option", "-t", name, "status", "off", ";",
		// If the float session is destroyed (its shell exited), detach
		// instead of letting the client jump into another session.
		"set-option", "-t", name, "detach-on-destroy", "on", ";",
		"set-option", "-t", name, "@perch-float-label", label,
	}
	if base != "" {
		setup = append(setup, ";", "set-option", "-t", name, floatMarker, base)
	}
	tmuxStr(setup...)
}

func followPath(name, path string) {
	if opt("@perch-change-path", "on") != "on" || path == "" {
		return
	}
	if tmuxStr("display", "-t", name, "-p", "#{pane_current_path}") == path {
		return
	}
	foreground := tmuxStr("display", "-t", name, "-p", "#{pane_current_command}")
	if !shells[foreground] {
		return // a TUI is in front; do not type into it
	}
	// shQuote: the path is typed into the user's shell, so it must be safe
	// even for directory names containing quotes, $ or backticks.
	tmuxStr("send-keys", "-t", name, "-R", " cd "+shQuote(path), "C-m")
}

// ── popup ──────────────────────────────────────────────────────────────────

func defaultTitle(label string) string {
	prefix := tmuxStr("show-options", "-gv", "prefix")
	menuKey := opt("@perch-bind-menu", "P")
	icon := ""
	if iconName := opt("@perch-icon", "🐦"); iconName != "" {
		icon = iconName + " "
	}
	return fmt.Sprintf(" %s%s │ %s %s: menu ", icon, label, prefix, menuKey)
}

// openPopup blocks until the popup is dismissed; it returns the exit code.
func openPopup(name, label, targetClient string) int {
	width := opt("@perch-cur-width", "")
	if width == "" {
		width = opt("@perch-width", "80%")
	}
	height := opt("@perch-cur-height", "")
	if height == "" {
		height = opt("@perch-height", "80%")
	}
	title := tmuxStr("show-option", "-gqv", "@perch-title")
	if title == "" {
		title = defaultTitle(label)
	}
	socketPath := tmuxStr("display", "-p", "#{socket_path}")

	attach := fmt.Sprintf("env -u TMUX tmux -S %s attach-session -t %s",
		shQuote(socketPath), shQuote(name))

	args := []string{
		"popup",
		"-S", "fg=" + opt("@perch-border-color", "magenta"),
		"-s", "fg=" + opt("@perch-text-color", "default"),
		"-T", title,
		"-w", width,
		"-h", height,
		"-b", "rounded",
		"-E",
	}
	if targetClient != "" {
		args = append(args, "-c", targetClient)
	}
	args = append(args, attach)

	// No timeout: this call blocks for as long as the float stays open.
	cmd := exec.Command("tmux", args...)
	_ = cmd.Run()
	if cmd.ProcessState != nil {
		return cmd.ProcessState.ExitCode()
	}
	return 1
}

func openPopupWithRetry(name, label, targetClient string) {
	started := time.Now()
	code := openPopup(name, label, targetClient)
	failedFast := time.Since(started) < 1500*time.Millisecond
	if code == 0 || !failedFast || !sessionExists(name) {
		return
	}
	// The popup could not open (typically a stale runtime size). Reset the
	// runtime size and try once more.
	unsetOpt("@perch-cur-width")
	unsetOpt("@perch-cur-height")
	openPopup(name, label, targetClient)
}

// ── hotkeys (opt-in via @perch-hotkeys on) ─────────────────────────────────

var hotkeyActions = []struct {
	key  string
	args []string
}{
	{"C-M-s", []string{"zoom", "in"}},
	{"C-M-b", []string{"zoom", "out"}},
	{"C-M-f", []string{"zoom", "full"}},
	{"C-M-r", []string{"zoom", "reset"}},
}

func bindHotkeys() {
	if opt("@perch-hotkeys", "off") != "on" {
		return
	}
	var args []string
	for _, h := range hotkeyActions {
		if len(args) > 0 {
			args = append(args, ";")
		}
		args = append(args, "bind-key", "-n", h.key, "run-shell",
			scriptCmdTTY(h.args...))
	}
	tmuxStr(args...)
}

func unbindHotkeys() {
	var args []string
	for _, key := range []string{"C-M-s", "C-M-b", "C-M-f", "C-M-r", "C-M-u"} {
		if len(args) > 0 {
			args = append(args, ";")
		}
		args = append(args, "unbind-key", "-q", "-n", key)
	}
	tmuxStr(args...)
}

// ── commands ───────────────────────────────────────────────────────────────

func detachCurrent(tty string) {
	if tty != "" {
		tmuxStr("detach-client", "-t", tty)
	} else {
		tmuxStr("detach-client")
	}
}

func toggle(tty string) {
	// One call gives both the session and the pane path for the context.
	info := displayFor(tty, "#{session_name}|#{pane_current_path}")
	cur, path, _ := strings.Cut(info, "|")
	if cur == "" {
		// No client context at all (invoked without an attached client and
		// no MRU client). Opening a float for a nonexistent base would
		// create junk sessions like "perch-"; bail out instead.
		return
	}
	name, base, inside := floatContext(tty, cur)
	if inside {
		unbindHotkeys()
		detachCurrent(tty)
		return
	}

	label := projectLabel(path, cur)
	setOpt("@perch-origin", cur)
	gcOrphans()
	ensureSession(name, path, base, label)
	followPath(name, path)
	bindHotkeys()
	target := tty
	if target == "" {
		target = outerClient(name)
	}
	openPopupWithRetry(name, label, target)
}

func closeCmd(tty string) {
	_, _, inside := floatContext(tty, "")
	if inside {
		unbindHotkeys()
		detachCurrent(tty)
	}
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func stepPercent(value string, delta int) string {
	number := strings.TrimSuffix(value, "%")
	if !isDigits(number) {
		number = strings.TrimSuffix(opt("@perch-width", "80%"), "%")
		if !isDigits(number) {
			number = "80"
		}
	}
	n, _ := strconv.Atoi(number)
	n += delta
	if n < 20 {
		n = 20
	}
	if n > 100 {
		n = 100
	}
	return fmt.Sprintf("%d%%", n)
}

func zoom(direction, tty string) {
	switch direction {
	case "in", "out", "full", "reset":
	default:
		notify("unknown zoom direction: " + direction)
		return
	}
	name, base, inside := floatContext(tty, "")
	if !inside {
		notify("open the float first")
		return
	}

	// Capture the outer client BEFORE detaching: the popup's own client is
	// gone afterwards, and a re-open without an explicit target fails.
	target := outerOfFloat(name)
	label := sessionOption(name, "@perch-float-label")
	if label == "" {
		label = base
	}
	if label == "" {
		label = "perch"
	}

	var width, height string
	switch direction {
	case "full":
		width, height = "100%", "100%"
	case "reset":
		width = opt("@perch-width", "80%")
		height = opt("@perch-height", "80%")
	default: // "in" shrinks (menu: size down), "out" grows (size up)
		delta := 5
		if direction == "in" {
			delta = -5
		}
		curW := opt("@perch-cur-width", "")
		if curW == "" {
			curW = opt("@perch-width", "80%")
		}
		curH := opt("@perch-cur-height", "")
		if curH == "" {
			curH = opt("@perch-height", "80%")
		}
		width = stepPercent(curW, delta)
		height = stepPercent(curH, delta)
	}

	setOpt("@perch-cur-width", width)
	setOpt("@perch-cur-height", height)
	detachCurrent(tty)
	openPopupWithRetry(name, label, target)
}

func menu(tty string) {
	_, _, inside := floatContext(tty, "")
	var items []string
	if inside {
		items = []string{
			"size down", "-", `run-shell "` + scriptCmdTTY("zoom", "in") + `"`,
			"size up", "+", `run-shell "` + scriptCmdTTY("zoom", "out") + `"`,
			"full screen", "f", `run-shell "` + scriptCmdTTY("zoom", "full") + `"`,
			"reset size", "r", `run-shell "` + scriptCmdTTY("zoom", "reset") + `"`,
			"close float", "q", `run-shell "` + scriptCmdTTY("close") + `"`,
		}
	} else {
		items = []string{
			"open float", "o", `run-shell "` + scriptCmdTTY("toggle") + `"`,
		}
	}
	args := append([]string{"menu", "-T", " perch "}, items...)
	// No timeout: the menu blocks until an item is chosen or it is closed.
	_ = exec.Command("tmux", args...).Run()
}

func main() {
	selfPath, _ = os.Executable()

	args := os.Args[1:]
	command := "toggle"
	var rest []string
	if len(args) > 0 {
		command = args[0]
		rest = args[1:]
	}

	tty := ""
	direction := "reset"
	for _, a := range rest {
		if tty == "" && strings.HasPrefix(a, "/") {
			tty = a
		}
		switch a {
		case "in", "out", "full", "reset":
			direction = a
		}
	}

	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintf(os.Stderr, "perch: %v\n", r)
			notify(fmt.Sprintf("error: %v", r))
		}
	}()

	switch command {
	case "toggle":
		toggle(tty)
	case "menu":
		menu(tty)
	case "zoom":
		zoom(direction, tty)
	case "close":
		closeCmd(tty)
	case "gc":
		gcOrphans()
	default:
		fmt.Fprintln(os.Stderr,
			"usage: perch {toggle|menu|zoom {in|out|full|reset}|close|gc}")
		os.Exit(2)
	}
}
