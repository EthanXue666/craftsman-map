"""
命令层测试 —— 验证 6 个给大模型的命令输出契约稳定。
每个命令必须:
  - 返回 dict 且含 "command" 字段
  - 含 next_actions 引导 (LLM 友好核心设计)
  - 内容真实反映图结构
"""
from __future__ import annotations

from craftsman_map.commands import core


def test_overview(indexed_graph):
    r = core.cmd_overview(indexed_graph)
    assert r["command"] == "overview"
    assert r["node_kinds"]  # 有节点分类统计
    assert "module" in r["node_kinds"]
    assert r["next_actions"]


def test_map_progressive_disclosure(indexed_graph):
    """map 只给功能块摘要, 不倒全量节点 —— 省 token 的核心。"""
    r = core.cmd_map(indexed_graph)
    assert r["command"] == "map"
    assert r["cluster_count"] >= 1
    # 每个 cluster 只给 key_symbols (<=5), 不是全部成员
    for c in r["clusters"]:
        assert len(c["key_symbols"]) <= 5
    assert r["next_actions"]


def test_search_hit(indexed_graph):
    r = core.cmd_search(indexed_graph, "LoginService")
    assert r["command"] == "search"
    assert r["count"] >= 1
    names = {hit["name"] for hit in r["results"]}
    assert any("LoginService" in n for n in names)


def test_search_fuzzy(indexed_graph):
    """模糊搜索 'login' 应命中 LoginService。"""
    r = core.cmd_search(indexed_graph, "login")
    assert r["count"] >= 1


def test_search_miss(indexed_graph):
    r = core.cmd_search(indexed_graph, "ZzzNonExistent")
    assert r["count"] == 0
    assert r["results"] == []


def test_symbol_detail(indexed_graph):
    r = core.cmd_symbol(indexed_graph, "auth/login.py::LoginService.authenticate")
    assert r["command"] == "symbol"
    assert r["node"]["name"].endswith("authenticate")
    # authenticate 调用 audit → uses 里应有
    used_targets = {u["target"] for u in r["uses"]}
    assert "auth/login.py::audit" in used_targets


def test_symbol_by_name_fallback(indexed_graph):
    """传名字而非完整 ID 也能容错命中。"""
    r = core.cmd_symbol(indexed_graph, "LoginService")
    assert r["command"] == "symbol"
    assert "error" not in r


def test_symbol_not_found(indexed_graph):
    r = core.cmd_symbol(indexed_graph, "totally::bogus::id")
    assert "error" in r


def test_impact_nonzero(indexed_graph):
    """AuthProvider 被继承 → 影响面 > 0 (核心回归防线)。"""
    r = core.cmd_impact(indexed_graph, "auth/base.py::AuthProvider")
    assert r["command"] == "impact"
    assert r["impacted_count"] >= 1
    # LoginService 应在受影响列表里
    all_impacted = []
    for depth_nodes in r["by_depth"].values():
        all_impacted.extend(n["id"] for n in depth_nodes)
    assert "auth/login.py::LoginService" in all_impacted


def test_impact_layered(indexed_graph):
    """影响面按深度分层返回。"""
    r = core.cmd_impact(indexed_graph, "auth/base.py::MAX_RETRY")
    assert "by_depth" in r
    # 所有 key 是字符串深度
    for k in r["by_depth"]:
        assert k.isdigit()


def test_explore(indexed_graph):
    r = core.cmd_explore(indexed_graph, "auth/login.py::LoginService", depth=1)
    assert r["command"] == "explore"
    assert r["node_count"] >= 1
    assert r["center"] == "auth/login.py::LoginService"
    # 展开应包含它的方法
    node_ids = {n["id"] for n in r["nodes"]}
    assert "auth/login.py::LoginService.authenticate" in node_ids


def test_explore_depth2_wider(indexed_graph):
    """depth=2 应比 depth=1 覆盖更多或相等节点。"""
    r1 = core.cmd_explore(indexed_graph, "auth/login.py::LoginService", depth=1)
    r2 = core.cmd_explore(indexed_graph, "auth/login.py::LoginService", depth=2)
    assert r2["node_count"] >= r1["node_count"]


def test_all_commands_have_next_actions(indexed_graph):
    """LLM 友好契约: 除错误外每个命令都带 next_actions 引导。"""
    cmds = [
        core.cmd_overview(indexed_graph),
        core.cmd_map(indexed_graph),
        core.cmd_search(indexed_graph, "Login"),
        core.cmd_symbol(indexed_graph, "auth/login.py::audit"),
        core.cmd_impact(indexed_graph, "auth/login.py::audit"),
        core.cmd_explore(indexed_graph, "auth/login.py::audit"),
    ]
    for r in cmds:
        assert "next_actions" in r, f"{r['command']} 缺 next_actions"
