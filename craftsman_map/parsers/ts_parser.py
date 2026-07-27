"""
Tree-sitter 多语言解析器 (JS / TS / Go)
========================================
把设计里的"多语言支持"落地。复用现有 Node/Edge 模型与 linker,
只负责从 tree-sitter 语法树抽取节点与边, 上层 indexer/linker/store 不变。

置信度约定 (与 PythonParser 对齐):
  - contains / imports: 1.0 (语法树直接给出, 铁证)
  - calls / inherits:   0.7 (名字级, 交给 linker 二次消解)

依赖 (可选): tree-sitter>=0.25 + tree-sitter-{javascript,typescript,go}
  未安装时本模块的 available() 返回 False, indexer 自动跳过, 不影响 Python 解析。

设计取舍:
  各语言语法树节点类型不同, 但"函数/类/方法/import/调用/继承"这些概念通用。
  用每语言一张"节点类型映射表"驱动统一提取逻辑, 新增语言只加一张表。
"""
from __future__ import annotations

from .base import BaseParser, ParseResult
from ..graph.model import Node, Edge, NodeKind, EdgeKind


# ---- 懒加载语言 (import 失败则该语言不可用, 不拖垮整体) ----
def _load_languages() -> dict:
    langs: dict[str, object] = {}
    try:
        from tree_sitter import Language, Parser  # noqa
    except Exception:
        return langs

    def _try(mod_name: str, attr: str, keys: list[str]):
        try:
            mod = __import__(mod_name)
            lang_fn = getattr(mod, attr)
            from tree_sitter import Language, Parser
            L = Language(lang_fn())
            p = Parser(L)
            for k in keys:
                langs[k] = p
        except Exception:
            pass

    _try("tree_sitter_javascript", "language", ["js"])
    # TS 包同时提供 typescript / tsx 两个语言函数
    try:
        import tree_sitter_typescript as tst
        from tree_sitter import Language, Parser
        langs["ts"] = Parser(Language(tst.language_typescript()))
        langs["tsx"] = Parser(Language(tst.language_tsx()))
    except Exception:
        pass
    _try("tree_sitter_go", "language", ["go"])
    return langs


_PARSERS = _load_languages()

EXT_LANG = {
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "ts", ".tsx": "tsx",
    ".go": "go",
}


def _seg(rel_path: str, qual: str = "") -> str:
    return f"{rel_path}::{qual}" if qual else rel_path


class _Extractor:
    """从 tree-sitter 语法树抽取节点/边的通用逻辑, 由语言配置驱动。"""

    def __init__(self, lang: str, rel_path: str, source: bytes, result: ParseResult):
        self.lang = lang
        self.rel = rel_path
        self.src = source
        self.result = result
        self.module_id = _seg(rel_path)

    def text(self, node) -> str:
        return self.src[node.start_byte:node.end_byte].decode("utf-8", "ignore")

    def line(self, node) -> int:
        return node.start_point[0] + 1

    def child_field(self, node, field: str):
        return node.child_by_field_name(field)

    # ---- 入口 ----
    def run(self, tree) -> None:
        # module 节点
        self.result.add_node(Node(
            id=self.module_id, kind=NodeKind.MODULE,
            name=self.rel.rsplit("/", 1)[-1], qualified_name=self.rel,
            path=self.rel, line_start=1,
            line_end=tree.root_node.end_point[0] + 1, confidence=1.0,
        ))
        self._walk(tree.root_node, container=self.module_id, scope=[])

    # ---- 递归遍历 ----
    def _walk(self, node, container: str, scope: list[str]) -> None:
        for child in node.children:
            handled = self._handle(child, container, scope)
            if not handled:
                self._walk(child, container, scope)

    def _handle(self, node, container: str, scope: list[str]) -> bool:
        t = node.type

        # ---- import ----
        if t in ("import_statement", "import_declaration"):
            self._import(node, container)
            return True
        if self.lang == "go" and t == "import_spec":
            self._go_import(node, container)
            return True

        # ---- class (JS/TS) ----
        if t in ("class_declaration", "class"):
            self._class(node, container, scope)
            return True

        # ---- interface (TS) ----
        if t == "interface_declaration":
            self._class(node, container, scope, as_interface=True)
            return True

        # ---- function ----
        if t in ("function_declaration", "function", "generator_function_declaration",
                 "method_definition", "function_definition"):
            self._function(node, container, scope)
            return True

        # ---- Go: func / method ----
        if self.lang == "go" and t in ("function_declaration", "method_declaration"):
            self._function(node, container, scope)
            return True

        # ---- Go: type struct/interface ----
        if self.lang == "go" and t == "type_declaration":
            self._go_type(node, container, scope)
            return True

        # ---- arrow func 赋给 const: const foo = () => {} ----
        if t in ("lexical_declaration", "variable_declaration"):
            self._maybe_arrow_func(node, container, scope)
            return False  # 继续遍历内部

        return False

    # ---- handlers ----
    def _name(self, node) -> str:
        n = self.child_field(node, "name")
        if n is not None:
            return self.text(n)
        return ""

    def _import(self, node, container: str) -> None:
        # JS/TS: import ... from "source"
        src_node = self.child_field(node, "source")
        if src_node is None:
            # go import_declaration 包含多个 spec, 交给 walk
            for c in node.children:
                if c.type == "import_spec":
                    self._go_import(c, container)
            return
        dep = self.text(src_node).strip('"\'`')
        dep_id = f"external::{dep}"
        self.result.add_node(Node(
            id=dep_id, kind=NodeKind.IMPORT, name=dep.rsplit("/", 1)[-1],
            qualified_name=dep, path="", confidence=1.0, meta={"external": True}))
        self.result.add_edge(Edge(
            src=container, dst=dep_id, kind=EdgeKind.IMPORTS,
            confidence=1.0, line=self.line(node)))

    def _go_import(self, node, container: str) -> None:
        path_node = self.child_field(node, "path") or node
        dep = self.text(path_node).strip('"`')
        if not dep:
            return
        dep_id = f"external::{dep}"
        self.result.add_node(Node(
            id=dep_id, kind=NodeKind.IMPORT, name=dep.rsplit("/", 1)[-1],
            qualified_name=dep, path="", confidence=1.0, meta={"external": True}))
        self.result.add_edge(Edge(
            src=container, dst=dep_id, kind=EdgeKind.IMPORTS,
            confidence=1.0, line=self.line(node)))

    def _class(self, node, container: str, scope: list[str],
               as_interface: bool = False) -> None:
        name = self._name(node)
        if not name:
            return
        qual = ".".join(scope + [name]) if scope else name
        cid = _seg(self.rel, qual)
        self.result.add_node(Node(
            id=cid, kind=NodeKind.INTERFACE if as_interface else NodeKind.CLASS,
            name=name, qualified_name=qual, path=self.rel,
            line_start=self.line(node),
            line_end=node.end_point[0] + 1,
            signature=f"{'interface' if as_interface else 'class'} {name}",
            confidence=1.0))
        self.result.add_edge(Edge(
            src=container, dst=cid, kind=EdgeKind.CONTAINS,
            confidence=1.0, line=self.line(node)))
        # 继承 (JS/TS: class_heritage / extends_clause)
        self._heritage(node, cid)
        # 类体里的方法
        body = self.child_field(node, "body")
        if body is not None:
            self._walk(body, container=cid, scope=scope + [name])

    def _heritage(self, node, cid: str) -> None:
        for c in node.children:
            if c.type in ("class_heritage", "extends_clause", "extends_type_clause",
                          "implements_clause"):
                for ident in c.children:
                    if ident.type in ("identifier", "type_identifier",
                                      "member_expression"):
                        base = self.text(ident)
                        kind = (EdgeKind.IMPLEMENTS
                                if c.type == "implements_clause"
                                else EdgeKind.INHERITS)
                        self.result.add_edge(Edge(
                            src=cid, dst=f"?::{base}", kind=kind,
                            confidence=0.7, line=self.line(c),
                            meta={"unresolved": base}))

    def _function(self, node, container: str, scope: list[str]) -> None:
        name = self._name(node)
        if not name:
            return
        qual = ".".join(scope + [name]) if scope else name
        fid = _seg(self.rel, qual)
        params_node = self.child_field(node, "parameters")
        params = self.text(params_node) if params_node else "()"
        self.result.add_node(Node(
            id=fid, kind=NodeKind.FUNCTION, name=name, qualified_name=qual,
            path=self.rel, line_start=self.line(node),
            line_end=node.end_point[0] + 1,
            signature=f"{name}{params}", confidence=1.0))
        self.result.add_edge(Edge(
            src=container, dst=fid, kind=EdgeKind.CONTAINS,
            confidence=1.0, line=self.line(node)))
        # 函数体内的调用
        body = self.child_field(node, "body")
        if body is not None:
            self._collect_calls(body, fid)
            # 嵌套函数/类
            self._walk(body, container=fid, scope=scope + [name])

    def _collect_calls(self, node, fid: str) -> None:
        for c in self._descendants(node):
            if c.type in ("call_expression",):
                fn = self.child_field(c, "function")
                if fn is not None:
                    callee = self._callee_name(fn)
                    if callee:
                        self.result.add_edge(Edge(
                            src=fid, dst=f"?::{callee}", kind=EdgeKind.CALLS,
                            confidence=0.7, line=self.line(c),
                            meta={"unresolved": callee}))

    def _callee_name(self, node) -> str:
        if node.type in ("identifier", "type_identifier", "field_identifier"):
            return self.text(node)
        if node.type in ("member_expression", "selector_expression"):
            prop = self.child_field(node, "property") or self.child_field(node, "field")
            if prop is not None:
                return self.text(prop)
        return ""

    def _descendants(self, node):
        """遍历后代但不下钻进嵌套函数体 (避免把内层调用算到外层)。"""
        for c in node.children:
            if c.type in ("function_declaration", "function", "method_definition",
                          "arrow_function", "function_definition",
                          "method_declaration"):
                continue
            yield c
            yield from self._descendants(c)

    def _maybe_arrow_func(self, node, container: str, scope: list[str]) -> None:
        # const foo = (...) => {...}
        for decl in node.children:
            if decl.type != "variable_declarator":
                continue
            name_node = self.child_field(decl, "name")
            val = self.child_field(decl, "value")
            if name_node is None or val is None:
                continue
            if val.type in ("arrow_function", "function"):
                name = self.text(name_node)
                qual = ".".join(scope + [name]) if scope else name
                fid = _seg(self.rel, qual)
                self.result.add_node(Node(
                    id=fid, kind=NodeKind.FUNCTION, name=name,
                    qualified_name=qual, path=self.rel,
                    line_start=self.line(decl), line_end=val.end_point[0] + 1,
                    signature=f"{name} = (...) =>", confidence=1.0))
                self.result.add_edge(Edge(
                    src=container, dst=fid, kind=EdgeKind.CONTAINS,
                    confidence=1.0, line=self.line(decl)))
                body = self.child_field(val, "body")
                if body is not None:
                    self._collect_calls(body, fid)

    def _go_type(self, node, container: str, scope: list[str]) -> None:
        # type Foo struct {...} / type Bar interface {...}
        for spec in node.children:
            if spec.type != "type_spec":
                continue
            name_node = self.child_field(spec, "name")
            type_node = self.child_field(spec, "type")
            if name_node is None:
                continue
            name = self.text(name_node)
            cid = _seg(self.rel, name)
            is_iface = type_node is not None and type_node.type == "interface_type"
            self.result.add_node(Node(
                id=cid, kind=NodeKind.INTERFACE if is_iface else NodeKind.CLASS,
                name=name, qualified_name=name, path=self.rel,
                line_start=self.line(spec), line_end=spec.end_point[0] + 1,
                signature=f"type {name} {'interface' if is_iface else 'struct'}",
                confidence=1.0))
            self.result.add_edge(Edge(
                src=container, dst=cid, kind=EdgeKind.CONTAINS,
                confidence=1.0, line=self.line(spec)))


class TreeSitterParser(BaseParser):
    """多语言解析器。支持的扩展名取决于成功加载的语言包。"""

    extensions = tuple(EXT_LANG.keys())

    @staticmethod
    def available() -> bool:
        return len(_PARSERS) > 0

    def supports(self, ext: str) -> bool:
        lang = EXT_LANG.get(ext.lower())
        return lang is not None and lang in _PARSERS

    def parse(self, abs_path: str, rel_path: str, source: str) -> ParseResult:
        result = ParseResult()
        ext = "." + rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
        lang = EXT_LANG.get(ext)
        parser = _PARSERS.get(lang) if lang else None
        if parser is None:
            return result
        try:
            src_bytes = source.encode("utf-8", "ignore")
            tree = parser.parse(src_bytes)
        except Exception as e:
            result.add_node(Node(
                id=_seg(rel_path), kind=NodeKind.MODULE,
                name=rel_path.rsplit("/", 1)[-1], qualified_name=rel_path,
                path=rel_path, confidence=0.3, meta={"parse_error": str(e)}))
            return result
        ext_obj = _Extractor(lang, rel_path, src_bytes, result)
        ext_obj.run(tree)
        return result
