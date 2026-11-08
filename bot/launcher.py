"""The Marketing Manager window. The bot runs while this window is open; closing it stops the bot.

No network server or dashboard. Stopping is graceful (a stop request the bot picks up after its current
action); no process is ever killed, so other programs on this machine are never touched.
"""
import argparse
import os
import subprocess
import time
import tkinter as tk
from bot.config import ROOT,load_config
from bot.db import Store
from bot.runtime import is_running

STOP = ROOT/"data"/"stop.request"


def request_stop():
    (ROOT/"data").mkdir(exist_ok=True)
    STOP.write_text("stop",encoding="ascii")


def start_bot():
    # Reopening the window cancels a stop that the bot has not acted on yet.
    STOP.unlink(missing_ok=True)
    if not is_running():
        pythonw = ROOT/".venv"/"Scripts"/"pythonw.exe"
        subprocess.Popen([str(pythonw),"-m","bot"],cwd=ROOT,
                         creationflags=subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop",action="store_true",help="ask a running bot to stop (used by update)")
    parser.add_argument("--start-only",action="store_true",help="start without the window (older update scripts)")
    args = parser.parse_args()
    if args.stop:
        request_stop()
        return
    start_bot()
    if args.start_only:
        return
    last_start = [time.monotonic()]
    window = tk.Tk()
    window.title("Marketing Manager - keep this window open (closing it stops the bot)")
    window.geometry("760x540")
    box = tk.Text(window,wrap="word",font=("Segoe UI",10),padx=16,pady=16)
    box.pack(fill="both",expand=True)
    controls = tk.Frame(window);controls.pack(fill="x",padx=12,pady=10)

    def refresh():
        settings,campaigns,_ = load_config()
        running = is_running()
        if not running and time.monotonic() - last_start[0] > 60:
            # The window is open, so the bot should be running: start it again (at most once a minute).
            last_start[0] = time.monotonic()
            start_bot()
        db = Store(ROOT/settings["database"])
        try:
            text = ("Running" if running else "Starting…") + "\nClose this window to stop the bot.\n"
            saved = db.get("status_text", "Complete setup first. Connected accounts and next jobs appear when the bot starts.")
            text += saved.removeprefix("Marketing Manager is running\n")
            text += "\nLast heartbeat: " + db.get("heartbeat","not started")
        finally:
            db.close()
        box.configure(state="normal");box.delete("1.0","end");box.insert("1.0",text);box.configure(state="disabled")
        window.after(5000,refresh)

    def log():
        folder = ROOT/"logs";folder.mkdir(exist_ok=True)
        target = folder/"marketing.log"
        target.touch(exist_ok=True)
        os.startfile(str(target))

    def close():
        request_stop()
        window.destroy()

    tk.Button(controls,text="View log",command=log).pack(side="left")
    tk.Button(controls,text="Close and stop the bot",command=close).pack(side="right")
    window.protocol("WM_DELETE_WINDOW",close)
    window.after(1200,refresh)
    window.mainloop()


if __name__ == "__main__":
    main()
