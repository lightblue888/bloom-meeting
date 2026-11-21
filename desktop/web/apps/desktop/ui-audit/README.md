# ui-audit — 主窗口和访客页的界面自动检查

机器替人把每个界面、每个状态挨个打开，在 4 个窗口尺寸 × 深色/浅色 × 中文/英文 下检查排版问题，截图留档，最后出一份按严重程度排好的报告。后端全部是假的（mock），**不会碰正在跑的 Bloom（127.0.0.1:47330）**，也不用启动 Tauri。

## 跑一次

```powershell
cd blurt\web\apps\desktop
npm run test:ui
```

- 全量约 5–6 分钟（4 个并行 worker，本机 Edge）；进度会写进 `/progress` 窗口（任务名 `ui_audit_run`）。
- 结果：
  - `ui-audit/out/report.md` — 自动生成的问题清单（按 blocker / major / minor / info 排序，同一问题跨屏幕/尺寸/主题/语言合并成一条，带截图路径和“最可能的源码位置”）。
  - `ui-audit/out/shots/<屏幕>/<语言>-<主题>-<尺寸>.png` — 每个状态的截图；状态切换前后的截图是 `<语言>-shift-<尺寸>-NN-<切换名>.png`。
  - `ui-audit/out/findings.json` — 同一份结果的机器可读版。
  - `out/` 每次运行都会覆盖；要留档的复核结论另存（例如 `ui-audit/REPORT.md`）。
- 只重新生成报告（不开浏览器）：`npm run test:ui:report`。

常用开关（PowerShell 写法 `$env:X='...'; npm run test:ui`）：

| 变量 | 作用 |
|---|---|
| `UI_AUDIT_SCREENS=lobby,meeting-live` | 只跑这几个屏幕（按名字前缀匹配） |
| `UI_AUDIT_LANGS=zh` | 只跑一种语言（默认 `zh,en`） |
| `UI_AUDIT_WORKERS=2` | 并行数（默认 4） |
| `UI_AUDIT_BROWSER=chrome` | 用 Chrome 代替 Edge |
| `UI_AUDIT_HEADED=1` | 打开浏览器窗口看它跑 |
| `UI_AUDIT_STRICT=1` | 有 blocker（页面横向溢出、控件跑出窗口、页面报错）就让测试失败，适合发布前卡关 |

也可以用 Playwright 自己的筛选：`npm run test:ui -- -g "lobby"`。

## 查什么

每个屏幕 × 每个尺寸（1024×640、1280×800、1600×900、1920×1080）× 每个主题：

| 检查 | 说明 |
|---|---|
| 页面横向溢出 | 页面比窗口宽（出现横向滚动条或内容被切） |
| 面板横向滚动 | 列表/面板因为一长串没断行的文字（URL、型号名）出现横向滚动 |
| 文字被截断 | 文字被 `overflow:hidden` 的框切掉而没有 “…”；多行文字有整行被藏 |
| “…” 截断 | 用户输入的名字/标题/型号名用 “…” 收起是**预期行为**（只记 info）；应用自己的文字被 “…” 收起才算问题 |
| 文字溢出 | 文字跑出它所在的芯片/按钮/卡片背景 |
| 文字重叠 | 两段文字压在一起（同一层；弹出层盖住页面不算） |
| 控件被盖住 / 被切 / 跑出窗口 / 互相重叠 | 用 `elementFromPoint` 在控件上取 5 个点，看点击到底会落到谁身上 |
| 输入框太窄 | 占位符放不下；按 maxlength 填满（24 字名字等）后看不全 |
| 布局跳动 | 每个状态切换（静音、AI 状态变化、来了新句子、来了新人、出现提示、散会…）前后量一遍关键元素的位置，报告“不该动却动了”的；同时记录浏览器的 layout-shift 分数 |
| 无障碍（axe-core） | WCAG 2.2 AA 规则，深浅两套主题都查对比度；对比度 < 3:1 记 major，其余记 minor |
| 控制台 | 页面未捕获异常、console.error/warn、失败的请求、4xx/5xx 应答 |

压力数据（`stress: true` 的屏幕）：24 字的主持人名（中文最宽）、24 字 AI 名、32 字访客名、60 字会议名、很长的自定义模型名、14 个参会人、一句 1500 字带长 URL 的发言、40 场历史会议；还有各种空状态。

## 加一个屏幕

所有屏幕/状态都在 **`screens.ts` 的 `SCREENS` 列表**里。加一项就行：

```ts
{
  name: 'lobby-importing',                          // 文件名、报告里用
  what: '大厅：论文正在导入',                        // 报告里的一句话说明
  page: 'main',                                      // 'main' 主窗口 | 'guest' 访客页
  stress: false,                                     // true = 用最长名字/很多人的数据
  world: (w) => { w.current.paper = null; },         // 改假后端的数据（fixtures/world.ts）
  go: async (c) => {                                 // 从刚打开的窗口怎么点到这个状态
    await c.page.getByTestId('lobby-pick-paper').click();
  },
  ready: '[data-testid="lobby"]',                    // 到了以后一定可见的元素
  scope: undefined,                                  // 模态框：只查框里（例：'[data-testid="onboarding"]'）
  transitions: [                                     // 可选：要量“布局跳动”的状态切换
    {
      name: 'import finishes',
      act: async (c) => { c.world.current.paper = { ...}; await c.waitPoll(); },
      expect: ['[data-testid="lobby-paper"]'],       // 这些元素本来就该变，不算跳动
    },
  ],
},
```

`Ctx`（`c`）里能用的：`page`（Playwright）、`world`（假后端的数据，改了以后 `await c.waitPoll()` 等应用下一次轮询）、`push('mic' | 'brain', 数据)`（往 SSE 推一条）、`bumpRelay()`（访客页）、`settle()`、`T(中文, English)`。

假后端的数据从 `fixtures/world.ts` 的 `baseWorld(lang, stress)` 来：一场进行中的会（3 人、逐字稿、任务板）、6 场历史会议、DeepSeek 模型来源、已下载的转写模型。现成的变体：`idle`（会前）、`fresh`（新装、跳过了引导）、`firstRun`（新装、引导还没走）、`ended`、`processing`。

应用加了新的后端接口时，报告的 info 里会出现 “Endpoint not mocked: GET /api/xxx”——在 `lib/mock-backend.ts` 的 `zuhui()` 里加一个 `case`，数据放进 `World`。

## 怎么做到不碰真后端

- 页面用 `?zuhui=<审计自己的地址>/__zuhui` 打开（`src/meetingApi.ts` 本来就支持这个参数），所有 `/api` 请求都发到审计自己的端口，由 Playwright 的 `context.route` 当场应答（`lib/mock-backend.ts`），一个字节都不出浏览器。
- 万一有请求直奔 127.0.0.1:47330 或别的主机，统一拦下并在报告里记成 blocker（“Request to the live backend”）。
- SSE（`/api/mic/stream`、`/api/brain/stream`）：首条消息由 route 应答，之后由测试用 `push()` 推；`lib/inpage.js` 把 `EventSource` 换成一个可控的版本（真正的 SSE 应答没法在 Playwright 里保持打开再往里推）。
- Tauri：浏览器里 `src/api.ts` 本来就会改用 `src/mock.ts`；审计的 Vite 配置（`vite.audit.config.ts`）把这个 `./mock` 换成 `fixtures/tauri-mock.ts`，好让测试决定 Rust 那边说什么（比如转写模型下没下）。`src/` 一行没改。
- 访客页的 LiveKit 换成 `lib/fake-livekit.js`（“一连就通”），中继接口 `/view/<room>/…`、`/token` 也由 route 应答。
- 时间固定在 2026-11-18 10:30（北京时间），时钟、“N 秒前”在每张截图里都一样。

## 文件

```
ui-audit/
  playwright.config.ts   入口：Edge、4 个 worker、自带一个 Vite 开发服务器（端口 41497，不用 1420）
  vite.audit.config.ts   审计专用的 Vite：独立缓存、不监视文件、换掉 src/mock.ts
  audit.spec.ts          每个屏幕 × 每种语言一个测试
  screens.ts             ★ 屏幕/状态清单（加屏幕只改这里）
  fixtures/world.ts      假后端的数据（含压力数据）
  fixtures/tauri-mock.ts Tauri 命令的替身
  fixtures/paper.html    论文 iframe 的替身（只会说 zhState 协议）
  fixtures/rec.webm      3 秒的假录屏
  lib/inpage.js          页面里跑的检查（溢出、截断、重叠、遮挡、跳动、可控 SSE）
  lib/audit.ts           一个屏幕的完整流程（尺寸 × 主题 × 截图 × axe × 状态切换）
  lib/mock-backend.ts    所有网络请求的应答
  lib/report.ts          汇总、合并、定位源码，写 out/report.md
  lib/progress-reporter.ts  进度写进 ~/.claude/progress/ui_audit_run.json
  lib/global-setup.ts    清理上次结果、预热 Vite
```

## 已知限制

- 论文 iframe 里一般是替身页面，真实的修订对比页（`zuhui/view.py`）由后端自己的浏览器测试覆盖（`tests/test_diff_page_browser.py`、`tests/test_paper_reading_browser.py`）。
  例外是 `paper-live-*` 几个屏幕（`paper-live.ts`：选中后的「引用」、写批注的框、改动落地后停在哪）：开跑前 `fixtures/paper_live.py` 用 `zuhui/view.py` 把一篇编出来的论文做成真页面（要 Python），放进 `out/paper-live/`。审计自己的检查不看 iframe 里面，这几个屏幕在自己的步骤里量，出问题记成 audit-step-failed。
- 跑的是 Vite 开发模式（React 开发版）：会多报一些 React 的开发期警告，生产包里不会有。
- 字体用的是本机的（微软雅黑 / Segoe UI），和用户的 Windows 一致；Mac 上的排版没覆盖。
- 截图对比（baseline diff）没开：界面每天在改，基线会天天失效。界面定稿后可以在 `lib/audit.ts` 里给截图加 `expect(page).toHaveScreenshot()`。
