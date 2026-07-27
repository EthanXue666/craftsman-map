"""
Linker —— 引用消解
==================
解析器产出的 calls/inherits/decorates 边目标是 "?::name" 形式的未解析占位符。
Linker 把这些名字匹配到真实节点 ID:
  1. 优先同文件内定义的符号
  2. 其次全局唯一同名符号
  3. 匹配成功 → 置信度提升; 仍无法唯一确定 → 保留占位并降置信度

这一步是"猜测 → 铁证"的关键: 消解成功的边 confidence 提到 0.95,
无法消解的标记 unresolved=True 并保持低 confidence, 让大模型知道边界。
"""
from __future__ import annotations

from collections import defaultdict

from .model import Node, Edge


def link(nodes: list[Node], edges: list[Edge]) -> tuple[list[Node], list[Edge]]:
    # 建索引: 短名 -> [node_id], 限定名尾段 -> [node_id]
    by_shortname: dict[str, list[str]] = defaultdict(list)
    id_set = {n.id for n in nodes}
    node_file: dict[str, str] = {n.id: n.path for n in nodes}
    for n in nodes:
        by_shortname[n.name].append(n.id)

    resolved_edges: list[Edge] = []
    for e in edges:
        if not e.dst.startswith("?::"):
            resolved_edges.append(e)
            continue
        raw = e.dst[3:]                      # 去掉 "?::"
        short = raw.split(".")[-1]           # 取最后一段 (obj.method -> method)
        all_cands = by_shortname.get(short, [])

        # calls/inherits/decorates 应优先指向项目内真实定义,
        # external import 节点仅作兜底 (否则构造调用会被消解到 import 节点,
        # 导致真实类的 used_by 丢失)。
        candidates = [c for c in all_cands if not c.startswith("external::")]
        if not candidates:
            candidates = all_cands

        if not candidates:
            # 无法消解: 保留为悬空引用, 低置信
            e.meta["unresolved"] = raw
            e.confidence = min(e.confidence, 0.4)
            resolved_edges.append(e)
            continue

        # 优先同文件
        src_file = node_file.get(e.src, "")
        same_file = [c for c in candidates if node_file.get(c) == src_file]
        chosen = None
        if len(same_file) == 1:
            chosen = same_file[0]
            conf = 0.95
        elif len(candidates) == 1:
            chosen = candidates[0]
            conf = 0.95
        else:
            # 多个同名候选, 歧义: 选第一个但保持中等置信 + 记录歧义
            chosen = candidates[0]
            conf = 0.6
            e.meta["ambiguous"] = candidates

        e.dst = chosen
        e.confidence = conf
        e.meta.pop("unresolved", None)
        resolved_edges.append(e)

    # 去重 (同 src,dst,kind 只留最高置信)
    best: dict[tuple, Edge] = {}
    for e in resolved_edges:
        k = e.key
        if k not in best or e.confidence > best[k].confidence:
            best[k] = e
    return nodes, list(best.values())
