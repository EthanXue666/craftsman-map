"""
索引器 —— INGEST 层入口
=======================
把整个流程串起来:
  扫描目录 → 分派解析器 → 汇总节点/边 → linker 消解 → 聚类 → 存盘

对应设计三层的最底层 (摄取)。产出 .craftsman-map/graph.json。
"""
from __future__ import annotations

import fnmatch
import os
import time

from .parsers.python_parser import PythonParser
from .parsers.doc_parser import DocParser, AssetParser
from .parsers.ts_parser import TreeSitterParser
from .parsers.base import ParseResult
from .graph.store import CodeGraph
from .graph.linker import link

# 默认忽略目录（精确匹配）
DEFAULT_IGNORE = {
    ".git", ".craftsman-map", "__pycache__", "node_modules",
    ".venv", "venv", "env", ".env", "dist", "build", ".idea",
    ".vscode", ".pytest_cache", ".mypy_cache", "site-packages",
}

# 默认忽略目录 glob 模式（fnmatch，支持通配符）
DEFAULT_IGNORE_DIR_PATTERNS: list[str] = [
    "audit_probe_cache_*",  # 测试产物缓存目录
    "audit_probe_cache",    # 测试产物缓存目录（无后缀版）
    "audit_outputs",        # 审计输出产物目录
    "uploads",              # 测试输出/上传产物目录
    "*.egg-info",           # Python 打包产物
    "v7_extracted_projects", # 工匠生成的示例项目产物，不是源码
    "v6_baseline_20260715",  # v6 历史基线快照，不是当前代码
]

# 默认忽略文件 glob 模式（fnmatch，支持通配符）
DEFAULT_IGNORE_FILE_PATTERNS: list[str] = [
    "_tmp_*.py",            # 临时探针/调试文件
    "_build_output*.txt",   # 打包输出产物
    "_pyi_*.txt",           # pyinstaller 产物
]


class Indexer:
    def __init__(
        self,
        ignore: set[str] | None = None,
        ignore_dir_patterns: list[str] | None = None,
        ignore_file_patterns: list[str] | None = None,
    ) -> None:
        # PythonParser 优先 (.py 走内置 ast, 零依赖最可靠);
        # TreeSitterParser 补齐 JS/TS/Go, 未装语言包时其 supports() 恒 False, 自动跳过。
        self.parsers = [PythonParser(), TreeSitterParser(), DocParser(), AssetParser()]
        self.ignore = ignore or DEFAULT_IGNORE
        self.ignore_dir_patterns = ignore_dir_patterns if ignore_dir_patterns is not None else DEFAULT_IGNORE_DIR_PATTERNS
        self.ignore_file_patterns = ignore_file_patterns if ignore_file_patterns is not None else DEFAULT_IGNORE_FILE_PATTERNS

    def _pick(self, ext: str):
        for p in self.parsers:
            if p.supports(ext):
                return p
        return None

    def _load_gitignore_dirs(self, root: str) -> set[str]:
        """从 .gitignore 提取'纯目录名'模式并入忽略集。

        简化版: 只取无路径分隔、无通配符的行 (覆盖 node_modules/dist/coverage
        这类最常见的目录排除)。完整 glob 语义不在此处理——宁可少排除也不错杀,
        且排除结果会写进 meta.exclusions 供调用方核对 (透明化)。
        """
        dirs: set[str] = set()
        gi = os.path.join(root, ".gitignore")
        if not os.path.exists(gi):
            return dirs
        try:
            with open(gi, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("!"):
                        continue
                    name = line.rstrip("/")
                    if "/" not in name and "*" not in name and "?" not in name:
                        dirs.add(name)
        except Exception:
            pass
        return dirs

    def index(self, root: str, with_git: bool = True) -> CodeGraph:
        root = os.path.abspath(root)
        t0 = time.time()
        agg = ParseResult()
        file_count = 0
        parsed_count = 0
        blind_spots: list[dict] = []   # 有意跳过/读取失败的文件 (诚实标注盲区)

        # 排除集 = 内置默认 + .gitignore 提取的纯目录名 (透明化)
        gitignore_dirs = self._load_gitignore_dirs(root)
        effective_ignore = self.ignore | gitignore_dirs

        def _dir_excluded(d: str) -> bool:
            if d in effective_ignore:
                return True
            return any(fnmatch.fnmatch(d, pat) for pat in self.ignore_dir_patterns)

        def _file_excluded(fn: str) -> bool:
            return any(fnmatch.fnmatch(fn, pat) for pat in self.ignore_file_patterns)

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _dir_excluded(d)]
            for fn in filenames:
                if _file_excluded(fn):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                parser = self._pick(ext)
                if not parser:
                    continue
                file_count += 1
                abs_path = os.path.join(dirpath, fn)
                rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
                # 二进制资产不读文本
                if isinstance(parser, AssetParser):
                    res = parser.parse(abs_path, rel_path, "")
                else:
                    try:
                        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                            source = f.read()
                    except Exception as e:
                        # 读取失败 = 真盲区, 记录不静默吞
                        blind_spots.append({
                            "path": rel_path, "reason": "read_failed",
                            "detail": str(e)[:200],
                        })
                        continue
                    try:
                        res = parser.parse(abs_path, rel_path, source)
                    except Exception as e:
                        # 解析器崩溃 (超出其内部容错) = 盲区
                        blind_spots.append({
                            "path": rel_path, "reason": "parse_crashed",
                            "detail": str(e)[:200],
                        })
                        continue
                agg.merge(res)
                parsed_count += 1

        # linker 消解
        nodes, edges = link(agg.nodes, agg.edges)

        g = CodeGraph()
        g.add_nodes(nodes)
        g.add_edges(edges)
        g.reindex()
        n_clusters = g.cluster()

        # 语法错误盲区: 解析器内部为语法错误建了 confidence=0.3 的 module 节点,
        # 汇总成 blind_spots 让调用方知道'这些文件只有壳、内部符号缺失'
        for n in g.nodes.values():
            if n.meta.get("parse_error"):
                blind_spots.append({
                    "path": n.path, "reason": "syntax_error",
                    "detail": str(n.meta.get("parse_error"))[:200],
                })

        g.meta = {
            "root": root,
            "indexed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round(time.time() - t0, 3),
            "files_scanned": file_count,
            "files_parsed": parsed_count,
            "node_count": len(g.nodes),
            "edge_count": len(g.edges),
            "cluster_count": n_clusters,
            "version": "0.1.0-mvp",
            "schema_version": CodeGraph.SCHEMA_VERSION,
            "fingerprint": "",  # 由调用方 (indexer.index or load_auto) 在 save() 前填入
            "blind_spots": blind_spots,
            "blind_spot_count": len(blind_spots),
            "exclusions": {
                "builtin": sorted(self.ignore),
                "from_gitignore": sorted(gitignore_dirs),
                "dir_patterns": self.ignore_dir_patterns,
                "file_patterns": self.ignore_file_patterns,
                "note": "builtin/from_gitignore 是精确目录名; dir_patterns/file_patterns 是 fnmatch 通配符模式。",
            },
        }

        # git 历史维度 (GitNexus 式): 变更热度/最近修改/共变耦合
        if with_git:
            try:
                from . import git_history
                git_data = git_history.analyze(root)
                git_history.attach_to_graph(g, git_data)
            except Exception as e:
                g.meta["git"] = {"available": False, "error": str(e)}
        else:
            g.meta["git"] = {"available": False, "skipped": True}
        return g
