from __future__ import annotations
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys

from bot.config import ROOT,load_secrets


def redact(text,store=None):
    # Avoid exception/HTTP debug logs disclosing keys, tokens or signed file URLs.
    secrets = load_secrets()
    values = [v for k,v in secrets.items() if len(v)>5 and k not in {"TELEGRAM_OWNER_CHAT_ID","META_APP_ID"}]
    if store:
        values += [r["value"] for r in store.rows("SELECT value FROM tokens")]
    for value in sorted(set(values),key=len,reverse=True):
        if value:
            text = text.replace(value,"[REDACTED]")
    text = re.sub(r"(?i)(access_token|client_secret|appsecret_proof|input_token|fb_exchange_token)=([^&\s]+)",r"\1=[REDACTED]",text)
    text = re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot[REDACTED]",text)
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(self,store=None):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.store = store

    def format(self,record):
        return redact(super().format(record),self.store)


def configure_logging(store=None):
    folder = ROOT/"logs"
    folder.mkdir(exist_ok=True)
    handler = RotatingFileHandler(folder/"marketing.log",maxBytes=10*1024*1024,backupCount=5,encoding="utf-8")
    handler.setFormatter(RedactingFormatter(store))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stdout:
        stream = logging.StreamHandler()
        stream.setFormatter(RedactingFormatter(store))
        root.addHandler(stream)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self):
        self.handle = None

    def __enter__(self):
        folder = ROOT/"data"
        folder.mkdir(exist_ok=True)
        self.handle = (folder/"bot.lock").open("a+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise AlreadyRunning("Marketing Manager is already running") from None
        return self

    def __exit__(self,*_):
        if self.handle:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle,fcntl.LOCK_UN)
            self.handle.close()


def is_running():
    try:
        with InstanceLock():
            return False
    except AlreadyRunning:
        return True


def below_normal_priority():
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p,ctypes.c_uint)
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(),0x00004000):
            raise OSError("Unable to set BelowNormal process priority")
