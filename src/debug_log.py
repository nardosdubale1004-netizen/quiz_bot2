# src/debug_log.py
"""
File-backed deep debug logger for the tournament subsystem.

WHY THIS EXISTS: the local CLI keeps an active prompt_toolkit prompt_async()
session open at the "Choice > " prompt almost the entire time the bot is
running. prompt_toolkit's patch_stdout(), which wraps that prompt, is known
to sometimes drop or badly interleave plain print() output coming from OTHER
asyncio tasks/threads while the prompt is idle-waiting for input -- exactly
the situation the tournament watcher loop and finalize_tournament_round run
under. That means print()-only debugging of "why did the tournament silently
stop" can look like NOTHING happened at all, even though code executed (or
even raised an exception).

This module writes every deep-debug line BOTH to stdout (so `docker logs` /
attached terminals still show it when possible) AND appends it to a plain
file on disk, which can never be swallowed by a terminal UI library.

Tail it live with:
    docker exec -it quiz_bot2 tail -f logs/tournament_debug.log
"""
import os
import traceback
from datetime import datetime, timezone

_LOG_PATH = "logs/tournament_debug.log"


def dlog(message: str):
    """Write a timestamped line to stdout AND to logs/tournament_debug.log."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{ts} UTC] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(_LOG_PATH) or ".", exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # Never let logging itself crash the caller.
        pass


def dlog_exception(context: str, exc: Exception):
    """Write a full traceback for `exc`, tagged with `context`, to stdout + file."""
    tb_str = traceback.format_exc()
    dlog(f"[EXCEPTION] {context}: {exc}")
    dlog(f"[EXCEPTION-TRACEBACK] {context}:\n{tb_str}")