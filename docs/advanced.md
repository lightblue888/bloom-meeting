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

安装时带上了 `edge-tts`（微软的在线声音），默认用它；连不上时用 Windows 自带的声音。克隆音色（IndexTTS2、GPT-SoVITS）要另外装，路径写在 `~/.zuhui/paths.json`。

## 数据存在哪

| 位置 | 内容 |
|---|---|
| `~/.zuhui/` | 会议、逐字稿、个人资料、模型来源 |
| `~/Blurt/` | 会议录屏 |
| `~/.blurt/` | 桌面端设置、语音识别和判停模型（`models/`）、没有 winget 时装的 ffmpeg（`bin/`） |
| `%LOCALAPPDATA%\bloom-meeting\app\` | 后端程序 |

## 会议存在哪

每场会一个文件夹 `~/.zuhui/meetings/<日期-时间>/`，比如 `20261114-212733`。里面都是 UTF-8 的文本文件，Claude Code 这类工具直接读这些文件就行，不用另接接口。

| 文件 | 内容 |
|---|---|
| `meta.json` | 会议名、开始和结束时间、时长、会上打开过的论文（`papers`）、录屏文件夹（`session`） |
| `participants.json` | 谁来了：名字，主持人、共同主持人还是参会者 |
| `transcript.jsonl` | 逐字稿，一行一句：`t`（会上的分:秒）、`sec`、`who`（`teacher` 主持人、`student` AI、`guest` 其他人、`system` 提示）、`name`、`text`。散会后重新转写过的话，这里是更准的那一版 |
| `transcript.live.jsonl` | 开会时屏幕上显示的那一版 |
| `transcript.clean.jsonl` | 整理后的版本，去掉了说错改口、重复，`i` 是对应原始版的第几行 |
| `cleanup.json` | 整理时删改了哪些，每一条都能在回顾里恢复 |
| `transcript.edits.json` | 在回顾里手改逐字稿的记录：改字、整场替换、改说话人 |
| `retranscribe.json` | 散会后重新转写的统计 |
| `summary.json` / `summary.md` | 纪要，一份结构化的，一份 Markdown |
| `board.json` | 修改清单：每条的标题、当时说的话（`remark`）、改了什么（`result`）、状态 |
| `diff.html` | 散会时的修订对比，一个文件，浏览器直接打开 |
| `bookmarks.json` | 会上标记的地方 |
| `agenda.json` | 议程，每一项讲了多久 |
| `carry_in.json` / `carry.json` | 开会时带进来的上次遗留 / 这场留下的事勾掉了哪些 |
| `usage.json` | 用 API 时这场会的用量和估算费用 |

没用到的功能就没有对应的文件。录屏和录音不在这里，在 `meta.json` 的 `session` 指的文件夹里（默认在 `~/Blurt/inbox/sessions/` 下）。论文的历次版本在 `~/.zuhui/versions/`。自带的示例会议在 `~/.zuhui/sample-meeting/`，文件格式一样。

在 设置 → 存储 里打开"自动删除逐字稿和纪要"后，到期的会议里逐字稿、纪要、书签和遗留的勾选会移到回收站，`meta.json`、`participants.json`、`agenda.json`、`board.json`、`diff.html` 留着。

## 目录

| 路径 | 内容 |
|---|---|
| `zuhui/` | 会议后端（`python -m zuhui serve`，127.0.0.1:47330） |
| `prompts/` | 提示词 |
| `desktop/` | 桌面端（Tauri + React） |
| `relay/` | 会议中继（可选） |
| `vendor/html-diff/` | 修订对比页的生成器 |
| `samples/` | 自带的示例会议 |
| `install.ps1` | 一行安装脚本 |

## 从源码构建

```powershell
uv sync
cd desktop\web; pnpm install; pnpm build
cd ..\apps\desktop; npx @tauri-apps/cli@2 build --bundles nsis
```
