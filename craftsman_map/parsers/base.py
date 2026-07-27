"""
解析器抽象接口
==============
所有语言解析器实现此接口。MVP 只实现 PythonParser (用内置 ast, 零依赖 100% 可靠)。
v2 加 tree-sitter 支持 JS/TS/Go 等,只需新增一个实现类,不改上层。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..graph.model import Node, Edge


class ParseResult:
    """单个文件的解析产物。"""
    def __init__(self) -> None:
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []

    def add_node(self, node: Node) -> None:
        self.nodes.append(node)

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)

    def merge(self, other: "ParseResult") -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)


class BaseParser(ABC):
    """语言解析器基类。"""

    #: 该解析器支持的文件扩展名 (小写, 带点)
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, abs_path: str, rel_path: str, source: str) -> ParseResult:
        """解析单个文件的源码, 返回节点与边。

        Args:
            abs_path: 文件绝对路径
            rel_path: 相对项目根的路径 (用于生成稳定 ID)
            source:   文件源码文本
        """
        raise NotImplementedError

    def supports(self, ext: str) -> bool:
        return ext.lower() in self.extensions
