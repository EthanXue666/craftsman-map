"""
核心命令层 —— 给大模型的"遥控器"
================================
每个命令返回结构化 dict, 统一含 next_actions 字段: 直接告诉大模型
"下一步可以调哪些命令", 消除猜测。这是 craftsman-map 对 LLM 友好的核心设计。

MVP 命令:
  map      —— 功能块总览 (地图/目录页, 渐进披露第一层)
  symbol   —— 单符号详情 (定义/签名/邻居)
  search   —— 按名字/关键词找符号
  impact   —— 影响面分析 (改这里会波及谁, NAVIGATE 层)
  explore  —— 从某节点出发展开 N 层邻居 (渐进披露钻取)
  overview —— 索引元信息统计
"""
from __future__ import annotations

import re

from ..graph.store import CodeGraph
from ..graph.model import NodeKind, EdgeKind


# 命名动词 → 中文意图, 用于无 docstring 时的规则兜底摘要 (诚实标注为推断)
_NAME_VERB = {
    "get": "获取", "fetch": "获取", "load": "加载", "read": "读取",
    "set": "设置", "save": "保存", "write": "写入", "store": "存储",
    "build": "构建", "make": "构造", "create": "创建", "gen": "生成",
    "generate": "生成", "add": "添加", "append": "追加", "insert": "插入",
    "remove": "移除", "delete": "删除", "clear": "清空", "pop": "弹出",
    "update": "更新", "modify": "修改", "edit": "编辑", "patch": "打补丁",
    "find": "查找", "search": "搜索", "lookup": "查找", "query": "查询",
    "parse": "解析", "render": "渲染", "format": "格式化", "emit": "输出",
    "handle": "处理", "process": "处理", "run": "运行", "execute": "执行",
    "exec": "执行", "init": "初始化", "setup": "初始化", "close": "关闭",
    "open": "打开", "start": "启动", "stop": "停止", "reset": "重置",
    "check": "检查", "validate": "校验", "verify": "验证", "ensure": "确保",
    "is": "判断是否", "has": "判断是否含", "can": "判断能否",
    "to": "转换为", "as": "转为", "on": "事件响应", "cmd": "命令",
    "resolve": "解析", "compute": "计算", "calc": "计算", "count": "计数",
    "list": "列出", "iter": "遍历", "sort": "排序", "filter": "过滤",
    "merge": "合并", "split": "拆分", "join": "连接", "reindex": "重建索引",
    "index": "建立索引", "inject": "注入", "describe": "描述", "trace": "追踪",
    "explore": "展开", "cluster": "聚类",
}

_KIND_LABEL = {"class": "类", "function": "函数", "method": "方法",
               "module": "模块", "import": "导入"}


def _first_word(name: str) -> str:
    """取符号名的首个语义单词 (snake_case 取第一段, camelCase 取开头小写段)。"""
    if not name:
        return ""
    if "_" in name:
        return name.split("_")[0].lower()
    m = re.match(r"^[a-z]+", name)
    return m.group(0).lower() if m else name.lower()


def _rule_summary(n) -> str:
    """无 docstring 时, 从 kind/命名规则生成一句结构兜底摘要。
    明确标 [推断] 前缀 —— 这是从命名推断的结构描述, 不是真实文档, 不冒充。"""
    kind_label = _KIND_LABEL.get(n.kind.value, n.kind.value)
    name = n.name or ""
    verb = _first_word(name)
    hint = _NAME_VERB.get(verb)
    if hint:
        return f"[推断] {kind_label} · 疑似“{hint}”相关操作 (据命名 {name}, 无 docstring)"
    return f"[推断] {kind_label} {name} (无 docstring, 仅结构信息, 需看源码确认职责)"


def _page_meta(total: int, shown: int, offset: int, limit: int) -> dict:
    """统一分页元信息 —— 让大模型知道'看到的是全部还是一部分', 防上下文被撑爆
    也防误判'项目就这么大'。has_more=True 时给出续读的 offset。"""
    has_more = offset + shown < total
    meta = {
        "total": total,
        "shown": shown,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
    }
    if has_more:
        meta["next_offset"] = offset + shown
        meta["note"] = f"仅显示 {offset}~{offset + shown} / 共 {total} 条, 用 --offset {offset + shown} 续读。"
    return meta


def _node_brief(n) -> dict:
    # summary 优先用真实 docstring; 为空时规则兜底 + 标 summary_source 诚实区分。
    #   docstring —— 真实文档 (作者写的)
    #   rule      —— 从 kind/命名推断的结构描述 (带 [推断] 前缀, 不冒充真实文档)
    if n.docstring:
        summary, src = n.docstring, "docstring"
    else:
        summary, src = _rule_summary(n), "rule"
    return {
        "id": n.id, "name": n.qualified_name, "kind": n.kind.value,
        "path": n.path, "line": n.line_start,
        "signature": n.signature, "summary": summary, "summary_source": src,
        "confidence": n.confidence, "cluster": n.cluster,
    }


def cmd_overview(g: CodeGraph) -> dict:
    kinds: dict[str, int] = {}
    for n in g.nodes.values():
        kinds[n.kind.value] = kinds.get(n.kind.value, 0) + 1
    edge_kinds: dict[str, int] = {}
    for e in g.edges:
        edge_kinds[e.kind.value] = edge_kinds.get(e.kind.value, 0) + 1
    return {
        "command": "overview",
        "meta": g.meta,
        "node_kinds": kinds,
        "edge_kinds": edge_kinds,
        "next_actions": [
            {"cmd": "map", "why": "看功能块总览, 找到目标模块"},
            {"cmd": "search <name>", "why": "按名字定位具体符号"},
        ],
    }


def cmd_map(g: CodeGraph, top: int = 12, offset: int = 0) -> dict:
    """地图/目录页 —— 渐进披露第一层。只给功能块摘要, 不倒全量。
    默认只给前 12 个功能块 (按 size 降序, 最大块最先看); 要更多用 offset 翻页。
    配合 cluster_summary 里 docstring 只取首行, 单页体积可控, 不撑爆上下文。"""
    summ = g.cluster_summary()
    total = len(summ)
    page = summ[offset:offset + top]
    return {
        "command": "map",
        "cluster_count": total,
        "clusters": page,
        "page": _page_meta(total, len(page), offset, top),
        "hint": "每个 cluster 是一个功能块。锁定目标后用 explore <cluster内symbol id> 钻进去, 避免一次读全库。",
        "next_actions": [
            {"cmd": "explore <symbol_id>", "why": "钻进某功能块的关键符号"},
            {"cmd": "search <keyword>", "why": "跨功能块按关键词找"},
        ],
    }


def cmd_search(g: CodeGraph, query: str, limit: int = 15, offset: int = 0) -> dict:
    if not query or not query.strip():
        return {
            "command": "search",
            "error": "查询词不能为空",
            "hint": "请提供符号名称关键词，如 cmd_wiki / CodeGraph / inject",
            "next_actions": [{"cmd": "wiki --view summary", "why": "想浏览所有功能块? 用 wiki 命令"}],
        }
    all_hits = g.find_by_name(query, fuzzy=True)
    total = len(all_hits)
    page = all_hits[offset:offset + limit]
    return {
        "command": "search",
        "query": query,
        "count": len(page),
        "results": [_node_brief(n) for n in page],
        "page": _page_meta(total, len(page), offset, limit),
        "next_actions": [
            {"cmd": "symbol <id>", "why": "看某个结果的完整定义与邻居"},
            {"cmd": "impact <id>", "why": "看改动它会影响谁"},
        ],
    }


def cmd_symbol(g: CodeGraph, node_id: str) -> dict:
    n = g.nodes.get(node_id)
    if not n:
        # 容错: 当成名字搜
        cands = g.find_by_name(node_id, fuzzy=True)
        if not cands:
            return {"command": "symbol", "error": f"未找到符号: {node_id}",
                    "next_actions": [{"cmd": "search <name>", "why": "换个名字搜"}]}
        n = cands[0]
    # 出边 (它用了谁) / 入边 (谁用了它)
    uses = [{"kind": e.kind.value, "target": e.dst,
             "target_name": g.nodes[e.dst].qualified_name if e.dst in g.nodes else e.dst,
             "confidence": e.confidence, "line": e.line}
            for e in g.out_edges(n.id)]
    used_by = [{"kind": e.kind.value, "source": e.src,
                "source_name": g.nodes[e.src].qualified_name if e.src in g.nodes else e.src,
                "confidence": e.confidence, "line": e.line}
               for e in g.in_edges(n.id)]
    return {
        "command": "symbol",
        "node": _node_brief(n),
        "uses": uses,
        "used_by": used_by,
        "next_actions": [
            {"cmd": f"impact {n.id}", "why": "评估改动影响面"},
            {"cmd": f"explore {n.id}", "why": "展开更多层邻居"},
        ],
    }


def cmd_impact(g: CodeGraph, node_id: str, max_depth: int = 3,
               max_nodes: int = 200) -> dict:
    """NAVIGATE 层核心 —— 反向追溯: 改这个节点, 谁会受影响。
    叠加 git 维度: 受影响符号带变更热度; 目标文件的共变耦合一并给出。
    max_nodes 封顶: 高扇入的核心符号可能波及上千节点, 到达上限即停止并标注 truncated。"""
    n = g.nodes.get(node_id)
    if not n:
        cands = g.find_by_name(node_id, fuzzy=True)
        if not cands:
            return {"command": "impact", "error": f"未找到符号: {node_id}"}
        n = cands[0]
    # BFS 反向 (沿入边: 谁调用/继承/引用了我)
    visited: dict[str, int] = {n.id: 0}
    layers: list[list[dict]] = [[] for _ in range(max_depth + 1)]
    frontier = [n.id]
    truncated = False
    for depth in range(1, max_depth + 1):
        nxt = []
        for cur in frontier:
            for e in g.in_edges(cur):
                if e.kind in (EdgeKind.CONTAINS,):
                    continue  # 容器边不算影响传播
                if e.src not in visited and e.src in g.nodes:
                    if len(visited) - 1 >= max_nodes:
                        truncated = True
                        break
                    visited[e.src] = depth
                    src_n = g.nodes[e.src]
                    layers[depth].append({
                        "id": src_n.id, "name": src_n.qualified_name,
                        "kind": src_n.kind.value, "path": src_n.path,
                        "via": e.kind.value, "confidence": e.confidence,
                        "git_churn": src_n.meta.get("git_churn"),
                    })
                    nxt.append(e.src)
            if truncated:
                break
        frontier = nxt
        if truncated or not frontier:
            break
    total = len(visited) - 1

    _brief = _node_brief(n)
    result = {
        "command": "impact",
        "node": _brief,
        "target": _brief,  # 向后兼容别名, 语义已统一到 node, 将来可移除
        "direction": "reverse",  # impact 只算反向依赖(谁调用我), 不含正向调用
        "impacted_count": total,
        "truncated": truncated,
        "by_depth": {str(d): layers[d] for d in range(1, max_depth + 1) if layers[d]},
        "hint": "impact 只统计反向依赖(谁调用/继承/引用了我); confidence < 0.5 的边是启发式推断, 需人工/LLM 复核。",
        "next_actions": [
            {"cmd": "symbol <impacted_id>", "why": "查看受影响符号的细节"},
        ],
    }
    if total == 0:
        # 反向为 0 不等于"改动安全"——它可能是顶层入口/对外 API, 没有内部调用者,
        # 但仍是外部契约, 改了会影响下游使用者。诚实提示避免误导。
        result["hint"] = ("反向依赖为 0: 没有其它符号调用/继承/引用它。"
                          "但这不代表改动安全——它可能是顶层入口或对外 API(外部调用者不在本图), "
                          "改动仍会影响下游使用者。用 symbol 看它自己调用了谁(正向依赖)。")
        result["next_actions"] = [
            {"cmd": f"symbol {n.id}", "why": "反向为 0, 改看正向调用(它依赖谁)"},
        ]
    if truncated:
        result["hint"] += (f" 影响面已达 max_nodes={max_nodes} 上限被截断——"
                           f"实际受影响范围更大, 说明这是高扇入核心符号, 改动需格外谨慎。")

    # git 共变耦合: 目标文件历史上常和谁一起改 (静态图可能看不到的隐式依赖)
    co = g.meta.get("git_co_change", {})
    if n.path and n.path in co:
        coupled = [{"path": p, "co_change_count": c} for p, c in co[n.path][:8]]
        if coupled:
            result["git_coupled_files"] = coupled
            result["hint"] += (" git_coupled_files 是历史上常一起改动的文件, "
                               "即使无静态调用边也可能需要同步修改。")
    if n.meta.get("git_churn") is not None:
        result["target"]["git_churn"] = n.meta.get("git_churn")
    return result


def cmd_hotspots(g: CodeGraph, top: int = 15) -> dict:
    """git 变更热点 —— 被提交碰得最多的文件, 通常是风险/活跃/技术债聚集区。"""
    git = g.meta.get("git", {})
    if not git.get("available"):
        # 无 git 环境: 降级到"静态复杂度热点"——用符号数 + 连接度(出入边)
        # 近似"哪些文件最复杂/最中心"。诚实标注: 这不是真实 churn, 只是结构代理信号,
        # 无法反映"谁最近在改/谁改得最频繁", 仅供无 git 时定位复杂模块。
        from collections import defaultdict
        file_symbols: dict[str, int] = defaultdict(int)
        file_degree: dict[str, int] = defaultdict(int)
        for n in g.nodes.values():
            if not n.path:
                continue
            file_symbols[n.path] += 1
            file_degree[n.path] += len(g.out_edges(n.id)) + len(g.in_edges(n.id))
        ranked = sorted(
            file_symbols.keys(),
            key=lambda p: (file_degree[p], file_symbols[p]),
            reverse=True,
        )
        fallback = [
            {"path": p, "symbols": file_symbols[p], "degree": file_degree[p]}
            for p in ranked[:top]
        ]
        return {
            "command": "hotspots",
            "git_available": False,
            "fallback": "static_complexity",
            "static_hotspots": fallback,
            "hint": ("当前不是 git 仓库或未装 git, 无历史 churn 数据。"
                     "已降级到静态复杂度热点(按符号数+连接度排序): 这是结构代理信号, "
                     "反映'哪些文件最复杂/最中心', 不代表'谁最近改/改得最频繁'。"
                     "要真实变更热点请在 git 仓库内运行。"),
            "next_actions": [
                {"cmd": "map", "why": "看静态功能块地图"},
                {"cmd": f"explore <symbol_in_top_file>", "why": "钻取复杂文件里的符号关系"},
            ],
        }
    return {
        "command": "hotspots",
        "git_available": True,
        "commits_analyzed": git.get("commits_analyzed", 0),
        "hotspots": git.get("top_hotspots", [])[:top],
        "hint": "churn 高的文件历史改动频繁, 建议重点关注测试覆盖与影响面。",
        "next_actions": [
            {"cmd": "impact <symbol_in_hotspot>", "why": "看热点文件里符号的影响面"},
            {"cmd": "search <name>", "why": "定位热点文件里的具体符号"},
        ],
    }


def cmd_explore(g: CodeGraph, node_id: str, depth: int = 1,
                max_nodes: int = 100) -> dict:
    """渐进披露钻取 —— 从一个节点向外展开 depth 层双向邻居。
    max_nodes 封顶: 大项目里一次 BFS 可能展开成千上万节点撑爆上下文,
    到达上限即停止并诚实标注 truncated, 提示缩小 depth 或改用 impact 聚焦。"""
    n = g.nodes.get(node_id)
    if not n:
        cands = g.find_by_name(node_id, fuzzy=True)
        if not cands:
            return {"command": "explore", "error": f"未找到符号: {node_id}"}
        n = cands[0]
    visited = {n.id}
    result_nodes = [_node_brief(n)]
    frontier = [n.id]
    edges_out = []
    truncated = False
    for _ in range(depth):
        nxt = []
        for cur in frontier:
            for e in g.out_edges(cur) + g.in_edges(cur):
                other = e.dst if e.src == cur else e.src
                edges_out.append({"src": e.src, "dst": e.dst,
                                  "kind": e.kind.value, "confidence": e.confidence})
                if other not in visited and other in g.nodes:
                    if len(result_nodes) >= max_nodes:
                        truncated = True
                        break
                    visited.add(other)
                    result_nodes.append(_node_brief(g.nodes[other]))
                    nxt.append(other)
            if truncated:
                break
        frontier = nxt
        if truncated:
            break
    # 去重边
    seen = set()
    uniq_edges = []
    for e in edges_out:
        k = (e["src"], e["dst"], e["kind"])
        if k not in seen:
            seen.add(k)
            uniq_edges.append(e)
    result = {
        "command": "explore",
        "center": n.id,
        "depth": depth,
        "node_count": len(result_nodes),
        "nodes": result_nodes,
        "edges": uniq_edges,
        "truncated": truncated,
        "next_actions": [
            {"cmd": "explore <id> --depth 2", "why": "更大范围展开"},
            {"cmd": "impact <id>", "why": "聚焦某节点的影响面"},
        ],
    }
    if truncated:
        result["hint"] = (f"已达 max_nodes={max_nodes} 上限, 结果被截断——这不是全部邻居。"
                          f" 缩小 --depth 或用 impact 聚焦单向影响面。")
    return result
