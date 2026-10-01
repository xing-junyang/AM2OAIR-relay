#!/usr/bin/env python3
"""SQLite snapshot backup, stopped-service restore, and metadata cleanup.

Uses SQLite's online backup API, never a raw copy of a live WAL database.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def database_path() -> Path:
    url = os.environ.get("AM2OAIR_RELAY_DATABASE_URL") or "sqlite:////data/relay.db"
    if not url.startswith("sqlite:///"):
        raise ValueError("Only file-backed SQLite is supported")
    return Path(url.removeprefix("sqlite:///")).resolve()


def validate(path: Path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Invalid backup")
        connection.execute("SELECT request_id FROM request_logs LIMIT 1")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    targets = backup.add_mutually_exclusive_group(required=True)
    targets.add_argument("--output", type=Path)
    targets.add_argument("--stdout", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("--stdin", action="store_true", required=True)
    restore.add_argument(
        "--service-stopped",
        action="store_true",
        required=True,
        help="The relay service must be stopped before restore",
    )
    clean = commands.add_parser("clear")
    bounds = clean.add_mutually_exclusive_group(required=True)
    bounds.add_argument("--before", help="Timezone-aware ISO datetime, exclusive")
    bounds.add_argument("--all", action="store_true")
    args = parser.parse_args()
    path = database_path()
    if args.command == "backup":
        if args.output and args.output.resolve() == path:
            raise ValueError("Backup target must differ from live database")
        with tempfile.TemporaryDirectory() as directory:
            destination = args.output or Path(directory) / "snapshot.db"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise ValueError("Backup target already exists")
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source:
                with sqlite3.connect(destination) as target:
                    source.backup(target)
            validate(destination)
            if args.stdout:
                with destination.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        sys.stdout.buffer.write(chunk)
            else:
                print("Backup completed")
    elif args.command == "restore":
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".restore", delete=False)
        temporary = Path(handle.name)
        try:
            with handle:
                while chunk := sys.stdin.buffer.read(1024 * 1024):
                    handle.write(chunk)
            validate(temporary)
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            temporary.replace(path)
            print("Restore completed")
        finally:
            temporary.unlink(missing_ok=True)
    else:
        before = None
        if args.before:
            parsed = datetime.fromisoformat(args.before)
            if parsed.tzinfo is None:
                raise ValueError("Cleanup cutoff must include timezone")
            before = parsed.timestamp()
        with sqlite3.connect(path, timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA secure_delete=ON")
            deleted = connection.execute(
                "DELETE FROM request_logs"
                + (" WHERE requested_at < ?" if before is not None else ""),
                (before,) if before is not None else (),
            ).rowcount
        print(f"Removed {deleted} metadata records; statistics reflect the remaining history")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (sqlite3.Error, ValueError, OSError):
        print(
            "Database operation failed; check input, permissions and service state", file=sys.stderr
        )
        raise SystemExit(1) from None
