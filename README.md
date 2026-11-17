# Bloom

**组会上说定的改法，直接改进论文。** Bloom 是给课题组开组会用的 AI 助手：它在会上听大家讨论，被叫到时用语音回答问题；主持人或共同主持人拍板一处改法，它就排队把论文改好，修订标记当场长在稿子上，改完口头汇报一句。散会后有修订对比、逐字稿和纪要。

**Spoken decisions in research meetings become live paper revisions.** Bloom listens to a group meeting, answers when it is addressed, and when the host or a co-host settles a change, it revises the paper on the spot — the tracked changes appear in the paper while the meeting goes on.

## 安装 · Install

Windows 10/11（64 位）。在 PowerShell 里运行一行：

```powershell
irm https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/install.ps1 | iex
```

它会装好 uv（Python 管理）和 ffmpeg（录会议窗口）、Bloom 的后端（`%LOCALAPPDATA%\bloom-meeting`，自带 Python 环境）和桌面端（开始菜单里的 Bloom）。再运行一次就是更新，你的 `config.json` 不会被覆盖。

Bloom 用**你自己的 Claude 账号**思考和改稿：需要装好并登录 [Claude Code](https://docs.claude.com/en/docs/claude-code)（套餐或 API key 都行）。没装的话安装脚本最后会告诉你怎么装：

```powershell
irm https://claude.ai/install.ps1 | iex
claude        # 登录一次
```

## 用法 · Usage

1. 开始菜单打开 **Bloom**，把论文拖进窗口（PDF、Word、Markdown、LaTeX）。
2. 点 **开会**：Bloom 只录它自己的窗口，边录边转写。
3. 对着论文讨论。叫 AI 的名字提问（默认按模型叫 Claude，可以在 **设置 → 通用 → 个人资料** 里改），它会用语音回答；说定一处改法（“这句就改成……，定了”），它记下并开始改，改好的地方出现删红增绿的修订标记。
4. 点 **散会**：逐字稿整理、修订对比、纪要。以往的会在 **回顾** 里。

身份：开 Bloom 的人是主持人；主持人可以把参会的人设为**共同主持人**（每场会重新设）。只有主持人和共同主持人说的改法会照办。

## 邀请别人进会（可选）

别人用浏览器打开邀请链接就能进会、听和说、看到同样的论文和逐字稿，也能加自己的论文。这需要一台有公网地址的服务器跑会议中继（LiveKit + 一个小服务），见 [relay/README.md](relay/README.md)。不配中继也能用：一个人开会照常。

## 设置

昵称、头像和 AI 的名字在 **设置 → 通用 → 个人资料** 里改。其余在 `%LOCALAPPDATA%\bloom-meeting\app\config.json`：

| 键 | 作用 |
|---|---|
| `meeting.me` | 你的名字（主持人）；设置里改过的优先 |
| `meeting.me_asr` | 你的名字常被语音识别听成什么（正则列表），用来纠正逐字稿 |
| `hands.model` / `hands.effort` | 改稿用的模型和思考强度（也能在会议页 AI 的卡片上换） |
| `share.base_url` | 会议中继的网址（不填就不开邀请链接） |

语音合成默认用 Windows 自带的声音；装了 `edge-tts`（`uv sync --extra voice`）会用它。克隆音色（IndexTTS2、GPT-SoVITS）要另装，路径写在 `~/.zuhui/paths.json`。

## 目录

| 路径 | 内容 |
|---|---|
| `zuhui/` | 会议后端（`python -m zuhui serve`，127.0.0.1:47330）：听、判断接不接话、记改稿任务、调 Claude Code 改论文、修订对比页、纪要 |
| `prompts/` | 对话模型和改稿的提示词 |
| `desktop/` | 桌面端（Tauri + React），基于 [Blurt](https://github.com/AGIHunt/blurt)（MIT）：开会录屏、实时转写、会议页、回顾、访客页 |
| `relay/` | 会议中继的小服务和说明（可选） |
| `vendor/html-diff/` | 修订对比页的生成器 |
| `install.ps1` | 一行安装脚本 |

## 从源码构建 · Build from source

```powershell
uv sync                                   # 后端
cd desktop\web; pnpm install; pnpm --filter @blurt/overlays build; pnpm --filter @blurt/desktop build
cd ..\apps\desktop; npx @tauri-apps/cli@2 build --bundles nsis
```

## 隐私

会议的录屏、逐字稿、论文都只存在你自己的电脑上（`~/.zuhui`、`~/Blurt`）。对话和改稿通过你自己的 Claude Code 调用 Anthropic 的模型。开了邀请链接时，会议内容经你自己的中继转给进会的人：论文、逐字稿只放在中继的内存里，磁盘上只记会议名、参会人和头像。

## License

MIT. `desktop/` is derived from Blurt (MIT). MathJax is Apache-2.0. See [LICENSE](LICENSE).
