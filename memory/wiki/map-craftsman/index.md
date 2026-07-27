---
title: craftsman-map：代码库理解 CLI
created: 2026-07-21
updated: 2026-07-21
---

# craftsman-map：代码库理解 CLI

> 把任意代码库编译成分层知识图谱（节点=模块/类/函数，边=继承/调用/import），通过一组确定性命令给大模型做「渐进披露式」代码导航——不用一次性吃完整个仓库，按需逐层展开，省 token。

## 是什么

一个零依赖（Python 内置 ast）为核心、tree-sitter 可插拔扩展多语言（JS/TS/Go）的 CLI 工具。定位：给大模型当「代码库遥控器」。核心价值验证点是**渐进披露省 token**——先给一张导航图，再按需钻取具体符号/影响面/调用链，而不是把整个代码库塞进上下文。

双通道：CLI（`python -m craftsman_map <命令>`）+ MCP Server（16 个工具对外）。

工作区：`projects/master/craftsman-map/`。

## 命令体系（16 个）

- **冷启动/导航**：`index`（建图）、`orient`（导航图：规模/语言/盲区/建议路线）、`overview`、`report`（客观认知原料包，只报 A 类事实不吐完成度百分比）
- **结构视图**：`map`（功能簇，Louvain 聚类）、`layers`（架构分层）、`entrypoints`（候选入口，排除测试节点）、`hotspots`
- **符号钻取**：`search`、`symbol`（正向依赖+反向依赖，每条边带 confidence 分级）、`impact`（反向影响面）、`explore`
- **理解层**：`wiki`（功能块描述，summary/full 两视图）、`describe`（生成描述原料）、`desc`/`desc-project`（回写描述）、`trace`（调用链追踪，call_tree 默认折叠省体积）

## 诚实性地基（这工具的灵魂）

贯穿全工具的铁律：**能力边界诚实标注，绝不装懂**。
- 解析盲区 `blind_spots` 显式标注（如 Py2 语法文件 `syntax_error` 老实报，不吞错）
- `trace` 的 `limitations` 点名静态分析抓不到的四类调用（动态派发/回调/多态/跨语言）+ 统计低置信边
- `orient` 检测多语言时标注「跨语言调用边连不起来」
- `report` 严格区分 A 类客观事实（报）vs B 类主观判断（完成度百分比等，绝不吐）
- `impact` count=0 时 `direction=reverse` + hint 明说「反向为 0 ≠ 改动安全」，不误导

## 当前状态（2026-07-21）

- **测试**：97~102 passed（含 test_honesty.py 诚实性专项覆盖 P0-P5）
- **真实第一视角体验分**：约 9.5/10（跨两个陌生仓库实测得出，非自测）
- **验证方式**：requests(19文件/574节点/55块) + olefile(带README) 两个陌生 Python 库跑通冷启动全链路，三个真 bug 全消
- **git 维度**：`git_history.py` 用 dulwich 库实现（环境 git 不在 PATH 时降级 `git_available:false`）

## 关键演进节点

| 阶段 | 内容 |
|---|---|
| MVP | 内置 ast 建图，跑通 index→map→symbol→search→impact→explore 全链路 |
| 诚实性 P0-P4 | 图可信度/体积治理/异步重建/orient/report 落地 |
| 诚实性 P5 | trace 静态天花板 + 跨语言断链只标注不硬啃 |
| 实测修复批次 | trace 体积折叠省 67.7%、report TODO 假阳性根治、聚类换 Louvain、entrypoints 排除测试 |
| 陌生仓库压测 | impact 反向语义、stale/needs_description 语义分离、orient what README 回退——三刀跨仓库真实验证 |

## 踩过的坑（防复发）

1. **自测陷阱**：早期所有测试在 craftsman-map 自己代码库上跑（结构干净、docstring 齐全），是最容易通过的情况。真实价值必须拿陌生、混乱、文档匮乏的仓库验证。
2. **话术膨胀**：一度误报「满分」，被主人质疑后收回。工具返回成功 ≠ 真实场景通过；单测 passed ≠ 陌生仓库实测通过。评分必须有跨仓库实测证据。
3. **stderr 污染 stdout**：曾用 `2>&1` 把 `_auto_rebuilt` 提示混入 stdout 造成「CLI 双 JSON 崩溃」假象——真实调用方读 stdout 是干净单段 JSON。
4. **半吊子修复**：stale_clusters 语义修复时只加新字段没治旧的，导致 55 块全误报 stale。改动要治根，不留双语义并存的矛盾信号。
