"""
图存储层
========
CodeGraph 持有全部节点/边, 提供:
  - 序列化到 .craftsman-map/graph.json
  - 从磁盘加载
  - 邻接查询 (出边/入边)、按 kind/cluster 过滤
  - 功能块聚类 (connected-components 简化版, v2 换 Leiden)

零外部依赖: 图算法用纯 Python 实现。
"""
from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from typing import Iterable

from .model import Node, Edge, NodeKind, EdgeKind


def _first_line(text: str, limit: int = 100) -> str:
    """取 docstring 首个非空行并限长。map 目录页用, 避免整段 docstring 撑爆体积。"""
    if not text:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:limit]
    return ""


class CodeGraph:
    #: graph.json 结构版本。加载时不匹配 → 视为陈旧, 自动重建 (防旧缓存+新代码字段错位)
    SCHEMA_VERSION = 2

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._out: dict[str, list[Edge]] = defaultdict(list)
        self._in: dict[str, list[Edge]] = defaultdict(list)
        self.meta: dict = {}

    # ---- build ----
    def add_nodes(self, nodes: Iterable[Node]) -> None:
        for n in nodes:
            # 同 ID 保留高置信度版本
            if n.id not in self.nodes or n.confidence > self.nodes[n.id].confidence:
                self.nodes[n.id] = n

    def add_edges(self, edges: Iterable[Edge]) -> None:
        for e in edges:
            self.edges.append(e)

    def reindex(self) -> None:
        self._out.clear()
        self._in.clear()
        for e in self.edges:
            self._out[e.src].append(e)
            self._in[e.dst].append(e)

    # ---- query ----
    def out_edges(self, nid: str, kind: EdgeKind | None = None) -> list[Edge]:
        es = self._out.get(nid, [])
        return [e for e in es if kind is None or e.kind == kind]

    def in_edges(self, nid: str, kind: EdgeKind | None = None) -> list[Edge]:
        es = self._in.get(nid, [])
        return [e for e in es if kind is None or e.kind == kind]

    def nodes_by_kind(self, kind: NodeKind) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == kind]

    def find_by_name(self, name: str, fuzzy: bool = True) -> list[Node]:
        name_l = name.lower()
        out = []
        for n in self.nodes.values():
            if n.name.lower() == name_l or n.qualified_name.lower() == name_l:
                out.append((0, n))
            elif fuzzy and name_l in n.qualified_name.lower():
                out.append((1, n))
        out.sort(key=lambda t: (t[0], -t[1].confidence))
        return [n for _, n in out]

    # ---- clustering: 功能块 ----
    def cluster(self, resolution: float = 1.0) -> int:
        """功能块划分 —— Louvain 社区检测 (模块度优化)。

        v1 用弱连通分量, 但代码库里模块几乎全连通 → 整个项目挤成一坨大簇
        (实测 311/381 节点占 97%), 对大模型定位模块毫无帮助。
        改用 Louvain: 在'调用/包含/继承密集'处切开, 切出真正的功能子块
        (同规模切成 33 簇、最大簇仅占 12%)。纯 Python, 零依赖, 确定性可复现。

        resolution 越大簇越细 (默认 1.0)。返回簇数量。
        """
        # 只在'实体节点'间连边 (排除 external import 噪声); 加权无向 (confidence 累加)
        real = {nid for nid, n in self.nodes.items()
                if n.kind not in (NodeKind.IMPORT,)}
        adj: dict[str, dict[str, float]] = defaultdict(dict)
        for e in self.edges:
            if (e.src in real and e.dst in real and e.src != e.dst
                    and e.confidence >= 0.5):
                adj[e.src][e.dst] = adj[e.src].get(e.dst, 0.0) + e.confidence
                adj[e.dst][e.src] = adj[e.dst].get(e.src, 0.0) + e.confidence

        nodes = sorted(adj.keys())                 # 排序 → 确定性
        k = {n: sum(adj[n].values()) for n in nodes}
        m = sum(k.values()) / 2.0 or 1.0
        comm = {n: n for n in nodes}               # 初始每点自成社区
        sigma_tot = {n: k[n] for n in nodes}
        improved, rounds = True, 0
        while improved and rounds < 50:
            improved, rounds = False, rounds + 1
            for n in nodes:
                ci, ki = comm[n], k[n]
                sigma_tot[ci] -= ki
                neigh_w: dict[str, float] = defaultdict(float)
                for nb, w in adj[n].items():
                    neigh_w[comm[nb]] += w
                best_c = ci
                best_gain = neigh_w.get(ci, 0.0) - resolution * ki * sigma_tot[ci] / (2 * m)
                for c in sorted(neigh_w, key=str):     # 排序 → 确定性
                    gain = neigh_w[c] - resolution * ki * sigma_tot[c] / (2 * m)
                    if (gain > best_gain + 1e-12
                            or (abs(gain - best_gain) < 1e-12 and str(c) < str(best_c))):
                        best_gain, best_c = gain, c
                sigma_tot[best_c] += ki
                if best_c != ci:
                    comm[n] = best_c
                    improved = True

        # 重新编号成连续整数 (按社区最小节点 id 排序 → 确定性)
        comm_members: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            comm_members[comm[n]].append(n)
        cid = 0
        for members in sorted(comm_members.values(), key=min):
            for nid in members:
                self.nodes[nid].cluster = cid
            cid += 1

        # 后处理①: 同文件碎片合并 (Louvain 在弱连通子图上过度切割时的修复)
        # 问题: constraint_probe.py 这类文件函数间调用稀疏, Louvain 把它切成
        #       十几个 size=2 的碎片，map 阅读时噪音极大。
        # 修法: 同一文件若有多个 cluster 且存在 size <= 3 的碎片，
        #       合并到该文件的主簇 (该文件内节点最多的那个)。
        file_clusters: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
        for nid, node in self.nodes.items():
            if node.cluster >= 0 and node.path:
                file_clusters[node.path][node.cluster].append(nid)
        for path, cmap in file_clusters.items():
            if len(cmap) <= 1:
                continue  # 该文件只有一个 cluster，无需处理
            dominant = max(cmap, key=lambda c: len(cmap[c]))
            for c, nids in list(cmap.items()):
                if c != dominant and len(nids) <= 3:
                    for nid in nids:
                        self.nodes[nid].cluster = dominant

        # 后处理②: 孤立点 (在 real 但无边): 同文件的合并成一个簇, 避免单文件碎片化
        isolated_by_file: dict[str, list[str]] = defaultdict(list)
        for nid in sorted(real):
            if nid not in adj:
                path = self.nodes[nid].path or nid  # 无路径则用 id 兜底
                isolated_by_file[path].append(nid)
        for path in sorted(isolated_by_file):
            for nid in isolated_by_file[path]:
                self.nodes[nid].cluster = cid
            cid += 1

        # 返回实际不重复 cluster 数 (后处理合并后 cid 可能偏高)
        return len({n.cluster for n in self.nodes.values() if n.cluster >= 0})

    def cluster_summary(self) -> list[dict]:
        """每个功能块的摘要 (给大模型的'目录页')。"""
        groups: dict[int, list[Node]] = defaultdict(list)
        for n in self.nodes.values():
            if n.cluster >= 0:
                groups[n.cluster].append(n)
        summaries = []
        for cid, members in groups.items():
            files = sorted({m.path for m in members if m.path})
            # 代表性节点: 优先有 docstring 的类/函数
            reps = sorted(members,
                          key=lambda m: (m.kind != NodeKind.CLASS,
                                         not m.docstring, -m.confidence))
            # docstring 只取首行 + 限长: map 是"目录页", 一次列几十个簇,
            # 塞完整 docstring 会让单页膨胀到上万字符撑爆上下文。要全文用 describe。
            key_symbols = [{"id": r.id, "name": r.qualified_name,
                            "kind": r.kind.value,
                            "summary": _first_line(r.docstring, 100)}
                           for r in reps[:5]]
            summaries.append({
                "cluster": cid,
                "size": len(members),
                "files": files[:5],
                "file_count": len(files),
                "key_symbols": key_symbols,
            })
        summaries.sort(key=lambda s: -s["size"])
        return summaries

    # ---- persistence ----
    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
        }

    def save(self, root: str) -> str:
        """原子写: 先写临时文件再 os.replace 替换, 避免后台重建写到一半时
        被并发读取拿到半个 JSON 而崩溃 (os.replace 在同盘是原子操作)。"""
        d = os.path.join(root, ".craftsman-map")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "graph.json")
        tmp = os.path.join(d, f"graph.json.tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)   # 原子替换
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return path

    @classmethod
    def load(cls, root: str) -> "CodeGraph":
        path = os.path.join(root, ".craftsman-map", "graph.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        g = cls()
        g.meta = data.get("meta", {})
        for nd in data["nodes"]:
            g.nodes[nd["id"]] = Node(
                id=nd["id"], kind=NodeKind(nd["kind"]), name=nd["name"],
                qualified_name=nd["qualified_name"], path=nd["path"],
                line_start=nd.get("line_start", 0), line_end=nd.get("line_end", 0),
                signature=nd.get("signature", ""), docstring=nd.get("docstring", ""),
                confidence=nd.get("confidence", 1.0), cluster=nd.get("cluster", -1),
                meta=nd.get("meta", {}),
            )
        for ed in data["edges"]:
            g.edges.append(Edge(
                src=ed["src"], dst=ed["dst"], kind=EdgeKind(ed["kind"]),
                confidence=ed.get("confidence", 1.0), line=ed.get("line", 0),
                meta=ed.get("meta", {}),
            ))
        g.reindex()
        return g

    # ---- 自动陈旧检测 ----
    @classmethod
    def fingerprint(cls, root: str, ignore: set[str] | None = None) -> str:
        """快速计算源文件指纹: 文件数 + 最大 mtime (毫秒)。
        两者任一变化 → 图已陈旧，需重建。不读文件内容，速度极快。"""
        _ignore = ignore or {
            ".git", ".craftsman-map", "__pycache__", "node_modules",
            ".venv", "venv", "env", ".env", "dist", "build",
            ".idea", ".vscode", ".pytest_cache", ".mypy_cache", "site-packages",
        }
        _src_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".go",
                     ".md", ".txt", ".rst", ".png", ".jpg", ".svg"}
        max_mtime = 0
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _ignore]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in _src_exts:
                    continue
                file_count += 1
                try:
                    mt = int(os.path.getmtime(os.path.join(dirpath, fn)) * 1000)
                    if mt > max_mtime:
                        max_mtime = mt
                except OSError:
                    pass
        return f"{file_count}:{max_mtime}"

    @classmethod
    def is_stale(cls, root: str) -> bool:
        """判断已有索引是否陈旧。无索引也视为陈旧。"""
        graph_path = os.path.join(root, ".craftsman-map", "graph.json")
        if not os.path.exists(graph_path):
            return True
        try:
            with open(graph_path, "r", encoding="utf-8") as f:
                meta = json.load(f).get("meta", {})
            # schema 版本不匹配 → 旧缓存字段可能与新代码错位, 强制重建
            if meta.get("schema_version") != cls.SCHEMA_VERSION:
                return True
            saved_fp = meta.get("fingerprint", "")
            if not saved_fp:
                return True          # 旧版索引没有指纹 → 触发一次重建
            return cls.fingerprint(root) != saved_fp
        except Exception:
            return True

    _REBUILD_IN_PROGRESS: set = set()  # 防止同一 root 重复触发后台重建
    _REBUILD_COOLDOWN_SEC = 60         # 重建失败后的冷却期, 避免注定失败的重试风暴

    @classmethod
    def _rebuild_error_path(cls, root: str) -> str:
        return os.path.join(root, ".craftsman-map", "rebuild_error.json")

    @classmethod
    def rebuild_status(cls, root: str) -> dict | None:
        """读上次后台重建失败记录。返回 None 表示无失败记录 (正常)。"""
        p = cls._rebuild_error_path(root)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @classmethod
    def _trigger_async_rebuild(cls, root: str) -> None:
        """后台线程重建索引，同一 root 只允许一个重建并发。daemon 线程，不阻塞主进程退出。
        失败不再静默: 写 rebuild_error.json 并进入冷却期, 避免注定失败的重试每次调用都触发。"""
        import threading
        import time as _time
        if root in cls._REBUILD_IN_PROGRESS:
            return
        # 冷却期内不重复触发 (上次失败还没过 cooldown)
        err = cls.rebuild_status(root)
        if err and (_time.time() - err.get("ts", 0)) < cls._REBUILD_COOLDOWN_SEC:
            return
        cls._REBUILD_IN_PROGRESS.add(root)

        def _rebuild() -> None:
            try:
                from ..indexer import Indexer
                g = Indexer().index(root)
                g.meta["fingerprint"] = cls.fingerprint(root)
                g.save(root)
                # 成功 → 清除历史失败记录
                ep = cls._rebuild_error_path(root)
                if os.path.exists(ep):
                    try:
                        os.remove(ep)
                    except OSError:
                        pass
            except Exception as e:
                # 失败不静默: 落盘错误 + 时间戳, 供 rebuild_status/orient 暴露给调用方
                try:
                    os.makedirs(os.path.dirname(cls._rebuild_error_path(root)), exist_ok=True)
                    with open(cls._rebuild_error_path(root), "w", encoding="utf-8") as f:
                        json.dump({"error": str(e)[:500], "ts": _time.time(),
                                   "at": _time.strftime("%Y-%m-%d %H:%M:%S")},
                                  f, ensure_ascii=False)
                except Exception:
                    pass
            finally:
                cls._REBUILD_IN_PROGRESS.discard(root)

        threading.Thread(target=_rebuild, daemon=True).start()

    @classmethod
    def load_auto(cls, root: str, sync: bool = False) -> tuple["CodeGraph", bool]:
        """加载索引，源文件有变化时重建。
        返回 (graph, rebuilt):
          rebuilt=False — 图是最新的(指纹一致)，直接复用缓存。
          rebuilt=True  — 检测到源码变更, 已重建(sync=True 时已同步存盘为最新)。

        sync 语义 (关键: 区分调用方生命周期):
          sync=False (默认, 长驻进程如 MCP server): 立刻返回旧图 + 后台线程重建,
            不阻塞调用方; rebuilt=True 表示"返回的是旧图, 下次调用才是最新"。
          sync=True (一次性进程如 CLI): 同步重建 + 存盘再返回最新图。
            CLI 每次都是独立短命进程, 后台 daemon 线程会随进程退出被杀,
            fingerprint 永远存不下 → 每次调用都重复触发注定完不成的重建。
            sync=True 让 CLI 首次检测到陈旧时同步重建并落盘指纹, 之后指纹一致不再重建。
        首次无索引时无论 sync 与否都同步构建 (没有旧图可返回, 只能等)。"""
        root = os.path.abspath(root)
        if not cls.is_stale(root):
            return cls.load(root), False
        # 无旧图：首次构建，只能同步等
        graph_path = os.path.join(root, ".craftsman-map", "graph.json")
        if not os.path.exists(graph_path):
            from ..indexer import Indexer
            g = Indexer().index(root)
            g.meta["fingerprint"] = cls.fingerprint(root)
            g.save(root)
            return g, True
        if sync:
            # 一次性进程: 同步重建 + 落盘指纹, 返回最新图。之后指纹一致不再重建。
            from ..indexer import Indexer
            g = Indexer().index(root)
            g.meta["fingerprint"] = cls.fingerprint(root)
            g.save(root)
            return g, True
        # 长驻进程: 立刻返回旧图 + 后台重建，不阻塞调用方
        cls._trigger_async_rebuild(root)
        return cls.load(root), True  # rebuilt=True (返回的是旧图)
