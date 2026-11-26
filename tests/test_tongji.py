"""同济校园集成：绩点换算、课表解析与 ics、借阅到期、官方日历订阅规范化。

这些断言锁的是「官方口径」：本科与研究生两套分段边界必须不同、
课表要能展开成具体日期、借阅只认应还日期、官方 ics 的伪全天要转成真全天。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from noticehub.tongji import calendar_feed, gpa, jwc, library, reader, watches
from noticehub.tongji.session import TongjiAuthError, TongjiSession


class Store:
    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def meta_get(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default)

    def meta_set(self, key: str, value: str) -> None:
        self.meta[key] = value


class GpaTests(unittest.TestCase):
    def test_undergraduate_bands_follow_the_official_table(self) -> None:
        self.assertEqual(gpa.point_of(95), 5.0)
        self.assertEqual(gpa.point_of(89.5), 5.0)
        self.assertEqual(gpa.point_of(89.4), 4.0)
        self.assertEqual(gpa.point_of(79.5), 4.0)
        self.assertEqual(gpa.point_of(69.5), 3.0)
        self.assertEqual(gpa.point_of(59.5), 2.0)
        self.assertEqual(gpa.point_of(59.4), 0.0)

    def test_failed_course_converts_to_30_not_zero(self) -> None:
        self.assertEqual(gpa.band_of(50).converted, 30.0)

    def test_graduate_bands_differ_at_the_boundary(self) -> None:
        # 89.7 在本科是「优 = 5」，在研究生是「B = 4」——这是两套口径的关键差别。
        self.assertEqual(gpa.point_of(89.7, gpa.UNDERGRADUATE), 5.0)
        self.assertEqual(gpa.point_of(89.7, gpa.GRADUATE), 4.0)
        self.assertEqual(gpa.point_of(90, gpa.GRADUATE), 5.0)
        self.assertEqual(gpa.point_of(79.9, gpa.GRADUATE), 3.0)

    def test_weighted_gpa_uses_credits(self) -> None:
        result = gpa.compute_gpa([
            {"name": "高数", "score": 92, "credit": 5},
            {"name": "英语", "score": 78, "credit": 3},
        ])
        self.assertEqual(result["gpa"], 4.25)
        self.assertEqual(result["credits"], 8.0)
        self.assertEqual([row["label"] for row in result["courses"]], ["优 A", "中 C"])

    def test_empty_input_has_no_gpa(self) -> None:
        self.assertIsNone(gpa.compute_gpa([])["gpa"])


class TimetableTests(unittest.TestCase):
    def test_week_weekday_and_period_parsing(self) -> None:
        self.assertEqual(jwc.parse_week_range("1-16周"), (1, 16))
        self.assertEqual(jwc.parse_week_range("3-15周(单)"), (3, 15))
        self.assertEqual(jwc.parse_weekday("星期一"), 0)
        self.assertEqual(jwc.parse_weekday("周日"), 6)
        self.assertEqual(jwc.parse_periods("1-2节"), (1, 2))
        self.assertEqual(jwc.parse_periods("第5节"), (5, 5))

    def test_rows_expand_into_dated_sessions(self) -> None:
        rows = [{"course": "高数", "room": "A301", "weekday": 0, "period_start": 1, "period_end": 2, "weeks": (1, 2)}]
        events = jwc.expand_sessions(rows, dt.date(2026, 9, 9))  # 周三开学，第一周从 9/7 算
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["start"], dt.datetime(2026, 9, 7, 8, 0))
        self.assertEqual(events[1]["start"], dt.datetime(2026, 9, 14, 8, 0))
        self.assertEqual(events[0]["end"], dt.datetime(2026, 9, 7, 9, 30))

    def test_no_term_start_means_no_events(self) -> None:
        rows = [{"course": "高数", "weekday": 0, "period_start": 1, "period_end": 2, "weeks": (1, 2)}]
        self.assertEqual(jwc.expand_sessions(rows, None), [])

    def test_ics_contains_one_event_per_session(self) -> None:
        rows = [{"course": "高数", "room": "A301", "weekday": 0, "period_start": 1, "period_end": 2, "weeks": (1, 1)}]
        body = jwc.render_timetable_ics(jwc.expand_sessions(rows, dt.date(2026, 9, 7)))
        self.assertTrue(body.startswith("BEGIN:VCALENDAR"))
        self.assertEqual(body.count("BEGIN:VEVENT"), 1)
        self.assertIn("SUMMARY:高数 @A301", body)
        self.assertIn("TZID=Asia/Shanghai", body)


class LibraryTests(unittest.TestCase):
    def test_due_date_only_reads_norm_return_date(self) -> None:
        self.assertEqual(library.due_of({"normReturnDate": "2026-12-05"}), dt.date(2026, 12, 5))
        # returnDate 是历史归还日期，不能拿来算到期
        self.assertIsNone(library.due_of({"returnDate": "2020-01-01"}))

    def test_only_near_due_books_produce_records(self) -> None:
        rows = [
            {"title": "算法导论", "normReturnDate": "2026-12-01"},
            {"title": "很远的一本", "normReturnDate": "2027-03-01"},
        ]
        records = library.loan_records(rows, now=dt.date(2026, 11, 28))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["msg_id"], "tongji:loan:算法导论:2026-12-01")
        self.assertIn("还有 3 天到期", records[0]["content"])

    def test_overdue_book_is_reported(self) -> None:
        records = library.loan_records([{"title": "书", "normReturnDate": "2026-11-20"}], now=dt.date(2026, 11, 28))
        self.assertIn("已逾期 8 天", records[0]["content"])


class CalendarFeedTests(unittest.TestCase):
    def test_pseudo_all_day_becomes_a_real_all_day_event(self) -> None:
        raw = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART:20260907T000000\r\n"
            "DTEND:20260907T235959\r\nSUMMARY:开学\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        result = calendar_feed.normalize(raw)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pseudo_all_day"], 1)
        event = result["events"][0]
        self.assertTrue(event["all_day"])
        self.assertEqual(event["start"], "20260907")
        self.assertEqual(event["end"], "20260908")
        self.assertIn("DTSTART;VALUE=DATE:20260907", result["ics"])

    def test_timed_event_keeps_its_clock(self) -> None:
        raw = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:b\r\nDTSTART:20260907T140000\r\n"
            "DTEND:20260907T160000\r\nSUMMARY:讲座\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        result = calendar_feed.normalize(raw)
        self.assertEqual(result["pseudo_all_day"], 0)
        self.assertFalse(result["events"][0]["all_day"])

    def test_feed_id_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            calendar_feed.feed_url("../../etc/passwd")


class WatchTests(unittest.TestCase):
    def test_bad_watch_rows_are_dropped_not_fatal(self) -> None:
        parsed = watches.parse_watches(json.dumps([
            {"label": "教务通知", "url": "https://1.tongji.edu.cn/notice"},
            {"label": "没有地址"},
            {"url": "ftp://nope"},
        ]))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].label, "教务通知")

    def test_fingerprint_ignores_whitespace_noise(self) -> None:
        self.assertEqual(watches.fingerprint("a  b\n\nc"), watches.fingerprint("a b c"))

    def test_record_carries_label_and_link(self) -> None:
        watch = watches.Watch(id="w1", label="教务通知", url="https://1.tongji.edu.cn/n", keyword="选课")
        record = watches.build_record(watch, "选课通知：第一轮选课即将开始")
        self.assertEqual(record["source"], "tongji")
        self.assertEqual(record["group_id"], "w1")
        self.assertIn("https://1.tongji.edu.cn/n", record["content"])


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str]] = {}
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server 约定
        path = self.path.split("?")[0]
        status, body = self.routes.get(path, (404, "not found"))
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        type(self).seen.append(self.headers.get("X-Token", ""))

    def log_message(self, *args):
        return


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.routes = {
            "/api/sessionservice/session/getSessionUser": (200, json.dumps(
                {"code": 200, "data": {"name": "同学", "uid": "u1"}}
            )),
            "/api/scoremanagementservice/scoreGrades/getMyGrades": (200, json.dumps(
                {"code": 200, "data": [{"courseName": "高数", "score": 92}]}
            )),
        }
        _Handler.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _settings(self, **overrides):
        base = dict(
            tongji_cookie="",
            tongji_jwc_session="sid-1",
            tongji_jwc_uid="u1",
            tongji_library_jwt="",
            tongji_library_jwt_header="",
            tongji_cal_feed="",
            tongji_scan_interval_seconds=1800,
            http_timeout=5,
            tongji_enabled=True,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_jwc_client_sends_the_session_id_as_x_token(self) -> None:
        client = jwc.JwcClient("sid-1", base=self.base, timeout=5)
        grades = client.grades("u1")
        self.assertEqual(grades[0]["courseName"], "高数")
        self.assertEqual(_Handler.seen[-1], "sid-1")

    def test_reader_delivers_new_grades(self) -> None:
        import os

        os.environ["NOTICEHUB_TONGJI_JWC_BASE"] = self.base
        try:
            delivered: list[dict] = []
            settings = self._settings()
            reader_obj = reader.TongjiReader(settings, lambda record: delivered.append(record) or True, store=Store())
            self.assertEqual(reader_obj.scan_once(), 1)
            self.assertEqual(delivered[0]["event"], "GRADE_PUBLISHED")
            self.assertIn("成绩公布：高数", delivered[0]["content"])
        finally:
            os.environ.pop("NOTICEHUB_TONGJI_JWC_BASE", None)

    def test_missing_session_fails_fast(self) -> None:
        client = jwc.JwcClient("", base=self.base, timeout=5)
        with self.assertRaises(TongjiAuthError):
            client.grades("u1")

    def test_watch_baseline_does_not_push_on_first_round(self) -> None:
        _Handler.routes["/notice"] = (200, "<html><body>选课通知：第一轮</body></html>")
        store = Store()
        reader.save_watches(store, [watches.Watch(id="w1", label="教务通知", url=self.base + "/notice", keyword="选课")])
        delivered: list[dict] = []
        settings = self._settings(tongji_jwc_session="", tongji_jwc_uid="", tongji_cookie="c=1")
        reader_obj = reader.TongjiReader(settings, lambda record: delivered.append(record) or True, store=store)
        self.assertEqual(reader_obj.scan_once(), 0, "首轮只建基线")
        _Handler.routes["/notice"] = (200, "<html><body>选课通知：第二轮开始了</body></html>")
        self.assertEqual(reader_obj.scan_once(), 1, "页面变了才推")
        self.assertEqual(delivered[0]["group_id"], "w1")

    def test_login_page_is_reported_as_auth_error(self) -> None:
        _Handler.routes["/x"] = (200, "<html>ActionAuthChain 请登录</html>")
        session = TongjiSession("c=1", timeout=5)
        with self.assertRaises(TongjiAuthError):
            session.get(self.base + "/x")


if __name__ == "__main__":
    unittest.main()
