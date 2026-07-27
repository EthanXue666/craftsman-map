"""
UNDERSTAND / VIEW / TRACE 命令封装层
====================================
把 understand 包的能力包装成统一 dict 输出 (含 next_actions)，
供 CLI 和 MCP server 共用。
"""
from __future__ import annotations

from ..graph.store import CodeGraph
from ..understand import wiki as wiki_mod
from ..understand import view as view_mod
from ..understand import trace as trace_mod


# ---------- wiki ----------

def _wiki_first_line(text: str, limit: int = 100) -> str:
    """取描述首个非空行并限长 (wiki 列表页用, 全文按需翻页/describe)。"""
    if not text:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:limit]
    return ""


def _cluster_risk(g: CodeGraph, key) -> dict:
    """基于客观图指标 (fan-in: 块外引用数) 的确定性风险分桶。
    只报客观计数 + 透明分级依据, 不做主观臆断 —— 判断留给调用方。
    fan_in = 指向本块成员、但来源在块外的边数。引用越多, 改动波及面越大。"""
    try:
        cid = int(key)
    except (ValueError, TypeError):
        return {"level": "low", "fan_in": 0, "basis": "n/a"}
    member_ids = {n.id for n in g.nodes.values() if n.cluster == cid}
    if not member_ids:
        return {"level": "low", "fan_in": 0, "basis": "空块"}
    fan_in = 0
    for mid in member_ids:
        for e in g.in_edges(mid):
            if e.src not in member_ids:
                fan_in += 1
    if fan_in >= 10:
        level = "high"
    elif fan_in >= 3:
        level = "medium"
    else:
        level = "low"
    return {"level": level, "fan_in": fan_in,
            "basis": f"{fan_in} 处来自块外的引用 (fan-in)"}


def cmd_wiki(g: CodeGraph, root: str, fmt: str = "summary",
             offset: int = 0, top: int = 15) -> dict:
    """读取/刷新 wiki 描述。自动检测哪些块失效并规则兜底。

    体积治理: wiki 是"全库块描述总览", 42 个块全量吐会到上万字符撑爆上下文。
      - view=full  : 全量返回每块完整描述 (调用方要完整数据时用, 自负体积)
      - view=summary(默认): 分页(每页 top 个) + description 只取首行摘要;
        要某块全文用 describe --cluster <id>, 要下一页用 offset。

    注意: 两种模式返回的都是 JSON 结构 (不是纯文本), 区别只在信息量。
    兼容旧值: json→full, human→summary。
    """
    # 兼容旧参数值 (human/json), 归一到新语义 (summary/full)
    _alias = {"human": "summary", "json": "full"}
    fmt = _alias.get(fmt, fmt)

    w = wiki_mod.build_wiki(root, g, refresh_rules=True)
    # 两个语义严格分开（wiki.py 已拆分, 这里如实透传, 不再混为一谈）:
    #   needs_description = source=rule, 从未注入过高质量描述, 调用方该生成
    #   stale_clusters    = 曾注入 injected 描述, 但代码变更导致指纹失效, 需重生成
    needs = w.get("needs_description", [])
    stale = w.get("stale_clusters", [])
    clusters = w.get("clusters", {})

    result = {
        "command": "wiki",
        "view": fmt,
        "cluster_count": len(clusters),
        "project": w.get("project", {}),
        "needs_description": needs,        # 尚未注入描述的块 (source=rule)
        "needs_description_count": len(needs),
        "stale_clusters": stale,           # 曾注入但代码已变更失效的块 (真 stale)
        "stale_count": len(stale),
    }
    # 公共排序键: cluster id 统一按数字升序, 保证 0,1,2,...42 自然顺序
    def _ckey(k):
        try:
            return (0, int(k))
        except (ValueError, TypeError):
            return (1, str(k))

    if fmt == "full":
        # full: 全量完整描述 + 结构化 risk, clusters 返回 list (与 map 命令对齐)
        result["clusters"] = [
            {"id": int(k) if k.isdigit() else k, **clusters[k], "risk": _cluster_risk(g, k)}
            for k in sorted(clusters.keys(), key=_ckey)
        ]
        # 体积警告: full 视图按块数估算, 超过阈值提醒调用方注意 token 消耗
        estimated_chars = sum(
            len(str(clusters[k].get("description", ""))) + 200
            for k in clusters
        )
        if estimated_chars > 10000:
            result["volume_warning"] = (
                f"full 视图体积约 {estimated_chars // 1000}k 字符 ({len(clusters)} 块), "
                f"建议先用 wiki --view summary 定位目标块, 再用 describe --cluster <id> 按需展开。"
            )
    else:
        # summary: 分页 + 首行摘要, clusters 也返回 list (按数字升序翻页)
        keys = sorted(clusters.keys(), key=_ckey)
        total = len(keys)
        page_keys = keys[offset:offset + top]
        result["clusters"] = [
            {"id": int(k) if k.isdigit() else k,
             "title": clusters[k].get("title", ""),
             "description": _wiki_first_line(clusters[k].get("description", ""), 120),
             "source": clusters[k].get("source", "rule"),
             "stale": bool(clusters[k].get("stale_note")),
             "risk": _cluster_risk(g, k)}
            for k in page_keys
        ]
        result["page"] = {
            "total": total, "shown": len(page_keys),
            "offset": offset, "limit": top,
            "has_more": offset + len(page_keys) < total,
            "next_offset": offset + top if offset + len(page_keys) < total else None,
        }
        result["detail_hint"] = ("列表只给每块首行摘要以控体积; 要某块完整描述用 "
                                 "describe --cluster <id>, 要看更多块用 offset 翻页。")
    if stale:
        result["hint"] = (f"{len(stale)} 个功能块的描述是规则兜底(或已失效)。"
                          "调用方可用 describe 拿原料 → 生成描述 → desc 回写, 提升质量。")
        result["next_actions"] = [
            {"cmd": f"describe --cluster {stale[0]} --format prompt",
             "why": "拿该块原料包给你的模型生成描述"},
            {"cmd": "desc --cluster <id> --text <生成的描述>",
             "why": "把生成的描述回写缓存"},
        ]
    else:
        result["hint"] = "所有功能块描述均为最新的高质量注入描述。"
        result["next_actions"] = [{"cmd": "layers", "why": "看分层架构"}]
    return result


def cmd_describe(g: CodeGraph, cluster_id: int | None, fmt: str = "prompt") -> dict:
    """输出功能块的描述原料包 (给调用方 LLM 生成描述用)。"""
    from collections import defaultdict
    groups: dict[int, list] = defaultdict(list)
    for n in g.nodes.values():
        if n.cluster >= 0:
            groups[n.cluster].append(n)

    if cluster_id is not None:
        if cluster_id not in groups:
            return {"command": "describe", "error": f"功能块 {cluster_id} 不存在",
                    "available_clusters": sorted(groups.keys())[:30]}
        targets = {cluster_id: groups[cluster_id]}
    else:
        targets = dict(groups)

    materials = []
    for cid, members in targets.items():
        mat = wiki_mod.describe_material(g, cid, members)
        if fmt == "prompt":
            mat["prompt"] = wiki_mod.build_describe_prompt(mat)
        # 空 docstring 提示: 地基类没有文档, 提醒调用方去看源码
        empty_doc_syms = [
            s["name"] for s in mat.get("symbols", [])
            if not s.get("docstring") and s.get("kind") in ("class", "function")
        ]
        if empty_doc_syms:
            mat["hint_empty_docstring"] = (
                f"以下 {len(empty_doc_syms)} 个符号无 docstring，"
                f"描述质量有限，建议结合源码理解: {', '.join(empty_doc_syms[:5])}"
                + ("..." if len(empty_doc_syms) > 5 else "")
            )
        materials.append(mat)

    return {
        "command": "describe",
        "count": len(materials),
        "format": fmt,
        "materials": materials,
        "hint": "把每个 material 的 prompt 喂给你自己的模型, 拿到描述后用 "
                "desc --cluster <id> --text <描述> 回写。描述绑当前代码指纹, 代码变了自动失效。",
        "next_actions": [
            {"cmd": "desc --cluster <id> --text <描述>", "why": "回写生成的描述"},
            {"cmd": "symbol <符号id>", "why": "深挖块内某个具体函数/类的签名和调用关系"},
            {"cmd": "impact <符号id>", "why": "评估改动该块内某符号的影响面"},
        ],
    }


def cmd_inject_desc(g: CodeGraph, root: str, cluster_id: int,
                    text: str, title: str = "") -> dict:
    """回写调用方生成的功能块描述。"""
    r = wiki_mod.inject_description(root, g, cluster_id, text, title)
    r["command"] = "desc"
    r["hint"] = "描述已绑定当前块指纹。该块代码变更后, wiki 会自动标记失效并降级到规则版。"
    r["next_actions"] = [{"cmd": "wiki", "why": "查看更新后的描述"}]
    return r


def cmd_inject_project(root: str, text: str) -> dict:
    r = wiki_mod.inject_project_description(root, text)
    r["command"] = "desc-project"
    r["next_actions"] = [{"cmd": "wiki", "why": "查看项目描述"}]
    return r


# ---------- layers ----------

def cmd_layers(g: CodeGraph) -> dict:
    return view_mod.build_layers(g)


# ---------- trace ----------

def cmd_trace(g: CodeGraph, entry_id: str, max_depth: int = 8,
              max_tree_nodes: int = 40, full: bool = False) -> dict:
    return trace_mod.trace_chain(g, entry_id, max_depth=max_depth,
                                 max_tree_nodes=max_tree_nodes, full=full)


def cmd_entrypoints(g: CodeGraph, top: int = 20, include_tests: bool = False) -> dict:
    return trace_mod.list_entrypoints(g, top=top, include_tests=include_tests)
