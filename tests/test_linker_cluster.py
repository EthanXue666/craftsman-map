"""
Linker 消解 + 聚类测试
======================
锁定两个关键行为:
1. Linker 把 "?::name" 占位符消解到项目内真实定义 (而非 external import),
   这是之前修过的 bug —— 构造调用/继承被错误消解到 import 节点导致影响面丢失。
2. 聚类把相关符号归到同一功能块。
"""
from __future__ import annotations

from craftsman_map.graph.model import EdgeKind
from craftsman_map.graph.linker import link
from craftsman_map.graph.model import Node, Edge, NodeKind


def test_inherits_resolved_to_real_class(indexed_graph):
    """LoginService inherits AuthProvider —— 应消解到项目内真实类节点。"""
    edges = indexed_graph.out_edges("auth/login.py::LoginService")
    inherit_edges = [e for e in edges if e.kind == EdgeKind.INHERITS]
    assert inherit_edges, "应有继承边"
    # 目标必须是项目内真实节点, 不是 external
    dst = inherit_edges[0].dst
    assert dst == "auth/base.py::AuthProvider"
    assert not dst.startswith("external::")


def test_call_resolved_within_file(indexed_graph):
    """authenticate 调用 audit —— 同文件调用应消解且高置信。"""
    edges = indexed_graph.out_edges("auth/login.py::LoginService.authenticate",
                                    EdgeKind.CALLS)
    targets = {e.dst for e in edges}
    assert "auth/login.py::audit" in targets
    audit_edge = next(e for e in edges if e.dst == "auth/login.py::audit")
    assert audit_edge.confidence >= 0.9  # 同文件唯一匹配 → 高置信


def test_used_by_not_lost(indexed_graph):
    """核心回归: AuthProvider 应被 LoginService used_by (影响面不为0)。
    这正是之前 external 优先级 bug 导致丢失的场景。"""
    in_edges = indexed_graph.in_edges("auth/base.py::AuthProvider")
    sources = {e.src for e in in_edges}
    assert "auth/login.py::LoginService" in sources


def test_unresolved_kept_low_confidence():
    """无法消解的引用应保留并降到低置信, 不静默丢弃。"""
    nodes = [Node(id="m.py", kind=NodeKind.MODULE, name="m.py",
                  qualified_name="m.py", path="m.py")]
    edges = [Edge(src="m.py", dst="?::NonExistentThing", kind=EdgeKind.CALLS,
                  confidence=0.7)]
    _, out_edges = link(nodes, edges)
    assert len(out_edges) == 1
    assert out_edges[0].confidence <= 0.4
    assert out_edges[0].meta.get("unresolved") == "NonExistentThing"


def test_ambiguous_marked():
    """多个同名候选 → 标记 ambiguous, 中等置信。"""
    nodes = [
        Node(id="a.py::foo", kind=NodeKind.FUNCTION, name="foo",
             qualified_name="foo", path="a.py"),
        Node(id="b.py::foo", kind=NodeKind.FUNCTION, name="foo",
             qualified_name="foo", path="b.py"),
        Node(id="c.py", kind=NodeKind.MODULE, name="c.py",
             qualified_name="c.py", path="c.py"),
    ]
    edges = [Edge(src="c.py", dst="?::foo", kind=EdgeKind.CALLS, confidence=0.7)]
    _, out_edges = link(nodes, edges)
    assert out_edges[0].confidence == 0.6
    assert "ambiguous" in out_edges[0].meta


def test_edge_dedup():
    """同 (src,dst,kind) 重复边只保留最高置信。"""
    nodes = [
        Node(id="a", kind=NodeKind.FUNCTION, name="a", qualified_name="a", path="x.py"),
        Node(id="b", kind=NodeKind.FUNCTION, name="b", qualified_name="b", path="x.py"),
    ]
    edges = [
        Edge(src="a", dst="b", kind=EdgeKind.CALLS, confidence=0.5),
        Edge(src="a", dst="b", kind=EdgeKind.CALLS, confidence=0.9),
    ]
    _, out_edges = link(nodes, edges)
    assert len(out_edges) == 1
    assert out_edges[0].confidence == 0.9


def test_clustering_groups_related(indexed_graph):
    """社区检测语义: 类与它自己的方法应聚在同一功能块。

    (v1 弱连通分量时代的假设 '继承类必须同簇' 已废弃 —— 换 Louvain 后
     '同簇 = 功能内聚', 接口定义 (AuthProvider) 与其实现 (LoginService)
     分属不同功能块是合理且期望的行为, 正是消灭'311 节点挤成一坨'的关键。)"""
    svc = indexed_graph.nodes["auth/login.py::LoginService"]
    method = indexed_graph.nodes["auth/login.py::LoginService.authenticate"]
    assert svc.cluster >= 0
    assert method.cluster >= 0
    # 类与其自身方法 → 必在同一功能块 (强内聚: contains 边)
    assert svc.cluster == method.cluster


def test_cluster_summary_structure(indexed_graph):
    """功能块摘要包含 size/files/key_symbols, 按大小降序。"""
    summ = indexed_graph.cluster_summary()
    assert len(summ) >= 1
    for s in summ:
        assert "cluster" in s and "size" in s and "key_symbols" in s
    sizes = [s["size"] for s in summ]
    assert sizes == sorted(sizes, reverse=True)
