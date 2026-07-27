"""
craftsman-map CLI 入口
======================
给大模型用的确定性命令行。所有命令默认输出 JSON (LLM 原生可解析)。

用法:
  craftsman-map index [PATH]              建立/刷新代码库索引
  craftsman-map overview                  索引统计概览
  craftsman-map map [--top N]             功能块地图 (渐进披露第一层)
  craftsman-map search <QUERY>            按名字/关键词找符号
  craftsman-map symbol <ID>               单符号详情 (定义+邻居)
  craftsman-map impact <ID> [--depth N]   影响面分析
  craftsman-map explore <ID> [--depth N]  从节点展开邻居

全局参数:
  --root PATH   指定项目根 (默认当前目录)
  --pretty      人类可读缩进 (默认紧凑 JSON 省 token)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .indexer import Indexer
from .graph.store import CodeGraph
from .commands import core
from .commands import understand as u_cmd
from .commands import report as r_cmd


def _emit(obj: dict, pretty: bool) -> None:
    if pretty:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


# 命令→正确用法示例, 参数错误时给 LLM 一条可照抄的正确命令 (而非 argparse 通用 usage)
_CMD_USAGE = {
    "index": "craftsman-map index [PATH]",
    "overview": "craftsman-map overview",
    "map": "craftsman-map map [--top N]",
    "search": "craftsman-map search <QUERY> [--limit N]",
    "symbol": "craftsman-map symbol <ID>",
    "impact": "craftsman-map impact <ID> [--depth N]",
    "explore": "craftsman-map explore <ID> [--depth N]",
    "hotspots": "craftsman-map hotspots [--top N]",
    "wiki": "craftsman-map wiki [--view summary|full] [--offset N] [--top N]",
    "describe": "craftsman-map describe <CLUSTER> | describe --cluster <CLUSTER> [--format prompt|raw]",
    "desc": "craftsman-map desc --cluster <ID> --text <描述> [--title <标题>]",
    "desc-project": "craftsman-map desc-project --text <描述>",
    "layers": "craftsman-map layers",
    "orient": "craftsman-map orient",
    "report": "craftsman-map report",
    "trace": "craftsman-map trace <ID> [--depth N] [--tree-nodes N] [--full]",
    "entrypoints": "craftsman-map entrypoints [--top N] [--include-tests]",
}


class _JsonArgumentParser(argparse.ArgumentParser):
    """参数错误时吐结构化 JSON (而不是 argparse 的人类 usage 文本)。
    craftsman-map 是给 LLM 用的: 报错也要机器可解析 + 给一条可照抄的正确用法。"""

    def error(self, message: str):  # noqa: A003
        # 从 prog 里取子命令名 (形如 'craftsman-map describe')
        cmd = self.prog.split()[-1] if self.prog else ""
        payload = {
            "error": "参数错误",
            "detail": message,
            "command": cmd,
        }
        usage = _CMD_USAGE.get(cmd)
        if usage:
            payload["correct_usage"] = usage
        else:
            payload["available_commands"] = sorted(_CMD_USAGE.keys())
            payload["hint"] = "用 craftsman-map <命令> 调用; 上面是全部可用命令。"
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
              file=sys.stderr)
        sys.exit(2)


def _load_graph(root: str, pretty: bool = False) -> CodeGraph:
    """加载图谱，源文件有变化时自动重建（零配置陈旧检测）。"""
    root = os.path.abspath(root)
    idx_path = os.path.join(root, ".craftsman-map", "graph.json")
    if not os.path.exists(idx_path):
        _emit({"error": "尚未建立索引", "hint": "先运行: craftsman-map index",
               "root": root}, True)
        sys.exit(2)
    # CLI 是一次性短命进程: 用 sync=True 同步重建并落盘指纹,
    # 否则后台 daemon 线程随进程退出被杀, 指纹存不下 → 每次调用都重复触发重建。
    g, rebuilt = CodeGraph.load_auto(root, sync=True)
    if rebuilt:
        # 精简一行提示到 stderr (不污染 stdout 的 JSON), 只给关键规模数字,
        # 不再倾倒完整 exclusions/meta —— 那坨每次调用重复, 是纯噪音。
        m = g.meta or {}
        print(json.dumps({
            "_rebuilt": True, "reason": "source files changed",
            "nodes": m.get("node_count"), "edges": m.get("edge_count"),
            "clusters": m.get("cluster_count"),
        }, ensure_ascii=False), file=sys.stderr)
    return g


def build_parser() -> argparse.ArgumentParser:
    # 全局参数在主 parser + 每个子命令都注册, 让 --root/--pretty 在子命令前后都能用
    # (LLM 不该被参数位置卡住)。子命令侧用 SUPPRESS 默认值, 未提供时不覆盖主 parser 的值。
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--root", default=".", help="项目根目录 (默认当前目录)")
    g.add_argument("--pretty", action="store_true", help="人类可读缩进输出")

    gsub = argparse.ArgumentParser(add_help=False)
    gsub.add_argument("--root", default=argparse.SUPPRESS, help="项目根目录")
    gsub.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                      help="人类可读缩进输出")

    p = _JsonArgumentParser(prog="craftsman-map", parents=[g],
                            description="代码库理解 CLI (地图+导航+全量理解, 专为大模型设计)")
    # parser_class 让所有子命令 parser 也用 _JsonArgumentParser → 子命令参数错误同样吐 JSON
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_JsonArgumentParser)

    sub.add_parser("index", parents=[gsub], help="建立/刷新索引"
                   ).add_argument("path", nargs="?", default=None)
    sub.add_parser("overview", parents=[gsub], help="索引统计概览")

    sp = sub.add_parser("map", parents=[gsub], help="功能块地图")
    sp.add_argument("--top", type=int, default=12, help="每页显示多少个功能块 (默认 12, 控体积)")

    sp = sub.add_parser("search", parents=[gsub], help="找符号")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=15)

    sp = sub.add_parser("symbol", parents=[gsub], help="符号详情")
    sp.add_argument("id")

    sp = sub.add_parser("impact", parents=[gsub], help="影响面分析")
    sp.add_argument("id")
    sp.add_argument("--depth", type=int, default=3)

    sp = sub.add_parser("explore", parents=[gsub], help="展开邻居")
    sp.add_argument("id")
    sp.add_argument("--depth", type=int, default=1)

    sp = sub.add_parser("hotspots", parents=[gsub], help="git 变更热点")
    sp.add_argument("--top", type=int, default=15)

    # ---- v2 UNDERSTAND / VIEW / TRACE ----
    sp = sub.add_parser("wiki", parents=[gsub], help="读取/刷新代码库人话描述")
    sp.add_argument("--view", choices=["summary", "full"], default="summary",
                    help="summary=分页+首行摘要(默认,省上下文); full=全量完整描述。两者都返回JSON")
    sp.add_argument("--format", choices=["human", "json"], default=None,
                    help="[已弃用,请用 --view] 旧值 human→summary, json→full")
    sp.add_argument("--offset", type=int, default=0, help="分页偏移 (summary 模式)")
    sp.add_argument("--top", type=int, default=15, help="每页显示多少个功能块 (summary 模式)")

    sp = sub.add_parser("describe", parents=[gsub],
                        help="输出功能块描述原料包(给调用方LLM生成描述)")
    # 位置参数与 --cluster 二选一, 统一其它命令的位置参数风格 (describe 7 == describe --cluster 7)
    sp.add_argument("cluster_pos", nargs="?", type=int, default=None,
                    metavar="CLUSTER", help="指定功能块 id (位置参数, 不填=全部)")
    sp.add_argument("--cluster", type=int, default=None, help="指定功能块 (等价于位置参数)")
    sp.add_argument("--format", choices=["prompt", "raw"], default="prompt")

    sp = sub.add_parser("desc", parents=[gsub], help="回写调用方生成的功能块描述")
    sp.add_argument("--cluster", type=int, required=True)
    sp.add_argument("--text", required=True, help="描述文本")
    sp.add_argument("--title", default="", help="可选标题")

    sp = sub.add_parser("desc-project", parents=[gsub], help="回写项目级描述")
    sp.add_argument("--text", required=True)

    sub.add_parser("layers", parents=[gsub], help="分层架构视图")

    # ---- v2.1 项目级认知入口 ----
    sub.add_parser("orient", parents=[gsub],
                   help="项目导航图: 一次调用给出规模/语言/盲区/建议路线")
    sub.add_parser("report", parents=[gsub],
                   help="项目认知原料包: 目标/验收/缺失/进展线索(客观事实)")

    sp = sub.add_parser("trace", parents=[gsub], help="从入口追工作链(调用链路)")
    sp.add_argument("id")
    sp.add_argument("--depth", type=int, default=8)
    sp.add_argument("--tree-nodes", type=int, default=40,
                    help="call_tree 最多输出节点数(防上下文爆炸, 默认 40)")
    sp.add_argument("--full", action="store_true",
                    help="展开完整调用树(默认折叠到 2 层)")

    sp = sub.add_parser("entrypoints", parents=[gsub], help="列出候选入口函数")
    sp.add_argument("--top", type=int, default=20)
    sp.add_argument("--include-tests", action="store_true",
                    help="包含测试代码节点(默认排除 tests/conftest/test_* 去噪)")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root
    pretty = args.pretty

    if args.cmd == "index":
        target = os.path.abspath(args.path or root)
        idx = Indexer()
        g = idx.index(target)
        g.meta["fingerprint"] = CodeGraph.fingerprint(target)
        g.save(target)
        _emit({"command": "index", "status": "ok", "meta": g.meta,
               "next_actions": [{"cmd": "map", "why": "查看功能块地图"},
                                {"cmd": "overview", "why": "查看统计概览"}]}, pretty)
        return 0

    g = _load_graph(root, pretty)

    if args.cmd == "overview":
        _emit(core.cmd_overview(g), pretty)
    elif args.cmd == "map":
        _emit(core.cmd_map(g, top=args.top), pretty)
    elif args.cmd == "search":
        _emit(core.cmd_search(g, args.query, limit=args.limit), pretty)
    elif args.cmd == "symbol":
        _emit(core.cmd_symbol(g, args.id), pretty)
    elif args.cmd == "impact":
        _emit(core.cmd_impact(g, args.id, max_depth=args.depth), pretty)
    elif args.cmd == "explore":
        _emit(core.cmd_explore(g, args.id, depth=args.depth), pretty)
    elif args.cmd == "hotspots":
        _emit(core.cmd_hotspots(g, top=args.top), pretty)
    # ---- v2 ----
    elif args.cmd == "wiki":
        # 优先新参数 --view; 用户仍用旧 --format 时降级兼容 (None 表示没给)
        _wiki_fmt = args.format if getattr(args, "format", None) else args.view
        _emit(u_cmd.cmd_wiki(g, os.path.abspath(root), fmt=_wiki_fmt,
                             offset=args.offset, top=args.top), pretty)
    elif args.cmd == "describe":
        # 位置参数优先, 回退到 --cluster (两者等价, 统一参数风格)
        _cid = args.cluster_pos if args.cluster_pos is not None else args.cluster
        _emit(u_cmd.cmd_describe(g, _cid, fmt=args.format), pretty)
    elif args.cmd == "desc":
        _emit(u_cmd.cmd_inject_desc(g, os.path.abspath(root), args.cluster,
                                    args.text, title=args.title), pretty)
    elif args.cmd == "desc-project":
        _emit(u_cmd.cmd_inject_project(os.path.abspath(root), args.text), pretty)
    elif args.cmd == "layers":
        _emit(u_cmd.cmd_layers(g), pretty)
    elif args.cmd == "trace":
        _emit(u_cmd.cmd_trace(g, args.id, max_depth=args.depth,
                              max_tree_nodes=args.tree_nodes, full=args.full), pretty)
    elif args.cmd == "entrypoints":
        _emit(u_cmd.cmd_entrypoints(g, top=args.top,
                                    include_tests=args.include_tests), pretty)
    # ---- v2.1 项目级认知入口 ----
    elif args.cmd == "orient":
        _emit(r_cmd.cmd_orient(g, os.path.abspath(root)), pretty)
    elif args.cmd == "report":
        _emit(r_cmd.cmd_report(os.path.abspath(root), g), pretty)
    else:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
