"""
文档 & 资产解析器
=================
DocParser: 把 .md/.rst/.txt 记为 DOC 节点, 抽取标题/首段作摘要。
AssetParser: 把 图片/其它多模态文件记为 ASSET 节点 (v1 只记路径+元信息,
             不做视觉理解——按主人拍板 v2 再上)。
"""
from __future__ import annotations

import os

from .base import BaseParser, ParseResult
from ..graph.model import Node, NodeKind


class DocParser(BaseParser):
    extensions = (".md", ".rst", ".txt")

    def parse(self, abs_path: str, rel_path: str, source: str) -> ParseResult:
        result = ParseResult()
        lines = source.splitlines()
        title = ""
        summary = ""
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if not title:
                title = s.lstrip("#").strip()
                continue
            if not s.startswith("#"):
                summary = s
                break
        result.add_node(Node(
            id=f"{rel_path}", kind=NodeKind.DOC,
            name=title or rel_path.rsplit("/", 1)[-1],
            qualified_name=rel_path, path=rel_path,
            line_start=1, line_end=len(lines),
            docstring=summary[:200],
            confidence=1.0,
            meta={"title": title, "lines": len(lines)},
        ))
        return result


class AssetParser(BaseParser):
    extensions = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp")

    def parse(self, abs_path: str, rel_path: str, source: str) -> ParseResult:
        result = ParseResult()
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = 0
        ext = os.path.splitext(rel_path)[1].lower().lstrip(".")
        result.add_node(Node(
            id=f"{rel_path}", kind=NodeKind.ASSET,
            name=rel_path.rsplit("/", 1)[-1],
            qualified_name=rel_path, path=rel_path,
            confidence=1.0,
            meta={"asset_type": ext, "bytes": size,
                  "visual_understood": False},  # v2 视觉理解开关
        ))
        return result

    def is_binary(self) -> bool:
        return True
