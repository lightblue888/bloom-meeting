# -*- coding: utf-8 -*-
"""改稿工人的 API 版（zuhui/hands_api.py）：模型来源选 API 时不起 Claude Code，改稿 / 标注 / 查论文 / 看屏幕对外和 Claude 那一路一样
（任务板、进度、改动记录、汇报的话）。

全离线、不花钱：本进程里起一个假的 Anthropic 兼容接口（照脚本回话、记下每次请求；照 fake_anthropic.py 改的），
ZUHUI_HOME 指到临时目录（和别的测试模块一起跑时用 zuhui 已经在用的那个），provider.json 写在那里，不碰真实的 ~/.zuhui。

    python -X utf8 -m unittest tests.test_hands_api -v
"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-hands-api-")     # 必须在 import zuhui 之前；给了就用给的（下面拦真实目录）
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import hands as handsmod  # noqa: E402
from zuhui import hands_api, netenv, provider  # noqa: E402
from zuhui.board import Board  # noqa: E402


# ---------- 假接口 ----------
class Fake:
    script: list = []          # 一次请求用掉一条：str = 回这段文字；{"status": 400, "message": …} = 回错误
    seen: list = []            # 每次请求的 body
    reject_images = False      # True：带图的请求一律 400（看不了图的型号）
    lock = threading.Lock()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with Fake.lock:
            Fake.seen.append({"path": self.path, "body": body,
                              "auth_tail": (self.headers.get("Authorization") or "")[-4:]})
            step = Fake.script.pop(0) if Fake.script else "好"
        if not self.path.startswith("/v1/messages"):
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "no"}})
        has_img = any(isinstance(c, dict) and c.get("type") == "image"
                      for m in body.get("messages") or [] if isinstance(m.get("content"), list) for c in m["content"])
        if Fake.reject_images and has_img:
            return self._json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                               "message": "image input is not supported by this model"}})
        if isinstance(step, dict):
            return self._json(step.get("status", 500), {"type": "error", "error": {"type": "api_error", "message": step.get("message", "")}})
        self._json(200, {"id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model"),
                         "content": [{"type": "text", "text": step}], "stop_reason": "end_turn", "stop_sequence": None,
                         "usage": {"input_tokens": 10, "output_tokens": 1}})

    def _json(self, status, d):
        b = json.dumps(d, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


PAPER = ("# 组会助手\n\n## 1 引言\n\n组会是课题组开展科研训练的主要形式。导师和同学提出的修改意见多为口头表达，会后依靠笔记整理，容易漏改。\n\n"
         "## 2 方法\n\n我们的方法在三个数据集上评测。另外，我们的方法在三个数据集上评测。\n\n"
         "## 3 实验\n\n表 2 给出主结果，准确率为 92.7%。\n")


def setUpModule():
    global SERVER, PORT, SAVED_PROVIDER, SAVED_PROXY
    SERVER = ThreadingHTTPServer(("127.0.0.1", 0), H)
    PORT = SERVER.server_address[1]
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    SAVED_PROVIDER = provider.PROVIDER_FILE.read_bytes() if provider.PROVIDER_FILE.exists() else None
    SAVED_PROXY = netenv.proxy
    netenv.proxy = lambda: None                 # 本机假接口：别经本机代理绕一圈


def tearDownModule():
    SERVER.shutdown()
    netenv.proxy = SAVED_PROXY
    if SAVED_PROVIDER is None:
        provider.PROVIDER_FILE.unlink(missing_ok=True)
    else:
        provider.PROVIDER_FILE.write_bytes(SAVED_PROVIDER)
    provider._CACHE["mtime"] = None


def set_provider(d: dict | None) -> None:
    if d is None:
        provider.PROVIDER_FILE.unlink(missing_ok=True)
    else:
        provider.PROVIDER_FILE.write_text(json.dumps(d), encoding="utf-8")
    provider._CACHE["mtime"] = None             # 连写两次 mtime 可能一样：缓存作废


def edit_json(edits, report="", unsure="无", note=None) -> str:
    return json.dumps({"edits": edits, "report": report, "unsure": unsure, "note": note}, ensure_ascii=False)


class Base(unittest.TestCase):
    lang = "zh"

    def setUp(self):
        set_provider({"kind": "custom", "base_url": f"http://127.0.0.1:{PORT}", "key": "test-key-1234", "fast": "f", "main": "m"})
        self.assertEqual(provider.mode(), "api")
        Fake.script, Fake.seen, Fake.reject_images = [], [], False
        self.dir = Path(tempfile.mkdtemp(prefix="hands-api-paper-"))
        self.paper = self.dir / "paper.md"
        self.raw = b"\xef\xbb\xbf" + PAPER.replace("\n", "\r\n").encode("utf-8")      # 带 BOM、CRLF：写回要照原样
        self.paper.write_bytes(self.raw)
        self.board = Board(self.dir / "board.json")
        self.done = []
        self.hands = handsmod.Hands({"hands": {"timeout_s": 30}, "meeting": {"lang": self.lang}}, self.board,
                                    self.done.append, self.paper)
        self.hands.RETRY_WAITS = (0, 0)
        self.ids = []

    def tearDown(self):
        for i in self.ids:
            (HOME / "changes" / f"{i}.json").unlink(missing_ok=True)

    def run_task(self, remark, title, note_job=False):
        pp = {"id": "p1", "name": self.paper.name, "path": str(self.paper)}
        if note_job:
            t = self.board.add_note(remark, title, plan_job=True, paper=pp)
        else:
            t = self.board.add(remark, title, section="1 引言", focus={"section": "1 引言", "text": "组会是课题组"}, paper=pp)
        self.ids.append(t["id"])
        claimed = self.board.claim_next()
        self.assertEqual(claimed["id"], t["id"])
        self.hands._run(claimed)
        return self.board.get(t["id"])

    def text(self) -> str:
        return self.paper.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


class EditFlow(Base):
    def test_edit_applies_exactly_and_finishes(self):
        old, new = "会后依靠笔记整理，容易漏改。", "会后靠笔记整理，常有遗漏。"
        Fake.script = [edit_json([{"old": old, "new": new}], report="在 1 引言第一段把“容易漏改”改成了“常有遗漏”")]
        t = self.run_task("第一段最后那句改一下，别说漏改", "改引言末句")
        want = b"\xef\xbb\xbf" + PAPER.replace(old, new).replace("\n", "\r\n").encode("utf-8")
        self.assertEqual(self.paper.read_bytes(), want)                 # 只换了那一句；BOM、CRLF 照旧
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["board.json", "paper.md"])   # 没碰别的文件、没留临时文件
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["result"], "在 1 引言第一段把“容易漏改”改成了“常有遗漏”。无。")
        self.assertFalse(t.get("note"))
        prog = t["progress"]
        self.assertEqual(prog["step"], "改完了")
        self.assertEqual(prog["edits"], 1)
        self.assertIn("读论文", prog["log"])
        self.assertTrue(any("改第 1 处" in s for s in prog["log"]), prog["log"])
        self.assertIn("检查改动", prog["log"])
        ch = json.loads((HOME / "changes" / f"{t['id']}.json").read_text(encoding="utf-8"))
        self.assertTrue(ch["changed"])
        self.assertTrue(ch["ins"])
        self.assertIn(new, ch["after"])
        self.assertEqual(len(Fake.seen), 1)
        body = Fake.seen[0]["body"]
        self.assertEqual(body["model"], "m")                            # 改稿用主力那档
        self.assertIn("别说漏改", body["system"])
        self.assertIn("只输出一个 JSON 对象", body["system"])
        self.assertNotIn("@AI", body["system"])
        self.assertIn("容易漏改", body["messages"][0]["content"])         # 论文全文在用户消息里
        self.assertEqual(self.done, [])                                  # on_done 是工人循环调的，_run 本身不调

    def test_missing_old_retries_then_succeeds(self):
        Fake.script = [edit_json([{"old": "表2给出主结果，准确率为 92.7%", "new": "表 2 给出主结果，准确率为 93.1%。"}], report="改了"),
                       edit_json([{"old": "准确率为 92.7%。", "new": "准确率为 93.1%。"}], report="在 3 实验把准确率改成 93.1%")]
        t = self.run_task("准确率改成 93.1", "改准确率")
        self.assertEqual(self.text(), PAPER.replace("92.7%", "93.1%"))
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["result"], "在 3 实验把准确率改成 93.1%。无。")
        self.assertEqual(len(Fake.seen), 2)
        msgs = Fake.seen[1]["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertIn("找不到", msgs[2]["content"])
        self.assertIn("论文里最像的是「", msgs[2]["content"])                # 给了论文里最像的那段
        self.assertIn("表 2 给出主结果", msgs[2]["content"].split("最像的是", 1)[1])

    def test_ambiguous_old_never_writes_partially(self):
        dup = "我们的方法在三个数据集上评测。"
        bad = edit_json([{"old": "会后依靠笔记整理", "new": "会后靠笔记整理"}, {"old": dup, "new": "我们在三个数据集上评测。"}],
                        report="改了两处")
        Fake.script = [bad, bad]
        t = self.run_task("引言那句和方法那句都改一下", "改两处")
        self.assertEqual(self.paper.read_bytes(), self.raw)             # 第一处能改也不改：一个字都没动
        self.assertEqual(len(Fake.seen), 2)
        self.assertIn("出现了 2 次", Fake.seen[1]["body"]["messages"][2]["content"])
        self.assertEqual(t["status"], "done")
        self.assertTrue(t.get("note") and t.get("unchanged"))           # 和 Claude 那一路"正文没动"一样：记进修改清单
        self.assertTrue(t["result"].startswith("没改成，正文没动，记进修改清单："), t["result"])
        self.assertEqual(t["progress"]["step"], "没改成")
        ch = json.loads((HOME / "changes" / f"{t['id']}.json").read_text(encoding="utf-8"))
        self.assertFalse(ch["changed"])

    def test_cant_do_in_meeting_becomes_note(self):
        note = {"anchor": "表 2 给出主结果，准确率为 92.7%", "section": "3 实验", "plan": "表 2 加一列行业固定效应的结果",
                "need": "重跑 2 次回归"}
        Fake.script = [edit_json([], report="", unsure="", note=note)]
        t = self.run_task("表二加上行业固定效应再跑一遍", "加固定效应")
        self.assertEqual(self.paper.read_bytes(), self.raw)
        self.assertEqual(t["status"], "done")
        self.assertTrue(t["note"])
        self.assertFalse(t.get("unchanged"))
        self.assertEqual(t["plan"], note["plan"])
        self.assertEqual(t["need"], note["need"])
        self.assertEqual(t["section"], "3 实验")
        self.assertEqual(t["focus"]["text"], note["anchor"])
        self.assertEqual(t["result"], "会上改不了，正文没动，记进修改清单")
        self.assertEqual(t["progress"]["step"], "标注了")

    def test_not_changed_reports_why(self):
        Fake.script = [edit_json([], report="论文里没找到“We believe”这句，所以没改", unsure="你指的是不是 1 引言第一段那句")]
        t = self.run_task("把 We believe 那句拆开", "拆句")
        self.assertEqual(self.paper.read_bytes(), self.raw)
        self.assertTrue(t["note"] and t["unchanged"])
        self.assertIn("没找到“We believe”这句", t["result"])

    def test_annotate_job(self):
        Fake.script = ["#note\n位置：表 2 给出主结果，准确率为 92.7%\n小节：3 实验\n怎么改：表 2 补一列 2015–2023 样本期\n要补：无"]
        t = self.run_task("先别改，记一下表二要补样本期", "表二补样本期", note_job=True)
        self.assertEqual(self.paper.read_bytes(), self.raw)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["result"], "只记下了，没改论文")
        self.assertEqual(t["plan"], "表 2 补一列 2015–2023 样本期")
        self.assertIsNone(t.get("need"))
        self.assertIn("准确率为 92.7%", Fake.seen[0]["body"]["messages"][0]["content"])
        self.assertNotIn("用 Grep 或 Read", Fake.seen[0]["body"]["system"])

    def test_api_error_fails_like_claude(self):
        Fake.script = [{"status": 401, "message": "invalid key"}]
        t = self.run_task("第一段压短", "压短")
        self.assertEqual(t["status"], "failed")
        self.assertIn("401", t["error"])
        self.assertEqual(t["progress"]["step"], "没改成")
        self.assertEqual(self.paper.read_bytes(), self.raw)


class EnglishMeeting(Base):
    lang = "en"

    def test_two_sentences_in_english(self):
        Fake.script = [edit_json([{"old": "容易漏改。", "new": "常有遗漏。"}], report="Reworded the last sentence of the introduction",
                                 unsure="")]
        t = self.run_task("reword that", "Reword")
        self.assertEqual(t["result"], "Reworded the last sentence of the introduction. None.")
        self.assertIn("英文", Fake.seen[0]["body"]["system"])


class Ask(Base):
    def test_ask_paper(self):
        Fake.script = ["论文里写的是 92.7%，在 3 实验那一节。网上的我这边查不了。"]
        steps = []
        ans, err = self.hands.ask_paper("准确率是多少？顺便上网查一下别人的", {"section": "3 实验"}, "我：在的",
                                        on_step=lambda k, v: steps.append((k, v)))
        self.assertIsNone(err)
        self.assertEqual(ans, "论文里写的是 92.7%，在 3 实验那一节。网上的我这边查不了。")
        self.assertEqual(steps, [("读论文", "全文")])
        body = Fake.seen[0]["body"]
        self.assertEqual(body["model"], "m")
        self.assertIn("查不了网", body["system"])
        self.assertIn("顺便上网查一下", body["system"])
        self.assertIn("准确率为 92.7%", body["messages"][0]["content"])
        ans, err = self.hands.ask_paper("准确率出现几次", None, "", quick=True)
        self.assertEqual((ans, err), ("好", None))
        self.assertEqual(Fake.seen[1]["body"]["model"], "f")               # 快查用快速那档
        ev = threading.Event()
        ev.set()
        self.assertEqual(self.hands.ask_paper("x", None, "", cancel=ev), ("", "被打断"))
        self.assertEqual(len(Fake.seen), 2)                               # 打断了就不发请求

    def shot(self) -> str:
        from PIL import Image
        p = self.dir / "shot-0001.png"
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (200, 30, 30)).save(buf, "PNG")
        p.write_bytes(buf.getvalue())
        return str(p)

    def test_ask_screen_sends_image(self):
        Fake.script = ["截图上是 3 实验那张表。"]
        ans, err = self.hands.ask_screen("你看这里", self.shot(), {"section": "3 实验"}, "")
        self.assertEqual((ans, err), ("截图上是 3 实验那张表。", None))
        content = Fake.seen[0]["body"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertEqual(content[1]["type"], "text")

    def test_ask_screen_model_without_vision(self):
        Fake.reject_images = True
        ans, err = self.hands.ask_screen("你看这里", self.shot(), None, "")
        self.assertIsNone(err)
        self.assertEqual(ans, hands_api.NO_VISION[0])


class NotSetUp(Base):
    def test_mode_none_fails_fast(self):
        set_provider({"kind": "unset"})
        self.assertEqual(provider.mode(), "none")
        t = self.run_task("第一段压短", "压短")
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["error"], "还没设置模型来源（设置 → 通用 → 模型来源）")
        self.assertEqual(t["progress"]["step"], "没改成")
        self.assertEqual(self.hands.ask_paper("x", None, ""), ("", hands_api.UNSET))
        self.assertEqual(self.hands.ask_screen("x", None, None, ""), ("", hands_api.UNSET))
        n = self.run_task("记一下", "记一下", note_job=True)
        self.assertEqual((n["status"], n["result"], n.get("plan")), ("done", "只记下了，没改论文", None))
        self.assertEqual(Fake.seen, [])                                  # 一个请求都没发
        self.assertEqual(self.paper.read_bytes(), self.raw)


class ClaudeModeUntouched(Base):
    def test_claude_mode_still_runs_claude(self):
        """模型来源是 Claude Code 账号：照旧走 claude -p（这里把 call 换成假的），不碰接口；收场（_conclude）和以前一样。"""
        set_provider({"kind": "claude"})
        calls = []

        def fake_call(args, **kw):
            calls.append(args)
            self.paper.write_bytes(self.raw.replace("容易漏改".encode("utf-8"), "常有遗漏".encode("utf-8")))
            return {"type": "result", "result": "改好了。无。", "is_error": False}, "", 0
        self.hands.call = fake_call
        t = self.run_task("第一段压短", "压短")
        self.assertEqual(len(calls), 1)
        self.assertIn("--tools", calls[0])
        self.assertEqual((t["status"], t["result"], t["progress"]["step"]), ("done", "改好了。无。", "改完了"))
        self.assertEqual(Fake.seen, [])


if __name__ == "__main__":
    unittest.main()
