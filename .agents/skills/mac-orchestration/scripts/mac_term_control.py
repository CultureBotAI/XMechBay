#!/usr/bin/env python3
"""Guard Terminal/tmux input and report contract states.

Inputs: host, window and file/short-command arguments. Outputs: current screen,
status rows and metadata-only refusal/override records. Added 2026-10-25.
Transport arguments are quoted once for SSH; prompt files never become shell code.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[4]
GOAL = re.compile(r"Pursuing goal|Goal paused|Goal stalled|Goal achieved|Goal hit usage limits")
PROMPTS = (
    ("SIGN-IN", r"Sign in with (?:ChatGPT|your)|authentication required|Log in to continue"),
    ("TRUST-PROMPT", r"Trust this folder|Do you trust|trust the (?:files|contents)"),
    ("UPDATE-PROMPT", r"Update available|Skip until next version|Update now"),
    ("APPROVAL", r"approval request|Allow command|Requesting permission|Do you want to|"
                 r"Allow sandbox bypass|Confirm the command is safe|Would you like to (?:run|proceed)|"
                 r"Allow access to this file\?|Run this command\?|Accept changes\?|"
                 r"Would you like to (?:make the following edits|grant these permissions|"
                 r"send input to the existing terminal)\?"),
    ("QUESTION", r"Queued follow-up|⇧← to answer|(?:^|\n)\s*\?\s+(?:Which|.*question)|"
                 r"How's the CLI experience so far\?|Switch to (?:gpt-|claude-|gemini-)\S+|"
                 r"(?:^|\n)\s*(?:Question|Questions)\s*[:(]|"
                 r"Type your answer|to submit answer|to navigate questions|"
                 r"(?:^|\n)\s*(?:Select|Choose) (?:an? option|one of the following)|"
                 r"(?:^|\n)\s*[›❯>]?\s*\d+[.)]\s+(?:Yes|No|Allow|Deny|Approve|Reject|Cancel)(?:[,.:\s]|$)"),
)


class Refused(RuntimeError):
    pass


def prompt_states(screen):
    return [state for state, pattern in PROMPTS if re.search(pattern, screen, re.I)]


def status_state(screen):
    """Only emit contract vocabulary; an unreadable/nonempty composer is not idle."""
    prompts = prompt_states(screen)
    if prompts:
        return ", ".join(prompts)
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    # Prefer the latest model footer to incidental goal text in conversation history.
    model_lines = [line for line in lines if re.search(r"GPT-|gpt-|Gemini|Claude|Argon|Opus|Sonnet", line)]
    footer = model_lines[-1] if model_lines else ""
    goals = GOAL.findall(footer)
    if not goals:
        goals = GOAL.findall("\n".join(lines[-4:]))
    if goals:
        return goals[-1]
    if re.search(r"esc to interrupt|Working \(|● Running \(", "\n".join(lines[-6:]), re.I):
        return "Working"
    if re.search(r"\bGoal\b|Pursuing goal", footer):
        return ""  # Unrecognised goal state is not proof that no goal exists.
    composers = [index for index, line in enumerate(lines) if re.match(r"^[›❯>]", line)]
    if composers:
        start = composers[-1]
        if re.fullmatch(r"[›❯>]\s*(?:[│▏])?", lines[start]):
            # A blank marker is insufficient: wrapped/drafted lines belong to the
            # composer until the model footer (or screen end). Require that whole
            # region to be empty; unknown UI lines conservatively stay unknown.
            footer_index = max((index for index, line in enumerate(lines) if line == footer), default=len(lines))
            end = footer_index if footer_index > start else len(lines)
            if not lines[start + 1:end]:
                return "idle"
    return ""  # Unknown or drafted text: preserve the screen detail, never guess idle.


class Transport:
    def __init__(self, host):
        self.host = "" if host == "local" else host
        if self.host and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.host):
            raise ValueError("host must be a bare hostname")

    def run(self, args, input=None):
        command = list(args)
        if self.host:
            command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                       self.host, shlex.join(command)]
        result = subprocess.run(command, input=input, text=True, capture_output=True)
        if result.returncode:
            # Do not echo arguments or stderr: either can contain prompt text.
            raise RuntimeError(f"{args[0]} failed with exit {result.returncode}")
        return result.stdout

    def apple(self, script, *args):
        return self.run(["osascript", "-", *map(str, args)], input=script)

    def need_terminal(self):
        if not re.search(r"/Terminal\.app/Contents/MacOS/Terminal$",
                         self.run(["ps", "-axo", "comm="]), re.M):
            raise Refused("Terminal is not running; refusing to launch a GUI app")

    @contextmanager
    def prompt_file(self, source):
        """Use a private unique staging directory and remove it on every exit."""
        # Validate the exact staged bytes so edits to the source cannot race SCP.
        data = source.read_bytes()
        validate_prompt(data)
        with tempfile.TemporaryDirectory(prefix="xmechbay-mac-term-") as local_folder:
            local = Path(local_folder) / "prompt.txt"
            local.write_bytes(data)
            local.chmod(0o600)
            if not self.host:
                yield str(local)
                return
            folder = self.run(["mktemp", "-d", "/tmp/xmechbay-mac-term.XXXXXXXX"]).strip()
            if not re.fullmatch(r"/tmp/xmechbay-mac-term\.[A-Za-z0-9]+", folder):
                raise Refused("unexpected remote staging directory")
            target = folder + "/prompt.txt"
            try:
                result = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o",
                                         "ConnectTimeout=10", str(local),
                                         f"{self.host}:{target}"], capture_output=True)
                if result.returncode:
                    raise RuntimeError("scp failed; no input sent")
                yield target
            finally:
                self.run(["rm", "-rf", "--", folder])


SCREEN_SCRIPT = '''on run argv
tell application "Terminal" to return contents of selected tab of window id ((item 1 of argv) as integer)
end run
'''
SEND_SCRIPT = '''on run argv
tell application "Terminal" to do script (item 2 of argv) in selected tab of window id ((item 1 of argv) as integer)
end run
'''
PASTE_SCRIPT = '''on run argv
set f to POSIX file (item 2 of argv)
set msg to read f as «class utf8»
set esc to ASCII character 27
tell application "Terminal" to do script (esc & "[200~" & msg & esc & "[201~") in selected tab of window id ((item 1 of argv) as integer)
end run
'''
WINDOWS_SCRIPT = '''tell application "Terminal"
set output to ""
repeat with w in windows
if (visible of w) and not (miniaturized of w) then
set agentKind to ""
set windowTitle to (name of w) as text
ignoring case
if windowTitle contains "codex" then
set agentKind to "codex"
else if windowTitle contains "agy" then
set agentKind to "agy"
else if windowTitle contains "claude" then
set agentKind to "claude"
end if
end ignoring
if agentKind is not "" then set output to output & (id of w) & "|" & agentKind & linefeed
end if
end repeat
return output
end tell
'''


ALL_WINDOWS_SCRIPT = '''tell application "Terminal"
set output to ""
repeat with w in windows
if (visible of w) and not (miniaturized of w) then
set b to bounds of w
set output to output & (index of w) & "|" & (id of w) & "|" & (item 1 of b) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b) & "|" & (tty of selected tab of w) & "|" & (name of w) & linefeed
end if
end repeat
return output
end tell
'''
OPEN_SCRIPT = '''on run argv
set commandText to "cd -- " & quoted form of (item 1 of argv)
if (count of argv) is 2 then set commandText to commandText & " && " & (item 2 of argv)
tell application "Terminal"
do script commandText
return id of front window
end tell
end run
'''
FRONT_SCRIPT = '''on run argv
tell application "Terminal"
set index of window id ((item 1 of argv) as integer) to 1
activate
return name of front window
end tell
end run
'''
BOUNDS_SCRIPT = '''on run argv
tell application "Terminal"
set w to window id ((item 1 of argv) as integer)
set index of w to 1
set bounds of w to {(item 2 of argv) as integer, (item 3 of argv) as integer, (item 4 of argv) as integer, (item 5 of argv) as integer}
return bounds of w
end tell
end run
'''
SCREEN_GEOMETRY_SCRIPT = '''ObjC.import("AppKit"); const s=$.NSScreen.mainScreen;
const f=s.visibleFrame; const F=s.frame;
[F.size.width,F.size.height-f.origin.y-f.size.height,F.size.height-f.origin.y].join(" ")'''
# Fixed programs receive all user data as positional arguments. They never
# interpolate directories, timeout strings, or user commands into shell source.
RESOLVE_DIRECTORY_SCRIPT = 'cd -- "$1" && pwd -P'
WAIT_FILE_SCRIPT = '''end=$(( $(date +%s) + $2 ))
while [ "$(date +%s)" -lt "$end" ]; do
  if [ -s "$1" ]; then printf 'present\\n'; exit 0; fi
  sleep 5
done
printf 'timeout\\n'; exit 1
'''


def window_rows(text):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit():
            raise Refused("invalid window inventory row; stopped")
        bounds = parts[2].split(",")
        if len(bounds) != 4 or any(not re.fullmatch(r"-?[0-9]+", item) for item in bounds):
            raise Refused("invalid window bounds; stopped")
        rows.append({"index": int(parts[0]), "id": parts[1], "bounds": tuple(map(int, bounds)),
                     "tty": parts[3], "name": parts[4]})
    return sorted(rows, key=lambda row: row["index"])


def cascade_moves(rows, geometry, width, height, dx, dy, cols, split, origin, ids):
    """Compute layout without transport or shell arithmetic; supports stock macOS."""
    if width <= 0 or height <= 0 or cols <= 0 or dx < 0 or dy < 0:
        raise ValueError("width, height and columns must be positive; steps must be nonnegative")
    if origin not in ("bottom-left", "bottom-right"):
        raise ValueError("origin must be bottom-left or bottom-right")
    if ids:
        requested = ids.split()
        if any(not item.isdigit() for item in requested) or len(set(requested)) != len(requested):
            raise ValueError("ids must be distinct numeric window ids")
        by_id = {row["id"]: row for row in rows}
        if any(item not in by_id for item in requested):
            raise Refused("requested window is not in the current inventory")
        rows = [by_id[item] for item in requested]
    else:
        rows = sorted(rows, key=lambda row: int(row["id"]))
    groups = {}
    for row in rows:
        groups.setdefault(row["bounds"], []).append(row["id"])
    if not groups:
        return []
    if split:
        if not re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", split):
            raise ValueError("split must be comma-separated positive row counts")
        sizes = list(map(int, split.split(",")))
        cols = len(sizes)
        if sum(sizes) < len(groups):
            raise ValueError("split must have enough slots for all windows")
    else:
        sizes = [math.ceil(len(groups) / cols)] * cols
    screen_width, top, bottom = geometry
    maxrows = max(sizes)
    column_width = width + (maxrows - 1) * dx
    gap = (screen_width - cols * column_width) // (cols - 1) if cols > 1 else 0
    if gap < 0 or column_width > screen_width:
        raise Refused("columns would overlap or extend beyond the screen; lower width or step")
    if height + (maxrows - 1) * dy > bottom - top:
        raise Refused("layout would extend beyond the screen; lower height or step")
    groups = list(groups.values())
    moves = []
    for column, size in enumerate(sizes):
        for row in range(size):
            if len(moves) == len(groups):
                return moves
            if origin == "bottom-left":
                x = column * (column_width + gap) + row * dx
            else:
                x = screen_width - column_width - column * (column_width + gap) + (maxrows - 1 - row) * dx
            y2 = bottom - row * dy
            moves.append((groups[len(moves)], (x, y2 - height, x + width, y2)))
    return moves


class Controller:
    def __init__(self, transport, command, override=None, records=None, session=None):
        self.transport = transport
        self.command = command
        self.override = override
        self.override_available = bool(override)
        self.override_bound = False
        self.override_screen = None
        self.records = Path(records or os.environ.get("XMECHBAY_RECORDS_DIR")
                            or ROOT / ".local" / "records").expanduser()
        self.session = session if session is not None else os.environ.get("XMECHBAY_TMUX_SESSION", "fleet")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", self.session):
            raise ValueError("tmux session must contain only letters, digits, underscores or hyphens")

    def record(self, target, event, states):
        now = datetime.now(timezone.utc)
        row = {"time": now.isoformat(), "host": self.transport.host or "local",
               "target": target, "command": self.command, "event": event, "states": states}
        if event == "explicit-user-override":
            row["reason"] = self.override
        self.records.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.records / f"mac-term-guard-{now.date()}.jsonl"
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def screen(self, target, tmux=False):
        try:
            if tmux:
                session = self.transport.run(["tmux", "display-message", "-p", "-t", target, "#{session_name}"]).strip()
                if session != self.session:
                    raise Refused("pane is outside the configured owned session")
                screen = self.transport.run(["tmux", "capture-pane", "-p", "-t", target])
            else:
                self.transport.need_terminal()
                screen = self.transport.apple(SCREEN_SCRIPT, target)
            if not screen.strip():
                raise Refused("screen is empty or unreadable")
            return screen
        except (RuntimeError, OSError) as exc:
            self.record(target, "refused", ["screen-unavailable"])
            raise Refused("cannot read target screen; no further input sent") from exc

    def guard(self, target, tmux=False, consume=False):
        screen = self.screen(target, tmux)
        states = prompt_states(screen)
        if re.search(r"Select Model|Select Reasoning", screen):
            states.append("QUESTION")
        # An override applies to the prompt observed at the first check and one
        # input only. Preflight reads may repeat without spending it, but may not
        # arm it for a prompt that appears later or transfer it to another target.
        signature = (str(target), tmux, tuple(states),
                     hashlib.sha256(screen.encode("utf-8")).hexdigest())
        if not self.override_bound:
            self.override_bound = True
            self.override_screen = signature if self.override_available and states else None
        if states:
            if (not self.override_available or signature != self.override_screen
                    or "SIGN-IN" in states):
                self.override_available = False
                self.record(target, "refused", states)
                raise Refused("target shows " + ", ".join(states) + "; no input sent")
            if consume:
                self.record(target, "explicit-user-override", states)
        if consume:
            # Expire before transport: a failed write may already have delivered
            # input, so retrying must never reuse this authorization.
            self.override_available = False
        return screen

    def send(self, target, text):
        self.guard(target, consume=True)
        self.transport.apple(SEND_SCRIPT, target, text)
        time.sleep(0.8)
        return self.screen(target)

    def resolve_pane(self, window):
        if window.startswith(self.session + ":"):
            window = window[len(self.session) + 1:]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", window):
            raise Refused("tmux-paste accepts an owned window name, never a worker/session/pane selector")
        rows = self.transport.run(["tmux", "list-panes", "-s", "-t", "=" + self.session,
                                   "-F", "#{window_name}|#{pane_id}"]).splitlines()
        panes = [row.split("|", 1)[1] for row in rows if row.split("|", 1)[0] == window]
        if len(panes) != 1 or not re.fullmatch(r"%[0-9]+", panes[0]):
            raise Refused("target must name exactly one pane in the configured owned session")
        return panes[0]

    def paste(self, target, source, tmux=False):
        if tmux:
            target = self.resolve_pane(target)
        self.guard(target, tmux)
        with self.transport.prompt_file(source) as path:
            if tmux:
                buffer = "xmechbay-" + uuid.uuid4().hex
                loaded = False
                try:
                    self.transport.run(["tmux", "load-buffer", "-b", buffer, path])
                    loaded = True
                    self.guard(target, True, consume=True)
                    self.transport.run(["tmux", "paste-buffer", "-p", "-b", buffer, "-t", target])
                    time.sleep(0.8)
                    self.guard(target, True, consume=True)
                    self.transport.run(["tmux", "send-keys", "-t", target, "Enter"])
                    time.sleep(0.8)
                    return self.screen(target, True)
                finally:
                    if loaded:
                        self.transport.run(["tmux", "delete-buffer", "-b", buffer])
            self.guard(target, consume=True)
            self.transport.apple(PASTE_SCRIPT, target, path)
            time.sleep(0.8)
            return self.send(target, "")


KEYS = {"enter": "", "esc": "\x1b", "ctrlc": "\x03", "ctrld": "\x04",
        "ctrlu": "\x15", "del": "\x7f", "backspace": "\x7f", "tab": "\t",
        "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D"}


def validate_prompt(data):
    if not 0 < len(data) <= 4000:
        raise ValueError("prompt must contain 1..4000 bytes; keep goals under 1000 bytes")
    data.decode("utf-8")
    if any(byte < 32 and byte not in (9, 10) for byte in data) or 127 in data:
        raise ValueError("prompt contains terminal control characters")


def checked_file(value):
    path = Path(value).expanduser().resolve(strict=True)
    data = path.read_bytes()
    validate_prompt(data)
    if len(data) > 1000:
        print("warning: large paste may collapse; verify that /goal parsed", file=sys.stderr)
    return path


def auxiliary_command(args, transport, control, parser):
    """Non-input commands, using argument transport rather than generated shells."""
    command, values = args.command, args.args
    if command not in ("windows", "screen", "open", "front", "cascade", "wait-file"):
        return False
    if args.override_prompt is not None:
        parser.error("override only applies to guarded input commands")
    if command in ("windows", "cascade") and values:
        parser.error(command + " accepts options only")
    if command in ("screen", "front"):
        if not values or not values[0].isdigit():
            parser.error("Terminal window id must be numeric")
        if len(values) > (2 if command == "screen" else 1):
            parser.error("too many arguments")
    if command == "screen":
        count = values[1] if len(values) == 2 else "12"
        if not count.isdigit() or not 1 <= int(count) <= 10000:
            parser.error("screen line count must be 1..10000")
        screen = control.screen(values[0])
        lines = [line[:200] for line in screen.splitlines() if line.strip()]
        print("\n".join(lines[-int(count):]))
        return True
    if command == "wait-file":
        if not 1 <= len(values) <= 2:
            parser.error("wait-file requires a path and optional timeout in seconds")
        seconds = values[1] if len(values) == 2 else "1800"
        if not seconds.isdigit() or not 1 <= int(seconds) <= 86400:
            parser.error("wait-file timeout must be 1..86400 seconds")
        print(transport.run(["sh", "-c", WAIT_FILE_SCRIPT, "xmechbay", values[0], seconds]).strip())
        return True
    if command == "open":
        if not 1 <= len(values) <= 2 or any(char in values[0] for char in "\r\n\0"):
            parser.error("open requires a directory and optionally one quoted shell command")
        transport.need_terminal()
        directory = transport.run(["sh", "-c", RESOLVE_DIRECTORY_SCRIPT, "xmechbay", values[0]]).rstrip("\n")
        if not directory or "\n" in directory or "\r" in directory:
            raise Refused("target directory could not be resolved")
        print(transport.apple(OPEN_SCRIPT, directory, *values[1:]).strip())
        return True
    transport.need_terminal()
    if command == "front":
        print(transport.apple(FRONT_SCRIPT, values[0]).strip())
        return True
    inventory = transport.apple(ALL_WINDOWS_SCRIPT)
    rows = window_rows(inventory)
    if command == "windows":
        print(inventory.strip())
        return True
    geometry_text = transport.run(["osascript", "-l", "JavaScript", "-e", SCREEN_GEOMETRY_SCRIPT])
    geometry = tuple(int(float(value)) for value in geometry_text.split())
    if len(geometry) != 3:
        raise Refused("screen geometry could not be read")
    moves = cascade_moves(rows, geometry, args.width, args.height, args.dx, args.dy,
                          args.cols, args.split, args.origin, args.ids)
    for ids, bounds in moves:
        if args.dry_run:
            print("would move " + ",".join(ids) + " -> " + ",".join(map(str, bounds)))
            continue
        for target in ids:
            actual = transport.apple(BOUNDS_SCRIPT, target, *bounds)
            observed = tuple(map(int, re.findall(r"-?[0-9]+", actual)))
            if observed == bounds:
                print("moved " + target + " -> " + ",".join(map(str, bounds)))
                break
        else:
            raise Refused("window bounds did not match requested layout")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-H", "--host", default=os.environ.get("MAC_TERM_HOST", "local"),
                        help="local or an explicit bare SSH hostname; no built-in host aliases")
    parser.add_argument("--override-prompt", metavar="USER_INSTRUCTION",
                        help="only for the user's explicit instruction to answer this prompt; recorded")
    parser.add_argument("command", choices=["windows", "screen", "open", "front", "cascade", "wait-file",
                                            "status", "send", "key", "paste", "tmux-paste", "quit-codex"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--w", dest="width", type=int, default=1100, help="cascade window width")
    parser.add_argument("--h", dest="height", type=int, default=520, help="cascade window height")
    parser.add_argument("--dx", type=int, default=24, help="cascade horizontal step")
    parser.add_argument("--dy", type=int, default=22, help="cascade vertical step")
    parser.add_argument("--cols", type=int, default=1, help="cascade column count")
    parser.add_argument("--split", default="", help="cascade explicit row counts, such as 3,4")
    parser.add_argument("--origin", choices=("bottom-right", "bottom-left"), default="bottom-right")
    parser.add_argument("--ids", default="", help="cascade numeric window ids separated by spaces")
    parser.add_argument("--dry-run", action="store_true", help="print cascade moves without changing layout")
    args = parser.parse_args(argv)
    if args.override_prompt is not None and not args.override_prompt.strip():
        parser.error("override requires the user's explicit instruction")
    transport = Transport(args.host)
    control = Controller(transport, args.command, args.override_prompt)
    if auxiliary_command(args, transport, control, parser):
        return
    if args.command == "status":
        transport.need_terminal()
        for row in transport.apple(WINDOWS_SCRIPT).splitlines():
            if not row.strip():
                continue
            match = re.fullmatch(r"([0-9]+)\|(codex|agy|claude)", row.strip())
            if not match:
                raise Refused("invalid window inventory row; stopped")
            target, kind = match.groups()
            screen = control.screen(target)
            lines = [line.strip() for line in screen.splitlines() if line.strip()]
            model = [line for line in lines if re.search(r"GPT-|gpt-|Gemini|Claude|Argon|Opus|Sonnet", line)]
            detail = (model or lines)[-1].replace("|", " ")[:150]
            print(f"{target}|{kind}|{status_state(screen)}|{detail}")
        return
    if not args.args:
        parser.error("target required")
    target, *values = args.args
    if args.command != "tmux-paste" and not re.fullmatch(r"[0-9]+", target):
        parser.error("Terminal window id must be numeric")
    if args.command == "send":
        if len(values) != 1 or len(values[0]) > 1000:
            parser.error("send requires one short command; use a file with paste for prompts/goals")
        if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in values[0]):
            parser.error("send text cannot contain control characters; use guarded key commands")
        screen = control.send(target, values[0])
    elif args.command == "key":
        if not values:
            parser.error("one or more key names required")
        for value in values:
            if value not in KEYS and (len(value) != 1 or ord(value) < 32 or 127 <= ord(value) < 160):
                parser.error("use a named key or one printable character")
        for value in values:
            screen = control.send(target, KEYS.get(value, value))
    elif args.command in ("paste", "tmux-paste"):
        if len(values) != 1:
            parser.error("one prompt file required")
        screen = control.paste(target, checked_file(values[0]), args.command == "tmux-paste")
    else:
        if values:
            parser.error("quit-codex only accepts a window id")
        screen = control.guard(target)
        if status_state(screen) != "idle":
            raise Refused("quit-codex requires an observed empty idle composer")
        control.send(target, "\x03")
        screen = control.send(target, "\x03")
        tty = transport.apple('''on run argv
tell application "Terminal" to return tty of selected tab of window id ((item 1 of argv) as integer)
end run
''', target).strip()
        processes = transport.run(["ps", "-t", tty, "-o", "comm="])
        if re.search(r"(?:^|/)(?:codex|node)(?:\s|$)", processes, re.M):
            raise Refused("Codex exit not confirmed")
    print("\n".join(screen.splitlines()[-14:]))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"mac-term: {exc}", file=sys.stderr)
        sys.exit(5)
