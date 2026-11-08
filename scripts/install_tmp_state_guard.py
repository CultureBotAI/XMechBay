#!/usr/bin/env python3
"""Install the tmp-state guard as a private, per-user macOS LaunchAgent.

Purpose: run scripts/tmp_state_guard.py every evening (21:30 local, before
tmp_cleaner's 00:00 run) so agent work left in /private/tmp or the per-user
temporary directory is copied out first. tmp-state-guard PR, 2026-11-08.
Inputs: the bundled tmp_state_guard.py and explicit --install/--uninstall.
Outputs: an immutable release copy named by its source hash, a backed-up
LaunchAgent plist and a private 0700 state directory, all under
~/Library/Application Support/XMechBay/tmp-state-guard/ by default.
The default is a read-only plan. Installation requires one fresh observation
from the started job and rolls back without it. Python 3.9+, standard library;
system paths only, so the same plan works on Apple Silicon and Intel Macs.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import time


LABEL = "org.xmechbay.tmp-state-guard"
SCRIPT_NAME = "tmp_state_guard.py"
RUN_AT = {"Hour": 21, "Minute": 30}
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
REPORT_STATUSES = {"running", "ok", "rescued", "unrescued", "incomplete"}


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def launchctl(*args, required=True):
    result = subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, timeout=20, check=False
    )
    if required and result.returncode:
        raise RuntimeError("launchctl " + args[0] + " failed (exit " + str(result.returncode) + ")")
    return result.returncode == 0


def load_guard(source):
    spec = importlib.util.spec_from_file_location("tmp_state_guard_source", str(source))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_locations(source, dest, state):
    """Refuse a destination or state directory that a cleaner would delete."""
    guard = load_guard(source)
    roots = [("tmp", os.path.realpath("/private/tmp"))]
    user_temp = guard.darwin_user_temp_dir() if sys.platform == "darwin" else None
    if user_temp:
        roots.append(("darwin-user-temp", os.path.realpath(user_temp)))
    settings = guard.Settings(roots=roots, dest=str(dest), state_dir=str(state))
    try:
        guard.check_refusal(settings, guard.system_cleaned_roots())
    except guard.Refusal as error:
        raise ValueError(str(error)) from error


def default_python():
    # /usr/bin/python3 exists on every supported macOS release on both CPU
    # architectures; Homebrew prefixes differ (/opt/homebrew vs /usr/local).
    system = Path("/usr/bin/python3")
    return system if system.exists() else Path(sys.executable)


def make_plan(home, root, python, source, dest, notify=False, interval_hours=None,
              excludes=(), top_excludes=()):
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    destination = root / "releases" / digest / SCRIPT_NAME
    state = root / "state"
    plist_path = home / "Library" / "LaunchAgents" / (LABEL + ".plist")
    arguments = [str(python), "-B", str(destination), "--state-dir", str(state), "--dest", str(dest)]
    if notify:
        arguments.append("--notify")
    for pattern in excludes:
        arguments += ["--exclude", pattern]
    for pattern in top_excludes:
        arguments += ["--exclude-top", pattern]
    plist = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "StartCalendarInterval": dict(RUN_AT),
        # A load (install, login) runs once: that is the required fresh
        # observation, and it catches up after a night the Mac was off.
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "Umask": 0o077,
        "EnvironmentVariables": {"PATH": SYSTEM_PATH},
        "StandardOutPath": "/dev/null",
        # The report is the evidence; stderr keeps a crash traceback, if any.
        "StandardErrorPath": str(state / "launchd-stderr.log"),
    }
    if interval_hours:
        plist["StartInterval"] = int(interval_hours) * 3600
    return {
        "label": LABEL, "script_sha256": digest,
        "source": str(source), "script": str(destination), "state_dir": str(state),
        "dest": str(dest), "plist_path": str(plist_path), "root": str(root), "plist": plist,
    }


def validate_owned_plist(path, root):
    if path.is_symlink():
        raise RuntimeError("Refusing a symlink at the LaunchAgent path")
    if not path.exists():
        return None
    content = path.read_bytes()
    old = plistlib.loads(content)
    arguments = old.get("ProgramArguments", [])
    if old.get("Label") != LABEL or len(arguments) < 3:
        raise RuntimeError("Existing LaunchAgent does not match this guard")
    executable = Path(arguments[2])
    try:
        executable.relative_to(root / "releases")
    except ValueError as error:
        raise RuntimeError("Existing LaunchAgent belongs to a different installation") from error
    if executable.name != SCRIPT_NAME:
        raise RuntimeError("Existing LaunchAgent does not run this guard")
    return content


def backup(path, content):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(path.name + ".bak-tmp-state-guard-" + stamp)
    atomic_write(target, content)
    return target


def validate_private_directory(path):
    if path.is_symlink():
        raise RuntimeError("Guard directories must not be symlinks")
    if path.exists():
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise RuntimeError("Guard directories must be owned by this user with mode 0700")


def check_python(python):
    result = subprocess.run([str(python), "-c", "import sys; print(int(sys.version_info >= (3, 9)))"],
                            capture_output=True, timeout=60, check=False)
    if result.returncode or result.stdout.strip() != b"1":
        raise RuntimeError("The selected Python must run and be version 3.9 or later")


def wait_for_report(state, started, timeout=600):
    """A report written by the started job: its observation phase is enough."""
    deadline = time.monotonic() + timeout
    path = state / "latest.json"
    while time.monotonic() < deadline:
        try:
            if path.is_symlink():
                raise RuntimeError("Refusing a symlinked guard report")
            with path.open("rb") as stream:
                data = stream.read(16 * 1024 * 1024 + 1)
            if len(data) <= 16 * 1024 * 1024:
                report = json.loads(data)
                if (isinstance(report, dict) and report.get("schema_version") == 1
                        and report.get("tool") == "tmp-state-guard"
                        and report.get("phase") in {"observed", "complete"}
                        and report.get("status") in REPORT_STATUSES):
                    observed = datetime.fromisoformat(report["observed_at"])
                    if observed.tzinfo is not None and observed >= started:
                        return report
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Started guard produced no fresh observation; setup was rolled back "
                       "(a very large temporary directory may need a longer --wait)")


def install(plan, content, uid, wait=600):
    root = Path(plan["root"])
    plist_path = Path(plan["plist_path"])
    destination = Path(plan["script"])
    state = Path(plan["state_dir"])
    if root.is_symlink() or destination.is_symlink():
        raise RuntimeError("Refusing a symlink at the installation path")
    validate_private_directory(root)
    validate_private_directory(state)
    previous = validate_owned_plist(plist_path, root)
    domain = "gui/" + str(uid)
    target = domain + "/" + LABEL
    # A login session is required. Never start a GUI application to create one.
    launchctl("print", domain)
    loaded = launchctl("print", target, required=False)
    if loaded and previous is None:
        raise RuntimeError("A job with this label is loaded without our owned plist")
    if destination.exists() and destination.read_bytes() != content:
        raise RuntimeError("Immutable release copy does not match its content hash")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not destination.exists():
        atomic_write(destination, content)
    saved = backup(plist_path, previous) if previous is not None else None
    manifest = root / "installation.json"
    if manifest.exists():
        backup(manifest, manifest.read_bytes())
    if loaded:
        launchctl("bootout", target)
    new_loaded = False
    try:
        started = datetime.now(timezone.utc)
        atomic_write(plist_path, plistlib.dumps(plan["plist"]))
        launchctl("bootstrap", domain, str(plist_path))
        new_loaded = True
        report = wait_for_report(state, started, wait)
    except Exception:
        # Preserve the old configuration and restore its previous loaded state.
        if new_loaded:
            launchctl("bootout", target)
        if previous is not None:
            atomic_write(plist_path, previous)
            if loaded:
                launchctl("bootstrap", domain, str(plist_path))
        elif plist_path.exists():
            plist_path.unlink()
        raise
    receipt = {**plan, "installed_at": datetime.now(timezone.utc).isoformat(),
               "backup": str(saved) if saved else None}
    atomic_write(manifest, (json.dumps(receipt, indent=2) + "\n").encode())
    return {"action": "installed", "label": LABEL, "state_dir": plan["state_dir"], "dest": plan["dest"],
            "script_sha256": plan["script_sha256"], "backup": receipt["backup"],
            "first_observed_at": report["observed_at"], "first_phase": report["phase"],
            "initial_status": report["status"], "at_risk": report.get("at_risk", {}).get("entries")}


def uninstall(home, root, uid):
    path = home / "Library" / "LaunchAgents" / (LABEL + ".plist")
    previous = validate_owned_plist(path, root)
    if previous is None:
        return {"action": "already-absent", "label": LABEL}
    saved = backup(path, previous)
    target = "gui/" + str(uid) + "/" + LABEL
    if launchctl("print", target, required=False):
        launchctl("bootout", target)
    path.unlink()
    return {"action": "uninstalled", "label": LABEL, "backup": str(saved),
            "preserved": str(root), "backups_preserved": "copies under the --dest directory are kept"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    parser.add_argument("--notify", action="store_true", help="local macOS notification when a run needs attention")
    parser.add_argument("--root", type=Path, help="private installation directory")
    parser.add_argument("--dest", type=Path, help="backup directory (default ~/agent-state-backups/auto)")
    parser.add_argument("--python", type=Path, help="Python 3.9+ for the job (default /usr/bin/python3)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="extra exclusion passed to the guard (repeatable)")
    parser.add_argument("--exclude-top", action="append", default=[], metavar="GLOB",
                        help="extra top-level exclusion passed to the guard (repeatable)")
    parser.add_argument("--interval-hours", type=int,
                        help="also run every N hours (1-24) in addition to 21:30")
    parser.add_argument("--wait", type=int, default=600, help="seconds to wait for the first observation")
    args = parser.parse_args(argv)
    if args.interval_hours is not None and not 1 <= args.interval_hours <= 24:
        parser.error("--interval-hours must be between 1 and 24")
    if not 10 <= args.wait <= 7200:
        parser.error("--wait must be between 10 and 7200 seconds")
    if args.uninstall and (args.notify or args.exclude or args.exclude_top or args.interval_hours):
        parser.error("--notify, --exclude, --exclude-top and --interval-hours are installation options")
    home = Path.home()
    root = (args.root or home / "Library" / "Application Support" / "XMechBay" / "tmp-state-guard").expanduser().absolute()
    dest = (args.dest or home / "agent-state-backups" / "auto").expanduser().absolute()
    python = (args.python or default_python()).expanduser().absolute()
    if not python.is_file():
        parser.error("--python must identify an existing Python executable")
    if (args.install or args.uninstall) and (sys.platform != "darwin" or os.getuid() == 0):
        parser.error("Installation requires a non-root macOS login user")
    try:
        if args.uninstall:
            result = uninstall(home, root, os.getuid())
        else:
            source = Path(__file__).resolve().with_name(SCRIPT_NAME)
            guard = load_guard(source)
            for pattern in args.exclude + args.exclude_top:
                guard.exclude_glob(pattern)
            check_locations(source, dest, root / "state")
            plan = make_plan(home, root, python, source, dest, args.notify, args.interval_hours,
                             args.exclude, args.exclude_top)
            if args.install:
                check_python(python)
                result = install(plan, source.read_bytes(), os.getuid(), args.wait)
            else:
                result = {"action": "plan", **plan}
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, plistlib.InvalidFileException) as error:
        print("tmp-state guard setup failed: " + str(error), file=sys.stderr)
        return 1
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))


if __name__ == "__main__":
    sys.exit(main())
