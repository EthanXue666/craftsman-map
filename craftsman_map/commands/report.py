"""
ORIENT / REPORT 命令层 —— 项目级认知入口
=========================================
两个命令都只吐"客观事实原料", 主观判断(完成度百分比/重构建议)留给调用方 LLM。
这是 craftsman-map 的铁律: 工具只报事实, 判断留给能负责的大模型,
且事实与判断之间有清晰的墙 —— 绝不自己吐"完成度 60%"这类猜测。

  orient  —— 项目导航图: 一次调用回答"这项目是什么/多大/什么语言/有多少盲区/
             建议先调哪几个命令", 消除大模型面对 14 个并列命令的选择困惑。
  report  —— 项目认知原料包(A 类客观事实):
             [目标]  README/docstring 抽取的原文 (标来源, 没写就明说"未声明")
             [验收]  测试套件/CI/门禁的客观清单
             [缺失]  无测试模块 + TODO/FIXME + 空实现 (全是可测量事实)
             [进展线索] git 活跃度 + TODO 密度 + 测试覆盖 (标"线索非结论")
"""
from __future__ import annotations

import os
import re

from ..graph.store import CodeGraph
from ..graph.model import NodeKind, EdgeKind


# ---------------------------------------------------------------------------
# 共享: 语言构成统计
# ---------------------------------------------------------------------------
_EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go",
    ".java": "Java", ".rb": "Ruby", ".rs": "Rust", ".c": "C",
    ".cpp": "C++", ".h": "C/C++", ".cs": "C#", ".php": "PHP",
    ".md": "Markdown", ".rst": "reStructuredText",
}


def _language_breakdown(g: CodeGraph) -> dict:
    """从 module 节点的路径扩展名统计语言构成。"""
    counts: dict[str, int] = {}
    for n in g.nodes.values():
        if n.kind != NodeKind.MODULE:
            continue
        ext = os.path.splitext(n.path)[1].lower()
        lang = _EXT_LANG.get(ext, ext or "unknown")
        counts[lang] = counts.get(lang, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# 文档/标记类语言不算"编程语言", 不触发跨语言断链警告
_DOC_LANGS = {"Markdown", "reStructuredText", "unknown"}


def _programming_langs(langs: dict) -> list[str]:
    """从语言构成里筛出真正的编程语言 (排除文档/标记格式)。"""
    return [lang for lang in langs
            if lang not in _DOC_LANGS and not lang.startswith(".")]


# ---------------------------------------------------------------------------
# orient —— 项目导航图
# ---------------------------------------------------------------------------
def cmd_orient(g: CodeGraph, root: str = "") -> dict:
    """一次调用给大模型一张导航图, 而不是 14 个并列按钮。"""
    meta = g.meta or {}
    langs = _language_breakdown(g)
    module_count = sum(1 for n in g.nodes.values() if n.kind == NodeKind.MODULE)
    cluster_count = meta.get("cluster_count", 0)
    blind = meta.get("blind_spot_count", 0)

    # 规模分档 (纯客观阈值, 不是主观判断)
    node_count = len(g.nodes)
    if node_count < 200:
        scale = "small"
    elif node_count < 2000:
        scale = "medium"
    else:
        scale = "large"

    # 项目级描述: wiki 缓存是唯一真相源(desc-project 写在这里), 优先读它;
    # 读不到再兜底 meta(旧路径), 保证不同存储都能命中。
    project_desc = ""
    if root:
        try:
            from ..understand.wiki import load_wiki
            proj_cache = (load_wiki(root) or {}).get("project") or {}
            project_desc = proj_cache.get("description", "") or ""
        except Exception:
            project_desc = ""
    if not project_desc:
        proj = meta.get("project_description")
        if isinstance(proj, dict):
            project_desc = proj.get("text", "")
        elif isinstance(proj, str):
            project_desc = proj

    # 冷启动兜底: 描述未注入时, 直接从 README 抽一句 tagline/intro 内联进 what,
    # 让调用方第一眼就知道"这项目干嘛", 不用再跳一次 report。
    # 标注 what_source, 诚实区分"注入的描述" vs "README 原文抽取" vs "无"。
    what_source = "injected"
    what_text = project_desc
    if not what_text and root:
        try:
            goal = _extract_goal(root)
            if goal.get("declared"):
                readme_hint = goal.get("tagline") or goal.get("intro") or goal.get("title") or ""
                if readme_hint:
                    what_text = "(README 原文抽取, 非注入描述) " + readme_hint[:300]
                    what_source = "readme"
        except Exception:
            pass
    if not what_text:
        what_text = "(项目级描述未注入, 且未找到 README; 调用 report 看客观原料, 或 desc-project 回写一句话总纲)"
        what_source = "none"

    result = {
        "command": "orient",
        "what": what_text,
        "what_source": what_source,
        "scale": scale,
        "size": {
            "files": module_count,
            "nodes": node_count,
            "edges": len(g.edges),
            "clusters": cluster_count,
        },
        "languages": langs,
        "indexed_at": meta.get("indexed_at", ""),
        "reliability": {
            "blind_spots": blind,
            "note": ("图中有 %d 个盲区(读取/解析失败的文件), 相关区域可能符号缺失。"
                     % blind) if blind else "无盲区: 所有匹配到解析器的文件都成功解析。",
        },
    }

    # 跨语言断链诚实标注: 检测到 >=2 种编程语言时, 明确告诉大模型
    # "跨语言调用边连不起来" —— 这是静态分析的物理边界, 非遗漏
    prog_langs = _programming_langs(langs)
    if len(prog_langs) >= 2:
        result["reliability"]["cross_language_gap"] = {
            "languages": prog_langs,
            "note": ("检测到 %d 种编程语言 (%s)。craftsman-map 按单语言分别解析, "
                     "跨语言调用边无法连起 —— 例如前端 %s 调后端 API、"
                     "或语言 A 通过 FFI/子进程调语言 B。这些调用链在图中断开, "
                     "trace/impact 不会跨越语言边界。这是静态分析的物理边界, 非遗漏。"
                     % (len(prog_langs), "/".join(prog_langs), prog_langs[0])),
            "affected_commands": ["trace", "impact", "map"],
        }

    # 建议路线 —— 给大模型一条"从零理解这个项目"的推荐顺序
    # 明确标注: 这是最短理解路径, 其他命令按需调用
    suggested = [
        {"cmd": "report", "why": "看项目目标/验收标准/缺失项/进展线索(客观事实原料)"},
        {"cmd": "layers", "why": "看分层架构: 哪些是入口/核心/工具/配置"},
        {"cmd": "map", "why": "看功能块地图, 定位感兴趣的模块"},
        {"cmd": "entrypoints", "why": "找真入口(main/CLI/路由/事件处理器)"},
        {"cmd": "wiki --view summary", "why": "读每个功能块的人话描述 + 风险分级"},
    ]
    if blind:
        suggested.insert(0, {"cmd": "overview",
                             "why": "先看 meta.blind_spots 清单, 了解哪些文件没进图"})
    result["suggested_route"] = suggested
    result["suggested_route_note"] = (
        "这是从零理解项目的最短路径(5步)。"
        "其他命令按需调用: search(找符号) / symbol(看定义) / "
        "impact(改动影响面) / trace(调用链) / describe(深挖某块)。"
        "desc/desc-project 用于回写描述(调用方生成后写回缓存)。"
    )
    result["hint"] = ("这是项目导航图。按 suggested_route 的顺序调命令, "
                      "就能从零逐层理解这个项目。orient 只报客观规模与结构, "
                      "完成度/质量评估这类判断请你基于 report 的原料自行得出。")
    result["next_actions"] = suggested[:3]
    return result


# ---------------------------------------------------------------------------
# report —— 项目认知原料包 (A 类客观事实)
# ---------------------------------------------------------------------------
# 只匹配"注释里紧跟的" TODO 标记 (注释符 + 可选空白 + 标记词)。
# 这样避免把字符串字面量 / docstring 里"提到" TODO 这个词的说明文字
# (如本文件里 "无测试模块 + TODO/FIXME" 这种说明) 误算成真待办标记。
# 覆盖 Python(#) / JS-TS-Go-C(// 和 /*) / JSDoc 续行(^ *) / HTML(<!--)。
_TODO_RE = re.compile(
    r"(?:#|//|/\*|^\s*\*|<!--)\s*(TODO|FIXME|XXX|HACK)\b",
    re.IGNORECASE,
)
# craftsman-map 自身的扫描器文件 —— 分析"自己"时要排除, 否则本文件里
# 检测 TODO 的注释/字面量会被算进目标项目的 TODO 数 (自指假阳性)。
_SELF_PATH = os.path.normcase(os.path.abspath(__file__))


def _read_text(path: str, limit: int = 4000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except Exception:
        return ""


def _extract_goal(root: str) -> dict:
    """[目标] 从 README 抽取原文 —— 是'作者说了什么', 是事实。没写就明说未声明。"""
    for name in ("README.md", "README.rst", "README.txt", "readme.md", "README"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            text = _read_text(p, 2000)
            # 取正文首个非标题、非引用的实质段落 + 首个 > 引用(常是一句话简介)
            lines = [ln.rstrip() for ln in text.splitlines()]
            title = ""
            tagline = ""
            paras: list[str] = []
            for ln in lines:
                s = ln.strip()
                if not s:
                    continue
                if s.startswith("#") and not title:
                    title = s.lstrip("#").strip()
                    continue
                if s.startswith(">") and not tagline:
                    tagline = s.lstrip(">").strip()
                    continue
                if not s.startswith(("#", ">", "!", "[", "```", "|", "-", "*")):
                    paras.append(s)
                if len(paras) >= 3:
                    break
            return {
                "source": name,
                "title": title,
                "tagline": tagline,
                "intro": " ".join(paras)[:500],
                "declared": True,
            }
    return {"source": None, "declared": False,
            "note": "未找到 README; 项目目标未声明。不要臆测目标。"}


def _is_test_file(rel: str) -> bool:
    """判断相对路径是否属于测试代码 (排除测试目录/测试文件命名)。
    统一口径, 供验收统计与 TODO 扫描共用, 避免测试码污染'项目进展'信号。"""
    base = os.path.basename(rel).lower()
    slashed = "/" + rel.replace(os.sep, "/")
    if "/tests/" in slashed or "/test/" in slashed:
        return True
    if base.startswith("test_") or base.startswith("test-"):
        return True
    if base.endswith((".test.js", ".test.ts", ".test.jsx", ".test.tsx",
                      ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx")):
        return True
    if base in ("conftest.py",):
        return True
    return False


def _scan_source_files(root: str, ignore: set[str]) -> list[str]:
    """收集项目内源文件相对路径 (供 TODO/测试扫描)。"""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java",
                       ".rb", ".rs", ".c", ".cpp", ".cs", ".php"):
                rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
                out.append(rel)
    return out


def _extract_acceptance(root: str, g: CodeGraph, src_files: list[str]) -> dict:
    """[验收] 测试套件/CI/门禁的客观清单 —— 全是硬证据。"""
    # 测试文件
    test_files = [f for f in src_files
                  if "test" in os.path.basename(f).lower()
                  or "/tests/" in ("/" + f) or f.startswith("tests/")
                  or "/test/" in ("/" + f) or f.startswith("test/")]
    # 测试函数 (Python: test_ 开头的 function 节点)
    test_funcs = [n for n in g.nodes.values()
                  if n.kind == NodeKind.FUNCTION
                  and (n.name.startswith("test_") or n.name.startswith("Test"))]
    # CI / 门禁配置
    ci_files = []
    for rel in (".github/workflows", ".gitlab-ci.yml", "Makefile", "makefile",
                "tox.ini", "pytest.ini", "setup.cfg", "pyproject.toml",
                ".pre-commit-config.yaml", "Jenkinsfile", "noxfile.py"):
        p = os.path.join(root, rel)
        if os.path.exists(p):
            if os.path.isdir(p):
                try:
                    for wf in os.listdir(p):
                        ci_files.append(f"{rel}/{wf}")
                except OSError:
                    pass
            else:
                ci_files.append(rel)
    return {
        "test_files": len(test_files),
        "test_functions": len(test_funcs),
        "test_file_samples": sorted(test_files)[:10],
        "ci_and_gates": ci_files,
        "note": ("这些是客观存在的验收设施。'有 N 个测试/CI 跑什么'是事实; "
                 "'测试是否足够/覆盖是否达标'是判断, 请你自行评估。"),
    }


def _extract_gaps(root: str, g: CodeGraph, src_files: list[str]) -> dict:
    """[缺失] 无测试模块 + TODO/FIXME + 空实现 —— 全是可测量事实, 不是意图缺失。"""
    # 待办标记扫描 (TODO / FIXME / XXX / HACK)
    # 区分生产码/测试码: 只有生产码的 TODO 才算"项目进展信号",
    # 测试码的 TODO 单独计数并透明标注 (test_ 里的样例/占位不代表项目缺口)。
    todos: list[dict] = []
    todo_count = 0
    test_todo_count = 0
    for rel in src_files:
        p = os.path.join(root, rel)
        # 排除 craftsman-map 自身的扫描器文件 (自指假阳性)
        if os.path.normcase(os.path.abspath(p)) == _SELF_PATH:
            continue
        is_test = _is_test_file(rel)
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if _TODO_RE.search(line):
                        if is_test:
                            test_todo_count += 1
                            continue
                        todo_count += 1
                        if len(todos) < 40:
                            todos.append({"path": rel, "line": i,
                                          "text": line.strip()[:120]})
        except Exception:
            continue

    # 空实现函数 (pass-only / 只有 docstring / raise NotImplementedError)
    empty_funcs: list[dict] = []
    for n in g.nodes.values():
        if n.kind != NodeKind.FUNCTION:
            continue
        if n.meta.get("is_stub") or n.meta.get("empty_body"):
            empty_funcs.append({"id": n.id, "name": n.qualified_name, "path": n.path})

    # 无测试保护的模块: 有 module 节点但没有对应 test 文件覆盖其名字 (粗略启发, 标线索)
    test_bases = set()
    for f in src_files:
        base = os.path.basename(f).lower()
        if base.startswith("test_"):
            test_bases.add(base[5:].replace(".py", "").replace(".js", "").replace(".ts", ""))
        elif base.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
            test_bases.add(base.split(".")[0])
    untested_modules: list[str] = []
    for n in g.nodes.values():
        if n.kind != NodeKind.MODULE:
            continue
        stem = os.path.splitext(os.path.basename(n.path))[0].lower()
        if "test" in stem:
            continue
        if stem not in test_bases:
            untested_modules.append(n.path)

    return {
        "todo_fixme_count": todo_count,
        "todo_samples": todos,
        "test_todo_fixme_count": test_todo_count,
        "empty_implementations": len(empty_funcs),
        "empty_impl_samples": empty_funcs[:20],
        "untested_modules_hint": len(untested_modules),
        "untested_module_samples": sorted(untested_modules)[:20],
        "note": ("这些是可测量的客观缺失(代码里就有的 TODO/空函数/无同名测试)。"
                 "todo_fixme_count 只统计生产码; 测试码里的 TODO 单列在 "
                 "test_todo_fixme_count(测试样例/占位, 不代表项目缺口)。"
                 "'缺一个登录功能'这类意图层面的缺失, 工具不知道项目该有什么, 不做判断。"
                 "untested_modules 是基于'同名测试文件'的粗略启发, 是线索不是结论。"),
    }


def _extract_progress_signals(root: str, g: CodeGraph, acceptance: dict,
                              gaps: dict) -> dict:
    """[进展线索] git 活跃度 + TODO 密度 + 测试覆盖 —— 是'线索'不是'百分比'。"""
    meta = g.meta or {}
    git = meta.get("git", {})
    signals = {
        "git_available": bool(git.get("available", False)) if isinstance(git, dict) else False,
        "todo_density": gaps.get("todo_fixme_count", 0),
        "test_functions": acceptance.get("test_functions", 0),
        "empty_implementations": gaps.get("empty_implementations", 0),
    }
    if isinstance(git, dict) and git.get("available"):
        signals["recent_activity"] = git.get("recent_commits") or git.get("commit_count")
        signals["hotspot_files"] = git.get("hotspot_count")
    signals["note"] = ("这些是进展的'线索', 不是完成度百分比。craftsman-map "
                       "绝不吐'完成了 60%'——它不知道目标全集。请你综合这些线索"
                       "与 report 的目标/验收, 自行推断进展, 且标注'这是推断'。")
    return signals


def cmd_report(root: str, g: CodeGraph) -> dict:
    """项目认知原料包 —— A 类客观事实, 供调用方 LLM 下 B 类主观判断。"""
    from ..indexer import DEFAULT_IGNORE
    ignore = set(DEFAULT_IGNORE)
    src_files = _scan_source_files(root, ignore)

    goal = _extract_goal(root)
    acceptance = _extract_acceptance(root, g, src_files)
    gaps = _extract_gaps(root, g, src_files)
    progress = _extract_progress_signals(root, g, acceptance, gaps)

    return {
        "command": "report",
        "contract": ("本命令只报客观事实(A 类)。项目建议/完成度百分比/意图层面的缺失"
                     "(B 类)是主观判断, 工具不产出 —— 请调用方 LLM 基于以下原料自行生成, "
                     "并明确标注'这是基于现有信息的推断'。"),
        "goal": goal,
        "acceptance": acceptance,
        "gaps": gaps,
        "progress_signals": progress,
        "hint": "拿这份原料包生成'项目现状报告 + 建议 + 完成度评估', 记得区分事实与你的推断。",
        "next_actions": [
            {"cmd": "orient", "why": "看项目导航图与建议路线"},
            {"cmd": "layers", "why": "看分层架构"},
            {"cmd": "map", "why": "看功能块地图"},
        ],
    }
