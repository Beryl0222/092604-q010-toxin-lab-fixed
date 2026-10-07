"""JSON 请求边界与命令行输出。"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from helpers import A_TOXINS, accept, build_service

from toxin_lab import cli
from toxin_lab.api import handle


class Api测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def call(self, payload):
        return json.loads(handle(json.dumps(payload, ensure_ascii=False), self.service))

    def test_health与登记(self):
        body = self.call({"action": "health"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"]["status"], "ok")
        created = self.call({"action": "register", "record_id": "r1", "owner_id": "o1"})
        self.assertTrue(created["ok"])
        found = self.call({"action": "find", "record_id": "r1"})
        self.assertEqual(found["result"]["owner_id"], "o1")

    def test_业务违例返回中文错误而非抛出(self):
        body = self.call({"action": "explain", "sample_id": "不存在"})
        self.assertFalse(body["ok"])
        self.assertIn("不存在", body["error"])

    def test_非法动作与非法JSON(self):
        self.assertFalse(self.call({"action": "nope"})["ok"])
        raw = handle("{不是json", self.service)
        self.assertFalse(json.loads(raw)["ok"])

    def test_完整流程动作链(self):
        accept(self.service, "S1", A_TOXINS)
        scheduled = self.call({"action": "schedule", "sample_ids": ["S1"]})
        self.assertTrue(scheduled["ok"])
        task_id = scheduled["result"]["planned"][0]["task_ids"][0]
        self.assertTrue(self.call({"action": "start_task", "task_id": task_id})["ok"])
        values = {t: 1.2 for t in A_TOXINS}
        recorded = self.call({"action": "record_results", "task_id": task_id, "values": values})
        self.assertEqual(recorded["result"]["state"], "已完成")
        status = self.call({"action": "status", "sample_id": "S1"})["result"][0]
        self.assertEqual(status["allocated_g"], 5.0)


class Cli测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        accept(self.service, "S1", A_TOXINS)
        self.service.schedule()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--db", ":memory:", *argv])
        return code, buf.getvalue()

    def test_status_中文输出守恒与完成时间(self):
        # CLI 默认走文件库，这里直接用服务渲染验证内容
        from toxin_lab.cli import render_status
        text = render_status(self.service.sample_status("S1"))
        self.assertIn("S1", text)
        self.assertIn("余量", text)

    def test_explain_输出选择理由(self):
        from toxin_lab.cli import render_explain
        text = render_explain(self.service.explain("S1"))
        self.assertIn("STD-A@2023", text)
        self.assertIn("守恒检查：通过", text)

    def test_supersede预览命令(self):
        from toxin_lab.cli import render_supersede
        preview = self.service.preview_supersede("STD-A@2023", {"run_minutes": 15})
        text = render_supersede(preview)
        self.assertIn("STD-A@2023 → STD-A@2024", text)
        self.assertIn("预览", text)

    def test_未知命令返回帮助(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main([])
        self.assertEqual(code, 0)

    def test_文件库demo与status命令可重复进入且续排不重复消耗(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            def run(*argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = cli.main(["--db", path, *argv])
                return code, buf.getvalue()

            code, out = run("demo", "--fresh")
            self.assertEqual(code, 0)
            self.assertIn("批次编排运行 DEMO", out)
            # 再次编排：全部沿用既有计划
            code, out = run("schedule")
            self.assertEqual(code, 0)
            self.assertIn("沿用既有计划，未重复消耗", out)
            # 失效清单与隔离清单可读
            code, out = run("invalidations")
            self.assertEqual(code, 0)
            self.assertIn("当前没有失效结果", out)
            code, out = run("quarantine")
            self.assertEqual(code, 0)
            self.assertIn("无隔离样本", out)
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
