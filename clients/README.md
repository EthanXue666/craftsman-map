# craftsman-map MCP 客户端配置

各平台的 MCP 配置文件，复制到对应位置即可。

| 文件 | 适用平台 | 配置路径 |
|------|---------|---------|
| `cursor.json` | Cursor | `.cursor/mcp.json`（项目根目录）或全局 `~/.cursor/mcp.json` |
| `cline.json` | Cline (VS Code) | VS Code 设置 → Cline MCP Settings |
| `windsurf.json` | Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| `continue.json` | Continue | `~/.continue/config.json` 的 `mcpServers` 字段 |

## 使用前提

先安装 craftsman-map：

```bash
pip install craftsman-map
```

然后在你的项目根目录建立索引：

```bash
cd /path/to/your-project
craftsman-map index
```

之后 AI 即可通过 MCP 自主调用 16 个工具分析你的代码库。

## AWS Code

在项目根目录创建 `.kiro/settings/mcp.json`：

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
