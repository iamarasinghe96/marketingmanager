"""A small local status window; no network server or dashboard."""
import argparse
import os
import subprocess
import time
import tkinter as tk
from tkinter import messagebox
from bot.config import ROOT,load_config
from bot.db import Store
from bot.runtime import is_running


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop",action="store_true")
    parser.add_argument("--start-only",action="store_true")
    args = parser.parse_args()
    if args.stop:
        (ROOT/"data").mkdir(exist_ok=True)
        (ROOT/"data"/"stop.request").write_text("stop",encoding="ascii")
        if not args.start_only:
            win = tk.Tk();win.withdraw()
            messagebox.showinfo("Marketing Manager","Stop requested. The bot will finish its current action, save its state and exit. Other processes are untouched.")
            win.destroy()
        return
    already = is_running()
    if not already:
        pythonw = ROOT/".venv"/"Scripts"/"pythonw.exe"
        subprocess.Popen([str(pythonw),"-m","bot"],cwd=ROOT,
                         creationflags=subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS)
    if args.start_only:
        return
    window = tk.Tk()
    window.title("Marketing Manager")
    window.geometry("760x540")
    box = tk.Text(window,wrap="word",font=("Segoe UI",10),padx=16,pady=16)
    box.pack(fill="both",expand=True)
    controls = tk.Frame(window);controls.pack(fill="x",padx=12,pady=10)
    def refresh():
        settings,campaigns,_ = load_config()
        db = Store(ROOT/settings["database"])
        try:
            running = is_running()
            text = ("Already running.\n" if already and running else "") + ("Running\n" if running else "Stopped\n")
            saved = db.get("status_text", "Complete setup first. Connected accounts and next jobs appear when the bot starts.")
            text += saved.removeprefix("Marketing Manager is running\n")
            text += "\nLast heartbeat: " + db.get("heartbeat","not started")
        finally:
            db.close()
        box.configure(state="normal");box.delete("1.0","end");box.insert("1.0",text);box.configure(state="disabled")
    def log():
        folder = ROOT/"logs";folder.mkdir(exist_ok=True)
        target = folder/"marketing.log"
        target.touch(exist_ok=True)
        os.startfile(str(target))
    tk.Button(controls,text="Refresh",command=refresh).pack(side="left")
    tk.Button(controls,text="View log",command=log).pack(side="left",padx=8)
    tk.Button(controls,text="Close",command=window.destroy).pack(side="right")
    window.after(1200,refresh)
    window.mainloop()


if __name__ == "__main__":
    main()
