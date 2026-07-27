"""
VIEW 分层视图 —— 自动把功能块归到架构层
=======================================
把功能块再抽象一层，标出"入口层 / 核心层 / 工具层 / 配置层 / 数据层 / 测试层"，
让不写代码的人和大模型一眼看清项目骨架。

纯启发式，零 LLM。判断依据: 文件名/符号名关键词 + 图上的连接特征
(入口层通常出度高入度低; 工具层通常入度高)。
"""
from __future__ import annotations

from collections import defaultdict

from ..graph.store import CodeGraph
from ..graph.model import NodeKind


# 层次定义 (顺序 = 展示顺序，从上层到底层)
LAYER_ORDER = ["入口", "核心", "解析", "数据模型", "配置", "工具", "测试", "文档", "其他"]

_KEYWORDS = {
    "入口": ("cli", "main", "entry", "__main__", "app", "server", "run"),
    "测试": ("test", "spec", "benchmark", "conftest", "fixture"),
    "配置": ("config", "setting", "loader", "env"),
    "数据模型": ("model", "schema", "entity", "dataclass", "dto", "types"),
    "解析": ("parser", "lexer", "analyz", "scan", "tokeniz", "ast"),
    "工具": ("util", "helper", "common", "tool", "shared", "lib"),
    "文档": (),  # 由 DOC 节点类型判定
}


def _classify_cluster(g: CodeGraph, members: list) -> str:
    # 文档块: 全是 doc 节点
    if members and all(m.kind == NodeKind.DOC for m in members):
        return "文档"

    # 按"文件级投票"分类, 不再"任一符号命中即定层"——
    # 否则一个大块里混进一个 test 文件就会把整块误判成测试层。
    files = sorted({m.path for m in members if m.path})
    if not files:
        return "核心"

    def _file_layer(path: str) -> str:
        p = path.lower()
        for layer in ("测试", "配置", "数据模型", "解析", "入口", "工具"):
            for kw in _KEYWORDS[layer]:
                if kw in p:
                    return layer
        return "核心"

    votes: dict[str, int] = defaultdict(int)
    for f in files:
        votes[_file_layer(f)] += 1

    total = len(files)
    # 测试/工具层要求"占多数"才成立(避免核心块被少量 test 文件带偏)
    for special in ("测试",):
        if votes.get(special, 0) <= total / 2:
            votes[special] = 0  # 不足半数 → 不算测试层

    # 取得票最高的层; 全是核心则归核心
    best = max(votes.items(), key=lambda kv: (kv[1], kv[0] != "核心"))
    return best[0] if best[1] > 0 else "核心"


def build_layers(g: CodeGraph) -> dict:
    """返回分层视图。每层含所属功能块 + 规模。"""
    groups: dict[int, list] = defaultdict(list)
    for n in g.nodes.values():
        if n.cluster >= 0:
            groups[n.cluster].append(n)

    layer_map: dict[str, list] = defaultdict(list)
    for cid, members in groups.items():
        layer = _classify_cluster(g, members)
        files = sorted({m.path for m in members if m.path})
        layer_map[layer].append({
            "cluster": cid,
            "size": len(members),
            "file_count": len(files),
            "sample_files": files[:5],
        })

    layers = []
    for name in LAYER_ORDER:
        if name in layer_map:
            blocks = sorted(layer_map[name], key=lambda b: -b["size"])
            layers.append({
                "layer": name,
                "cluster_count": len(blocks),
                "total_nodes": sum(b["size"] for b in blocks),
                "clusters": blocks,
            })
    return {
        "command": "layers",
        "layer_count": len(layers),
        "layers": layers,
        "hint": "分层是启发式(基于命名+结构)。'核心'层是业务主体, '入口'层是调用起点。",
        "next_actions": [
            {"cmd": "explore <symbol_id>", "why": "钻进某层的功能块"},
            {"cmd": "trace <entry_id>", "why": "从入口层符号追一条工作链"},
        ],
    }
