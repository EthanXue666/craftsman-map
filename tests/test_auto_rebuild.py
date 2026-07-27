"""
自动陈旧检测 + 重建测试
========================
验证零配置自动重建: 源文件变化时 load_auto 自动重建, 不变时用缓存。
这是主人最想要的能力 —— 改完代码地图自动跟上, 无需手动 index。
"""
from __future__ import annotations

import os
import time

from craftsman_map.indexer import Indexer
from craftsman_map.graph.store import CodeGraph


def _write(root: str, rel: str, content: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _build_index(root: str) -> CodeGraph:
    g = Indexer().index(root)
    g.meta["fingerprint"] = CodeGraph.fingerprint(root)
    g.save(root)
    return g


def test_fingerprint_changes_on_file_add(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "def foo():\n    return 1\n")
    fp1 = CodeGraph.fingerprint(root)
    _write(root, "b.py", "def bar():\n    return 2\n")
    fp2 = CodeGraph.fingerprint(root)
    assert fp1 != fp2, "新增文件应改变指纹"


def test_fingerprint_changes_on_edit(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "def foo():\n    return 1\n")
    fp1 = CodeGraph.fingerprint(root)
    time.sleep(0.01)
    _write(root, "a.py", "def foo():\n    return 999\n")  # 改内容 → mtime 变
    fp2 = CodeGraph.fingerprint(root)
    assert fp1 != fp2, "修改文件应改变指纹"


def test_fingerprint_stable_when_unchanged(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "def foo():\n    return 1\n")
    fp1 = CodeGraph.fingerprint(root)
    fp2 = CodeGraph.fingerprint(root)
    assert fp1 == fp2, "无变化时指纹应稳定"


def test_is_stale_no_index(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "x = 1\n")
    assert CodeGraph.is_stale(root) is True, "无索引应视为陈旧"


def test_is_stale_after_edit(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "def foo():\n    return 1\n")
    _build_index(root)
    assert CodeGraph.is_stale(root) is False, "刚建索引不应陈旧"
    time.sleep(0.01)
    _write(root, "a.py", "def foo():\n    return 2\n")
    assert CodeGraph.is_stale(root) is True, "改文件后应陈旧"


def _wait_rebuild_done(root, timeout=5.0):
    """等后台重建线程跑完（_REBUILD_IN_PROGRESS 清空）。"""
    root = os.path.abspath(root)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root not in CodeGraph._REBUILD_IN_PROGRESS:
            return
        time.sleep(0.01)


def test_load_auto_rebuilds_on_change(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "def foo():\n    return 1\n")
    _build_index(root)

    g1, stale1 = CodeGraph.load_auto(root)
    assert stale1 is False, "无变化不应陈旧"
    assert "a.py::foo" in g1.nodes

    # 新增函数 → 下次 load_auto 立刻返回旧图 (stale=True) 并后台重建
    time.sleep(0.01)
    _write(root, "a.py", "def foo():\n    return 1\ndef baz():\n    return 3\n")
    g2, stale2 = CodeGraph.load_auto(root)
    assert stale2 is True, "源变化应标记陈旧并触发后台重建"
    assert "a.py::baz" not in g2.nodes, "本次返回的是旧图，尚不含新符号"

    # 等后台重建完成 → 再次加载应为最新、不再陈旧、含新符号
    _wait_rebuild_done(root)
    g3, stale3 = CodeGraph.load_auto(root)
    assert stale3 is False, "后台重建完成后不应再陈旧"
    assert "a.py::baz" in g3.nodes, "重建后应含新符号"


def test_load_auto_uses_cache_when_fresh(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "def foo():\n    return 1\n")
    _build_index(root)
    g, rebuilt = CodeGraph.load_auto(root)
    assert rebuilt is False
    assert g.meta.get("fingerprint") == CodeGraph.fingerprint(root)


def test_legacy_index_without_fingerprint_triggers_rebuild(tmp_path):
    """旧版索引没有 fingerprint 字段 → 首次加载无旧图可返回，走同步构建路径。"""
    root = str(tmp_path)
    _write(root, "a.py", "x = 1\n")
    g = Indexer().index(root)
    g.meta["fingerprint"] = ""      # 模拟旧索引（有 graph.json 但指纹为空）
    g.save(root)
    assert CodeGraph.is_stale(root) is True
    # 旧索引存在但指纹为空 → 视为"有旧图"，返回旧图 + 后台重建
    g2, stale = CodeGraph.load_auto(root)
    assert stale is True, "旧版索引（无指纹）应标记陈旧"
    # 等后台重建完成 → 指纹应被补上
    _wait_rebuild_done(root)
    g3, stale3 = CodeGraph.load_auto(root)
    assert stale3 is False
    assert g3.meta.get("fingerprint"), "重建后应补上指纹"
