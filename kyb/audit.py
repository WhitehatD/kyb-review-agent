"""Append-only audit log. Each entry carries the hash of the one before it, so editing or
deleting any past entry breaks the chain and `kyb audit verify` reports where."""
import hashlib
import json
import threading
import time

_lock = threading.Lock()


def _digest(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AuditLog:
    def __init__(self, path):
        self.path = path
        self.prev = "0" * 64
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    self.prev = json.loads(line)["hash"]
        except FileNotFoundError:
            pass

    def write(self, **kv):
        with _lock:
            entry = {"ts": round(time.time(), 3), **kv, "prev": self.prev}
            entry["hash"] = _digest(entry)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.prev = entry["hash"]
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
