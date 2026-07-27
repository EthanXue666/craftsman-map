"""
git 历史维度测试
================
用 dulwich.porcelain 建真实 git 仓库 + 造提交,
验证 churn/co-change/hotspots/impact 真实工作。
dulwich 是纯 Python git 实现, 不依赖系统 git 二进制。
"""
from __future__ import annotations

import os
import time

import pytest

from craftsman_map import git_history
from craftsman_map.indexer import Indexer
from craftsman_map.commands import core


# ── fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture
def git_repo(tmp_path):
    """用 dulwich.porcelain 建真实提交历史。

    造出可验证的信号:
      - a.py 改 3 次 → 热点
      - a.py + b.py 一起改 2 次 → 共变
    """
    from dulwich import porcelain

    root = str(tmp_path)
    porcelain.init(root)

    def _write(rel: str, content: str):
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(content)

    def _commit(files: dict[str, str], message: str):
        for name, content in files.items():
            _write(name, content)
        porcelain.add(root, list(files.keys()))
        porcelain.commit(
            root,
            message=message.encode(),
            author=b"Test User <test@test.com>",
            committer=b"Test User <test@test.com>",
            author_timestamp=int(time.time()),
            commit_timestamp=int(time.time()),
        )

    # commit 1: a.py + b.py 一起
    _commit({"a.py": "def f():\n    pass\n",
             "b.py": "def g():\n    pass\n"}, "c1")
    # commit 2: a.py + b.py 再一起改 (共变 count=2)
    _commit({"a.py": "def f():\n    return 1\n",
             "b.py": "def g():\n    return 2\n"}, "c2")
    # commit 3: 只改 a.py → a.py churn=3
    _commit({"a.py": "def f():\n    return 42\n"}, "c3")

    return root


# ── 测试 ─────────────────────────────────────────────────────────────────────

def test_available(git_repo, tmp_path):
    assert git_history.available(git_repo) is True
    non_git = str(tmp_path / "not_a_repo")
    os.makedirs(non_git, exist_ok=True)
    assert git_history.available(non_git) is False


def test_churn_counts(git_repo):
    data = git_history.analyze(git_repo)
    assert data["available"] is True
    assert data["commits_analyzed"] == 3
    assert data["file_churn"]["a.py"] == 3
    assert data["file_churn"]["b.py"] == 2


def test_hotspot_ranking(git_repo):
    data = git_history.analyze(git_repo)
    assert data["hotspots"][0][0] == "a.py"


def test_co_change(git_repo):
    data = git_history.analyze(git_repo)
    co = data["co_change"]
    assert "a.py" in co
    partners = {p for p, _ in co["a.py"]}
    assert "b.py" in partners


def test_backend_is_dulwich(git_repo):
    data = git_history.analyze(git_repo)
    assert data.get("backend") == "dulwich"


def test_attach_to_graph(git_repo):
    g = Indexer().index(git_repo)
    assert g.meta["git"]["available"] is True
    a = g.nodes.get("a.py")
    assert a is not None
    assert a.meta.get("git_churn") == 3


def test_hotspots_command(git_repo):
    g = Indexer().index(git_repo)
    r = core.cmd_hotspots(g)
    assert r["git_available"] is True
    assert r["hotspots"][0]["path"] == "a.py"


def test_impact_includes_git_coupling(git_repo):
    g = Indexer().index(git_repo)
    r = core.cmd_impact(g, "a.py::f")
    coupled = {c["path"] for c in r.get("git_coupled_files", [])}
    assert "b.py" in coupled


def test_hotspots_graceful_without_git(sample_repo):
    """非 git 仓库 → hotspots 优雅降级。"""
    g = Indexer().index(sample_repo, with_git=True)
    r = core.cmd_hotspots(g)
    assert r["git_available"] is False
    assert "next_actions" in r
