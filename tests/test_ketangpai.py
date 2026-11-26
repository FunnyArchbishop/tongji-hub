"""课堂派数据源：token 提取、endtime 解析、响应包装、待办→记录、兜底与失效判定。"""

from __future__ import annotations

import datetime as dt
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from noticehub.receivers import ketangpai_receiver as ktp


def _settings(**overrides):
    base = dict(
        ketangpai_cookie="token=tk-123; other=1",
        ketangpai_course_whitelist=(),
        ketangpai_scan_interval_seconds=1800,
        http_timeout=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class Store:
    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def meta_get(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default)

    def meta_set(self, key: str, value: str) -> None:
        self.meta[key] = value


def _epoch(year: int, month: int, day: int, hour: int = 23, minute: int = 59) -> int:
    return int(dt.datetime(year, month, day, hour, minute).timestamp())


class TokenAndTimeTests(unittest.TestCase):
    def test_token_is_read_from_the_cookie_header(self) -> None:
        self.assertEqual(ktp.extract_token("token=abc; sid=1"), "abc")
        self.assertEqual(ktp.extract_token("Cookie: token=abc; sid=1"), "abc")
        self.assertEqual(ktp.extract_token("abc"), "abc", "裸 token 也要认")
        self.assertEqual(ktp.extract_token("a=1; b=2"), "")

    def test_endtime_is_seconds_not_milliseconds(self) -> None:
        seconds = _epoch(2026, 12, 5)
        self.assertEqual(ktp.parse_endtime(seconds), dt.datetime(2026, 12, 5, 23, 59))
        self.assertEqual(ktp.parse_endtime(str(seconds)), dt.datetime(2026, 12, 5, 23, 59))
        self.assertEqual(ktp.parse_endtime(seconds * 1000), dt.datetime(2026, 12, 5, 23, 59))

    def test_zero_and_blank_mean_no_deadline(self) -> None:
        for value in (0, "0", "", None, False):
            self.assertIsNone(ktp.parse_endtime(value), value)


class EnvelopeTests(unittest.TestCase):
    def test_status_wrapper_is_unwrapped(self) -> None:
        self.assertEqual(ktp.KetangpaiClient._unwrap('{"status":1,"data":{"list":[]}}', "/x"), {"list": []})

    def test_code_wrapper_is_unwrapped(self) -> None:
        self.assertEqual(ktp.KetangpaiClient._unwrap('{"code":10000,"data":{"list":[]}}', "/x"), {"list": []})

    def test_auth_code_raises_auth_error(self) -> None:
        with self.assertRaises(ktp.KetangpaiAuthError):
            ktp.KetangpaiClient._unwrap('{"code":20003,"message":"未登录"}', "/x")

    def test_failed_status_raises_source_error(self) -> None:
        with self.assertRaises(ktp.KetangpaiError):
            ktp.KetangpaiClient._unwrap('{"status":0,"message":"参数错误"}', "/x")

    def test_non_json_raises_source_error(self) -> None:
        with self.assertRaises(ktp.KetangpaiError):
            ktp.KetangpaiClient._unwrap("<html>login</html>", "/x")


class RecordTests(unittest.TestCase):
    def test_record_shape_carries_course_title_and_deadline(self) -> None:
        item = {
            "contentid": "c9",
            "contenttitle": "第一次作业",
            "contenttype": "4",
            "courseid": "k1",
            "coursename": "数据结构",
            "contentdescription": "<p>交实验报告</p>",
        }
        record = ktp.to_record(item, dt.datetime(2026, 12, 5, 23, 59))
        self.assertEqual(record["msg_id"], "ketangpai:k1:4:c9")
        self.assertEqual(record["source"], "ketangpai")
        self.assertIn("数据结构 · 第一次作业", record["content"])
        self.assertIn("截止时间：2026-12-05 23:59", record["content"])
        self.assertIn("来源：作业", record["content"])
        self.assertIn("交实验报告", record["content"])
        self.assertEqual(record["_due"], "2026-12-05 23:59")

    def test_exam_content_type_is_labelled(self) -> None:
        item = {"contentid": "t1", "contenttitle": "期中测试", "contenttype": "6", "courseid": "k1", "coursename": "数据结构"}
        self.assertIn("来源：考试", ktp.to_record(item, dt.datetime(2026, 12, 5, 9, 0))["content"])


class FakeClient:
    def __init__(self, todos=None, pages=None, courses=None, contents=None):
        self.todos = todos or []
        self.pages = pages
        self._courses = courses or []
        self._contents = contents or {}

    def todo_list(self, *, page=1, limit=50, todo_type="", class_id=""):
        if self.pages is not None:
            return list(self.pages.get(page, []))
        return list(self.todos) if page == 1 else []

    def courses(self):
        return list(self._courses)

    def course_content(self, course_id, content_type, *, page=1, limit=50):
        return list(self._contents.get((str(course_id), str(content_type)), []))


class ReaderTests(unittest.TestCase):
    def _reader(self, client, settings=None, store=None):
        delivered: list[dict] = []
        reader = ktp.KetangpaiReader(settings or _settings(), lambda record: delivered.append(record) or True, store=store or Store())
        reader.build_client = lambda: client
        return reader, delivered

    def test_todo_with_deadline_is_delivered_once(self) -> None:
        todo = {"contentid": "c1", "contenttitle": "作业一", "contenttype": "4", "courseid": "k1",
                "coursename": "数据结构", "endtime": _epoch(2026, 12, 5), "timestate": "2"}
        reader, delivered = self._reader(FakeClient(todos=[todo]))
        self.assertEqual(reader.scan_once(), 1)
        self.assertEqual(reader.scan_once(), 0)
        self.assertEqual(len(delivered), 1)
        self.assertNotIn("_due", delivered[0])

    def test_finished_and_deadline_less_items_are_skipped(self) -> None:
        items = [
            {"contentid": "c1", "contenttitle": "已结束", "contenttype": "4", "courseid": "k1", "endtime": _epoch(2026, 12, 5), "timestate": "3"},
            {"contentid": "c2", "contenttitle": "不限时间", "contenttype": "4", "courseid": "k1", "endtime": 0},
            {"contentid": "c3", "contenttitle": "已提交", "contenttype": "4", "courseid": "k1", "endtime": _epoch(2026, 12, 5), "submitState": "1"},
        ]
        reader, delivered = self._reader(FakeClient(todos=items))
        self.assertEqual(reader.scan_once(), 0)
        self.assertEqual(delivered, [])

    def test_todo_list_is_paginated_until_short_page(self) -> None:
        def item(index: int) -> dict:
            return {"contentid": "c%d" % index, "contenttitle": "作业%d" % index, "contenttype": "4",
                    "courseid": "k1", "coursename": "数据结构", "endtime": _epoch(2026, 12, 5), "timestate": "2"}

        pages = {
            1: [item(index) for index in range(ktp.TODO_PAGE_SIZE)],
            2: [item(999)],
        }
        reader, delivered = self._reader(FakeClient(pages=pages))
        self.assertEqual(reader.scan_once(), ktp.TODO_PAGE_SIZE + 1)
        self.assertEqual(len(delivered), ktp.TODO_PAGE_SIZE + 1)

    def test_empty_todo_list_falls_back_to_course_content(self) -> None:
        courses = [{"courseid": "k1", "coursename": "数据结构"}]
        contents = {
            ("k1", "4"): [{"contentid": "h1", "contenttitle": "兜底作业", "contenttype": "4",
                           "courseid": "k1", "coursename": "数据结构", "endtime": _epoch(2026, 12, 8), "timestate": "2"}],
        }
        reader, delivered = self._reader(FakeClient(todos=[], courses=courses, contents=contents))
        self.assertEqual(reader.scan_once(), 1)
        self.assertEqual(delivered[0]["msg_id"], "ketangpai:k1:4:h1")

    def test_course_whitelist_filters_records(self) -> None:
        items = [
            {"contentid": "c1", "contenttitle": "A", "contenttype": "4", "courseid": "k1", "coursename": "数据结构", "endtime": _epoch(2026, 12, 5), "timestate": "2"},
            {"contentid": "c2", "contenttitle": "B", "contenttype": "4", "courseid": "k2", "coursename": "操作系统", "endtime": _epoch(2026, 12, 6), "timestate": "2"},
        ]
        reader, delivered = self._reader(FakeClient(todos=items), _settings(ketangpai_course_whitelist=("k2",)))
        reader.scan_once()
        self.assertEqual([record["group_id"] for record in delivered], ["k2"])

    def test_auth_failure_marks_the_source_expired(self) -> None:
        settings = _settings()
        reader, _delivered = self._reader(FakeClient([]), settings)
        reader._login_still_valid = lambda: False
        reader._record_failure(ktp.KetangpaiAuthError("token 失效"))
        status = ktp.ketangpai_status(settings)
        self.assertTrue(status["expired"])
        self.assertEqual(status["banner_text"], ktp.KETANGPAI_COOKIE_EXPIRED)


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict]] = {}
    seen_headers: list[dict[str, str]] = []
    seen_bodies: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server 约定
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen_headers.append({key.lower(): value for key, value in self.headers.items()})
        type(self).seen_bodies.append(body)
        status, payload = self.routes.get(path, (404, {"status": 0, "message": "not found"}))
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        return


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.routes = {
            "/Futurev2/Todo/getTodoList": (200, {"status": 1, "data": {"list": [
                {"contentid": "c1", "contenttitle": "作业一", "contenttype": "4",
                 "courseid": "k1", "coursename": "数据结构", "endtime": _epoch(2026, 12, 5), "timestate": "2"}
            ]}}),
        }
        _Handler.seen_headers = []
        _Handler.seen_bodies = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_todo_list_posts_token_header_and_reqtimestamp(self) -> None:
        client = ktp.KetangpaiClient("tk-123", base=self.base, timeout=5)
        rows = client.todo_list()
        self.assertEqual(rows[0]["contentid"], "c1")
        headers = _Handler.seen_headers[-1]
        self.assertEqual(headers.get("token"), "tk-123")
        self.assertIn("reqtimestamp", _Handler.seen_bodies[-1])

    def test_missing_token_fails_before_any_request(self) -> None:
        client = ktp.KetangpaiClient("", base=self.base, timeout=5)
        with self.assertRaises(ktp.KetangpaiAuthError):
            client.validate_login()

    def test_expired_token_raises_auth_error(self) -> None:
        _Handler.routes["/Futurev2/Todo/getTodoList"] = (200, {"code": 20003, "message": "未登录"})
        client = ktp.KetangpaiClient("tk-old", base=self.base, timeout=5)
        with self.assertRaises(ktp.KetangpaiAuthError):
            client.validate_login()

    def test_password_login_returns_the_token(self) -> None:
        _Handler.routes["/UserApi/login"] = (200, {"status": 1, "data": {"token": "tk-new"}})
        token = ktp.KetangpaiClient.login("stu@example.com", "pw-1", base=self.base, timeout=5)
        self.assertEqual(token, "tk-new")
        body = _Handler.seen_bodies[-1]
        self.assertEqual(body.get("email"), "stu@example.com")
        self.assertEqual(body.get("remember"), "1")

    def test_password_login_without_token_raises(self) -> None:
        _Handler.routes["/UserApi/login"] = (200, {"status": 1, "data": {}})
        with self.assertRaises(ktp.KetangpaiError):
            ktp.KetangpaiClient.login("stu@example.com", "pw-1", base=self.base, timeout=5)


class LoginRouteTests(unittest.TestCase):
    """POST /api/ketangpai/login：密码用完即弃，只把 token 落盘。"""

    def setUp(self) -> None:
        from noticehub.infrastructure.sqlite.store import Store
        from noticehub.platform.config import Settings
        from noticehub.web.webapp import TaskWebServer
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.mkdtemp(prefix="nh-ktp-login-")
        self.store = Store(Path(self.tmp) / "login.sqlite3")
        self.settings = Settings(
            group_whitelist=("g1",),
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
            data_dir=Path(self.tmp) / "data",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        self.base = "http://127.0.0.1:%d" % self.server.server.server_address[1]

    def _post(self, payload: dict) -> dict:
        import urllib.request
        request = urllib.request.Request(
            self.base + "/api/ketangpai/login",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"X-Token": "secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    def test_login_saves_only_the_token(self) -> None:
        from unittest import mock

        with mock.patch.object(ktp.KetangpaiClient, "login", return_value="tk-saved"), \
             mock.patch.object(ktp.KetangpaiClient, "validate_login", return_value=None):
            result = self._post({"email": "stu@example.com", "password": "pw-secret-1"})
        self.assertTrue(result.get("ok"), result)
        cookie_file = Path(self.tmp) / "data" / "ketangpai.cookie"
        self.assertTrue(cookie_file.is_file())
        saved = cookie_file.read_text(encoding="utf-8")
        self.assertEqual(saved, "token=tk-saved")
        self.assertNotIn("pw-secret-1", saved)
        self.assertTrue(self.settings.ketangpai_enabled)

    def test_login_failure_does_not_leak_the_password(self) -> None:
        from unittest import mock

        with mock.patch.object(ktp.KetangpaiClient, "login", side_effect=ktp.KetangpaiError("密码 pw-secret-1 不对")):
            result = self._post({"email": "stu@example.com", "password": "pw-secret-1"})
        self.assertFalse(result.get("ok"))
        self.assertNotIn("pw-secret-1", json.dumps(result, ensure_ascii=False))

    def test_missing_fields_are_rejected(self) -> None:
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post({"email": "", "password": ""})
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
