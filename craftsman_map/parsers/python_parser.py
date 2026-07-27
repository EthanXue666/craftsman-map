"""
Python 解析器 (基于内置 ast, 零依赖)
====================================
提取:
  - 节点: module / class / function / variable / import
  - 边:   contains / imports / inherits / calls / decorates / references

置信度约定:
  - contains / imports / inherits / decorates: 1.0 (AST 直接给出, 铁证)
  - calls: 名字级解析, 0.7 (可能有同名歧义, 需 linker 二次消解)
"""
from __future__ import annotations

import ast

from .base import BaseParser, ParseResult
from ..graph.model import Node, Edge, NodeKind, EdgeKind


def _seg(rel_path: str, qual: str = "") -> str:
    """生成稳定节点 ID。"""
    return f"{rel_path}::{qual}" if qual else rel_path


def _is_stub_body(node) -> bool:
    """判断函数体是否为'空实现'(可测量的客观事实, 供 report 统计)。

    判定为 stub 的情形:
      - 仅 pass
      - 仅 docstring (纯字符串表达式)
      - 仅 ...  (Ellipsis, 常见于类型存根/协议方法)
      - 仅 raise NotImplementedError
    以上组合(如 docstring + pass / docstring + raise NotImplementedError)也算。
    有任何实质语句则不是 stub。宁可少判也不错判——是线索性事实, 不做主观推断。
    """
    body = [s for s in node.body]
    if not body:
        return True
    real = []
    for s in body:
        # docstring: 独立字符串表达式
        if isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None),
                                                  (ast.Str, ast.Constant)):
            val = getattr(s.value, "value", getattr(s.value, "s", None))
            if isinstance(val, str) or val is Ellipsis:
                continue
        if isinstance(s, ast.Pass):
            continue
        # 裸 ... (Ellipsis 表达式)
        if isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant) \
                and s.value.value is Ellipsis:
            continue
        # raise NotImplementedError [()]
        if isinstance(s, ast.Raise):
            exc = s.exc
            name = None
            if isinstance(exc, ast.Name):
                name = exc.id
            elif isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            if name == "NotImplementedError":
                continue
        real.append(s)
    return len(real) == 0


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel_path: str, source: str, result: ParseResult) -> None:
        self.rel = rel_path
        self.src_lines = source.splitlines()
        self.result = result
        self.scope: list[str] = []          # 限定名栈
        self.module_id = _seg(rel_path)

    # ---- helpers ----
    def _qual(self, name: str) -> str:
        return ".".join(self.scope + [name]) if self.scope else name

    def _cur_container(self) -> str:
        if self.scope:
            return _seg(self.rel, ".".join(self.scope))
        return self.module_id

    def _line_src(self, node: ast.AST) -> str:
        try:
            return self.src_lines[node.lineno - 1].strip()
        except Exception:
            return ""

    # ---- module ----
    def build_module(self, tree: ast.Module) -> None:
        doc = ast.get_docstring(tree) or ""
        self.result.add_node(Node(
            id=self.module_id, kind=NodeKind.MODULE,
            name=self.rel.rsplit("/", 1)[-1], qualified_name=self.rel,
            path=self.rel, line_start=1,
            line_end=len(self.src_lines),
            docstring=doc.splitlines()[0] if doc else "",
            confidence=1.0,
        ))

    # ---- imports ----
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            dep = alias.name
            dep_id = f"external::{dep}"
            self.result.add_node(Node(
                id=dep_id, kind=NodeKind.IMPORT, name=dep,
                qualified_name=dep, path="", confidence=1.0,
                meta={"external": True},
            ))
            self.result.add_edge(Edge(
                src=self._cur_container(), dst=dep_id,
                kind=EdgeKind.IMPORTS, confidence=1.0, line=node.lineno,
            ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            full = f"{mod}.{alias.name}" if mod else alias.name
            dep_id = f"external::{full}"
            self.result.add_node(Node(
                id=dep_id, kind=NodeKind.IMPORT, name=alias.name,
                qualified_name=full, path="", confidence=1.0,
                meta={"external": True, "from": mod},
            ))
            self.result.add_edge(Edge(
                src=self._cur_container(), dst=dep_id,
                kind=EdgeKind.IMPORTS, confidence=1.0, line=node.lineno,
            ))
        self.generic_visit(node)

    # ---- class ----
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = self._qual(node.name)
        cid = _seg(self.rel, qual)
        doc = ast.get_docstring(node) or ""
        # 判定接口: 继承 ABC 或 Protocol
        base_names = [self._name_of(b) for b in node.bases]
        is_iface = any(b in ("ABC", "Protocol", "ABCMeta") for b in base_names)
        self.result.add_node(Node(
            id=cid, kind=NodeKind.INTERFACE if is_iface else NodeKind.CLASS,
            name=node.name, qualified_name=qual, path=self.rel,
            line_start=node.lineno, line_end=getattr(node, "end_lineno", node.lineno),
            signature=f"class {node.name}({', '.join(base_names)})" if base_names else f"class {node.name}",
            docstring=doc.splitlines()[0] if doc else "",
            confidence=1.0,
        ))
        self.result.add_edge(Edge(
            src=self._cur_container(), dst=cid,
            kind=EdgeKind.CONTAINS, confidence=1.0, line=node.lineno,
        ))
        # 继承边
        for b in base_names:
            if b and b not in ("object",):
                self.result.add_edge(Edge(
                    src=cid, dst=f"?::{b}",
                    kind=EdgeKind.IMPLEMENTS if b in ("ABC", "Protocol") else EdgeKind.INHERITS,
                    confidence=0.7, line=node.lineno, meta={"unresolved": b},
                ))
        # 装饰器
        self._decorators(node, cid)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    # ---- function ----
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node, is_async=True)

    def _function(self, node, is_async: bool = False) -> None:
        qual = self._qual(node.name)
        fid = _seg(self.rel, qual)
        doc = ast.get_docstring(node) or ""
        args = [a.arg for a in node.args.args]
        prefix = "async def" if is_async else "def"
        fmeta = {}
        if _is_stub_body(node):
            # 空实现: 函数体只有 pass / docstring / ... / raise NotImplementedError
            # 是可测量的客观事实, 供 report 的 empty_implementations 统计 (不静默吞)
            fmeta["is_stub"] = True
        self.result.add_node(Node(
            id=fid, kind=NodeKind.FUNCTION, name=node.name,
            qualified_name=qual, path=self.rel,
            line_start=node.lineno, line_end=getattr(node, "end_lineno", node.lineno),
            signature=f"{prefix} {node.name}({', '.join(args)})",
            docstring=doc.splitlines()[0] if doc else "",
            confidence=1.0, meta=fmeta,
        ))
        self.result.add_edge(Edge(
            src=self._cur_container(), dst=fid,
            kind=EdgeKind.CONTAINS, confidence=1.0, line=node.lineno,
        ))
        self._decorators(node, fid)
        # 调用边: 遍历函数体找 Call
        self.scope.append(node.name)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                callee = self._name_of(sub.func)
                if callee:
                    self.result.add_edge(Edge(
                        src=fid, dst=f"?::{callee}",
                        kind=EdgeKind.CALLS, confidence=0.7,
                        line=getattr(sub, "lineno", node.lineno),
                        meta={"unresolved": callee},
                    ))
        self.scope.pop()
        # 不 generic_visit 进函数体 (避免把内部调用当成嵌套定义重复), 但要抓嵌套函数/类
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.scope.append(node.name)
                self.visit(child)
                self.scope.pop()

    # ---- module-level variable ----
    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.scope:  # 仅模块级
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    vid = _seg(self.rel, t.id)
                    self.result.add_node(Node(
                        id=vid, kind=NodeKind.VARIABLE, name=t.id,
                        qualified_name=t.id, path=self.rel,
                        line_start=node.lineno, line_end=node.lineno,
                        confidence=1.0,
                    ))
                    self.result.add_edge(Edge(
                        src=self.module_id, dst=vid,
                        kind=EdgeKind.CONTAINS, confidence=1.0, line=node.lineno,
                    ))
        self.generic_visit(node)

    # ---- utils ----
    def _decorators(self, node, target_id: str) -> None:
        for dec in getattr(node, "decorator_list", []):
            dname = self._name_of(dec)
            if dname:
                self.result.add_edge(Edge(
                    src=f"?::{dname}", dst=target_id,
                    kind=EdgeKind.DECORATES, confidence=0.8,
                    line=getattr(dec, "lineno", node.lineno),
                    meta={"unresolved": dname},
                ))

    def _name_of(self, node) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._name_of(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call):
            return self._name_of(node.func)
        return ""


class PythonParser(BaseParser):
    extensions = (".py",)

    def parse(self, abs_path: str, rel_path: str, source: str) -> ParseResult:
        result = ParseResult()
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            # 解析失败也建一个 module 节点, 标低置信度, 不让整个索引崩
            result.add_node(Node(
                id=_seg(rel_path), kind=NodeKind.MODULE,
                name=rel_path.rsplit("/", 1)[-1], qualified_name=rel_path,
                path=rel_path, confidence=0.3,
                meta={"parse_error": str(e)},
            ))
            return result
        v = _Visitor(rel_path, source, result)
        v.build_module(tree)
        v.visit(tree)
        return result
