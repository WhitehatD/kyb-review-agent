"""The KYB tool server, spoken to over MCP (stdio). One server process per agent session.

This is where authorization lives. The harness starts it with the agent's signed token in
KYB_AGENT_TOKEN, and the server:
- verifies the token against the platform's PUBLIC key (it cannot mint tokens itself),
- lists only the tools in the token's scope,
- re-verifies on every call, so an expired token stops working mid-session,
- refuses any tool outside the scope, and any case other than the one the token is bound to,
- turns execute-stage actions into approval requests that wait for an Operator,
- writes every allow and deny to the audit log, and
- continues the caller's OpenTelemetry trace from the `traceparent` in each request's _meta.

    python -m kyb.mcp_server        # speaks MCP on stdin/stdout
"""
import asyncio
import json
import os
import time
import uuid

import mcp.types as types
import yaml
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from opentelemetry.trace import Status, StatusCode

from . import identity, telemetry
from .audit import AuditLog, file_lock
from .tools import SCHEMAS, Case, load_case

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = yaml.safe_load(open(os.path.join(ROOT, "policy", "stages.yaml"), encoding="utf-8"))
AUDIT = AuditLog(os.path.join(ROOT, "results", "audit.jsonl"))
APPROVALS = os.path.join(ROOT, "results", "approvals.jsonl")
PUBLIC_KEY = os.path.join(ROOT, "keys", "platform_ed25519.pub.pem")

tracer = telemetry.setup("kyb-mcp-server")
server = Server("kyb-tools")
TOKEN = os.environ.get("KYB_AGENT_TOKEN", "")


def _claims():
    if not os.path.exists(PUBLIC_KEY):
        raise identity.Denied("no platform public key")
    return identity.verify(TOKEN, open(PUBLIC_KEY, "rb").read())


try:
    _start = _claims()
    CASE = Case(load_case(_start["case"]))
except (identity.Denied, KeyError) as e:
    _start, CASE = None, None
    AUDIT.write(run="mcp-server", event="mcp.deny", reason=f"session refused at start: {e}")


def _queue_approval(claims, tool, args):
    rec = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 3), "case": claims["case"],
           "agent": claims["sub"], "stage": claims["stage"], "action": tool, "args": args, "status": "pending"}
    with file_lock(APPROVALS):
        with open(APPROVALS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec["id"]


@server.list_tools()
async def list_tools():
    if not _start:
        return []
    scope = set(_start["scope"].split())
    return [types.Tool(name=n, description=d, inputSchema={"type": "object", "required": req,
                                                         "properties": {k: {"type": v} for k, v in props.items()}})
            for n, (d, props, req) in SCHEMAS.items() if n in scope]


@server.call_tool()
async def call_tool(name, arguments):
    meta = server.request_context.meta
    carrier = meta.model_dump() if meta else {}
    run = carrier.get("kyb_run", "")
    with tracer.start_as_current_span(f"mcp.server {name}", context=telemetry.extract(carrier)) as span:
        span.set_attribute("mcp.tool", name)

        def deny(reason, claims=None):
            span.set_attribute("kyb.authz.decision", "deny")
            span.set_attribute("kyb.authz.reason", reason)
            span.set_status(Status(StatusCode.ERROR, reason))
            AUDIT.write(run=run, event="mcp.deny", tool=name, reason=reason,
                        agent=(claims or {}).get("sub"), stage=(claims or {}).get("stage"),
                        trace_id=format(span.get_span_context().trace_id, "032x"))
            raise PermissionError(f"denied: {reason}")

        try:
            claims = _claims()
        except identity.Denied as e:
            deny(str(e))
        span.set_attribute("kyb.agent", claims["sub"])
        span.set_attribute("kyb.stage", claims["stage"])
        if CASE is None or claims["case"] != CASE.case["id"]:
            deny("token is not bound to this session's case", claims)
        if name not in claims["scope"].split():
            deny(f"{name} is not in the scope of stage {claims['stage']}", claims)

        try:
            result = getattr(CASE, name)(**(arguments or {}))
        except TypeError as e:
            result = {"error": f"bad arguments: {e}"}
        if name in POLICY["stages"][claims["stage"]].get("approval_required", []):
            result = {"queued": True, "approval_id": _queue_approval(claims, name, arguments),
                      "status": "pending Operator approval"}
        span.set_attribute("kyb.authz.decision", "allow")
        AUDIT.write(run=run, event="mcp.allow", tool=name, args=arguments, agent=claims["sub"], stage=claims["stage"],
                    trace_id=format(span.get_span_context().trace_id, "032x"))
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
