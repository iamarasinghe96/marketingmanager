"""Clear today's daily session so "hi" starts fresh. Run by update.bat.

In live mode an already-published session is kept, so an update can never cause a duplicate post.
"""
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.config import ROOT, load_config


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
        sql = "DELETE FROM sessions WHERE day=?"
        if not settings.get("dry_run", True):
            sql += " AND state != 'published'"
        cleared = connection.execute(sql, (today,)).rowcount
        connection.commit()
    finally:
        connection.close()
    print(f"Cleared today's session ({cleared}). Send \"hi\" to start again.")


if __name__ == "__main__":
    main()
