"""
craftsman-map MCP Server
========================
把 craftsman-map 的能力暴露成 MCP (Model Context Protocol) 工具, 让支持 MCP 的
大模型客户端 (Claude Desktop / Cline / 其它) 能"自主调用"代码库理解能力。

设计要点:
- 零依赖: 手写 JSON-RPC 2.0 over stdio, 不引入 mcp SDK (对齐项目零依赖原则)。
- 协议对齐 MCP 规范: 实现 initialize / tools/list / tools/call 三个核心方法,
  外加 notifications/initialized 的容错。
- 每个 tool 的 inputSchema 用标准 JSON Schema, 让 LLM 知道怎么传参。
- 工具名用下划线 (craftsman_map_*), 因为 MCP 工具名不允许连字符。
- 图按 root 懒加载并缓存, 避免每次调用都重新读盘。

启动方式 (在 MCP 客户端配置里):
    command: python
    args: ["-m", "craftsman_map.mcp_server"]
    # 可选: 用 CRAFTSMAN_MAP_ROOT 环境变量固定项目根

stdio 契约: 一行一个 JSON-RPC 消息 (LSP 风格的 Content-Length 头也兼容)。
本实现用"行分隔 JSON" (newline-delimited), 主流客户端均支持。
"""
from __future__ import annotations

import json
import os
import sys
import traceback

from .indexer import Indexer
from .graph.store import CodeGraph
from .commands import core
from .commands import understand as u_cmd
from .commands import report as r_cmd


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "craftsman-map"
SERVER_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# 图缓存: 按 root 懒加载
# ---------------------------------------------------------------------------
class _GraphCache:
    def __init__(self) -> None:
        self._cache: dict[str, CodeGraph] = {}
        self._stale: dict[str, bool] = {}

    def get(self, root: str) -> tuple[CodeGraph, bool]:
        """返回 (graph, auto_indexed) — auto_indexed=True 表示本次自动建了索引。"""
        root = os.path.abspath(root or ".")
        idx_path = os.path.join(root, ".craftsman-map", "graph.json")
        auto_indexed = False
        if not os.path.exists(idx_path):
            # 路径不存在时不能静默建空图 —— 那会让错误被吞成"成功"。
            # 抛出异常, 由 handle_request 捕获成 isError=True 的软失败。
            if not os.path.isdir(root):
                raise FileNotFoundError(f"项目根目录不存在: {root}")
            # 自愈前置：自动建索引，不让调用方背锅
            g = Indexer().index(root)
            g.save(root)
            self._cache[root] = g
            self._stale[root] = False
            auto_indexed = True
            return g, auto_indexed
        # 自动陈旧检测：源文件有变化时立刻返回旧图 + 后台重建，stale=True 表示数据略旧
        g, stale = CodeGraph.load_auto(root)
        self._cache[root] = g          # 始终缓存最新可用图
        self._stale[root] = stale
        return self._cache[root], auto_indexed

    def set(self, root: str, g: CodeGraph) -> None:
        self._cache[os.path.abspath(root or ".")] = g

    def invalidate(self, root: str) -> None:
        self._cache.pop(os.path.abspath(root or "."), None)


_GRAPHS = _GraphCache()


def _default_root() -> str:
    return os.environ.get("CRAFTSMAN_MAP_ROOT", ".")


# ---------------------------------------------------------------------------
# 工具定义 (name -> schema + handler)
# ---------------------------------------------------------------------------
def _tool_defs() -> list[dict]:
    root_prop = {
        "type": "string",
        "description": "项目根目录 (默认用 CRAFTSMAN_MAP_ROOT 环境变量或当前目录)",
    }
    return [
        {
            "name": "craftsman_map_index",
            "description": "扫描并建立/刷新代码库知识图谱索引。"
                           "改代码前必须先调它——没有图谱就无法查影响面、追踪调用链、分析依赖。"
                           "首次使用、接手存量项目、或代码变更后都需要重新调用。返回节点数/边数/功能块数等统计。",
            "inputSchema": {
                "type": "object",
                "properties": {"root": root_prop},
            },
        },
        {
            "name": "craftsman_map_overview",
            "description": "代码库统计概览: 各类节点数量、各类关系数量、索引元信息。"
                           "用来快速了解代码库规模与构成。",
            "inputSchema": {"type": "object", "properties": {"root": root_prop}},
        },
        {
            "name": "craftsman_map_map",
            "description": "功能块地图 (渐进披露第一层)。把代码库切分成若干功能块, "
                           "每块只给摘要与代表符号。用它先纵览全局, 锁定目标块再钻取, "
                           "避免一次性读全库浪费上下文。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "top": {"type": "integer", "description": "返回前 N 个功能块 (默认 20)"},
                },
            },
        },
        {
            "name": "craftsman_map_search",
            "description": "按名字/关键词模糊查找符号 (类/函数/变量等)。"
                           "返回匹配符号的 id、类型、路径、行号。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "query": {"type": "string", "description": "要搜索的名字或关键词"},
                    "limit": {"type": "integer", "description": "最多返回条数 (默认 15)"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "craftsman_map_symbol",
            "description": "查看单个符号的完整详情: 定义位置、签名、docstring, "
                           "以及它用了谁 (uses) 和谁用了它 (used_by)。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "id": {"type": "string",
                           "description": "符号 ID (如 'auth/login.py::LoginService.authenticate') 或名字"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "craftsman_map_impact",
            "description": "改代码前必查——如果修改这个符号会波及哪些地方。"
                           "反向追溯调用/继承/引用链, 按深度分层返回, 避免改一处坏多处。"
                           "⚠️ 需要先调 craftsman_map_index 建图, 未建图时返回空结果。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "id": {"type": "string", "description": "符号 ID 或名字"},
                    "depth": {"type": "integer", "description": "追溯最大深度 (默认 3)"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "craftsman_map_explore",
            "description": "从某符号出发, 向外展开 N 层双向邻居 (渐进披露钻取)。"
                           "用来逐步理解一个符号周围的关系网络。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "id": {"type": "string", "description": "符号 ID 或名字"},
                    "depth": {"type": "integer", "description": "展开层数 (默认 1)"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "craftsman_map_hotspots",
            "description": "git 变更热点分析: 历史上被提交碰得最多的文件, 通常是风险/活跃/"
                           "技术债聚集区。改代码或做代码审查前用它锁定高风险区域。"
                           "非 git 仓库时返回 git_available=false。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "top": {"type": "integer", "description": "返回前 N 个热点 (默认 15)"},
                },
            },
        },
        # ── v2: UNDERSTAND / VIEW / TRACE ──────────────────────────────────
        {
            "name": "craftsman_map_wiki",
            "description": "生成/读取项目人话描述 (UNDERSTAND 层)。"
                           "改代码前用它搞清楚每个功能块「是干什么的」，避免改了不该改的地方。"
                           "返回每个功能块的中文描述、关键职责、主要类/函数、对外依赖。"
                           "调用方可读取 raw_prompt 字段后用自身模型生成高质量描述，"
                           "再调 craftsman_map_inject_desc 写回。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "cluster": {"type": "integer",
                                "description": "只查看指定功能块 (不传则返回全部)"},
                    "view": {"type": "string", "enum": ["summary", "full"],
                             "description": "summary=分页+首行摘要(省上下文,默认); full=全量完整描述。两者都返回JSON结构"},
                    "offset": {"type": "integer",
                               "description": "分页偏移 (summary 模式, 默认 0)"},
                    "top": {"type": "integer",
                            "description": "每页显示多少个功能块 (summary 模式, 默认 15)"},
                },
            },
        },
        {
            "name": "craftsman_map_describe",
            "description": "输出描述原料包: 为调用方生成'喂给 LLM 的 prompt'。"
                           "调用方把 raw_prompt 字段的内容发给自己的模型, 拿到描述后"
                           "调 craftsman_map_inject_desc 写回缓存。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "cluster": {"type": "integer",
                                "description": "指定功能块 (不传则输出全部块的原料包)"},
                    "stale_only": {"type": "boolean",
                                   "description": "只输出已过期(代码变了)的块 (默认 false)"},
                },
            },
        },
        {
            "name": "craftsman_map_inject_desc",
            "description": "把调用方生成的描述注入缓存 (三段式工作流第三步)。"
                           "注入后对应功能块的描述升级为 LLM 版, 并绑定当前代码指纹。"
                           "代码再次变更时该块自动失效, 需重新走 describe → inject 流程。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "cluster": {"type": "integer",
                                "description": "要注入描述的功能块编号"},
                    "description": {"type": "string",
                                    "description": "LLM 生成的描述文本"},
                },
                "required": ["cluster", "description"],
            },
        },
        {
            "name": "craftsman_map_layers",
            "description": "架构分层视图 (VIEW 层): 把功能块自动归类到"
                           "'入口层/核心层/工具层/配置层/数据层/测试层'。"
                           "让不写代码的人和大模型一眼看清项目骨架。",
            "inputSchema": {
                "type": "object",
                "properties": {"root": root_prop},
            },
        },
        {
            "name": "craftsman_map_entrypoints",
            "description": "自动检测项目真实入口点 (TRACE 层辅助)。"
                           "按'出度高、入度低、名字像入口'打分排序, "
                           "返回最可能是入口的函数/方法列表。用于确定 trace 的起点。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "top": {"type": "integer", "description": "返回前 N 个候选 (默认 8)"},
                    "include_tests": {"type": "boolean",
                                      "description": "是否包含测试代码节点 (默认 false, 排除 tests/conftest/test_* 去噪)"},
                },
            },
        },
        {
            "name": "craftsman_map_trace",
            "description": "工作链追踪 (TRACE 层核心): 从指定入口出发, "
                           "追踪'请求依次经过哪些函数'的调用路径。"
                           "返回主路径、触达函数总数、环检测结果。"
                           "大模型定位 bug / 理解数据流 / 改代码前必用。"
                           "call_tree 默认折叠(只到2层, 防上下文爆炸); 要完整分支树传 full=true。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": root_prop,
                    "entry": {"type": "string",
                              "description": "入口符号 ID 或名字 (如 'pipeline/orchestrator.py::Orchestrator.generate')"},
                    "max_depth": {"type": "integer",
                                  "description": "最大追踪深度 (默认 8)"},
                    "max_tree_nodes": {"type": "integer",
                                       "description": "call_tree 最多输出节点数, 防上下文爆炸 (默认 40)"},
                    "full": {"type": "boolean",
                             "description": "展开完整调用树 (默认 false 折叠到 2 层, 主路径始终完整)"},
                },
                "required": ["entry"],
            },
        },
        {
            "name": "craftsman_map_orient",
            "description": "项目导航图 (ORIENT): 接手陌生项目或改代码前的第一步。"
                           "一次调用返回'这项目是什么/多大/什么语言/有多少盲区/建议先调哪几个命令'。"
                           "先看这里再改代码，避免改错地方或返工。",
            "inputSchema": {
                "type": "object",
                "properties": {"root": root_prop},
            },
        },
        {
            "name": "craftsman_map_report",
            "description": "项目认知原料包 (REPORT): 输出目标/验收标准/缺失项/"
                           "进展线索的客观事实。铁律—只报事实(README原文/测试数/"
                           "TODO数/空实现), 绝不吐'完成度60%'或重构建议这类主观判断—"
                           "那些请调用方大模型基于原料自行推断并标注'这是推断'。",
            "inputSchema": {
                "type": "object",
                "properties": {"root": root_prop},
            },
        },
    ]


# ---------------------------------------------------------------------------
# 工具调用分派
# ---------------------------------------------------------------------------
def _call_tool(name: str, args: dict) -> dict:
    root = args.get("root") or _default_root()

    if name == "craftsman_map_index":
        target = os.path.abspath(root)
        idx = Indexer()
        g = idx.index(target)
        g.save(target)
        _GRAPHS.set(target, g)
        return {"command": "index", "status": "ok", "meta": g.meta,
                "next_actions": [{"cmd": "craftsman_map_map", "why": "查看功能块地图"}]}

    g, auto_indexed = _GRAPHS.get(root)
    _auto_hint = {"auto_indexed": True, "note": "首次调用，已自动建立索引"} if auto_indexed else {}

    if name == "craftsman_map_overview":
        return {**core.cmd_overview(g), **_auto_hint}
    if name == "craftsman_map_map":
        return {**core.cmd_map(g, top=int(args.get("top", 20))), **_auto_hint}
    if name == "craftsman_map_search":
        return {**core.cmd_search(g, args["query"], limit=int(args.get("limit", 15))), **_auto_hint}
    if name == "craftsman_map_symbol":
        return {**core.cmd_symbol(g, args["id"]), **_auto_hint}
    if name == "craftsman_map_impact":
        return {**core.cmd_impact(g, args["id"], max_depth=int(args.get("depth", 3))), **_auto_hint}
    if name == "craftsman_map_explore":
        return {**core.cmd_explore(g, args["id"], depth=int(args.get("depth", 1))), **_auto_hint}
    if name == "craftsman_map_hotspots":
        return {**core.cmd_hotspots(g, top=int(args.get("top", 15))), **_auto_hint}

    # ── v2: UNDERSTAND / VIEW / TRACE ──────────────────────────────────────
    if name == "craftsman_map_wiki":
        _fmt = args.get("view") or args.get("format") or "summary"
        return {**u_cmd.cmd_wiki(g, root, fmt=_fmt,
                                 offset=int(args.get("offset", 0)),
                                 top=int(args.get("top", 15))), **_auto_hint}
    if name == "craftsman_map_describe":
        return {**u_cmd.cmd_describe(g, cluster_id=args.get("cluster_id"),
                                     fmt=args.get("format", "prompt")), **_auto_hint}
    if name == "craftsman_map_inject_desc":
        return u_cmd.cmd_inject_desc(
            g, root,
            cluster_id=args["cluster_id"],
            text=args["text"],
            title=args.get("title", ""),
        )
    if name == "craftsman_map_layers":
        return {**u_cmd.cmd_layers(g), **_auto_hint}
    if name == "craftsman_map_entrypoints":
        return {**u_cmd.cmd_entrypoints(g, top=int(args.get("top", 5)),
                                        include_tests=bool(args.get("include_tests", False))), **_auto_hint}
    if name == "craftsman_map_trace":
        return {**u_cmd.cmd_trace(g, entry_id=args.get("entry") or args["entry_id"],
                                  max_depth=int(args.get("max_depth", 8)),
                                  max_tree_nodes=int(args.get("max_tree_nodes", 40)),
                                  full=bool(args.get("full", False))), **_auto_hint}

    # ── v2.1: 项目级认知入口 ──────────────────────────────────────────────
    if name == "craftsman_map_orient":
        return {**r_cmd.cmd_orient(g, root), **_auto_hint}
    if name == "craftsman_map_report":
        return {**r_cmd.cmd_report(root, g), **_auto_hint}

    raise ValueError(f"未知工具: {name}")


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 分派
# ---------------------------------------------------------------------------
def handle_request(req: dict) -> dict | None:
    """处理单条 JSON-RPC 请求。notification (无 id) 返回 None。"""
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    # notification (无需回复)
    if req_id is None:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    try:
        if method == "initialize":
            return ok({
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "tools/list":
            return ok({"tools": _tool_defs()})
        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments") or {}
            try:
                result = _call_tool(tool_name, tool_args)
                text = json.dumps(result, ensure_ascii=False)
                # 命令层的错误约定是返回带 "error" 键的 dict (不抛异常)。
                # 这里统一检测该键, 把它映射成 MCP 协议的 isError=True,
                # 一处覆盖所有 16 个工具的错误路径, 含未来新增工具。
                is_error = isinstance(result, dict) and "error" in result
                return ok({"content": [{"type": "text", "text": text}],
                           "isError": is_error})
            except Exception as e:  # 工具级错误: 返回给 LLM 而非中断连接
                text = json.dumps({"error": str(e)}, ensure_ascii=False)
                return ok({"content": [{"type": "text", "text": text}],
                           "isError": True})
        if method == "ping":
            return ok({})
        return err(-32601, f"Method not found: {method}")
    except Exception as e:  # noqa
        return err(-32603, f"Internal error: {e}\n{traceback.format_exc()}")


def serve(stdin=None, stdout=None) -> None:
    """stdio 主循环: 行分隔 JSON-RPC。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        # 支持 batch
        reqs = req if isinstance(req, list) else [req]
        for r in reqs:
            resp = handle_request(r)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
