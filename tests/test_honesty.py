"""
诚实性地基测试 (P0-P4)
======================
验证 craftsman-map 对大模型"诚实交底"的四类标注真的按预期工作:
  P0 图可信度   —— blind_spots / exclusions / schema_version
  P1 返回体积   —— 分页 page / impact-explore 的 truncated 上限
  P2 重建健壮性 —— 原子写 / schema 失效触发重建 / 失败落盘冷却
  P3 orient     —— 项目导航图字段
  P4 report     —— 客观事实原料包 (只报事实, 不吐主观判断)

全部基于真实 index, 不 mock。P2 用独立 tmp 仓库, 不污染 session fixture。
"""
from __future__ import annotations

import json
import os
import textwrap
import time

import pytest

from craftsman_map.indexer import Indexer
from craftsman_map.graph.store import CodeGraph
from craftsman_map.commands import core, report as r_cmd


# ---------------------------------------------------------------------------
# helper: 建一个独立的迷你仓库 (P2 / 定制场景用)
# ---------------------------------------------------------------------------
def _mini_repo(root: str, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p) or root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)


# ===========================================================================
# P0 图可信度地基
# ===========================================================================
def test_p0_meta_has_blind_spots_field(indexed_graph):
    """图 meta 必须含 blind_spots 结构 (即使为空), 让大模型知道盲区。"""
    meta = indexed_graph.meta
    assert "blind_spots" in meta
    assert "blind_spot_count" in meta
    assert isinstance(meta["blind_spots"], list)
    assert meta["blind_spot_count"] == len(meta["blind_spots"])


def test_p0_exclusions_transparent(indexed_graph):
    """排除规则透明化: 明确报告排除了哪些目录 + 哪些来自 gitignore。"""
    exc = indexed_graph.meta.get("exclusions")
    assert isinstance(exc, dict)
    assert "builtin" in exc and "from_gitignore" in exc
    assert "node_modules" in exc["builtin"]  # 内置黑名单核对


def test_p0_schema_version_recorded(indexed_graph):
    """graph.json 记录 schema_version, 供加载时失效判断。"""
    assert indexed_graph.meta.get("schema_version") == CodeGraph.SCHEMA_VERSION


def test_p0_blind_spot_captures_syntax_error(tmp_path):
    """故意放一个语法错误文件 → 必须出现在 blind_spots, 不被静默吞。"""
    root = str(tmp_path)
    _mini_repo(root, {
        "good.py": "def ok():\n    return 1\n",
        "broken.py": "def oops(:\n    this is not valid python\n",
    })
    g = Indexer().index(root)
    reasons = {b["reason"] for b in g.meta["blind_spots"]}
    paths = {b["path"] for b in g.meta["blind_spots"]}
    assert "broken.py" in paths
    assert "syntax_error" in reasons


# ===========================================================================
# P1 返回体积治理
# ===========================================================================
def test_p1_map_pagination(indexed_graph):
    """map 返回统一 page 元信息, 让大模型知道是全部还是一页。"""
    r = core.cmd_map(indexed_graph, top=1, offset=0)
    assert "page" in r
    pg = r["page"]
    for k in ("total", "shown", "offset", "limit", "has_more"):
        assert k in pg
    # 只取 1 个, 若总数 > 1 则 has_more 且给 next_offset
    if pg["total"] > 1:
        assert pg["has_more"] is True
        assert pg["next_offset"] == 1


def test_p1_search_pagination(indexed_graph):
    """search 也带 page。"""
    r = core.cmd_search(indexed_graph, "e", limit=1)  # 泛查, 大概率多命中
    assert "page" in r
    assert r["page"]["shown"] == r["count"]


def test_p1_impact_truncation(indexed_graph):
    """max_nodes 设 0 → 立刻截断并诚实标注 truncated, 不全量倒。"""
    # 找一个有入边的节点 (AuthProvider 被继承)
    r = core.cmd_impact(indexed_graph, "auth/base.py::AuthProvider", max_nodes=0)
    assert r["command"] == "impact"
    assert r["truncated"] is True


def test_p1_explore_has_truncated_flag(indexed_graph):
    """explore 恒返回 truncated 布尔字段。"""
    r = core.cmd_explore(indexed_graph, "auth/login.py::LoginService", depth=1)
    assert "truncated" in r
    assert isinstance(r["truncated"], bool)


# ===========================================================================
# P2 重建健壮性
# ===========================================================================
def test_p2_atomic_save_no_tmp_leftover(tmp_path):
    """原子写: save 后目录里不应残留 .tmp 文件, graph.json 是完整 JSON。"""
    root = str(tmp_path)
    _mini_repo(root, {"a.py": "def f():\n    return 1\n"})
    g = Indexer().index(root)
    g.meta["fingerprint"] = CodeGraph.fingerprint(root)
    g.save(root)
    d = os.path.join(root, ".craftsman-map")
    leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
    assert leftovers == []
    # graph.json 能被完整解析
    with open(os.path.join(d, "graph.json"), encoding="utf-8") as f:
        data = json.load(f)
    assert "meta" in data and "nodes" in data


def test_p2_schema_mismatch_triggers_stale(tmp_path):
    """磁盘上的 schema_version 与当前代码不符 → is_stale 判为陈旧。"""
    root = str(tmp_path)
    _mini_repo(root, {"a.py": "def f():\n    return 1\n"})
    g = Indexer().index(root)
    g.meta["fingerprint"] = CodeGraph.fingerprint(root)
    g.save(root)
    assert CodeGraph.is_stale(root) is False  # 刚建, 不陈旧
    # 篡改磁盘 schema_version 为旧值
    gp = os.path.join(root, ".craftsman-map", "graph.json")
    with open(gp, encoding="utf-8") as f:
        data = json.load(f)
    data["meta"]["schema_version"] = 0
    with open(gp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert CodeGraph.is_stale(root) is True  # schema 不匹配 → 陈旧


def test_p2_rebuild_status_none_when_healthy(tmp_path):
    """无失败记录时 rebuild_status 返回 None。"""
    root = str(tmp_path)
    _mini_repo(root, {"a.py": "def f():\n    return 1\n"})
    Indexer().index(root).save(root)
    assert CodeGraph.rebuild_status(root) is None


def test_p2_load_auto_stale_returns_old_graph(tmp_path):
    """改动源文件后 load_auto 立刻返回旧图 + stale=True (不阻塞卡等)。"""
    root = str(tmp_path)
    _mini_repo(root, {"a.py": "def f():\n    return 1\n"})
    g = Indexer().index(root)
    g.meta["fingerprint"] = CodeGraph.fingerprint(root)
    g.save(root)
    # 改动源文件 → 指纹变化
    time.sleep(0.02)
    _mini_repo(root, {"a.py": "def f():\n    return 1\n\ndef g2():\n    return 2\n"})
    graph, stale = CodeGraph.load_auto(root)
    assert stale is True
    assert graph is not None  # 立刻拿到旧图, 不是 None
    # 等后台重建落地后, 新符号应出现 (最多等 2 秒)
    for _ in range(40):
        if not CodeGraph.is_stale(root):
            break
        time.sleep(0.05)
    g2 = CodeGraph.load(root)
    names = {n.name for n in g2.nodes.values()}
    assert "g2" in names


# ===========================================================================
# P3 orient —— 项目导航图
# ===========================================================================
def test_p3_orient_shape(indexed_graph):
    r = r_cmd.cmd_orient(indexed_graph)
    assert r["command"] == "orient"
    for k in ("what", "scale", "size", "languages", "reliability",
              "suggested_route", "next_actions"):
        assert k in r
    assert r["scale"] in ("small", "medium", "large")
    assert r["size"]["files"] >= 1


def test_p3_orient_reliability_reflects_blind_spots(indexed_graph):
    """orient 的 reliability.blind_spots 必须与图 meta 一致 (不谎报干净)。"""
    r = r_cmd.cmd_orient(indexed_graph)
    assert r["reliability"]["blind_spots"] == indexed_graph.meta.get("blind_spot_count", 0)


def test_p3_orient_suggests_report_first(indexed_graph):
    """建议路线应把 report/layers 放在前面, 引导大模型正确起步。"""
    r = r_cmd.cmd_orient(indexed_graph)
    cmds = [a["cmd"] for a in r["suggested_route"]]
    assert "report" in cmds and "layers" in cmds


# ===========================================================================
# P4 report —— 客观事实原料包
# ===========================================================================
def test_p4_report_shape(sample_repo, indexed_graph):
    r = r_cmd.cmd_report(sample_repo, indexed_graph)
    assert r["command"] == "report"
    for k in ("contract", "goal", "acceptance", "gaps", "progress_signals"):
        assert k in r


def test_p4_report_extracts_readme_goal(sample_repo, indexed_graph):
    """README 存在 → goal.declared=True 且抓到标题原文 (是事实不是臆测)。"""
    r = r_cmd.cmd_report(sample_repo, indexed_graph)
    goal = r["goal"]
    assert goal["declared"] is True
    assert goal["source"] and goal["source"].lower().startswith("readme")
    assert "演示项目" in (goal.get("title", "") + goal.get("intro", ""))


def test_p4_report_counts_empty_implementations(sample_repo, indexed_graph):
    """AuthProvider.authenticate 是 '...' 空实现 → 必须被 gaps 计入 (不静默)。"""
    r = r_cmd.cmd_report(sample_repo, indexed_graph)
    assert r["gaps"]["empty_implementations"] >= 1
    names = {s["name"] for s in r["gaps"]["empty_impl_samples"]}
    assert any("authenticate" in n for n in names)


def test_p4_report_no_subjective_verdict(sample_repo, indexed_graph):
    """铁律: report 绝不吐完成度百分比/主观建议 —— 只报事实。"""
    r = r_cmd.cmd_report(sample_repo, indexed_graph)
    blob = json.dumps(r, ensure_ascii=False)
    # 不得出现 '完成度: NN%' 这类被工具焊死的主观结论
    assert "完成度" not in r.get("contract", "") or "不" in r.get("contract", "")
    # progress 只给线索, 不给百分比结论字段
    assert "completion_percent" not in r["progress_signals"]
    assert "percent" not in json.dumps(r["progress_signals"], ensure_ascii=False)


def test_p4_report_missing_readme(tmp_path):
    """无 README → goal.declared=False, 明说未声明, 不臆测目标。"""
    root = str(tmp_path)
    _mini_repo(root, {"a.py": "def f():\n    return 1\n"})
    g = Indexer().index(root)
    r = r_cmd.cmd_report(root, g)
    assert r["goal"]["declared"] is False


# ===========================================================================
# P5 诚实标注 —— trace 静态天花板 + 跨语言断链 (只标注, 不硬啃)
# ===========================================================================
from craftsman_map.commands import understand as u_cmd
from craftsman_map.graph.model import Node, NodeKind


def test_p5_trace_declares_limitations(indexed_graph):
    """trace 必须声明静态分析边界: 动态派发/回调/多态/跨语言这四类抓不到。
    这是诚实标注 —— 让大模型知道'链路可能不完整是物理边界, 非遗漏'。"""
    r = u_cmd.cmd_trace(indexed_graph, "LoginService.authenticate")
    assert "limitations" in r, "trace 必须带 limitations 能力边界声明"
    lim = r["limitations"]
    assert "cannot_capture" in lim and len(lim["cannot_capture"]) >= 4
    joined = " ".join(lim["cannot_capture"])
    # 四类边界都必须被明确点名
    for keyword in ("动态派发", "回调", "多态", "跨语言"):
        assert keyword in joined, f"limitations 未声明 '{keyword}' 这类抓不到的调用"


def test_p5_trace_counts_low_confidence_edges(indexed_graph):
    """trace 必须报出本次链路走过多少条边、其中多少条低置信 (启发式推断)。
    合成库里 calls 边置信度 0.7 会被 linker 消解, 但字段结构必须真实存在。"""
    r = u_cmd.cmd_trace(indexed_graph, "LoginService.authenticate")
    assert "traced_edges" in r and "low_confidence_edges" in r
    assert isinstance(r["traced_edges"], int)
    assert isinstance(r["low_confidence_edges"], int)
    assert r["low_confidence_edges"] <= r["traced_edges"]


def _lang_graph(langs: dict[str, int]) -> CodeGraph:
    """手动构造一个带指定语言 module 节点的图 (不依赖 tree-sitter 是否安装,
    保证测试稳定)。langs = {扩展名: 该语言文件数}。"""
    g = CodeGraph()
    nodes = []
    ext_of = {"Python": ".py", "JavaScript": ".js", "TypeScript": ".ts", "Go": ".go"}
    for lang, count in langs.items():
        ext = ext_of[lang]
        for i in range(count):
            rel = f"src/mod_{lang}_{i}{ext}"
            nodes.append(Node(id=rel, kind=NodeKind.MODULE, name=f"mod_{i}",
                              qualified_name=rel, path=rel, confidence=1.0))
    g.add_nodes(nodes)
    g.reindex()
    g.meta = {"cluster_count": 0, "blind_spot_count": 0, "indexed_at": ""}
    return g


def test_p5_orient_flags_cross_language_gap():
    """>=2 种编程语言 → orient 必须显式标注跨语言断链, 并点名受影响命令。
    这是诚实标注: 前端调后端 API、FFI 调用等边连不起来, 大模型必须知道。"""
    g = _lang_graph({"Python": 3, "JavaScript": 2})
    r = r_cmd.cmd_orient(g)
    gap = r["reliability"].get("cross_language_gap")
    assert gap is not None, "多语言项目 orient 必须带 cross_language_gap 标注"
    assert set(gap["languages"]) == {"Python", "JavaScript"}
    assert "trace" in gap["affected_commands"] and "impact" in gap["affected_commands"]
    assert "跨语言" in gap["note"]


def test_p5_orient_no_gap_for_single_language(indexed_graph):
    """单一编程语言 (合成库纯 Python) → 不得谎报跨语言断链。
    诚实的另一面: 没有的问题不许编造。"""
    r = r_cmd.cmd_orient(indexed_graph)
    assert "cross_language_gap" not in r["reliability"]


def test_p5_orient_docs_dont_trigger_gap():
    """Python + Markdown 不算跨语言 (Markdown 是文档不是编程语言) → 不触发标注。"""
    g = _lang_graph({"Python": 3})
    # 手动塞一个 markdown doc 模块, 模拟 README 等
    g.add_nodes([Node(id="README.md", kind=NodeKind.MODULE, name="README",
                      qualified_name="README.md", path="README.md", confidence=1.0)])
    g.reindex()
    r = r_cmd.cmd_orient(g)
    assert "cross_language_gap" not in r["reliability"]


# ===========================================================================
# P2-fix TODO 假阳性 —— 只数"注释里的"真标记, 不数说明文字/字符串字面量
# ===========================================================================
def test_todofix_only_real_comment_markers(tmp_path):
    """只有注释里紧跟的 TODO/FIXME 才算数。
    docstring/字符串里'提到' TODO 这个词的说明文字, 不许算成待办标记。"""
    root = str(tmp_path)
    _mini_repo(root, {
        "README.md": "# demo\n项目说明\n",
        "real.py": (
            "# TODO: 这是真的待办, 该数\n"
            "def f():\n"
            "    x = 1  # FIXME 行尾真标记, 也该数\n"
            "    return x\n"
        ),
        "fake.py": (
            '"""这个模块说明里提到 TODO 和 FIXME 这两个词, 但只是说明文字, 不该数。"""\n'
            "def g():\n"
            '    msg = "请在这里填 TODO 内容"  # 这是字符串字面量里的词, 不算\n'
            "    return msg\n"
        ),
    })
    g = Indexer().index(root)
    r = r_cmd.cmd_report(root, g)
    gaps = r["gaps"]
    # real.py 有 2 个真标记 (TODO + FIXME), fake.py 的 0 个
    assert gaps["todo_fixme_count"] == 2, (
        f"应只数 real.py 的 2 个真注释标记, 实得 {gaps['todo_fixme_count']}; "
        f"samples={gaps['todo_samples']}")
    paths = {s["path"] for s in gaps["todo_samples"]}
    assert "fake.py" not in paths, "说明文字/字符串字面量里的词被误当成 TODO 了"


def test_todofix_excludes_self_scanner(tmp_path):
    """自指防御: 把 craftsman-map 的 report.py (含检测正则+说明) 拷进目标项目,
    它自身那些 'TODO/FIXME' 字面量不该被算进目标项目的待办数。"""
    import craftsman_map.commands.report as _rep_mod
    root = str(tmp_path)
    with open(_rep_mod.__file__, "r", encoding="utf-8") as f:
        self_src = f.read()
    _mini_repo(root, {
        "README.md": "# demo\n",
        # 拷一份扫描器源码进来当普通文件 —— 它内部有 TODO/FIXME 字面量和注释
        "vendored_report.py": self_src,
        "clean.py": "# TODO: 唯一一个真待办\ndef h():\n    return 0\n",
    })
    g = Indexer().index(root)
    r = r_cmd.cmd_report(root, g)
    # vendored_report.py 里的检测正则/说明文字不该混进来; 只有 clean.py 的 1 个
    assert r["gaps"]["todo_fixme_count"] == 1, (
        f"vendored 扫描器的字面量被误数了, samples={r['gaps']['todo_samples']}")


# ===========================================================================
# P4 entrypoints 去噪 —— 默认排除测试代码 (tests/conftest/test_*), 诚实标注排除数
# ===========================================================================
def test_p4_entrypoints_excludes_tests_by_default(tmp_path):
    """真入口列表默认不含测试代码。
    测试函数/fixture 不是项目真入口, 混进来会污染'真入口'判断。"""
    root = str(tmp_path)
    _mini_repo(root, {
        "README.md": "# demo\n",
        "app.py": (
            "def main():\n"
            "    helper()\n"
            "    worker()\n"
            "def helper():\n    return 1\n"
            "def worker():\n    return 2\n"
        ),
        "tests/test_app.py": (
            "def test_main():\n"      # 测试用例: 调了别人但不是真入口
            "    assert run_all()\n"
            "def run_all():\n    return True\n"
        ),
        "tests/conftest.py": (
            "def make_fixture():\n"   # conftest 里的辅助: 不是真入口
            "    return setup()\n"
            "def setup():\n    return 0\n"
        ),
    })
    g = Indexer().index(root)
    r = u_cmd.cmd_entrypoints(g, top=20)
    paths = {e["path"].replace("\\", "/") for e in r["entrypoints"]}
    names = {e["name"].split(".")[-1] for e in r["entrypoints"]}
    # 真入口 main 必须在
    assert any("app.py" in p for p in paths), "真入口 app.py::main 被漏掉了"
    # 测试代码一个都不许混入
    assert not any("tests/" in p for p in paths), f"测试节点泄漏进入口列表: {paths}"
    assert "test_main" not in names and "make_fixture" not in names
    # 诚实标注: 必须报出排除了几个测试节点
    assert r.get("excluded_tests", 0) >= 1, "排除了测试节点却没诚实标注 excluded_tests"
    assert "note" in r and "include_tests" in r["note"]


def test_p4_entrypoints_include_tests_flag_reopens(tmp_path):
    """include_tests=True 时关闭过滤 —— 诚实的另一面: 用户要看就给, 不硬藏。"""
    root = str(tmp_path)
    _mini_repo(root, {
        "README.md": "# demo\n",
        "app.py": "def main():\n    helper()\ndef helper():\n    return 1\n",
        "tests/test_app.py": "def test_x():\n    run()\ndef run():\n    return 1\n",
    })
    g = Indexer().index(root)
    excluded = u_cmd.cmd_entrypoints(g, top=20)
    included = u_cmd.cmd_entrypoints(g, top=20, include_tests=True)
    # 打开开关后, 候选数应 >= 排除时 (测试节点重新纳入)
    assert included["count"] >= excluded["count"]
    inc_paths = {e["path"].replace("\\", "/") for e in included["entrypoints"]}
    assert any("tests/" in p for p in inc_paths), "include_tests=True 仍未纳入测试节点"
    # 打开后不应再报 excluded_tests 标注
    assert "excluded_tests" not in included


# ===========================================================================
# P6 体验修复 —— Agent 亲自实测暴露的 4 个问题, 每条钉死防回归
#   ① map 体积治理   ② wiki 体积治理   ③ hotspots 无 git 降级   ④ report TODO 分流
# ===========================================================================
def test_p6_map_default_top_and_docstring_trimmed(indexed_graph):
    """map 是目录页: 默认每页 12 个块, docstring 只取首行且限长, 不塞整段撑爆体积。"""
    r = core.cmd_map(indexed_graph)
    assert r["page"]["limit"] == 12, "map 默认每页应为 12 (控体积)"
    for c in r["clusters"]:
        assert len(c["files"]) <= 5, "map 每块 files 应截断到 5 个"
        for s in c["key_symbols"]:
            assert "\n" not in s["summary"], "key_symbol summary 应为首行, 不含换行"
            assert len(s["summary"]) <= 100, "key_symbol summary 应限长 <=100"


def test_p6_wiki_human_mode_paginated_summary(sample_repo, indexed_graph):
    """wiki summary 模式: 分页 + description 只给首行截断(非整段), 控体积。

    契约(2026-07-21 更新): 字段名统一为 description (summary/full 两视图对齐, 消除换名坑);
    体积治理靠"首行截断"保证, 靠顶层 view/page 标示是否精简, 不靠 key 名区分。
    """
    r = u_cmd.cmd_wiki(indexed_graph, sample_repo, fmt="human")
    assert "page" in r, "wiki summary 模式必须分页"
    assert r["view"] == "summary", "human 应归一到 summary 视图"
    for v in r["clusters"]:
        assert "description" in v, "字段名应统一为 description (与 full 视图对齐)"
        assert "\n" not in v["description"], "summary 模式 description 应为首行, 不含换行"
        assert len(v["description"]) <= 120, "summary 模式 description 应首行限长 <=120"


def test_p6_wiki_json_mode_not_paginated(sample_repo, indexed_graph):
    """json 模式全量返回, 不分页: 调用方要完整数据时不阉割。"""
    r = u_cmd.cmd_wiki(indexed_graph, sample_repo, fmt="json")
    assert "page" not in r, "wiki json 模式应全量返回, 不分页"


def test_p6_hotspots_fallback_when_no_git(tmp_path):
    """无 git 环境: hotspots 降级到静态复杂度热点, 并诚实标注这不是真 churn。"""
    root = str(tmp_path)
    _mini_repo(root, {
        "README.md": "# demo\n",
        "app.py": "def main():\n    helper()\ndef helper():\n    return 1\n",
    })
    g = Indexer().index(root)
    if g.meta.get("git", {}).get("available"):
        pytest.skip("临时目录被探测为 git 仓库, 跳过无 git 降级测试")
    r = core.cmd_hotspots(g)
    assert r["git_available"] is False
    assert r["fallback"] == "static_complexity", "无 git 应降级到静态复杂度热点"
    assert len(r["static_hotspots"]) >= 1, "静态热点不应为空"
    top1 = r["static_hotspots"][0]
    assert "symbols" in top1 and "degree" in top1
    assert "churn" in r["hint"], "必须诚实说明静态热点不等于真实变更热点"


def test_p6_report_todo_splits_production_and_test(tmp_path):
    """report TODO 口径: 生产码 TODO 计入 todo_fixme_count, 测试码 TODO 单列, 不污染进展。"""
    root = str(tmp_path)
    _mini_repo(root, {
        "README.md": "# demo\n",
        "app.py": "def f():\n    # TODO: 真实生产缺口\n    return 1\n",
        "tests/test_app.py": "def test_f():\n    # TODO: 测试占位, 不算项目缺口\n    assert True\n",
    })
    g = Indexer().index(root)
    r = r_cmd.cmd_report(root, g)
    gaps = r["gaps"]
    assert gaps["todo_fixme_count"] == 1, "生产码 TODO 应计 1"
    assert gaps["test_todo_fixme_count"] == 1, "测试码 TODO 应单列 1"
