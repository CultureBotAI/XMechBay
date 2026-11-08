#!/usr/bin/env python3
"""Rename an Antigravity (agy) session in conversation_summaries.db and its macOS Terminal tab.

Purpose: Update the session title in ~/.gemini/antigravity-cli/conversation_summaries.db
  (both the `conversation_summaries.title` column, the `raw_summary` protobuf field 1,
  and the `conversations_fts` FTS index) with a backup first, and set the macOS Terminal.app
  tab custom title on the specified TTY.
Inputs: --conversation-id <uuid>, --tty /dev/ttysNNN, --title <new-title>
Outputs: Updated conversation_summaries.db (with backup) and Terminal tab custom title.
Serves: User request 2026-11-07 to rename the LBNL library session and Terminal tab to "lbnl library".
Date: 2026-11-07
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SUMMARIES_DB = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
BACKUP_SUFFIX = ".bak-rename-lbnl-library-20261107"


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val >= 0x80:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val)
    return bytes(out)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7


def replace_proto_field1_string(raw: bytes, new_str: str) -> bytes:
    """Replace field 1 (string title) in CascadeTrajectorySummary protobuf."""
    if not raw or raw[0] != 0x0A:
        return raw
    old_len, pos = _read_varint(raw, 1)
    rest = raw[pos + old_len :]
    payload = new_str.encode("utf-8")
    return b"\x0a" + _encode_varint(len(payload)) + payload + rest


def rename_session(conversation_id: str, new_title: str) -> tuple[str, str]:
    bak = SUMMARIES_DB.with_name(SUMMARIES_DB.name + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy2(SUMMARIES_DB, bak)
    conn = sqlite3.connect(str(SUMMARIES_DB), timeout=10.0)
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT title, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Conversation {conversation_id} not found in {SUMMARIES_DB}")
        old_title = row[0] or ""
        raw = bytes(row[1] or b"")
        new_raw = replace_proto_field1_string(raw, new_title)
        cur.execute(
            "UPDATE conversation_summaries SET title = ?, raw_summary = ? WHERE conversation_id = ?",
            (new_title, new_raw, conversation_id),
        )
        cur.execute(
            "UPDATE conversations_fts SET title = ? WHERE conversation_id = ? AND chunk_index = -1",
            (new_title, conversation_id),
        )
        conn.commit()
        verify = cur.execute(
            "SELECT title FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        return old_title, verify
    finally:
        conn.close()


def rename_terminal_tab(tty: str, new_title: str) -> str:
    esc_title = new_title.replace("\\", "\\\\").replace('"', '\\"')
    esc_tty = tty.replace("\\", "\\\\").replace('"', '\\"')
    script = f"""
tell application "Terminal"
  repeat with i from 1 to (count of windows)
    try
      set w to window i
      repeat with j from 1 to (count of tabs of w)
        set t to tab j of w
        if tty of t is "{esc_tty}" then
          set custom title of t to "{esc_title}"
          set title displays custom title of t to true
          set title displays shell path of t to false
          set title displays device name of t to false
          set title displays window size of t to false
          return (id of w as string) & ":" & j & "|" & (custom title of t) & "|" & (name of w)
        end if
      end repeat
    end try
  end repeat
end tell
"""
    return subprocess.check_output(["osascript", "-e", script], text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--conversation-id",
        default="d077cb42-862d-4f58-9843-da6a3dc9bc11",
        help="Antigravity conversation UUID",
    )
    parser.add_argument(
        "--tty",
        default="/dev/ttys020",
        help="Terminal tab TTY path",
    )
    parser.add_argument(
        "--title",
        default="lbnl library",
        help="New title for session and Terminal tab",
    )
    parser.add_argument(
        "--persist-after-turn",
        action="store_true",
        help="Also spawn a short background pass to re-apply the session title after the current turn settles",
    )
    parser.add_argument(
        "--delayed-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.delayed_worker:
        for delay in (2.0, 4.0, 6.0):
            time.sleep(delay)
            try:
                rename_session(args.conversation_id, args.title)
                rename_terminal_tab(args.tty, args.title)
            except Exception:
                pass
        return 0

    old_title, verified_title = rename_session(args.conversation_id, args.title)
    tab_info = rename_terminal_tab(args.tty, args.title)
    if args.persist_after_turn:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--conversation-id",
                args.conversation_id,
                "--tty",
                args.tty,
                "--title",
                args.title,
                "--delayed-worker",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    print(f"session_id={args.conversation_id} old_title={old_title!r} verified_title={verified_title!r}")
    print(f"terminal_tab={tab_info}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
