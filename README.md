# craftsman-map

> 把任意代码库编译成分层知识图谱，通过 MCP 协议让 AI 编程工具在改代码前真正读懂项目——而不是凭印象乱改。

[![PyPI](https://img.shields.io/pypi/v/craftsman-map)](https://pypi.org/project/craftsman-map/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![Sponsor](https://img.shields.io/badge/Sponsor-爱发电-946ce6?logo=ko-fi&logoColor=white)](https://afdian.com/a/EthanXue)

**五层架构，对应五件事：**
- **INGEST（摄取）**：多语言解析 + 文档资产导入 → 统一图谱
- **MAP（地图）**：功能块聚类 + 渐进披露 → 省 token
- **NAVIGATE（导航）**：影响面分析 + git 历史 → 双轨证据
- **UNDERSTAND（理解）**：把代码翻译成人话 → wiki 描述 + 调用方 LLM 增强
- **TRACE（工作链）**：入口到出口调用链路追踪 → 大模型定位 bug 的核心能力

---

## 安装

```bash
pip install craftsman-map
```

依赖说明：
- **零强制依赖**：Python 解析用内置 `ast`，图谱用纯 Python，开箱即用
- **可选增强**：`pip install tree-sitter tree-sitter-javascript tree-sitter-typescript tree-sitter-go` → 解锁 JS/TS/Go 多语言
- **git 历史**：`pip install dulwich` → 无需安装 git 二进制，纯 Python 读 git 历史

---

## 快速上手

```bash
# 1. 在你的项目根目录建立索引（改代码前必须先跑这个）
cd /path/to/your-project
craftsman-map index

# 2. 看功能块地图（渐进披露第一层）
craftsman-map map

# 3. 找符号
craftsman-map search "UserService"

# 4. 看符号详情 + 邻居
craftsman-map symbol "src/auth/service.py::UserService"

# 5. 分析影响面（改这里会波及哪里）
craftsman-map impact "src/auth/service.py::UserService.login"

# 6. 钻取展开（渐进披露第二层）
craftsman-map explore "src/auth/service.py::UserService" --depth 2

# 7. 热点分析（哪些文件改动最频繁）
craftsman-map hotspots

# 8. 统计概览
craftsman-map overview

# 9. 分层架构视图（自动标出入口/核心/工具层）
craftsman-map layers

# 10. 自动识别真实入口
craftsman-map entrypoints

# 11. 工作链：追踪从入口到出口的调用路径
craftsman-map trace "src/main.py::main"

# 12. 生成 wiki 描述（规则版，零成本）
craftsman-map wiki

# 13. 拿某个功能块的描述原料包（给调用方 LLM 生成描述用）
craftsman-map describe --cluster 0

# 14. 把 LLM 生成的描述写回缓存
craftsman-map desc --cluster 0 --text "这个模块负责..."
```

---

## 命令全集

| 命令 | 作用 | 典型用途 |
|------|------|---------|
| `index [PATH]` | 建立/刷新索引 | 首次使用或代码变更后 |
| `overview` | 节点/边/语言统计 | 了解项目规模 |
| `map` | 功能块总览 | 渐进披露第一层，只看摘要 |
| `search QUERY` | 符号搜索 | 找函数/类/变量 |
| `symbol ID` | 符号详情 + 邻居 | 精确查看某个符号 |
| `impact ID` | 影响面分析 | 改动前评估波及范围 |
| `explore ID` | 渐进钻取 | 从某节点展开邻居 |
| `hotspots` | 变更热点 + 共变关系 | 找高风险区域 |
| `layers` | 分层架构视图 | 自动标出入口/核心/工具/配置层 |
| `entrypoints` | 识别真实入口 | 找出项目真正的调用起点 |
| `trace ID` | 工作链追踪 | 从入口到出口的完整调用路径 |
| `wiki` | 生成 wiki 描述 | 把代码翻译成人话（规则版，零成本） |
| `describe` | 输出描述原料包 | 给调用方 LLM 生成高质量描述用 |
| `desc` | 回写 LLM 描述 | 把调用方生成的描述缓存进图谱 |

所有命令默认输出 **JSON**（适合大模型解析），加 `--pretty` 格式化输出。

---

## 为什么给大模型用？

**LLM 的真痛点不是"看不懂代码"，是"找不准 + 看太多"。**

`craftsman-map` 的设计原则：

1. **渐进披露省 token**：`map` 先给功能块摘要（10 行），锁定目标后再 `explore` 钻进去——不一次吞下 5000 个函数。

2. **置信度体系**：每条边都带 `confidence` 字段（静态铁证 1.0 / 歧义 0.6 / 悬空 0.4），让大模型知道哪些是确定事实、哪些是推断。

3. **`next_actions` 引导**：每个输出都包含"下一步可以调哪些命令"，消除大模型的猜测。

4. **双轨影响面**：静态调用图 + git 共变历史。纯静态图会漏掉"没有直接调用关系但经常一起改"的隐式耦合。

---

## MCP 接入（让大模型自主调用）

`craftsman-map` 内置 MCP server，标准 stdio JSON-RPC 协议：

```bash
craftsman-map serve-mcp
```

在 **AWS Code / Cursor / Cline / Windsurf / Continue** 配置：

```json
{
  "mcpServers": {
    "craftsman-map": {
      "command": "craftsman-map",
      "args": ["serve-mcp"]
    }
  }
}
```

`clients/` 目录里有各平台配置文件，复制粘贴即用。MCP 提供 16 个工具，覆盖全部 CLI 命令，AI 可按需自主调用。

---

## 支持语言

| 语言 | 支持状态 | 后端 |
|------|---------|------|
| Python | ✅ 内置，零依赖 | 内置 `ast` |
| JavaScript | ✅ 可选 | tree-sitter |
| TypeScript | ✅ 可选 | tree-sitter |
| Go | ✅ 可选 | tree-sitter |
| Markdown / txt | ✅ 内置 | 文本解析 |
| 图片 / 二进制资产 | ✅ 内置（记录路径，不读内容） | AssetParser |

---

## 项目结构

```
craftsman-map/
├── craftsman_map/
│   ├── cli.py              # CLI 入口
│   ├── mcp_server.py       # MCP stdio server（16 个工具）
│   ├── indexer.py          # INGEST 层：扫描 + 解析 + 建图
│   ├── git_history.py      # git 历史维度（dulwich 纯 Python）
│   ├── graph/
│   │   ├── model.py        # Node / Edge 数据模型（含 confidence）
│   │   ├── store.py        # CodeGraph 存储 + 聚类 + 序列化
│   │   └── linker.py       # 引用消解
│   ├── parsers/
│   │   ├── base.py         # 解析器抽象接口
│   │   ├── python_parser.py
│   │   ├── ts_parser.py    # JS/TS/Go（tree-sitter）
│   │   └── doc_parser.py   # Markdown / 资产
│   ├── understand/
│   │   ├── wiki.py         # 理解层：规则描述 + 原料包 + 注入回写
│   │   ├── view.py         # 分层视图
│   │   └── trace.py        # 工作链追踪
│   └── commands/
│       ├── core.py
│       └── understand.py
├── tests/                  # 68 条测试，全部 passed
├── clients/                # 各平台 MCP 配置文件
└── pyproject.toml
```

---

## 开发 & 测试

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

**68 passed，0 skipped，0 failed。**

---

## 💝 赞助 / Sponsor

如果 craftsman-map 对你有帮助，欢迎赞助支持持续开发与维护。

If craftsman-map helps you, consider sponsoring to support ongoing development.

[![赞助作者 / Sponsor](https://img.shields.io/badge/Sponsor-爱发电-946ce6?logo=ko-fi&logoColor=white)](https://afdian.com/a/EthanXue)

---

## 设计哲学

`craftsman-map` 的核心信念：**LLM 理解代码库，靠的不是"看全部"，而是"看对的部分"。**

把代码库编译成结构化知识图谱，通过确定性 CLI 精准披露——五层架构对应五件事：摄取、分层、导航、理解、查询。每一层都可独立使用，也可以组合成完整的代码理解流水线。

欢迎提 issue 和 PR。

---

## 联系 / Contact

有任何问题、建议或合作意向，欢迎随时发邮件联系。

Feel free to reach out by email for any questions, suggestions, or collaboration.

- 📧 Email: [545118959@qq.com](mailto:545118959@qq.com)
- 💬 Issues: [github.com/EthanXue666/craftsman-map/issues](https://github.com/EthanXue666/craftsman-map/issues)

---

## License

MIT — 个人和商业项目均可免费使用。
