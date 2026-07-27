"""
TRACE 工作链 —— 追踪"请求从入口到出口走过哪些函数"
====================================================
静态调用图告诉你"谁能调谁"，工作链告诉你"从这个入口出发，实际会依次
走过哪条调用路径"。这是大模型定位 bug、理解数据流最需要的能力。

做法: 从入口节点沿 calls 边做 DFS，输出:
  - 主路径 (最深/最长的一条调用链)
  - 完整调用树 (分层)
  - 循环检测 (递归/环)
  - 出口 (叶子节点: 不再调用任何项目内函数的终点)

置信度: 沿途每条边的 confidence 取最小值 = 该路径整体可信度。
低置信度路径明确标注 —— 不拿猜测冒充真实调用链。
"""
from __future__ import annotations

from ..graph.store import CodeGraph
from ..graph.model import EdgeKind, NodeKind


def _resolve(g: CodeGraph, node_id: str):
    n = g.nodes.get(node_id)
    if n:
        return n
    cands = g.find_by_name(node_id, fuzzy=True)
    return cands[0] if cands else None


def _classify_cycles(g: CodeGraph, cycles: list[list[str]]) -> list[dict]:
    """给每个环分类 + 标注, 而不是甩一串裸路径让调用方自己猜哪些要紧。

    分类 (基于环内去重节点数):
      - direct_recursion   直接递归: 函数自己调自己 (环内只有 1 个不同节点)。
                           classmethod/工厂里的自引用多属此类, 通常是正常递归, 低风险。
      - mutual_recursion   相互递归: 两个函数互调 (环内 2 个不同节点)。多为设计使然。
      - cycle              多节点环: >=3 个节点成环。可能是正常回调, 也可能是意外循环依赖,
                           值得调用方留意。
    只报客观分类 + 路径, 不下"这是 bug"的主观结论 —— 判断留给调用方。"""
    out = []
    for c in cycles:
        readable = [g.nodes[i].qualified_name if i in g.nodes else i for i in c]
        # 环路径形如 [A, A] 或 [A, B, A]; 去掉尾部闭合重复再数不同节点
        distinct = set(c[:-1]) if len(c) > 1 and c[0] == c[-1] else set(c)
        n_distinct = len(distinct)
        if n_distinct <= 1:
            kind, note = "direct_recursion", "函数直接调用自身(直接递归); 通常正常, 低风险"
        elif n_distinct == 2:
            kind, note = "mutual_recursion", "两个函数互相调用(相互递归); 多为设计使然"
        else:
            kind, note = "cycle", "多节点调用环; 可能是回调或循环依赖, 建议留意"
        out.append({"path": readable, "type": kind,
                    "distinct_nodes": n_distinct, "note": note})
    return out


def trace_chain(g: CodeGraph, entry_id: str, max_depth: int = 8,
                max_tree_nodes: int = 40, full: bool = False) -> dict:
    """从 entry 出发追调用链。

    体积治理 (P1)——防止 call_tree 全量倾倒烧穿调用方上下文:
      - max_depth      : DFS 最大深度 (默认 8, 超过标 truncated)
      - max_tree_nodes : call_tree 最多输出多少节点 (默认 40, 超过停止展开并标注)
      - full           : 默认 False 只给折叠树 (depth<=2 骨架 + 分支计数);
                         True 给完整树 (仍受 max_tree_nodes 上限约束)
    无论如何, main_path (最长主流程链) + 统计 + cycles 始终完整返回,
    默认模式下已足够大模型理解主流程, 不必展开全树。
    """
    entry = _resolve(g, entry_id)
    if not entry:
        return {"command": "trace", "error": f"未找到入口符号: {entry_id}",
                "next_actions": [{"cmd": "search <name>", "why": "先定位入口符号"}]}

    # DFS 沿 calls 边; 记录路径与环
    tree = {"id": entry.id, "name": entry.qualified_name,
            "kind": entry.kind.value, "children": [], "confidence": 1.0}
    cycles: list[list[str]] = []
    all_paths: list[dict] = []          # 每条根到叶路径 + 最小置信度
    visited_global: set[str] = set()
    traced_edge_confs: list[float] = []  # 本次追踪走过的每条 calls 边置信度
    tree_nodes = [1]                     # call_tree 已生成节点计数 (entry 算 1)
    tree_capped = [False]                # 是否因 max_tree_nodes 截断过

    def dfs(node_id: str, node_obj: dict, stack: list[str],
            path_conf: float, depth: int):
        if depth >= max_depth:
            node_obj["truncated"] = "max_depth"
            all_paths.append({"path": stack[:], "confidence": path_conf,
                              "truncated": True})
            return
        call_edges = [e for e in g.out_edges(node_id, EdgeKind.CALLS)]
        # 也追 contains->method 后的 calls? 只追直接 calls, 保持链路纯粹
        real_targets = [(e, g.nodes[e.dst]) for e in call_edges
                        if e.dst in g.nodes]
        if not real_targets:
            # 叶子 = 出口
            all_paths.append({"path": stack[:], "confidence": path_conf,
                              "leaf": node_id})
            return
        for e, tgt in real_targets:
            if tgt.id in stack:
                # 环: 记录后不再深入 (环节点始终不占 call_tree 节点预算)
                cycles.append(stack[stack.index(tgt.id):] + [tgt.id])
                node_obj["children"].append({
                    "id": tgt.id, "name": tgt.qualified_name,
                    "kind": tgt.kind.value, "cycle": True,
                    "confidence": e.confidence})
                continue
            # 关键路径统计 (visited/置信度) 不受 tree 上限影响 —— 始终追全
            first_visit = tgt.id not in visited_global
            visited_global.add(tgt.id)
            traced_edge_confs.append(e.confidence)
            # call_tree 节点预算: 到顶就不再往树里塞节点, 但继续 DFS 统计
            if tree_nodes[0] >= max_tree_nodes:
                tree_capped[0] = True
                # 不建 child, 用一个轻量指针继续走统计 (不挂进树)
                dfs(tgt.id, {"children": []}, stack + [tgt.id],
                    min(path_conf, e.confidence), depth + 1)
                continue
            child = {"id": tgt.id, "name": tgt.qualified_name,
                     "kind": tgt.kind.value, "children": [],
                     "confidence": e.confidence}
            node_obj["children"].append(child)
            tree_nodes[0] += 1
            dfs(tgt.id, child, stack + [tgt.id],
                min(path_conf, e.confidence), depth + 1)

    dfs(entry.id, tree, [entry.id], 1.0, 0)

    # 本次追踪的低置信边统计 (诚实标注: 让大模型知道这条链有多少是启发式推断)
    low_conf_edges = sum(1 for c in traced_edge_confs if c < 0.6)
    total_edges = len(traced_edge_confs)

    # 主路径 = 最长的一条 (节点数最多), 平局取置信度高的
    main_path = max(all_paths, key=lambda p: (len(p["path"]), p["confidence"]),
                    default={"path": [entry.id], "confidence": 1.0})
    # 把 id 路径映射成可读名字
    main_readable = [g.nodes[i].qualified_name if i in g.nodes else i
                     for i in main_path["path"]]

    # ---- call_tree 体积治理: 默认折叠成骨架, full=True 才给完整树 ----
    def _fold(node: dict, depth: int) -> dict:
        """折叠模式: 只保留 depth<=2 的树, 更深的用 '{n} 个子调用未展开' 计数代替。"""
        out = {k: node[k] for k in ("id", "name", "kind", "confidence")
               if k in node}
        if node.get("cycle"):
            out["cycle"] = True
            return out
        children = node.get("children", [])
        if depth >= 2 and children:
            out["collapsed"] = f"{len(children)} 个子调用未展开 (full=true 查看)"
        elif children:
            out["children"] = [_fold(c, depth + 1) for c in children]
        return out

    if full:
        call_tree = tree
        tree_mode = "full"
    else:
        call_tree = _fold(tree, 0)
        tree_mode = "folded"

    result = {
        "command": "trace",
        "entry": {"id": entry.id, "name": entry.qualified_name,
                  "kind": entry.kind.value},
        "reached_functions": len(visited_global),
        "main_path": main_readable,
        "main_path_length": len(main_path["path"]),
        "main_path_confidence": round(main_path["confidence"], 2),
        "main_path_confidence_note": ("整条链最弱一环的置信度; <0.6 说明链路含启发式推断的调用, "
                                      "可能不准") if main_path["confidence"] < 0.6 else "静态可信",
        "call_tree": call_tree,
        "call_tree_mode": tree_mode,
        "call_tree_nodes_shown": tree_nodes[0],
        "cycles": _classify_cycles(g, cycles[:10]),
        "has_cycles": bool(cycles),
        "traced_edges": total_edges,
        "low_confidence_edges": low_conf_edges,
        "limitations": {
            "method": "静态调用图 DFS —— 只追代码里字面可见的 calls 边",
            "cannot_capture": [
                "动态派发: getattr(obj, name)() / dispatch table / 反射调用",
                "回调与高阶函数: 注册后由框架/事件循环触发的调用",
                "多态: 只连到声明类型, 运行时实际子类实现可能不同",
                "跨语言边界: 前端 JS 调后端 API、Python 调 C 扩展等无法连起",
            ],
            "note": (f"本次链路 {total_edges} 条调用边中 {low_conf_edges} 条置信度<0.6 (启发式推断)。"
                     if total_edges else "本次链路无项目内调用边。")
                    + " 以上四类调用不会出现在此链中 —— 链路可能不完整, 这是静态分析的物理边界, 非遗漏。",
        },
        "hint": "main_path 是最长调用链(最可能的主流程),默认已足够理解主流程。"
                "call_tree 默认折叠(只到 2 层);要完整分支树用 full=true。"
                "环(cycles)通常是递归或回调。低置信边=启发式推断,可能不准。",
        "next_actions": [
            {"cmd": "symbol <id>", "why": "查链路上某函数的细节"},
            {"cmd": "impact <id>", "why": "看链路上某节点被谁依赖"},
        ],
    }
    if tree_capped[0]:
        result["call_tree_truncated"] = {
            "reason": f"call_tree 达到 max_tree_nodes={max_tree_nodes} 上限已截断",
            "note": (f"实际可达 {len(visited_global)} 个函数, 树中只展示了 {tree_nodes[0]} 个节点。"
                     "统计数字(reached_functions/traced_edges)是全量真实值, 仅树的展示被裁剪。"),
        }
    return result


def _is_test_node(n) -> bool:
    """判断节点是否属于测试代码 —— 测试函数/fixture 不是项目真入口, 应去噪。

    命中任一即算测试:
      - 路径在 tests/ 目录下 (或以 test_ 开头 / _test 结尾的文件)
      - conftest.py (pytest fixture 聚集地)
      - 函数名以 test_ 开头 (pytest 用例命名约定)
    """
    p = n.path.replace("\\", "/").lower()
    parts = p.split("/")
    fname = parts[-1] if parts else p
    if "tests" in parts or "test" in parts:
        return True
    if fname == "conftest.py" or fname.startswith("test_") or fname.endswith("_test.py"):
        return True
    if n.qualified_name.split(".")[-1].startswith("test_"):
        return True
    return False


def list_entrypoints(g: CodeGraph, top: int = 20, include_tests: bool = False) -> dict:
    """自动找候选入口: 出度(calls)高、入度(被调)为0或极低的函数。

    默认排除测试代码 (tests/ 目录、conftest、test_ 用例) —— 它们不是项目
    真入口, 混进来会污染'真入口'列表。include_tests=True 可关闭该过滤。
    """
    candidates = []
    excluded_tests = 0
    for n in g.nodes.values():
        if n.kind not in (NodeKind.FUNCTION,):
            continue
        out_calls = len(g.out_edges(n.id, EdgeKind.CALLS))
        in_calls = len(g.in_edges(n.id, EdgeKind.CALLS))
        if out_calls == 0:
            continue
        # 去噪: 测试代码不是真入口
        if not include_tests and _is_test_node(n):
            excluded_tests += 1
            continue
        # 入口特征: 没人调它 (in_calls=0) 但它调很多 (out_calls 高)
        score = out_calls - in_calls * 2
        # main / cli / handler / run 命名加权
        nm = n.qualified_name.lower()
        if any(k in nm for k in ("main", "run", "handle", "execute", "cmd_", "generate", "build")):
            score += 5
        candidates.append((score, out_calls, in_calls, n))
    candidates.sort(key=lambda t: -t[0])
    entries = [{
        "id": n.id, "name": n.qualified_name, "path": n.path,
        "out_calls": oc, "in_calls": ic, "score": sc,
    } for sc, oc, ic, n in candidates[:top]]
    result = {
        "command": "entrypoints",
        "count": len(entries),
        "entrypoints": entries,
        "hint": "score 高 = 更像入口(调用别人多、被调用少)。用 trace <id> 追它的工作链。",
        "next_actions": [{"cmd": "trace <entry_id>", "why": "追某入口的完整调用链"}],
    }
    # 诚实标注: 明确告诉调用方排除了多少测试节点
    if excluded_tests:
        result["excluded_tests"] = excluded_tests
        result["note"] = (f"已排除 {excluded_tests} 个测试代码节点(tests/conftest/test_*)以去噪; "
                          f"如需包含请传 include_tests=True")
    return result
