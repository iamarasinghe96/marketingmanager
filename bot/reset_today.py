"""Clear today's daily session so "hi" starts fresh. Run by update.bat.

In live mode an already-published session is kept, so an update can never cause a duplicate post.
"""
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.config import ROOT, load_config


def clear(connection, today, dry_run=True):
    where = "day=?" + ("" if dry_run else " AND state != 'published'")
    # Keep a copy so replying to an old preview can bring that post back after the update.
    connection.execute("CREATE TABLE IF NOT EXISTS cleared_sessions AS SELECT * FROM sessions WHERE 0")
    columns = ",".join(row[1] for row in connection.execute("PRAGMA table_info(cleared_sessions)"))
    connection.execute(f"INSERT INTO cleared_sessions({columns}) SELECT {columns} FROM sessions WHERE {where}", (today,))
    cleared = connection.execute(f"DELETE FROM sessions WHERE {where}", (today,)).rowcount
    return cleared


def main():
    settings, _, _ = load_config()
    path = ROOT / settings["database"]
    if not path.exists():
        print("No sessions to clear.")
        return
    zone = ZoneInfo((settings.get("daily") or {}).get("timezone", "Australia/Sydney"))
    today = datetime.now(timezone.utc).astimezone(zone).date().isoformat()
    connection = sqlite3.connect(path, timeout=20)
    try:
        if not connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone():
            print("No sessions to clear.")
            return
        cleared = clear(connection, today, settings.get("dry_run", True))
        connection.commit()
    finally:
        connection.close()
    print(f"Cleared today's session ({cleared}). Send \"hi\" to start again.")


if __name__ == "__main__":
    main()
