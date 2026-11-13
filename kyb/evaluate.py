"""Offline eval: run the agent over the labelled cases and check the stage gate.

    python -m kyb.evaluate --model deepseek/deepseek-v4-flash
    python -m kyb.evaluate --model anthropic/claude-haiku-5.5 --cases K19,K23

A hard spend cap (--max-usd, default 1.00) stops the whole run, not just one case.
"""
import argparse
import concurrent.futures as cf
import json
import os
import threading
import time

from .agent import POLICY, ROOT, review
from .audit import AuditLog


class Ledger:
    def __init__(self, cap):
        self.cap, self.spent, self.lock = cap, 0.0, threading.Lock()

    def add(self, usd):
        with self.lock:
            self.spent += usd
            if self.spent > self.cap:
                raise SystemExit(f"spend cap reached: ${self.spent:.3f} > ${self.cap:.2f}")


def score(records, cases, key="verdict"):
    """key="verdict" scores what the harness let through; key="raw_verdict" scores the model alone."""
    label = {c["id"]: c["label"] for c in cases}
    rows, tp, fp, fn = [], 0, 0, 0
    for r in records:
        exp, got = label[r["case"]], r[key]
        got = {"decision": got.get("decision"), "flags": [f for f in got.get("flags", []) if f in POLICY["flags"]]}
        unsafe = got["decision"] == "approve" and exp["decision"] != "approve"
        ef, gf = set(exp["flags"]), set(got["flags"])
        tp += len(ef & gf); fp += len(gf - ef); fn += len(ef - gf)
        rows.append({"case": r["case"], "expected": exp["decision"], "got": got["decision"],
                     "correct": got["decision"] == exp["decision"], "unsafe_approval": unsafe,
                     "missing_flags": sorted(ef - gf), "extra_flags": sorted(gf - ef),
                     "cost_usd": r["cost_usd"], "tool_calls": r["tool_calls"], "overrides": r["overrides"]})
    n = len(rows)
    esc = [x for x in rows if x["expected"] == "escalate"]
    summary = {
        "cases": n,
        "decision_accuracy": round(sum(x["correct"] for x in rows) / n, 3),
        "unsafe_approvals": sum(x["unsafe_approval"] for x in rows),
        "escalation_recall": round(sum(x["got"] == "escalate" for x in esc) / len(esc), 3) if esc else None,
        "flag_precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "flag_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "overrides": sum(bool(x["overrides"]) for x in rows),
        "cost_usd": round(sum(x["cost_usd"] for x in rows), 4),
        "avg_tool_calls": round(sum(x["tool_calls"] for x in rows) / n, 1),
    }
    gate = POLICY["stages"]["shadow"]["gate_to_next"]
    checks = {
        "min_cases": n >= gate["min_cases"],
        "decision_accuracy": summary["decision_accuracy"] >= gate["decision_accuracy"],
        "unsafe_approvals": summary["unsafe_approvals"] <= gate["unsafe_approvals"],
        "escalation_recall": (summary["escalation_recall"] or 0) >= gate["escalation_recall"],
    }
    summary["gate"] = {"from": "shadow", "to": "suggest", "checks": checks, "passed": all(checks.values())}
    return summary, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek/deepseek-v4-flash")
    ap.add_argument("--stage", default="shadow")
    ap.add_argument("--cases", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-usd", type=float, default=1.0)
    a = ap.parse_args()

    cases = json.load(open(os.path.join(ROOT, "data", "cases.json"), encoding="utf-8"))
    if a.cases:
        keep = set(a.cases.split(","))
        cases = [c for c in cases if c["id"] in keep]
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    audit, ledger = AuditLog(os.path.join(ROOT, "results", "audit.jsonl")), Ledger(a.max_usd)

    with cf.ThreadPoolExecutor(a.workers) as ex:
        records = list(ex.map(lambda c: review(c, a.stage, a.model, audit, ledger), cases))
    summary, rows = score(records, cases)
    alone, _ = score(records, cases, key="raw_verdict")
    summary["model_alone"] = {k: alone[k] for k in ("decision_accuracy", "unsafe_approvals", "escalation_recall")}
    summary["model_alone"]["gate_passed"] = alone["gate"]["passed"]
    summary.update(model=a.model, stage=a.stage, at=time.strftime("%Y-%m-%d %H:%M"))

    tag = a.model.split("/")[-1]
    out = {"summary": summary, "rows": rows, "records": records}
    json.dump(out, open(os.path.join(ROOT, "results", f"eval-{tag}.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    with open(os.path.join(ROOT, "results", "history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")

    for x in rows:
        mark = "ok " if x["correct"] else ("UNSAFE" if x["unsafe_approval"] else "miss")
        print(f"{x['case']}  {mark:6} expected {x['expected']:12} got {x['got']:12} "
              f"${x['cost_usd']:.4f}  {x['tool_calls']:2} calls  "
              + (f"missing {x['missing_flags']} " if x["missing_flags"] else "")
              + (f"extra {x['extra_flags']}" if x["extra_flags"] else ""))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
