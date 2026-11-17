# 进阶

## 模型来源

设置 → 通用 → 模型来源里有两类：

- **API**：DeepSeek、Kimi、智谱 GLM、通义千问、Anthropic，或者任何兼容 Anthropic 格式的接口。填 API key 就能用，接口地址和两档模型（会上对话用的快速模型、改稿用的主力模型）选了服务商会自动填好，也可以改。API 方式下改稿和查论文不联网。
- **Claude Code 账号**：这台电脑装了 [Claude Code](https://docs.claude.com/en/docs/claude-code) 并登录了 Claude 套餐时可以选。改稿时会用 Claude Code 的工具，查资料能联网。

API key 存在本机的 `~/.zuhui/provider.json`，设置页上只显示最后四位。

## 配置文件

`%LOCALAPPDATA%\bloom-meeting\app\config.json`（更新时不会被覆盖）：

| 键 | 作用 |
|---|---|
| `meeting.me_asr` | 你的名字常被语音识别听成什么（正则列表），用来纠正逐字稿 |
| `share.base_url` | 会议中继的网址；不填就没有邀请链接 |

昵称、头像、颜色、AI 的名字在设置里改，不用改这个文件。

## 语音

默认用 Windows 自带的声音。装了 `edge-tts`（`uv sync --extra voice`）会用它。克隆音色（IndexTTS2、GPT-SoVITS）要另外装，路径写在 `~/.zuhui/paths.json`。

## 数据存在哪

| 位置 | 内容 |
|---|---|
| `~/.zuhui/` | 会议、逐字稿、个人资料、模型来源 |
| `~/Blurt/` | 会议录屏 |
| `%LOCALAPPDATA%\bloom-meeting\app\` | 后端程序 |

## 目录

| 路径 | 内容 |
|---|---|
| `zuhui/` | 会议后端（`python -m zuhui serve`，127.0.0.1:47330） |
| `prompts/` | 提示词 |
| `desktop/` | 桌面端（Tauri + React） |
| `relay/` | 会议中继（可选） |
| `vendor/html-diff/` | 修订对比页的生成器 |
| `install.ps1` | 一行安装脚本 |

## 从源码构建

```powershell
uv sync
cd desktop\web; pnpm install; pnpm build
cd ..\apps\desktop; npx @tauri-apps/cli@2 build --bundles nsis
```
