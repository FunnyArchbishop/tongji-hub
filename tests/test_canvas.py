"""Canvas 数据源：正文解析、记录形状、基线/改期、登录失效判定。

这些断言锁的是行为，不是类型：改期必须发新记录、基线只推一轮、白名单外课程不投递。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from noticehub.receivers import canvas_receiver as canvas


def _settings(**overrides):
    base = dict(
        canvas_cookie="canvas_session=abc",
        canvas_base_url=canvas.DEFAULT_BASE_URL,
        canvas_course_whitelist=(),
        canvas_scan_interval_seconds=1800,
        http_timeout=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class Store:
    """只实现 reader 用到的那两个 meta 方法。"""

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def meta_get(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default)

    def meta_set(self, key: str, value: str) -> None:
        self.meta[key] = value


class CanvasParsingTests(unittest.TestCase):
    def test_extract_deadlines_requires_a_deadline_hint(self) -> None:
        now = dt.datetime(2026, 11, 20, 10, 0)
        self.assertEqual(canvas.extract_deadlines("2026-11-25 上课地点 A301", now), [])
        hits = canvas.extract_deadlines("请在12月5日23:59前提交", now)
        self.assertEqual([due for due, _snippet in hits], [dt.datetime(2026, 12, 5, 23, 59)])

    def test_extract_deadlines_defaults_to_end_of_day(self) -> None:
        now = dt.datetime(2026, 11, 20, 10, 0)
        hits = canvas.extract_deadlines("截止日期：12月8日", now)
        self.assertEqual([due for due, _snippet in hits], [dt.datetime(2026, 12, 8, 23, 59)])

    def test_extract_deadlines_rolls_year_forward_only_when_far_behind(self) -> None:
        hits = canvas.extract_deadlines("截止 12月30日", dt.datetime(2026, 1, 5, 9, 0))
        self.assertEqual([due.year for due, _snippet in hits], [2026])

    def test_relative_time_is_resolved_against_the_posted_date(self) -> None:
        posted = dt.datetime(2026, 9, 7, 10, 0)
        # 「下周三」= 下周一 + 2 天；不能算成本周三。
        next_wednesday = posted + dt.timedelta(days=7 - posted.weekday() + 2)
        hits = canvas.resolve_relative_times("请于下周三前提交", posted)
        self.assertEqual(hits[0][0], next_wednesday.replace(hour=23, minute=59))

    def test_tomorrow_uses_posted_date_not_today(self) -> None:
        posted = dt.datetime(2026, 9, 1, 18, 0)
        hits = canvas.resolve_relative_times("明天下午交", posted)
        self.assertEqual(hits[0][0], dt.datetime(2026, 9, 2, 23, 59))

    def test_lock_at_counts_as_a_deadline(self) -> None:
        self.assertIsNotNone(canvas.due_of_assignment({"due_at": None, "lock_at": "2026-12-05T15:59:00Z"}))

    def test_submitted_assignment_is_detected(self) -> None:
        self.assertTrue(canvas._is_submitted({"submitted_at": "2026-11-01T00:00:00Z"}))
        self.assertTrue(canvas._is_submitted({"workflow_state": "graded"}))
        self.assertTrue(canvas._is_submitted({"excused": True}))
        self.assertFalse(canvas._is_submitted({"workflow_state": "unsubmitted"}))

    def test_record_shape_is_stable_and_carries_the_deadline(self) -> None:
        record = canvas.to_record("9", "高等数学", "a7", "第三章习题", dt.datetime(2026, 12, 5, 23, 59))
        self.assertEqual(record["msg_id"], "canvas:9:a7")
        self.assertEqual(record["source"], "canvas")
        self.assertEqual(record["group_id"], "9")
        self.assertIn("截止时间：2026-12-05 23:59", record["content"])
        self.assertEqual(record["_due"], "2026-12-05 23:59")

    def test_course_name_drops_trailing_course_code(self) -> None:
        self.assertEqual(canvas.clean_course_name("某课程 10220403"), "某课程")


class FakeClient:
    """collect() 用的替身：按课程给作业与公告，不发网络请求。"""

    def __init__(self, courses, assignments=None, topics=None, details=None):
        self._courses = courses
        self._assignments = assignments or {}
        self._topics = topics or {}
        self._details = details or {}

    def courses(self):
        return list(self._courses)

    def assignments(self, course_id):
        return list(self._assignments.get(str(course_id), []))

    def discussion_topics(self, course_id, *, only_announcements: bool = False):
        rows = self._topics.get(str(course_id), {})
        return list(rows.get("announcements" if only_announcements else "discussions", []))

    def discussion(self, course_id, topic_id):
        return dict(self._details.get(str(topic_id), {}))


def _assignment(**overrides):
    row = {
        "id": 7,
        "name": "第三章习题",
        "due_at": "2026-12-05T15:59:00Z",
        "html_url": "https://canvas.example/courses/9/assignments/7",
        "description": "<p>做完第 3 章</p>",
        "submission": {"workflow_state": "unsubmitted"},
    }
    row.update(overrides)
    return row


class CanvasReaderTests(unittest.TestCase):
    def _reader(self, client, settings=None, store=None):
        delivered: list[dict] = []
        reader = canvas.CanvasReader(settings or _settings(), lambda record: delivered.append(record) or True, store=store or Store())
        reader.build_client = lambda: client  # 不碰网络
        return reader, delivered

    def test_baseline_delivers_once_then_stays_quiet(self) -> None:
        client = FakeClient([{"id": 9, "name": "高等数学"}], {"9": [_assignment()]})
        reader, delivered = self._reader(client)
        self.assertEqual(reader.scan_once(), 1)
        self.assertEqual(reader.scan_once(), 0, "同一条 DDL 第二轮不该再投递")
        self.assertEqual(len(delivered), 1)
        self.assertNotIn("_due", delivered[0], "内部字段不能漏给下游")

    def test_submitted_assignment_is_not_delivered(self) -> None:
        client = FakeClient(
            [{"id": 9, "name": "高等数学"}],
            {"9": [_assignment(submission={"workflow_state": "submitted"})]},
        )
        reader, delivered = self._reader(client)
        self.assertEqual(reader.scan_once(), 0)
        self.assertEqual(delivered, [])

    def test_deadline_outside_the_window_is_ignored(self) -> None:
        far_future = (dt.datetime.now() + dt.timedelta(days=canvas.HORIZON_DAYS + 10)).isoformat()
        client = FakeClient([{"id": 9, "name": "高等数学"}], {"9": [_assignment(due_at=far_future)]})
        reader, _delivered = self._reader(client)
        self.assertEqual(reader.scan_once(), 0)

    def test_course_whitelist_filters_courses(self) -> None:
        client = FakeClient(
            [{"id": 9, "name": "高等数学"}, {"id": 10, "name": "线性代数"}],
            {"9": [_assignment()], "10": [_assignment(id=8, name="第二章")]},
        )
        reader, delivered = self._reader(client, _settings(canvas_course_whitelist=("10",)))
        reader.scan_once()
        self.assertEqual([record["group_id"] for record in delivered], ["10"])

    def test_announcement_body_deadline_becomes_a_record(self) -> None:
        topics = {"announcements": [{"id": 3, "title": "期末安排", "posted_at": "2026-11-01T00:00:00Z"}]}
        details = {"3": {"message": "<p>课程报告请在12月20日23:00前提交</p>", "title": "期末安排"}}
        client = FakeClient([{"id": 9, "name": "高等数学"}], {"9": []}, {"9": topics}, details)
        reader, delivered = self._reader(client)
        self.assertEqual(reader.scan_once(), 1)
        self.assertEqual(delivered[0]["msg_id"], "canvas:9:t3")
        self.assertIn("2026-12-20 23:00", delivered[0]["content"])

    def test_reschedule_emits_a_new_record(self) -> None:
        first = _assignment()
        moved = _assignment(due_at="2026-12-10T15:59:00Z")
        client = FakeClient([{"id": 9, "name": "高等数学"}], {"9": [first]})
        reader, delivered = self._reader(client)
        reader.scan_once()
        client._assignments["9"] = [moved]
        self.assertEqual(reader.scan_once(), 1)
        self.assertEqual(delivered[-1]["event"], "WORK_RESCHEDULED")
        self.assertTrue(delivered[-1]["msg_id"].endswith("@2026-12-10 23:59"))
        self.assertIn("原截止时间：2026-12-05 23:59", delivered[-1]["content"])

    def test_auth_failure_marks_the_source_expired(self) -> None:
        settings = _settings()
        reader, _delivered = self._reader(FakeClient([]), settings)
        reader._login_still_valid = lambda: False  # 复核结论：确实过期
        reader._record_failure(canvas.CanvasAuthError("Cookie 过期"))
        status = canvas.canvas_status(settings)
        self.assertTrue(status["expired"])
        self.assertEqual(status["banner_text"], canvas.CANVAS_COOKIE_EXPIRED)

    def test_unclear_auth_failure_keeps_the_previous_verdict(self) -> None:
        settings = _settings()
        reader, _delivered = self._reader(FakeClient([]), settings)
        reader._login_still_valid = lambda: None  # 复核本身失败
        reader._record_failure(canvas.CanvasAuthError("某个子站要求登录"))
        self.assertFalse(canvas.canvas_status(settings)["expired"])


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict | list]] = {}

    def do_GET(self):  # noqa: N802 - http.server 约定
        path = self.path.split("?")[0]
        status, payload = self.routes.get(path, (404, {"error": "not found"}))
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 测试里不要刷日志
        return


class CanvasClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routes = {
            "/api/v1/users/self/profile": (200, {"name": "同学"}),
            "/api/v1/courses": (200, [{"id": 9, "name": "高等数学 10220403"}]),
        }
        _Handler.routes = self.routes
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_courses_are_read_with_the_configured_cookie(self) -> None:
        seen: list[str] = []

        class Recorder(_Handler):
            def do_GET(self):  # noqa: N802
                seen.append(self.headers.get("Cookie", ""))
                super().do_GET()

        Recorder.routes = self.routes
        self.server.RequestHandlerClass = Recorder
        client = canvas.CanvasClient(self.base, cookie="canvas_session=abc", timeout=5)
        courses = client.courses()
        self.assertEqual(courses[0]["name"], "高等数学 10220403")
        self.assertTrue(all("canvas_session=abc" in value for value in seen))

    def test_unauthorized_profile_raises_auth_error(self) -> None:
        self.routes["/api/v1/users/self/profile"] = (401, {"error": "unauthorized"})
        client = canvas.CanvasClient(self.base, cookie="canvas_session=expired", timeout=5)
        with self.assertRaises(canvas.CanvasAuthError):
            client.validate_login()

    def test_missing_cookie_fails_before_any_request(self) -> None:
        client = canvas.CanvasClient(self.base, cookie="", timeout=5)
        with self.assertRaises(canvas.CanvasAuthError):
            client.validate_login()

    def test_access_token_is_sent_as_bearer(self) -> None:
        seen: list[str] = []

        class Recorder(_Handler):
            def do_GET(self):  # noqa: N802
                seen.append(self.headers.get("Authorization", ""))
                super().do_GET()

        Recorder.routes = self.routes
        self.server.RequestHandlerClass = Recorder
        client = canvas.CanvasClient(self.base, token="tok-123", timeout=5)
        client.validate_login()
        self.assertTrue(seen and all(value == "Bearer tok-123" for value in seen))

    def test_token_alone_counts_as_configured(self) -> None:
        settings = SimpleNamespace(
            canvas_cookie="",
            canvas_token="tok-123",
            canvas_base_url=self.base,
            canvas_course_whitelist=(),
            canvas_scan_interval_seconds=1800,
            http_timeout=5,
        )
        reader = canvas.CanvasReader(settings, lambda record: True)
        self.assertTrue(reader.has_credentials())
        settings.canvas_token = ""
        self.assertFalse(reader.has_credentials())


if __name__ == "__main__":
    unittest.main()
