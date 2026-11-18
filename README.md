https://github.com/user-attachments/assets/815369c9-5372-4953-84ec-d6c1b68a329c

# Bloom

<p align="center"><img src="docs/bloom-demo.gif" width="720" alt="会上说定一处改法，论文里马上出现修订"></p>

开组会用的 AI 助手。会上它听大家讨论，被叫到时用语音回答；主持人说定一处改法，它就去改论文，改动用修订标记显示在稿子上。散会后可以看修订对比、逐字稿和纪要。

## 安装

Windows 10/11（64 位）。在 PowerShell 里运行：

```powershell
irm https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/install.ps1 | iex
```

开会要用的东西（语音识别模型、录屏用的 ffmpeg 等，约 1 GB）都在这一步装好，第一次要等一会儿。装好后从开始菜单打开 Bloom。以后更新，再运行一次这条命令。

## 第一次打开

到 **设置 → 通用 → 模型来源**，选一家服务商（DeepSeek、Kimi、智谱 GLM、通义千问、Anthropic），粘贴 API key，点保存。费用按用量从你在那家的账户扣，每场会用了多少在回顾的纪要最后能看到，本月累计在设置里。也可以填任何 OpenAI 或 Anthropic 兼容的接口，或者用本机的 Ollama、LM Studio（不要 key）。

## 开会

1. 把论文拖进窗口。支持 PDF、Word、Markdown、LaTeX。
2. 点 **开会**。
3. 讨论时叫 AI 的名字提问。说定一处改法，它会记下来去改。
4. 点 **散会**。以往的会议在 **回顾** 里。

开 Bloom 的人是主持人，可以把参会的人设为共同主持人。只有主持人和共同主持人说的改法会被执行。

昵称、头像、颜色和 AI 的名字在 **设置 → 通用 → 个人资料** 里改。

## 邀请别人进会

会议页上点「邀请」复制链接发给别人，对方用浏览器打开就能进会，不用装东西。默认走公共中继，什么都不用配。

想用自己的服务器：在一台 Linux 服务器（Ubuntu / Debian）上运行

```bash
curl -fsSL https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/relay/setup.sh | sudo bash
```

最后会打印一行连接码，在 设置 → 通用 → 邀请 里选「自己的服务器」，粘进去。详细见 [relay/README.md](relay/README.md)。

## 在 AI 助手里用

`skills/bloom` 是一个技能（SKILL.md 格式），能让 Claude Code、Codex、Gemini CLI、Cursor、GitHub Copilot、OpenCode 这些 AI 助手读 Bloom 的会议。在 PowerShell 里运行：

```powershell
irm https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/skills/install.ps1 | iex
```

它装到 `~/.agents/skills/bloom`（Codex、Gemini CLI、Cursor、Copilot、OpenCode 读这里），电脑上有 Claude Code 的话再装一份到 `~/.claude/skills/bloom`。以后更新再运行一次。

装好后对 AI 助手说：

- "接上组会"（Claude Code 里也可以输入 `/bloom`）：接上正在开的会，会上新说的话一批批传过来，有人提问时它帮你想答法。
- "上次组会老师对第三章怎么说的""上周让我改的有哪些"：它去翻往期会议，回答带上是哪一场、几分几秒。

不认识技能的工具，让它先读一遍 `skills/bloom/SKILL.md` 再照着做。它只读会议，不改论文，也不往会里发东西。往期会议直接读本机 `~/.zuhui/meetings` 下的文件，Bloom 不用开着。

## 隐私

录屏、逐字稿和论文都存在你自己的电脑上，API key 也只存在本机。

开会时，说的话和论文内容会发给你选的模型服务商处理，用 Ollama、LM Studio 这类本机模型时不出这台电脑。查文献时检索词会发给 OpenAlex、arXiv、Crossref，不想这样，在 设置 → 通用 → 模型来源 里关掉「查文献」。AI 的声音默认用微软在线语音，它要念的句子会发给微软合成；不想这样，在 设置 → 语音 里换成 Windows 自带的语音，或者选不出声。

邀请默认走公共中继（Bloom 作者维护的一台服务器）。只有打开了邀请（复制邀请链接，或打开「允许别人加入」）才会连它，这时论文快照、字幕和声音经它转给进会的人，只放在内存里，散会就删。不想经过公共中继，在 设置 → 通用 → 邀请 里换成自己的服务器或者关闭。

更多设置和从源码构建见 [docs/advanced.md](docs/advanced.md)。

## 许可

MIT。桌面端参考了 Blurt 的设计，其许可见 [desktop/LICENSE](desktop/LICENSE)。MathJax 为 Apache-2.0。

---

Bloom is an AI assistant for research group meetings. It listens, answers when called by name, and when the host settles a change it revises the paper, with the changes shown as tracked edits. Install with the command above, then add an API key under Settings → General → Model source.
