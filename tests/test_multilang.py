"""
多语言解析测试 (JS / TS / Go, 基于 tree-sitter)。
未安装 tree-sitter 语言包时自动 skip, 不让测试套件失败。
"""
from __future__ import annotations

import os
import textwrap

import pytest

from craftsman_map.parsers.ts_parser import TreeSitterParser
from craftsman_map.indexer import Indexer
from craftsman_map.graph.model import NodeKind, EdgeKind


ts_available = pytest.mark.skipif(
    not TreeSitterParser.available(),
    reason="tree-sitter 语言包未安装")


@ts_available
def test_js_function_and_call():
    src = textwrap.dedent('''
        import { helper } from "./utils";

        function greet(name) {
            return helper(name);
        }

        const shout = (msg) => {
            greet(msg);
        };
    ''')
    p = TreeSitterParser()
    res = p.parse("a.js", "a.js", src)
    ids = {n.id for n in res.nodes}
    assert "a.js::greet" in ids
    assert "a.js::shout" in ids  # arrow func
    # import 边
    assert any(e.kind == EdgeKind.IMPORTS for e in res.edges)
    # 调用边 (greet 调 helper, shout 调 greet)
    calls = [e for e in res.edges if e.kind == EdgeKind.CALLS]
    assert calls


@ts_available
def test_js_class_inherits():
    src = textwrap.dedent('''
        class Animal {
            speak() {}
        }
        class Dog extends Animal {
            bark() { this.speak(); }
        }
    ''')
    p = TreeSitterParser()
    res = p.parse("z.js", "z.js", src)
    ids = {n.id for n in res.nodes}
    assert "z.js::Animal" in ids
    assert "z.js::Dog" in ids
    # 方法作为类的子节点
    assert "z.js::Dog.bark" in ids
    # 继承边
    inh = [e for e in res.edges if e.kind == EdgeKind.INHERITS]
    assert any("Animal" in e.meta.get("unresolved", "") for e in inh)


@ts_available
def test_ts_interface():
    src = textwrap.dedent('''
        interface Repository {
            find(id: string): void;
        }
        class UserRepo implements Repository {
            find(id: string) {}
        }
    ''')
    p = TreeSitterParser()
    res = p.parse("r.ts", "r.ts", src)
    ifaces = [n for n in res.nodes if n.kind == NodeKind.INTERFACE]
    assert any(n.name == "Repository" for n in ifaces)
    classes = [n for n in res.nodes if n.kind == NodeKind.CLASS]
    assert any(n.name == "UserRepo" for n in classes)


@ts_available
def test_go_func_and_struct():
    src = textwrap.dedent('''
        package main

        import "fmt"

        type Server struct {
            port int
        }

        func NewServer() *Server {
            return &Server{}
        }

        func main() {
            NewServer()
            fmt.Println("hi")
        }
    ''')
    p = TreeSitterParser()
    res = p.parse("main.go", "main.go", src)
    ids = {n.id for n in res.nodes}
    assert "main.go::Server" in ids       # struct → class
    assert "main.go::NewServer" in ids
    assert "main.go::main" in ids
    # import fmt
    assert any(e.kind == EdgeKind.IMPORTS and "fmt" in e.dst for e in res.edges)
    # main 调用 NewServer
    calls = [e for e in res.edges if e.kind == EdgeKind.CALLS]
    assert any("NewServer" in e.meta.get("unresolved", "") for e in calls)


@ts_available
def test_mixed_repo_end_to_end(tmp_path):
    """混合 Python + JS + Go 的仓库, 端到端索引应把三种语言都纳入图。"""
    files = {
        "svc.py": "class Service:\n    def run(self):\n        pass\n",
        "app.js": "function main() { render(); }\nfunction render() {}\n",
        "srv.go": 'package main\nfunc handle() {}\nfunc main() { handle() }\n',
    }
    for rel, content in files.items():
        with open(os.path.join(tmp_path, rel), "w", encoding="utf-8") as f:
            f.write(content)
    g = Indexer().index(str(tmp_path))
    paths = {n.path for n in g.nodes.values()}
    assert "svc.py" in paths
    assert "app.js" in paths
    assert "srv.go" in paths
    # 每种语言至少有一个函数节点
    langs_with_func = {n.path.rsplit(".", 1)[-1]
                       for n in g.nodes.values()
                       if n.kind == NodeKind.FUNCTION}
    assert {"py", "js", "go"}.issubset(langs_with_func)


@ts_available
def test_js_cross_file_call_resolves(tmp_path):
    """跨文件调用经 linker 消解到真实定义。"""
    with open(os.path.join(tmp_path, "a.js"), "w", encoding="utf-8") as f:
        f.write("function caller() { target(); }\n")
    with open(os.path.join(tmp_path, "b.js"), "w", encoding="utf-8") as f:
        f.write("function target() {}\n")
    g = Indexer().index(str(tmp_path))
    edges = g.out_edges("a.js::caller", EdgeKind.CALLS)
    targets = {e.dst for e in edges}
    assert "b.js::target" in targets
