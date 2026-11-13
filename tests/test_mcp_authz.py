"""End-to-end tests of the MCP tool server's authorization. No model calls, no API key.

Each test starts the real server process over stdio with a token, the same way the harness does.
"""
import asyncio
import json

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from kyb import identity
from kyb.agent import POLICY, call_tool, server_params


async def session(token, fn):
    async with stdio_client(server_params(token)) as (r, w), ClientSession(r, w) as mcp:
        await mcp.initialize()
        return await fn(mcp)


def run(token, fn):
    return asyncio.run(session(token, fn))


def token(stage="shadow", case="K01", scopes=None, ttl=600):
    return identity.issue("agent:test", stage, scopes or POLICY["stages"][stage]["tools"], case, ttl=ttl)


def test_shadow_server_lists_only_read_tools():
    async def fn(mcp):
        return {t.name for t in (await mcp.list_tools()).tools}
    names = run(token("shadow"), fn)
    assert names == set(POLICY["stages"]["shadow"]["tools"])
    assert "approve_customer" not in names


def test_out_of_scope_call_is_refused():
    async def fn(mcp):
        return await call_tool(mcp, "approve_customer", {"reason": "looks fine"}, "test")
    result, failed = run(token("shadow"), fn)
    assert failed and "not in the scope" in result["error"]


def test_in_scope_call_works():
    async def fn(mcp):
        return await call_tool(mcp, "registry_lookup", {"country": "GB", "company_number": "14820931"}, "test")
    result, failed = run(token("shadow"), fn)
    assert not failed and result["company_name"] == "LEDGERLINE SOFTWARE LTD"


def test_execute_approval_is_queued_not_done():
    async def fn(mcp):
        return await call_tool(mcp, "approve_customer", {"reason": "clean"}, "test")
    result, failed = run(token("execute"), fn)
    assert not failed and result["status"] == "pending Operator approval" and result["approval_id"]


def test_expired_token_is_refused():
    async def fn(mcp):
        return await call_tool(mcp, "list_documents", {}, "test")
    result, failed = run(token(ttl=-5), fn)
    assert failed


def test_forged_token_is_refused():
    claims = {"iss": "kyb-platform", "aud": "kyb-mcp", "sub": "agent:x", "stage": "autonomous",
              "scope": "approve_customer", "case": "K01", "exp": 9999999999}
    import jwt
    forged = jwt.encode(claims, "not-the-platform-key", algorithm="HS256")

    async def fn(mcp):
        return await call_tool(mcp, "approve_customer", {"reason": "x"}, "test")
    result, failed = run(forged, fn)
    assert failed
