"""Append-only audit log. Each entry carries the hash of the one before it, so editing or
deleting any past entry breaks the chain and the dashboard reports where.

Several processes write to it (the harness and one MCP server per agent session), so every write
takes a cross-process lock and re-reads the last hash from the file before appending.
"""
import contextlib
import hashlib
import json
import os
import time


@contextlib.contextmanager
def file_lock(path, timeout=30):
    lock = path + ".lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:  # a crashed writer left the lock behind
                with contextlib.suppress(FileNotFoundError):
                    os.remove(lock)
            time.sleep(0.005)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.remove(lock)


def _digest(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _last_hash(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos, size = f.tell(), f.tell()
            if not size:
                return "0" * 64
            buf = b""
            while pos > 0 and buf.count(b"\n") < 2:
                step = min(4096, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
            return json.loads(buf.strip().split(b"\n")[-1])["hash"]
    except FileNotFoundError:
        return "0" * 64


class AuditLog:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, **kv):
        with file_lock(self.path):
            entry = {"ts": round(time.time(), 3), **kv, "prev": _last_hash(self.path)}
            entry["hash"] = _digest(entry)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry["hash"]


def verify(path):
    prev, n = "0" * 64, 0
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            e = json.loads(line)
            h = e.pop("hash")
            if e["prev"] != prev or _digest(e) != h:
                return False, n
            prev = h
    return True, n
