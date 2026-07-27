"""
Git 历史维度 (GitNexus 式导航能力)
==================================
静态调用图告诉你"结构上谁连着谁", git 历史告诉你"现实中谁一起变、谁最近在动"。
两者叠加, 影响面分析才既有结构证据又有演化证据。

本模块从 git 提取三类信号 (优先用 dulwich 纯 Python 实现, 降级用 subprocess git):
  1. 变更热度 (churn): 每个文件被多少次提交碰过 → 高热度文件是风险/活跃区。
  2. 最近修改 (recency): 文件最后一次提交时间 → 判断代码新旧。
  3. 共变关系 (co-change): 两个文件多次在同一提交里一起改 → 隐式耦合,
     即使静态图上没有直接调用边, 它们也可能"一起坏"。

所有信号挂到 CodeGraph 的 module 节点 meta 上, 并可下沉到符号级 (按文件归属)。
非 git 仓库 → available() 返回 False, 相关命令优雅降级。

设计取舍: co-change 只统计"改动文件数 <= MAX_FILES_PER_COMMIT"的提交,
因为一个改了 200 个文件的提交 (如格式化/重命名) 不代表真实耦合, 会污染信号。
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Iterator

MAX_FILES_PER_COMMIT = 20   # 超过此数的提交不计入 co-change (噪声过滤)


# ─────────────────────────────────────────
# 后端：dulwich 优先，subprocess git 降级
# ─────────────────────────────────────────

def _dulwich_available() -> bool:
    try:
        import dulwich  # noqa
        return True
    except ImportError:
        return False


def _git_binary_available() -> bool:
    import subprocess
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def available(root: str) -> bool:
    """是否是可用的 git 仓库（dulwich 或 git 二进制任意一个可用即可）。"""
    root = os.path.abspath(root)
    if _dulwich_available():
        try:
            from dulwich.repo import Repo
            Repo(root)
            return True
        except Exception:
            return False
    if _git_binary_available():
        import subprocess
        try:
            r = subprocess.run(
                ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0 and r.stdout.strip() == "true"
        except Exception:
            return False
    return False


def _iter_commits_dulwich(root: str, max_commits: int) -> Iterator[tuple[int, list[str]]]:
    """用 dulwich 遍历提交，yield (timestamp, [changed_files])。"""
    from dulwich.repo import Repo
    from dulwich.diff_tree import tree_changes  # 正确 API，返回 TreeChange namedtuple

    repo = Repo(root)
    for entry in repo.get_walker(max_entries=max_commits):
        commit = entry.commit
        ts = commit.author_time

        changed: list[str] = []
        if commit.parents:
            parent = repo[commit.parents[0]]
            # tree_changes(store, old_tree_sha, new_tree_sha) → Iterator[TreeChange]
            # TreeChange.old / .new 都是 TreeChangeTuple(path, mode, sha)
            for change in tree_changes(repo.object_store, parent.tree, commit.tree):
                raw = change.new.path or change.old.path
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    changed.append(raw.replace("\\", "/"))
        else:
            # 初始提交：遍历整棵树
            tree_obj = repo.object_store[commit.tree]
            for item in tree_obj.items():
                path = item.path
                if isinstance(path, bytes):
                    path = path.decode("utf-8", errors="replace")
                if path:
                    changed.append(path.replace("\\", "/"))

        yield ts, changed


def _iter_commits_subprocess(root: str, max_commits: int) -> Iterator[tuple[int, list[str]]]:
    """用 git 二进制遍历提交，yield (timestamp, [changed_files])。"""
    import subprocess
    log = subprocess.run(
        ["git", "-C", root, "log",
         f"--max-count={max_commits}",
         "--no-merges", "--numstat",
         "--pretty=format:\x1f%ct"],
        capture_output=True, text=True, encoding="utf-8",
        errors="ignore", timeout=60,
    )
    if log.returncode != 0:
        return

    cur_ts = 0
    cur_files: list[str] = []

    def _emit():
        if cur_files:
            yield cur_ts, list(cur_files)

    for line in log.stdout.splitlines():
        if line.startswith("\x1f"):
            if cur_files:
                yield cur_ts, list(cur_files)
                cur_files = []
            try:
                cur_ts = int(line[1:].strip() or 0)
            except ValueError:
                cur_ts = 0
            continue
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 3:
            path = parts[2].replace("\\", "/")
            if " => " in path:
                path = path.split(" => ")[-1].strip("{}")
            cur_files.append(path)
    if cur_files:
        yield cur_ts, cur_files


def analyze(root: str, max_commits: int = 2000) -> dict:
    """提取 git 历史信号。返回:
    {
      "available": bool,
      "backend": "dulwich" | "git",
      "commits_analyzed": int,
      "file_churn": {relpath: commit_count},
      "file_last_ts": {relpath: unix_ts},
      "co_change": {relpath: [(other, count), ...]},  # 每文件 top 共变
      "hotspots": [(relpath, churn), ...]             # 全局热点排序
    }
    """
    root = os.path.abspath(root)
    if not available(root):
        return {"available": False}

    use_dulwich = _dulwich_available()
    backend = "dulwich" if use_dulwich else "git"

    file_churn: dict[str, int] = defaultdict(int)
    file_last_ts: dict[str, int] = {}
    co_pair: dict[tuple, int] = defaultdict(int)
    commits_analyzed = 0

    iter_fn = _iter_commits_dulwich if use_dulwich else _iter_commits_subprocess

    for ts, files in iter_fn(root, max_commits):
        commits_analyzed += 1
        for f in files:
            file_churn[f] += 1
            if f not in file_last_ts or ts > file_last_ts[f]:
                file_last_ts[f] = ts
        if len(files) <= MAX_FILES_PER_COMMIT:
            uniq = sorted(set(files))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    co_pair[(uniq[i], uniq[j])] += 1

    co_change: dict[str, list] = defaultdict(list)
    for (a, b), cnt in co_pair.items():
        if cnt >= 2:
            co_change[a].append((b, cnt))
            co_change[b].append((a, cnt))
    for k in co_change:
        co_change[k].sort(key=lambda t: -t[1])
        co_change[k] = co_change[k][:10]

    hotspots = sorted(file_churn.items(), key=lambda t: -t[1])

    return {
        "available": True,
        "backend": backend,
        "commits_analyzed": commits_analyzed,
        "file_churn": dict(file_churn),
        "file_last_ts": file_last_ts,
        "co_change": {k: v for k, v in co_change.items()},
        "hotspots": hotspots,
    }


def attach_to_graph(graph, git_data: dict) -> None:
    """把 git 信号写到图的 meta 与各 module 节点上。"""
    if not git_data.get("available"):
        graph.meta["git"] = {"available": False}
        return
    churn = git_data["file_churn"]
    last_ts = git_data["file_last_ts"]
    for n in graph.nodes.values():
        if n.path and n.path in churn:
            n.meta["git_churn"] = churn[n.path]
            n.meta["git_last_ts"] = last_ts.get(n.path, 0)
    graph.meta["git"] = {
        "available": True,
        "backend": git_data.get("backend", "unknown"),
        "commits_analyzed": git_data["commits_analyzed"],
        "top_hotspots": [
            {"path": p, "churn": c} for p, c in git_data["hotspots"][:15]
        ],
    }
    graph.meta["git_co_change"] = git_data["co_change"]
