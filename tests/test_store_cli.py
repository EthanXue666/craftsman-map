"""
存储序列化 + CLI 端到端测试。
"""
from __future__ import annotations

import json
import subprocess
import sys

from craftsman_map.cli import main


def test_save_load_roundtrip(indexed_graph, reloaded_graph):
    """存盘再加载, 节点数/边数一致, 图结构可复现。"""
    assert len(reloaded_graph.nodes) == len(indexed_graph.nodes)
    assert len(reloaded_graph.edges) == len(indexed_graph.edges)
    # 邻接索引重建正确
    orig_in = indexed_graph.in_edges("auth/base.py::AuthProvider")
    rel_in = reloaded_graph.in_edges("auth/base.py::AuthProvider")
    assert len(orig_in) == len(rel_in)


def test_meta_fields(indexed_graph):
    """索引 meta 含关键统计字段。"""
    m = indexed_graph.meta
    for k in ("root", "node_count", "edge_count", "cluster_count", "version"):
        assert k in m


def test_cli_index_and_query(sample_repo, capsys):
    """CLI 端到端: index → map → impact, 输出都是合法 JSON。"""
    # index
    rc = main(["index", "--root", sample_repo, sample_repo])
    assert rc == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["command"] == "index"
    assert doc["status"] == "ok"

    # map
    rc = main(["map", "--root", sample_repo])
    assert rc == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["command"] == "map"
    assert doc["cluster_count"] >= 1

    # impact
    rc = main(["impact", "auth/base.py::AuthProvider", "--root", sample_repo])
    assert rc == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["impacted_count"] >= 1


def test_cli_compact_json_default(sample_repo, capsys):
    """默认输出紧凑 JSON (省 token), 不带缩进空格。"""
    main(["overview", "--root", sample_repo])
    out = capsys.readouterr().out.strip()
    # 紧凑模式: 逗号后无空格
    assert '", "' not in out  # 缩进模式才会有 ", "
    assert json.loads(out)  # 仍是合法 JSON
