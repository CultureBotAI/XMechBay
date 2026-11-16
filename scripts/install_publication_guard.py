#!/usr/bin/env python3
"""Install a pinned, clone-local pre-push guard (2026-11-15).

Inputs: expected public GitHub repository ID/name and approved root commit.
Outputs: Git-local guard, scanner, hook, contract and a configuration backup.
Refuses conflicting hooks or a changed existing installation. No network calls.
Local hooks can be bypassed with no-verify, hook/config edits or other clients;
they do not provide server-side authorization. Run explicitly in every clone.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import publication_guard as guard


def install(root: Path, contract: dict, source: Path, deny_patterns: Path | None = None) -> bool:
    guard.validate_contract(contract)
    contract = dict(contract)
    deny_data = None
    if deny_patterns is not None:
        resolved = deny_patterns.resolve()
        checkout = root.resolve()
        lexical = Path(os.path.abspath(deny_patterns))
        if resolved == checkout or checkout in resolved.parents or lexical == checkout or checkout in lexical.parents:
            raise guard.Refusal("External deny patterns must be outside the publication checkout.")
        deny_data = guard.read_deny_pattern_data(deny_patterns)
        digest = hashlib.sha256(deny_data).hexdigest()
        if contract.get("deny_patterns_sha256", digest) != digest:
            raise guard.Refusal("External deny patterns differ from the supplied contract.")
        contract["deny_patterns_sha256"] = digest
    elif "deny_patterns_sha256" in contract:
        raise guard.Refusal("The configured external deny-pattern payload is required for installation.")
    guard.verify_history_available(root)
    if guard.git(root, "cat-file", "-t", contract["clean_root"]).strip() != b"commit" or guard.git(root, "rev-list", "--parents", "-n", "1", contract["clean_root"]).decode().split() != [contract["clean_root"]]:
        raise guard.Refusal("The approved root must be a locally available parentless commit.")
    common = Path(os.fsdecode(guard.git(root, "rev-parse", "--git-common-dir").strip()))
    if not common.is_absolute():
        common = (root / common).resolve()
    destination = common / "xmechbay-publication"
    hooks = destination / "hooks"
    current_hooks = guard.git(root, "config", "--get", "core.hooksPath", optional=True).decode().strip()
    payloads = {
        "publication_guard.py": (source / "scripts/publication_guard.py").read_bytes(),
        "check_public_content.py": (source / "scripts/check_public_content.py").read_bytes(),
        "hooks/pre-push": (source / ".githooks/pre-push").read_bytes(),
        "contract.json": (json.dumps(contract, indent=2, sort_keys=True) + "\n").encode(),
    }
    if deny_data is not None:
        payloads[guard.DENY_PATTERN_FILE] = deny_data
    if destination.exists():
        deny_target = destination / guard.DENY_PATTERN_FILE
        unexpected_deny_file = deny_data is None and (deny_target.exists() or deny_target.is_symlink())
        if not unexpected_deny_file and current_hooks == str(hooks) and all((destination / name).is_file() and not (destination / name).is_symlink() and (destination / name).read_bytes() == data for name, data in payloads.items()) and os.access(hooks / "pre-push", os.X_OK):
            return False
        raise guard.Refusal("Existing guard installation differs; review it before replacement.")
    if current_hooks:
        raise guard.Refusal("An existing hooksPath must be reviewed before installation.")
    default_hooks = common / "hooks"
    if default_hooks.exists() and any(not item.name.endswith(".sample") for item in default_hooks.iterdir()):
        raise guard.Refusal("Existing hooks must be reviewed; installation will not replace them.")
    config = common / "config"
    if not config.is_file() or config.is_symlink():
        raise guard.Refusal("Local Git configuration is unavailable or unsupported.")
    stage = Path(tempfile.mkdtemp(prefix="publication-install-", dir=common))
    try:
        (stage / "hooks").mkdir(mode=0o700)
        for name, data in payloads.items():
            target = stage / name
            target.write_bytes(data)
            target.chmod(0o700 if name == "hooks/pre-push" else 0o600)
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = config.with_name(config.name + ".bak-publication-" + timestamp)
    with backup.open("xb") as handle:
        handle.write(config.read_bytes())
    backup.chmod(0o600)
    guard.git(root, "config", "--local", "core.hooksPath", str(hooks))
    if guard.git(root, "config", "--get", "core.hooksPath").decode().strip() != str(hooks):
        raise guard.Refusal("Installed hook is not effective; inspect the retained configuration backup.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-id", type=int, required=True)
    parser.add_argument("--clean-root", required=True)
    parser.add_argument("--deny-patterns", type=Path, help="External UTF-8 regex file to pin locally; must be outside the checkout")
    args = parser.parse_args(argv)
    try:
        root = Path(os.fsdecode(guard.git(Path.cwd(), "rev-parse", "--show-toplevel").strip()))
        changed = install(root, {"schema_version": 1, "repository_id": args.repository_id, "repository_full_name": args.repository, "clean_root": args.clean_root, "remote_name": "origin"}, Path(__file__).resolve().parents[1], args.deny_patterns)
    except (guard.Refusal, OSError, ValueError, UnicodeError) as error:
        message = str(error) if isinstance(error, guard.Refusal) else "Installation failed; paths and values withheld."
        print("Publication guard not installed: " + message, file=sys.stderr)
        return 1
    print("Publication guard installed; prior Git configuration backed up beside config." if changed else "Identical publication guard already installed.")
    print("Install separately in every clone. Hook bypasses and other upload clients are outside this local guard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
