# bloom · 品牌

**名字**：bloom = blue + room，也是"绽放"。中文副题仍叫「组会」。

**配色**
| 用途 | 色值 |
|---|---|
| 深蓝（主色文字、按钮） | #1D4ED8 |
| 主蓝（强调、图标） | #3B82F6 |
| 浅蓝（次级、悬停） | #60A5FA |
| 花瓣（点缀） | #BFDBFE |
| 墨色（深色底） | #0F172A |
| 纸白（浅色文字） | #EFF6FF |

修订对比页的删红增绿不动。

**Logo 定稿：方案一，只留花（五瓣 + 深蓝花心 + 纸白花蕊，不带气泡尾巴）。** 资产：`bloom-mark.svg`、`bloom-wordmark.svg`、`png/`（16-1024）、`tauri-icons/`（已拷进 fork 的 `apps/desktop/src-tauri/icons/`，原图标备份在 `blurt-original-icons/`）。重新生成：`python brand/make_logo.py`。

候选记录：
1. 花瓣气泡：五瓣围一圈（一屋子人）+ 气泡尾巴（在说话）
2. 绽放的房间：圆角方块（room）一角开花，里面三个人
3. oo 花苞：两个 o 叠成花苞带花茎，直接做字标 `bl●●m`

**改名清单**（等 subagent 跑完再动，避免改到它们正在用的路径）
- 文件夹 `claude-zuhui` → `bloom`
- Python 包 `zuhui` → `bloom`，命令 `python -m bloom`
- 状态目录 `~/.zuhui` → `~/.bloom`，环境变量 `ZUHUI_*` → `BLOOM_*`
- 口喷鸡 fork：窗口标题 / productName → Bloom，图标换成定稿 logo；内部的 `blurt://`、`~/.blurt` 不动（脚本和 MCP 依赖它们）
