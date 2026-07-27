"""
解析层测试 —— 验证 PythonParser / DocParser / AssetParser 提取正确的节点与边。
锁定 INGEST 层不回归。
"""
from __future__ import annotations

from craftsman_map.graph.model import NodeKind, EdgeKind


def _ids(graph):
    return set(graph.nodes.keys())


def test_module_nodes_created(indexed_graph):
    """每个 .py 文件都应产出一个 module 节点。"""
    ids = _ids(indexed_graph)
    assert "auth/base.py" in ids
    assert "auth/login.py" in ids
    assert "utils/helpers.py" in ids


def test_class_and_interface_detection(indexed_graph):
    """继承 ABC 的类识别为 interface, 普通类识别为 class。"""
    base = indexed_graph.nodes.get("auth/base.py::AuthProvider")
    assert base is not None
    assert base.kind == NodeKind.INTERFACE  # 继承 ABC

    login = indexed_graph.nodes.get("auth/login.py::LoginService")
    assert login is not None
    assert login.kind == NodeKind.CLASS


def test_function_and_method_nodes(indexed_graph):
    """模块级函数与类方法都应有节点, 且限定名正确。"""
    assert "auth/login.py::audit" in indexed_graph.nodes
    assert "auth/login.py::LoginService.authenticate" in indexed_graph.nodes
    assert "auth/login.py::LoginService.verify" in indexed_graph.nodes


def test_module_level_constant(indexed_graph):
    """大写模块级赋值识别为 variable 节点。"""
    n = indexed_graph.nodes.get("auth/base.py::MAX_RETRY")
    assert n is not None
    assert n.kind == NodeKind.VARIABLE


def test_signature_and_docstring(indexed_graph):
    """函数签名与首行 docstring 被提取。"""
    audit = indexed_graph.nodes["auth/login.py::audit"]
    assert "def audit" in audit.signature
    assert "审计" in audit.docstring


def test_contains_edges(indexed_graph):
    """module contains class, class contains method。"""
    out = indexed_graph.out_edges("auth/login.py", EdgeKind.CONTAINS)
    contained = {e.dst for e in out}
    assert "auth/login.py::LoginService" in contained
    assert "auth/login.py::audit" in contained

    out2 = indexed_graph.out_edges("auth/login.py::LoginService", EdgeKind.CONTAINS)
    methods = {e.dst for e in out2}
    assert "auth/login.py::LoginService.authenticate" in methods


def test_import_edges(indexed_graph):
    """from-import 产出 external 节点 + imports 边。"""
    imports = indexed_graph.out_edges("auth/login.py", EdgeKind.IMPORTS)
    assert len(imports) >= 1
    # 至少有一条指向 external
    assert any(e.dst.startswith("external::") for e in imports)


def test_high_confidence_on_static_facts(indexed_graph):
    """contains/imports 是 AST 铁证, confidence == 1.0。"""
    for e in indexed_graph.edges:
        if e.kind in (EdgeKind.CONTAINS, EdgeKind.IMPORTS):
            assert e.confidence == 1.0


def test_doc_node(indexed_graph):
    """README.md 产出 doc 节点。"""
    n = indexed_graph.nodes.get("README.md")
    assert n is not None
    assert n.kind == NodeKind.DOC


def test_asset_node(indexed_graph):
    """图片产出 asset 节点。"""
    assets = indexed_graph.nodes_by_kind(NodeKind.ASSET)
    assert any(a.path.endswith("diagram.png") for a in assets)


def test_syntax_error_does_not_crash(tmp_path):
    """语法错误文件不应让索引崩溃, 而是低置信 module 节点。"""
    from craftsman_map.parsers.python_parser import PythonParser
    p = PythonParser()
    res = p.parse("x.py", "x.py", "def broken(:\n  pass")
    assert len(res.nodes) == 1
    assert res.nodes[0].confidence < 0.5
    assert "parse_error" in res.nodes[0].meta
