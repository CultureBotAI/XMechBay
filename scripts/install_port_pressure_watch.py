#!/usr/bin/env python3
"""Install a private, per-user macOS port-pressure LaunchAgent.

Purpose: persistent local warning for the port-pressure prevention PR; 2026-10-27.
Inputs: bundled check_port_pressure.py, explicit install/uninstall CLI options.
Outputs: immutable script copy, backed-up LaunchAgent plist, private state path.
Default is a read-only plan. No session input, signals, credentials or network.
Compatible with Python 3.9+ independently of other repository tools.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import time


LABEL = "org.xmechbay.port-pressure"


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


def make_plan(home, root, python, source, notify=False, interval=60):
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    destination = root / "releases" / digest / "check_port_pressure.py"
    state = root / "state"
    plist_path = home / "Library" / "LaunchAgents" / (LABEL + ".plist")
    arguments = [str(python), "-B", str(destination), "--state-dir", str(state)]
    if notify:
        arguments.append("--notify")
    plist = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "StartInterval": interval,
        "ProcessType": "Background",
        "Umask": 0o077,
        # Structured state is bounded by the monitor; avoid unbounded text logs.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    return {
        "label": LABEL, "script_sha256": digest,
        "source": str(source), "script": str(destination), "state_dir": str(state),
        "plist_path": str(plist_path), "root": str(root), "plist": plist,
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
        raise RuntimeError("Existing LaunchAgent does not match this monitor")
    executable = Path(arguments[2])
    try:
        executable.relative_to(root / "releases")
    except ValueError as error:
        raise RuntimeError("Existing LaunchAgent belongs to a different installation") from error
    if executable.name != "check_port_pressure.py":
        raise RuntimeError("Existing LaunchAgent does not run this monitor")
    return content


def backup(path, content):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(path.name + ".bak-port-pressure-" + stamp)
    atomic_write(target, content)
    return target


def validate_private_directory(path):
    if path.is_symlink():
        raise RuntimeError("Monitor directories must not be symlinks")
    if path.exists():
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise RuntimeError("Monitor directories must be owned by this user with mode 0700")


def wait_for_report(state, started, timeout=45):
    deadline = time.monotonic() + timeout
    path = state / "latest.json"
    while time.monotonic() < deadline:
        try:
            if path.is_symlink():
                raise RuntimeError("Refusing a symlinked health report")
            with path.open("rb") as stream:
                data = stream.read(1024 * 1024 + 1)
            if len(data) <= 1024 * 1024:
                report = json.loads(data)
                if (isinstance(report, dict) and report.get("schema_version") == 1
                        and report.get("status") in {"ok", "warning", "critical", "unknown"}):
                    observed = datetime.fromisoformat(report["observed_at"])
                    if observed.tzinfo is not None and observed >= started:
                        return report
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Started monitor produced no fresh durable report; setup was rolled back")


def install(plan, content, uid):
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
        report = wait_for_report(state, started)
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
    return {"action": "installed", "label": LABEL, "state_dir": plan["state_dir"],
            "script_sha256": plan["script_sha256"], "backup": receipt["backup"],
            "first_observed_at": report["observed_at"], "initial_status": report["status"],
            "host_status": report.get("host", {}).get("status", "unknown"),
            "notification": report.get("notification", {})}


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
            "preserved": str(root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    parser.add_argument("--notify", action="store_true", help="Enable best-effort local macOS notifications")
    parser.add_argument("--root", type=Path, help="Private installation directory")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if not 30 <= args.interval <= 3600:
        parser.error("--interval must be between 30 and 3600 seconds")
    if args.uninstall and args.notify:
        parser.error("--notify is an installation option")
    home = Path.home()
    root = (args.root or home / "Library" / "Application Support" / "XMechBay" / "port-pressure").expanduser().absolute()
    python = args.python.expanduser().absolute()
    if not python.is_file():
        parser.error("--python must identify an existing Python executable")
    if (args.install or args.uninstall) and (sys.platform != "darwin" or os.getuid() == 0):
        parser.error("Installation requires a non-root macOS login user")
    try:
        if args.uninstall:
            result = uninstall(home, root, os.getuid())
        else:
            source = Path(__file__).resolve().with_name("check_port_pressure.py")
            plan = make_plan(home, root, python, source, args.notify, args.interval)
            result = install(plan, source.read_bytes(), os.getuid()) if args.install else {"action": "plan", **plan}
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, plistlib.InvalidFileException) as error:
        print("Port-pressure watch setup failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
