"""Offline tests: no model calls, no API key. They pin the parts of the safety model that live in code."""
import json
import os

import pytest

from kyb.agent import POLICY, enforce, wrap
from kyb.audit import AuditLog, verify
from kyb.evaluate import score
from kyb.tools import Case, schema_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = {c["id"]: c for c in json.load(open(os.path.join(ROOT, "data", "cases.json"), encoding="utf-8"))}


def names(schema):
    return {t["function"]["name"] for t in schema}


def test_shadow_and_suggest_cannot_act():
    for stage in ("shadow", "suggest"):
        granted = names(schema_for(POLICY["stages"][stage]["tools"]))
        assert not granted & {"approve_customer", "request_documents"}


def test_execute_actions_need_operator_approval():
    st = POLICY["stages"]["execute"]
    assert {"approve_customer", "request_documents"} <= set(st["tools"]) <= set(st["tools"])
    assert set(st["approval_required"]) == {"approve_customer", "request_documents"}
    t = Case(CASES["K01"])
    assert t.approve_customer("clean")["status"] == "pending Operator approval"
    assert t.actions[0]["status"] == "pending_approval"


@pytest.mark.parametrize("flag", POLICY["rules"]["escalate_with"])
@pytest.mark.parametrize("decision", ["approve", "request_info"])
def test_blocking_flags_always_escalate(flag, decision):
    v, overrides = enforce({"decision": decision, "flags": [flag]}, "shadow")
    assert v["decision"] == "escalate" and overrides


def test_unknown_flags_dropped():
    v, overrides = enforce({"decision": "approve", "flags": ["looks_fine", "recently_incorporated"]}, "shadow")
    assert v["flags"] == ["recently_incorporated"] and v["decision"] == "approve" and overrides


def test_tool_output_is_labelled_untrusted():
    out = wrap("read_document", Case(CASES["K24"]).read_document("application"))
    assert out.startswith('<untrusted source="read_document">') and "SYSTEM NOTICE" in out


def test_sanctions_needs_more_than_a_name():
    hits = Case(CASES["K07"]).sanctions_screen("Daniel Kovacs")["candidates"]
    assert hits and hits[0]["date_of_birth"] != CASES["K07"]["application"]["beneficial_owners"][0]["date_of_birth"]


@pytest.mark.parametrize("query,expected", [("Alexander Petrov", "SX-2210"), ("Yelena Sokolova", "SX-5521"),
                                            ("Karimi Hamid Reza", "SX-3307"), ("Viktor Morozov", "SX-1042")])
def test_sanctions_catches_spelling_variants(query, expected):
    assert expected in [h["id"] for h in Case(CASES["K01"]).sanctions_screen(query)["candidates"]]


def test_audit_chain_detects_tampering(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = AuditLog(str(p))
    for i in range(5):
        log.write(run="r", event="tool", n=i)
    assert verify(str(p)) == (True, 5)
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace('"n": 2', '"n": 99')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify(str(p)) == (False, 3)


def test_unsafe_approval_fails_the_gate():
    cases = list(CASES.values())
    perfect = [{"case": c["id"], "verdict": c["label"], "cost_usd": 0, "tool_calls": 0, "overrides": []} for c in cases]
    assert score(perfect, cases)[0]["gate"]["passed"]
    bad = [dict(r, verdict={"decision": "approve", "flags": []}) if r["case"] == "K19" else r for r in perfect]
    s, _ = score(bad, cases)
    assert s["unsafe_approvals"] == 1 and not s["gate"]["passed"]


def test_fixture_labels_use_known_flags():
    for c in CASES.values():
        assert set(c["label"]["flags"]) <= set(POLICY["flags"]), c["id"]


def test_new_company_with_implausible_volume_escalates():
    v, overrides = enforce({"decision": "request_info", "flags": ["recently_incorporated", "volume_inconsistent"]}, "shadow")
    assert v["decision"] == "escalate" and overrides
    v, overrides = enforce({"decision": "request_info", "flags": ["volume_inconsistent"]}, "shadow")
    assert v["decision"] == "request_info" and not overrides
