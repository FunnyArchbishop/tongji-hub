"""教务「1 系统」只读客户端（`1.tongji.edu.cn`）。

接口来自第三方实现里被验证过的路径（Cinea4678/Tongji-EasyAPI、
JERRIYHAN/tongji-online、ShiGuangSchedule/shiguang_warehouse）：

* 鉴权：请求头 `X-Token: <sessionid>`，`sessionid` 就是浏览器 sessionStorage 里那个
* 会话校验：`GET /api/sessionservice/session/getSessionUser`
* 当前学期：`GET /api/baseresservice/schoolCalendar/currentTermCalendar?flag=0`
* 课表：`GET /api/electionservice/reportManagement/findStudentTimetab?calendarId=<学期ID>`
* 成绩：`GET /api/scoremanagementservice/scoreGrades/getMyGrades?studentId=<uid>`
* 节假日：`GET /api/baseresservice/holiday/queryHolidayByYear?year=<年>`

只读：不发选课、不提交评价、不改任何东西。所有请求串行，不做并发。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from noticehub.tongji.session import TongjiAuthError, TongjiError, _NoRedirect

BASE_URL = "https://1.tongji.edu.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
# 只给排障 / E2E 用；写进 .env 会把凭据发到别的主机。
BASE_ENV = "NOTICEHUB_TONGJI_JWC_BASE"


def resolve_base() -> str:
    import os

    override = str(os.environ.get(BASE_ENV) or "").strip()
    if not override:
        return BASE_URL
    if not override.lower().startswith(("http://", "https://")):
        raise ValueError(f"{BASE_ENV} 必须以 http:// 或 https:// 开头，收到：{override!r}")
    return override.rstrip("/")


class JwcClient:
    """最小只读客户端：一个 sessionid 打到底。"""

    def __init__(self, session_id: str, *, base: str = "", timeout: float = 15.0, logger: Any = None) -> None:
        self.session_id = str(session_id or "").strip()
        self.base = (base or resolve_base()).rstrip("/")
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.logger = logger or logging.getLogger(__name__)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT,
            "X-Token": self.session_id,
            "Referer": self.base + "/",
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.session_id:
            raise TongjiAuthError("教务会话未配置（需要浏览器里的 sessionid）")
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise TongjiAuthError("教务会话已失效（被重定向到登录页）") from exc
            if exc.code in (401, 403):
                raise TongjiAuthError("教务会话已失效或无权访问（HTTP %s）" % exc.code) from exc
            raise TongjiError("教务 HTTP %s：%s" % (exc.code, path)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TongjiError("教务请求失败：%s（%s）" % (path, exc)) from exc
        text = body.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            head = text[:400].lower()
            if "login" in head or "actionauthchain" in head:
                raise TongjiAuthError("教务返回登录页，会话已失效")
            raise TongjiError("教务响应不是 JSON：%s" % path)

    # -- 资源 -------------------------------------------------------------

    def session_user(self) -> dict[str, Any]:
        payload = self.get("/api/sessionservice/session/getSessionUser")
        data = _unwrap(payload)
        return data if isinstance(data, dict) else {}

    def validate_login(self) -> dict[str, Any]:
        user = self.session_user()
        if not user:
            raise TongjiAuthError("教务会话无效（拿不到用户信息）")
        return user

    def current_term(self) -> dict[str, Any]:
        data = _unwrap(self.get("/api/baseresservice/schoolCalendar/currentTermCalendar", {"flag": 0}))
        return data if isinstance(data, dict) else {}

    def term_list(self) -> list[dict[str, Any]]:
        data = _unwrap(self.get("/api/baseresservice/schoolCalendar/list"))
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def timetable(self, calendar_id: Any) -> list[dict[str, Any]]:
        data = _unwrap(self.get(
            "/api/electionservice/reportManagement/findStudentTimetab",
            {"calendarId": calendar_id},
        ))
        rows: list[dict[str, Any]] = []
        if isinstance(data, list):
            rows = [row for row in data if isinstance(row, dict)]
        elif isinstance(data, dict):
            for key in ("list", "timetable", "courseList", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    rows = [row for row in value if isinstance(row, dict)]
                    break
        return rows

    def grades(self, student_id: Any) -> list[dict[str, Any]]:
        data = _unwrap(self.get(
            "/api/scoremanagementservice/scoreGrades/getMyGrades",
            {"studentId": student_id},
        ))
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("list", "grades", "scoreList"):
                value = data.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
        return []

    def holidays(self, year: int) -> list[dict[str, Any]]:
        data = _unwrap(self.get("/api/baseresservice/holiday/queryHolidayByYear", {"year": year}))
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _unwrap(payload: Any) -> Any:
    """1 系统的信封：`{code:200, data:{...}}`；也见过直接给 data 的。"""
    if isinstance(payload, dict) and "data" in payload:
        code = payload.get("code", payload.get("errcode"))
        if code is not None and str(code) not in ("200", "0", "A00000"):
            message = payload.get("msg") or payload.get("message") or ""
            if "登录" in str(message) or "session" in str(message).lower():
                raise TongjiAuthError("教务会话已失效：%s" % message)
            raise TongjiError("教务接口返回失败（code=%s）：%s" % (code, message))
        return payload.get("data")
    return payload


# -- 课表 → 日历 -----------------------------------------------------------

# 1 系统课表里的节次编号 → 上课时间。同济四平路校区作息：第 1 节 08:00 起。
PERIOD_START = {
    1: (8, 0), 2: (8, 55), 3: (10, 0), 4: (10, 55),
    5: (13, 30), 6: (14, 25), 7: (15, 30), 8: (16, 25),
    9: (18, 30), 10: (19, 25), 11: (20, 20), 12: (21, 15),
}
PERIOD_MINUTES = 45
_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def parse_week_range(text: str) -> tuple[int, int] | None:
    """「1-16周」「3-15周(单)」「第1-8周」→ (起, 止)。"""
    match = re.search(r"(\d{1,2})\s*[-~到至]\s*(\d{1,2})\s*周", str(text or ""))
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"第?\s*(\d{1,2})\s*周", str(text or ""))
    if match:
        return int(match.group(1)), int(match.group(1))
    return None


def parse_weekday(text: str) -> int | None:
    match = re.search(r"(?:周|星期)([一二三四五六日天])", str(text or ""))
    if match:
        return _WEEKDAY_CN.get(match.group(1))
    return None


def parse_periods(text: str) -> tuple[int, int] | None:
    """「1-2节」「第3节」「5-6」→ (起, 止)。"""
    match = re.search(r"(\d{1,2})\s*[-~到至]\s*(\d{1,2})\s*节", str(text or ""))
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"第?\s*(\d{1,2})\s*节", str(text or ""))
    if match:
        return int(match.group(1)), int(match.group(1))
    match = re.search(r"(\d{1,2})\s*[-~]\s*(\d{1,2})", str(text or ""))
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def session_rows(timetable: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把课表行拍平成「每一次课」，字段尽量从多种命名里取。"""
    rows: list[dict[str, Any]] = []
    for course in timetable:
        name = _first(course.get("courseName"), course.get("name"), course.get("courseLable"))
        # roomLable 是官方的拼写错误，别改。
        room = _first(course.get("roomIdI18n"), course.get("roomLable"), course.get("roomName"))
        teacher = _first(course.get("teacherName"), course.get("teacherNames"), course.get("teacher"))
        time_text = _first(
            course.get("timeAndPlace"), course.get("classTime"),
            course.get("timeText"), course.get("periodText"),
        )
        weekdays = [parse_weekday(time_text)]
        periods = [parse_periods(time_text)]
        # 有的返回体把上课时间拆成数组：{dayOfWeek: 3, startUnit: 1, endUnit: 2, week: "1-16"}
        if course.get("dayOfWeek") is not None:
            weekdays = [int(course.get("dayOfWeek")) - 1 if int(course.get("dayOfWeek")) > 0 else 0]
        if course.get("startUnit") is not None:
            periods = [(int(course.get("startUnit")), int(course.get("endUnit") or course.get("startUnit")))]
        weeks = parse_week_range(_first(course.get("weekText"), course.get("week"), time_text))
        for weekday in weekdays:
            for period in periods:
                if weekday is None or period is None:
                    continue
                rows.append({
                    "course": name or "(未命名课程)",
                    "room": room,
                    "teacher": teacher,
                    "weekday": weekday,
                    "period_start": period[0],
                    "period_end": period[1],
                    "weeks": weeks,
                    "raw": time_text,
                })
    return rows


def _term_start(term: dict[str, Any]) -> dt.date | None:
    for key in ("startDate", "startTime", "beginDate", "schoolYearStart"):
        value = term.get(key)
        parsed = _to_date(value)
        if parsed:
            return parsed
    return None


def _to_date(value: Any) -> dt.date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e11:
            number /= 1000.0
        try:
            return dt.datetime.fromtimestamp(number).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def expand_sessions(rows: list[dict[str, Any]], term_start: dt.date | None, *, max_weeks: int = 20) -> list[dict[str, Any]]:
    """把「每周几、第几节、第几周」展开成具体日期时间。"""
    if term_start is None:
        return []
    # 学期第一周的周一
    first_monday = term_start - dt.timedelta(days=term_start.weekday())
    events: list[dict[str, Any]] = []
    for row in rows:
        weeks = row.get("weeks") or (1, max_weeks)
        start_week, end_week = weeks
        start_week = max(1, start_week)
        end_week = min(max_weeks, max(start_week, end_week))
        for week in range(start_week, end_week + 1):
            day = first_monday + dt.timedelta(days=(week - 1) * 7 + row["weekday"])
            begin = PERIOD_START.get(row["period_start"])
            if begin is None:
                continue
            start_at = dt.datetime.combine(day, dt.time(*begin))
            span = max(1, row["period_end"] - row["period_start"] + 1)
            end_at = start_at + dt.timedelta(minutes=PERIOD_MINUTES * span)
            events.append({
                "course": row["course"],
                "room": row.get("room") or "",
                "teacher": row.get("teacher") or "",
                "start": start_at,
                "end": end_at,
                "week": week,
            })
    events.sort(key=lambda item: item["start"])
    return events


def render_timetable_ics(events: list[dict[str, Any]], *, calendar_name: str = "同济课表") -> str:
    """课表 → ics。手机日历直接订阅，不占待办台。"""
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")

    def fold(text: str) -> str:
        return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TongjiHub//Timetable//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + fold(calendar_name),
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for index, event in enumerate(events):
        summary = event["course"] + ((" @" + event["room"]) if event.get("room") else "")
        description = " ".join(part for part in (event.get("teacher"), event.get("room")) if part)
        lines.extend([
            "BEGIN:VEVENT",
            "UID:tongji-class-%s-%d@noticehub" % (event["start"].strftime("%Y%m%d%H%M"), index),
            "DTSTAMP:" + stamp,
            "DTSTART;TZID=Asia/Shanghai:" + event["start"].strftime("%Y%m%dT%H%M%S"),
            "DTEND;TZID=Asia/Shanghai:" + event["end"].strftime("%Y%m%dT%H%M%S"),
            "SUMMARY:" + fold(summary),
            "DESCRIPTION:" + fold(description),
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# -- 成绩 → 待办记录 -------------------------------------------------------


def grade_records(grades: list[dict[str, Any]], *, student_name: str = "") -> list[dict[str, str]]:
    """每条新成绩一条记录；msg_id 用课程+成绩，重复抓不会重复报。"""
    from noticehub.platform.timeutil import iso, now_local

    records: list[dict[str, str]] = []
    stamp = iso(now_local())
    for row in grades:
        course = _first(row.get("courseName"), row.get("courseLable"), row.get("name"))
        score = _first(row.get("score"), row.get("scoreName"), row.get("gradePoint"))
        if not course or not score:
            continue
        credit = _first(row.get("credit"), row.get("courseCredit"))
        point = _first(row.get("gradePoint"), row.get("gpa"))
        lines = [f"成绩公布：{course}", f"分数：{score}"]
        if credit:
            lines.append("学分：" + credit)
        if point:
            lines.append("绩点：" + point)
        lines.append("来源：教务 1 系统")
        records.append({
            "msg_id": "tongji:grade:%s:%s" % (course, score),
            "source": "tongji",
            "event": "GRADE_PUBLISHED",
            "group_id": "jwc",
            "group_name": "教务 1 系统",
            "sender_id": "",
            "sender_name": student_name or "同济教务",
            "ts": stamp,
            "received_at": stamp,
            "content": "\n".join(lines),
        })
    return records
