"""
craftsman-map 图数据模型
========================
定义代码库知识图谱的节点(Node)与边(Edge)。

设计原则:
1. 每个节点/边都带 confidence 置信度——静态确定=1.0, 启发式推断<1.0。
   让大模型知道哪些是铁证、哪些是猜测,不拿推测冒充事实。
2. 节点稳定 ID = 相对路径 + 限定名(qualified name),跨索引可复现。
3. 一切可 JSON 序列化——craftsman-map 的输出契约就是结构化 JSON。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class NodeKind(str, Enum):
    """8 种节点类型 —— 系统'认识'代码库里的哪些东西。"""
    MODULE = "module"        # 文件/模块
    CLASS = "class"          # 类
    FUNCTION = "function"    # 函数/方法
    VARIABLE = "variable"    # 模块级变量/常量
    INTERFACE = "interface"  # 接口/协议/抽象基类
    IMPORT = "import"        # 外部依赖
    DOC = "doc"              # 文档 (md/rst/txt)
    ASSET = "asset"          # 多模态资产 (图片/其它)


class EdgeKind(str, Enum):
    """9 种关系类型 —— 决定大模型'能问出哪些问题'。"""
    CONTAINS = "contains"      # 模块包含类/函数; 类包含方法
    CALLS = "calls"            # 函数调用函数
    IMPORTS = "imports"        # 模块 import 模块/符号
    INHERITS = "inherits"      # 类继承类
    IMPLEMENTS = "implements"  # 类实现接口
    REFERENCES = "references"  # 引用变量/名字
    DECORATES = "decorates"    # 装饰器修饰
    RETURNS = "returns"        # 函数返回某类型 (启发式)
    DOCUMENTS = "documents"    # 文档描述某代码单元 (启发式)


@dataclass
class Node:
    id: str                              # 稳定唯一 ID, 如 "src/auth.py::LoginService.login"
    kind: NodeKind
    name: str                            # 短名, 如 "login"
    qualified_name: str                  # 限定名, 如 "LoginService.login"
    path: str                            # 相对文件路径
    line_start: int = 0
    line_end: int = 0
    signature: str = ""                  # 函数签名/类声明
    docstring: str = ""                  # 首行 docstring (摘要用)
    confidence: float = 1.0              # 置信度 [0,1]
    cluster: int = -1                    # 所属功能块 (聚类后填充, -1=未分配)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class Edge:
    src: str                             # 源节点 id
    dst: str                             # 目标节点 id
    kind: EdgeKind
    confidence: float = 1.0              # 置信度: 静态解析=1.0, 名字匹配推断<1.0
    line: int = 0                        # 关系发生的行号
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @property
    def key(self) -> tuple:
        return (self.src, self.dst, self.kind.value)
