"""The tools the agent can call. Plain deterministic functions over the fixtures.

Each tool declares the lowest stage that may use it (policy/stages.yaml lists them per stage).
The agent is only ever handed the tools its current stage grants.
"""
import difflib
import json
import os
import re
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    with open(os.path.join(ROOT, "data", f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


REGISTRY = _load("registry")
SANCTIONS = _load("sanctions")


def load_case(case_id):
    for name in ("cases", "heldout"):
        for c in _load(name):
            if c["id"] == case_id:
                return c
    raise KeyError(case_id)

_SUFFIXES = {"ltd": "limited", "bv": "b.v.", "plc": "public limited company", "&": "and"}


def norm_company(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[().,]", " ", s)
    return " ".join(_SUFFIXES.get(t, t) for t in s.split())


def norm_person(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    # common transliteration variants, so Aleksandr/Alexander and Yelena/Elena meet in the middle
    for a, b in (("ks", "x"), ("yel", "el"), ("iy", "y"), ("oleks", "alex"), ("ch", "c")):
        s = s.replace(a, b)
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


class Case:
    """Tool state for one case: its documents and the actions it queued."""

    def __init__(self, case):
        self.case = case
        self.docs = {"application": json.dumps(case["application"], indent=1, ensure_ascii=False)}
        self.docs.update(case["documents"])
        self.actions = []

    # ---- read tools (shadow and up)
    def list_documents(self):
        return {"documents": sorted(self.docs)}

    def read_document(self, name):
        if name not in self.docs:
            return {"error": f"no document named {name!r}", "documents": sorted(self.docs)}
        return {"name": name, "content": self.docs[name]}

    def registry_lookup(self, country, company_number):
        rec = REGISTRY.get(f"{country.upper()}:{company_number.strip()}")
        return rec or {"found": False, "country": country, "company_number": company_number}

    def registry_search(self, name):
        q = norm_company(name)
        scored = sorted(((difflib.SequenceMatcher(None, q, norm_company(r["company_name"])).ratio(), r)
                         for r in REGISTRY.values()), key=lambda x: -x[0])
        return {"results": [{"score": round(s, 2), "company_name": r["company_name"], "country": r["country"],
                             "company_number": r["company_number"], "status": r["status"]}
                            for s, r in scored[:3] if s > 0.6]}

    def sanctions_screen(self, name):
        q = norm_person(name)
        hits = []
        for e in SANCTIONS:
            best = max(difflib.SequenceMatcher(None, q, norm_person(n)).ratio() for n in [e["name"], *e["aliases"]])
            # a short query can be contained in a longer listed name (Viktor Morozov in Viktor Aleksandrovich Morozov)
            toks = set(q.split())
            if toks and toks <= set(norm_person(e["name"]).split()):
                best = max(best, 0.9)
            if best >= 0.75:
                hits.append({"score": round(best, 2), **e})
        return {"query": name, "candidates": sorted(hits, key=lambda h: -h["score"]),
                "note": "A candidate is a name match only. Confirm with date of birth and nationality."}

    # ---- write tools (execute and up). They never act directly: they queue for an Operator.
    def request_documents(self, message):
        self.actions.append({"action": "request_documents", "message": message, "status": "pending_approval"})
        return {"queued": True, "status": "pending Operator approval"}

    def approve_customer(self, reason):
        self.actions.append({"action": "approve_customer", "reason": reason, "status": "pending_approval"})
        return {"queued": True, "status": "pending Operator approval"}


SCHEMAS = {
    "list_documents": ("List the documents submitted with this application.", {}, []),
    "read_document": ("Read one submitted document by name.", {"name": "string"}, ["name"]),
    "registry_lookup": ("Look up a company in the company registry by country code (GB, NL, ...) and company number.",
                        {"country": "string", "company_number": "string"}, ["country", "company_number"]),
    "registry_search": ("Search the company registry by company name.", {"name": "string"}, ["name"]),
    "sanctions_screen": ("Screen one person's name against the sanctions lists. Returns name-match candidates.",
                         {"name": "string"}, ["name"]),
    "request_documents": ("Ask the applicant for more information. Queued for Operator approval.",
                          {"message": "string"}, ["message"]),
    "approve_customer": ("Approve the customer. Queued for Operator approval.", {"reason": "string"}, ["reason"]),
}

SUBMIT = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": "Submit your review. Call exactly once, at the end.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["approve", "request_info", "escalate"]},
                "flags": {"type": "array", "items": {"type": "string"}},
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "reasons": {"type": "string", "description": "Two to four sentences for the Operator."},
                "evidence": {"type": "array", "items": {"type": "object", "properties": {
                    "source": {"type": "string"}, "detail": {"type": "string"}}}},
            },
            "required": ["decision", "flags", "risk", "reasons", "evidence"],
        },
    },
}


def schema_for(granted):
    out = []
    for name in granted:
        desc, props, req = SCHEMAS[name]
        out.append({"type": "function", "function": {"name": name, "description": desc, "parameters": {
            "type": "object", "properties": {k: {"type": v} for k, v in props.items()}, "required": req}}})
    return out + [SUBMIT]
