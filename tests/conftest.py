"""
pytest 公共夹具
==============
构造一个"合成的小代码库", 覆盖 craftsman-map 需要识别的所有结构:
  - 模块 / 类 / 接口(ABC) / 函数 / 方法 / 模块级常量
  - import / from-import
  - 继承 / 实现接口 / 调用 / 装饰器
  - markdown 文档 / 图片资产

所有测试基于这个夹具跑真实 index, 不 mock —— 验证的是"端到端真实行为"。
"""
from __future__ import annotations

import os
import textwrap

import pytest

from craftsman_map.indexer import Indexer
from craftsman_map.graph.store import CodeGraph


# ---- 合成代码库内容 ----
FILES: dict[str, str] = {
    "auth/base.py": textwrap.dedent('''
        """认证基础模块。"""
        from abc import ABC, abstractmethod

        MAX_RETRY = 3

        class AuthProvider(ABC):
            """认证提供者接口。"""
            @abstractmethod
            def authenticate(self, user, pwd):
                ...
    '''),
    "auth/login.py": textwrap.dedent('''
        """登录服务。"""
        from auth.base import AuthProvider, MAX_RETRY

        def audit(msg):
            """记录审计日志。"""
            print(msg)

        class LoginService(AuthProvider):
            """处理用户登录。"""
            def authenticate(self, user, pwd):
                audit("login attempt")
                return self.verify(user, pwd)

            def verify(self, user, pwd):
                return len(pwd) > MAX_RETRY
    '''),
    "utils/helpers.py": textwrap.dedent('''
        """工具函数。"""
        import functools

        @functools.cache
        def slugify(text):
            """转成 slug。"""
            return text.lower().replace(" ", "-")
    '''),
    "README.md": textwrap.dedent('''
        # 演示项目
        这是一个用于测试 craftsman-map 的合成代码库。
        包含认证模块 LoginService 与工具函数。
    '''),
}

# 一个假的图片资产 (1x1 PNG 的最小字节)
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f6f0000000049454e44ae426082"
)


@pytest.fixture(scope="session")
def sample_repo(tmp_path_factory) -> str:
    """在临时目录里铺开合成代码库, 返回根路径。"""
    root = tmp_path_factory.mktemp("sample_repo")
    for rel, content in FILES.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    # 图片资产
    img = os.path.join(root, "docs", "diagram.png")
    os.makedirs(os.path.dirname(img), exist_ok=True)
    with open(img, "wb") as f:
        f.write(_PNG_1x1)
    return str(root)


@pytest.fixture(scope="session")
def indexed_graph(sample_repo) -> CodeGraph:
    """对合成代码库跑真实索引, 返回内存图。"""
    idx = Indexer()
    g = idx.index(sample_repo)
    g.save(sample_repo)
    return g


@pytest.fixture
def reloaded_graph(sample_repo, indexed_graph) -> CodeGraph:
    """从磁盘重新加载图 —— 验证序列化往返一致。"""
    return CodeGraph.load(sample_repo)
