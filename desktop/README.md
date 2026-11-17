# Bloom（组会）

Bloom 是一个仿腾讯会议的 Windows 桌面应用：开会时只录自己的窗口、边录边实时转写，会上 Claude 帮你改论文，
会后自动生成修订对比和纪要。会议后端是同目录下的 Python 包 `zuhui`（`python -m zuhui serve`，127.0.0.1:47330），
这个仓库是它的桌面壳和录制部分。

Bloom is derived from [Blurt](https://github.com/AGIHunt/blurt) (MIT License); see [LICENSE](LICENSE).

## 目录

| 路径 | 内容 |
|---|---|
| `apps/desktop/src-tauri` | Tauri 2 应用（Rust，产物仍叫 `blurt.exe`）：会议录制（只录主窗口）、灵动岛、托盘、单实例与 `blurt://meeting` / `blurt://stop` 深链、语音模型下载、存储、开机启动、启动会议后端 |
| `crates/blurt-capture` | 驱动 `record.py` 的 ffmpeg 录制引擎（`--status-jsonl`），以及灵动岛的透明浮层窗口 |
| `web/apps/desktop` | 主窗口（React）：会议 / 回顾 / 设置 |
| `web/apps/overlays` | 灵动岛页面（开会 · 复制链接、会议药丸、会后进度条） |
| `web/packages` | 共用的设计 token（`ui`）、图标（`icons`）、多语言（`i18n`） |
| `skills/blurt/scripts` | 录制与转写脚本：`record.py`（录屏、回声消除、麦克风选择）、`live.py`（实时转写）、`transcribe.py`（语音模型目录、下载、识别） |
| `tests` | 上面这些脚本的 Python 测试 |
| `assets` | 图标、配色、提示音的源文件 |

运行时数据沿用 Blurt 的位置：`~/.blurt`（配置、模型、任务、`run/control.json`）和 `~/Blurt/inbox/sessions`（每场会议的录屏目录）。

## 构建

```powershell
cd web
pnpm install
pnpm --filter @blurt/overlays build
pnpm --filter @blurt/desktop build
cd ..\apps\desktop\src-tauri
cargo build --profile quick --features tauri/custom-protocol
```

测试：`cargo test --profile quick --lib`（Rust）、`pnpm -r test`（web）、`uv run --with pytest --with numpy python -m pytest tests`（脚本）。
