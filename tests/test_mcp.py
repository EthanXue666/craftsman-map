"""
MCP server 协议测试 —— 验证 JSON-RPC 契约与工具调用。
用 handle_request 直接驱动 (不起真实进程), 覆盖 initialize/tools.list/tools.call。
"""
from __future__ import annotations

import json

import pytest

from craftsman_map import mcp_server


@pytest.fixture(autouse=True)
def _fresh_cache():
    """每个测试用独立缓存, 避免串扰。"""
    mcp_server._GRAPHS = mcp_server._GraphCache()
    yield


def _req(method, params=None, req_id=1):
    r = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        r["params"] = params
    return r


def test_initialize():
    resp = mcp_server.handle_request(_req("initialize"))
    assert resp["result"]["serverInfo"]["name"] == "craftsman-map"
    assert "protocolVersion" in resp["result"]
    assert "tools" in resp["result"]["capabilities"]


def test_tools_list_has_all_eight():
    resp = mcp_server.handle_request(_req("tools/list"))
    names = {t["name"] for t in resp["result"]["tools"]}
    expected = {
        # v1 原有
        "craftsman_map_index", "craftsman_map_overview", "craftsman_map_map",
        "craftsman_map_search", "craftsman_map_symbol", "craftsman_map_impact",
        "craftsman_map_explore", "craftsman_map_hotspots",
        # v2 新增
        "craftsman_map_wiki", "craftsman_map_describe", "craftsman_map_inject_desc",
        "craftsman_map_layers", "craftsman_map_entrypoints", "craftsman_map_trace",
        # v2.1 项目级认知入口
        "craftsman_map_orient", "craftsman_map_report",
    }
    assert names == expected


def test_tools_have_valid_schema():
    """每个工具都有 inputSchema, required 字段合法。"""
    resp = mcp_server.handle_request(_req("tools/list"))
    for t in resp["result"]["tools"]:
        assert "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"
        assert "description" in t and t["description"]


def test_notification_returns_none():
    """无 id 的 notification 不产生响应。"""
    resp = mcp_server.handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp is None


def test_unknown_method():
    resp = mcp_server.handle_request(_req("bogus/method"))
    assert resp["error"]["code"] == -32601


def test_index_and_call_flow(sample_repo):
    """端到端: index 工具 → map 工具, 都通过 MCP 通道。"""
    # index
    resp = mcp_server.handle_request(_req("tools/call", {
        "name": "craftsman_map_index",
        "arguments": {"root": sample_repo},
    }))
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "ok"

    # map (应命中缓存)
    resp = mcp_server.handle_request(_req("tools/call", {
        "name": "craftsman_map_map",
        "arguments": {"root": sample_repo},
    }))
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["command"] == "map"
    assert payload["cluster_count"] >= 1


def test_impact_via_mcp(sample_repo):
    mcp_server.handle_request(_req("tools/call", {
        "name": "craftsman_map_index", "arguments": {"root": sample_repo}}))
    resp = mcp_server.handle_request(_req("tools/call", {
        "name": "craftsman_map_impact",
        "arguments": {"root": sample_repo, "id": "auth/base.py::AuthProvider"},
    }))
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["impacted_count"] >= 1


def test_tool_error_is_soft(sample_repo):
    """未建索引时调用查询工具 → 返回 isError=True 但不崩溃。"""
    resp = mcp_server.handle_request(_req("tools/call", {
        "name": "craftsman_map_map",
        "arguments": {"root": "/nonexistent/path/xyz"},
    }))
    assert resp["result"]["isError"] is True
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "error" in payload


def test_serve_stdio_roundtrip(sample_repo):
    """真实走 stdio: 喂多行 JSON-RPC, 读回响应。"""
    import io
    inp = io.StringIO(
        json.dumps(_req("initialize", req_id=1)) + "\n" +
        json.dumps(_req("tools/list", req_id=2)) + "\n"
    )
    out = io.StringIO()
    mcp_server.serve(stdin=inp, stdout=out)
    lines = [l for l in out.getvalue().splitlines() if l.strip()]
    assert len(lines) == 2
    r1 = json.loads(lines[0])
    r2 = json.loads(lines[1])
    assert r1["id"] == 1 and "serverInfo" in r1["result"]
    assert r2["id"] == 2 and len(r2["result"]["tools"]) == 16
