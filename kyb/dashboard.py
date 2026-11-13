"""Operator dashboard. A stdlib HTTP server, no build step.

    python -m kyb.dashboard            # http://127.0.0.1:8090

Reads the latest eval file per model and the audit log. In suggest stage the Operator records
their own decision for each case; those decisions are the live agreement the suggest -> execute
gate is measured on, and each one is written to the hash-chained audit log.
"""
import glob
import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .agent import POLICY, ROOT
from .audit import AuditLog, verify

RESULTS = os.path.join(ROOT, "results")
DECISIONS = os.path.join(RESULTS, "operator_decisions.json")
E = html.escape


def evals():
    out = {}
    for p in sorted(glob.glob(os.path.join(RESULTS, "eval-*.json"))):
        d = json.load(open(p, encoding="utf-8"))
        out[d["summary"]["model"]] = d
    return out


def decisions():
    return json.load(open(DECISIONS, encoding="utf-8")) if os.path.exists(DECISIONS) else {}


CSS = """body{font:14px/1.45 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
header{background:#1d2330;color:#fff;padding:14px 24px;display:flex;gap:24px;align-items:baseline}
header a{color:#c9d1e0;text-decoration:none}main{padding:20px 24px;max-width:1200px}
table{border-collapse:collapse;width:100%;background:#fff}td,th{padding:7px 10px;border-bottom:1px solid #e6e8ec;text-align:left;vertical-align:top}
th{font-weight:600;color:#5b6475;font-size:12px;text-transform:uppercase}.card{background:#fff;border:1px solid #e6e8ec;border-radius:6px;padding:14px 16px;margin-bottom:16px}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.approve{background:#e3f5e9;color:#17663a}.request_info{background:#fff4d6;color:#7a5600}.escalate{background:#fde4e4;color:#9b1c1c}
.flag{background:#eef0f4;color:#3b4456;margin:0 3px 3px 0}.ok{color:#17663a}.bad{color:#9b1c1c}
.kpis{display:flex;gap:12px;flex-wrap:wrap}.kpi{background:#fff;border:1px solid #e6e8ec;border-radius:6px;padding:10px 14px;min-width:130px}
.kpi b{display:block;font-size:20px}pre{white-space:pre-wrap;background:#f1f3f6;padding:8px;border-radius:4px;font-size:12px}
button{padding:6px 12px;border:1px solid #c7ccd6;background:#fff;border-radius:4px;cursor:pointer}button:hover{background:#eef0f4}"""


def page(title, body, model):
    nav = " ".join(f'<a href="/?model={E(m)}">{E(m.split("/")[-1])}</a>' for m in evals())
    return (f"<!doctype html><meta charset=utf-8><title>{E(title)}</title><style>{CSS}</style>"
            f"<header><b>KYB review agent</b><span>Operator view</span>{nav}<a href='/audit'>audit log</a></header>"
            f"<main>{body}</main>").encode()


def pill(d):
    return f'<span class="pill {E(d)}">{E(d.replace("_", " "))}</span>'


def queue(model):
    ev = evals()
    if not ev:
        return page("No results", "<p>No eval results yet. Run <code>python -m kyb.evaluate</code>.</p>", model)
    model = model if model in ev else next(iter(ev))
    d, ops = ev[model], decisions()
    s = d["summary"]
    rows = {r["case"]: r for r in d["rows"]}
    agree = [ops[c] == rec["verdict"]["decision"] for c, rec in ((r["case"], r) for r in d["records"]) if c in ops]
    live = POLICY["stages"]["suggest"]["gate_to_next"]
    gate = s["gate"]
    kpis = [("Decision accuracy", f'{s["decision_accuracy"]:.0%}'), ("Unsafe approvals", s["unsafe_approvals"]),
            ("Escalation recall", f'{s["escalation_recall"]:.0%}'), ("Flag precision", s["flag_precision"]),
            ("Spend", f'${s["cost_usd"]:.3f}'), ("Avg tool calls", s["avg_tool_calls"]),
            ("Live agreement", f'{sum(agree)}/{len(agree)}' if agree else "none yet")]
    body = [f"<h2>{E(model)} <small>stage: {E(s['stage'])}</small></h2>",
            '<div class="kpis">' + "".join(f'<div class="kpi">{E(k)}<b>{E(str(v))}</b></div>' for k, v in kpis) + "</div>",
            f'<div class="card"><b>Gate {E(gate["from"])} → {E(gate["to"])}:</b> '
            + " · ".join(f'<span class="{"ok" if v else "bad"}">{E(k)} {"✓" if v else "✗"}</span>' for k, v in gate["checks"].items())
            + f' — <b class="{"ok" if gate["passed"] else "bad"}">{"passed" if gate["passed"] else "not passed"}</b>'
            + f'<br><small>Next gate (suggest → execute) needs {live["min_live_cases"]} live Operator decisions at '
              f'{live["live_agreement"]:.0%} agreement with 0 unsafe approvals.</small></div>',
            "<table><tr><th>Case</th><th>Company</th><th>Agent</th><th>Flags</th><th>Label</th><th>Operator</th><th>Cost</th></tr>"]
    for rec in d["records"]:
        r, v = rows[rec["case"]], rec["verdict"]
        mark = "" if r["correct"] else (' <b class="bad">UNSAFE</b>' if r["unsafe_approval"] else ' <b class="bad">differs</b>')
        body.append(f'<tr><td><a href="/case/{E(rec["case"])}?model={E(model)}">{E(rec["case"])}</a></td>'
                    f'<td>{E(rec["company"])}</td><td>{pill(v["decision"])}</td>'
                    f'<td>{"".join(f"<span class=\'pill flag\'>{E(f)}</span>" for f in v["flags"])}</td>'
                    f'<td>{pill(r["expected"])}{mark}</td><td>{pill(ops[rec["case"]]) if rec["case"] in ops else "–"}</td>'
                    f'<td>${rec["cost_usd"]:.4f}</td></tr>')
    body.append("</table>")
    return page("Queue", "".join(body), model)


def case_view(cid, model):
    d = evals().get(model)
    rec = next((r for r in (d or {}).get("records", []) if r["case"] == cid), None)
    if not rec:
        return page("Not found", "<p>Case not found.</p>", model)
    v, ops = rec["verdict"], decisions()
    ev = "".join(f"<li><b>{E(str(x.get('source', '')))}</b>: {E(str(x.get('detail', '')))}</li>" for x in v.get("evidence", []))
    trace = "".join(f"<tr><td>{i}</td><td>{E(t['tool'])}</td><td><code>{E(json.dumps(t['args'], ensure_ascii=False))}</code></td>"
                    f"<td><pre>{E(t['result'])}</pre></td></tr>" for i, t in enumerate(rec["trace"], 1))
    btns = "".join(f'<button name="decision" value="{d_}">{d_.replace("_", " ")}</button> '
                   for d_ in ("approve", "request_info", "escalate"))
    body = (f'<p><a href="/?model={E(model)}">← queue</a></p><h2>{E(cid)} · {E(rec["company"])}</h2>'
            f'<div class="card">Agent recommends {pill(v["decision"])} · risk <b>{E(v.get("risk", ""))}</b> · '
            f'{rec["tool_calls"]} tool calls · ${rec["cost_usd"]:.4f} · {E(rec["outcome"])}'
            f'<p>{E(v.get("reasons", ""))}</p><ul>{ev}</ul>'
            + (f'<p class="bad">Harness overrides: {E("; ".join(rec["overrides"]))}</p>' if rec["overrides"] else "")
            + f'</div><div class="card"><b>Your decision</b> '
              f'{"(recorded: " + pill(ops[cid]) + ")" if cid in ops else ""}'
              f'<form method="post" action="/decide?case={E(cid)}&model={E(model)}">{btns}</form></div>'
              f'<h3>Trace</h3><table><tr><th>#</th><th>Tool</th><th>Args</th><th>Result (truncated)</th></tr>{trace}</table>')
    return page(cid, body, model)


def audit_view():
    path = os.path.join(RESULTS, "audit.jsonl")
    if not os.path.exists(path):
        return page("Audit", "<p>No audit log yet.</p>", "")
    ok, n = verify(path)
    lines = open(path, encoding="utf-8").read().splitlines()[-200:]
    rows = "".join(f"<tr><td>{E(e.get('run', ''))}</td><td>{E(e['event'])}</td>"
                   f"<td><code>{E(json.dumps({k: v for k, v in e.items() if k not in ('run', 'event', 'prev', 'hash', 'ts')}, ensure_ascii=False)[:200])}</code></td>"
                   f"<td><code>{e['hash'][:10]}</code></td></tr>" for e in map(json.loads, reversed(lines)))
    status = f'<b class="ok">chain intact ({n} entries)</b>' if ok else f'<b class="bad">chain broken at entry {n}</b>'
    return page("Audit", f"<h2>Audit log</h2><p>{status}</p><table><tr><th>Run</th><th>Event</th><th>Detail</th><th>Hash</th></tr>{rows}</table>", "")


class H(BaseHTTPRequestHandler):
    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        model = q.get("model", "")
        if u.path == "/":
            self._send(queue(model))
        elif u.path.startswith("/case/"):
            self._send(case_view(u.path.split("/")[-1], model))
        elif u.path == "/audit":
            self._send(audit_view())
        else:
            self._send(b"not found", 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        choice = form.get("decision", [""])[0]
        if u.path == "/decide" and choice in ("approve", "request_info", "escalate"):
            ops = decisions()
            ops[q["case"]] = choice
            json.dump(ops, open(DECISIONS, "w", encoding="utf-8"), indent=1)
            AuditLog(os.path.join(RESULTS, "audit.jsonl")).write(
                run=f"operator-{q['case']}", event="operator_decision", case=q["case"], decision=choice, model=q.get("model"))
        self.send_response(303)
        self.send_header("Location", f"/case/{q.get('case', '')}?model={q.get('model', '')}")
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("PORT", 8090))
    print(f"Operator dashboard on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
