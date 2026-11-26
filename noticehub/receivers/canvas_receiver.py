"""Canvas LMS 只读抓取器：作业 / 公告 / 讨论里的截止时间 → 入站记录。

抓取核心从 CanvasDDL（`CanvasDDL-v2.3/canvas_ddl.py`）移植：那边是自带 PowerShell
界面的独立桌面程序，这里只保留「读 Canvas、认出 DDL」这一段，产出与超星/钉钉一样的
记录，入库、待办、月历、推送交给济务台。

和超星那边一样：只读、低频、凭 Cookie，不碰任何写接口。
"""
from __future__ import annotations

import datetime as dt
import html as html_module
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from noticehub.platform.endpoints import CANVAS_DEFAULT_BASE_URL
from noticehub.platform.timeutil import iso, now_local
from noticehub.qq.messages import CANVAS_COOKIE_EXPIRED
from noticehub.receivers.ddl_sync import (
    DeadlineAuthError,
    DeadlineSourceError,
    DeadlineSyncReader,
    source_status,
)

DEFAULT_BASE_URL = CANVAS_DEFAULT_BASE_URL
USER_AGENT = "notice-hub-canvas/0.1 (read-only)"
POLL_INTERVAL_SECONDS = 1800
# 每门课最多看这么多条公告/讨论（正文最耗时，一条一次请求）。
TOPIC_SCAN_LIMIT = 20
# 公告只回看这么久以内的，太老的不再解析正文。
ANNOUNCEMENT_LOOKBACK_DAYS = 60
# 只报这么久以内的 DDL；已经过期但在宽限期内的仍然报一次。
HORIZON_DAYS = 120
OVERDUE_GRACE_DAYS = 3
# 公告正文里没写年份的日期，落后超过这个天数就认为说的是明年。
YEAR_UNKNOWN_ROLLFORWARD_DAYS = 45
TOPIC_WORKERS = 4

# 仅供 E2E：把请求指到本地假服务。**不要写进 .env**——那是用户配置，
# 写错会把真实 Cookie 发到别的地址。未设置时行为与写死的主机完全一致。
CANVAS_BASE_ENV = "NOTICEHUB_CANVAS_BASE"

DEADLINE_HINTS = ("截止", "截至", "ddl", "deadline", "之前", "前提交", "前交", "上交", "提交时间", "前完成")
DATE_PAT = re.compile(r"(?:(?P<y>20\d{2})\s*[年/\-])?\s*(?P<m>\d{1,2})\s*[月/\-]\s*(?P<d>\d{1,2})\s*日?")
TIME_PAT = re.compile(r"(?P<H>\d{1,2})\s*[:：]\s*(?P<M>\d{2})")
# 相对时间表达：解析得了的换算成绝对时间，解析不了的留给人工确认。
RELATIVE_PAT = re.compile(
    r"(明天|后天|今天|本周[一二三四五六日天]?|这周|下周[一二三四五六日天]?|"
    r"下个月|月底|月末|月初|第\s*\d+\s*周|本学期第\s*\d+\s*周)"
)
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


class CanvasError(DeadlineSourceError):
    """Canvas 客户端可预期的失败。"""


class CanvasHTTPError(CanvasError):
    """非 2xx 响应。status 用来判断值不值得重试。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = int(status or 0)


class CanvasAuthError(DeadlineAuthError):
    """Cookie 缺失，或被重定向到登录页。"""


def resolve_base_url(configured: str = "") -> str:
    """决定要访问哪个 Canvas 主机；环境变量只给 E2E 用。"""
    override = str(os.environ.get(CANVAS_BASE_ENV) or "").strip()
    if override:
        if not override.lower().startswith(("http://", "https://")):
            raise ValueError(f"{CANVAS_BASE_ENV} 必须以 http:// 或 https:// 开头，收到：{override!r}")
        return override.rstrip("/")
    base = str(configured or "").strip() or DEFAULT_BASE_URL
    if not base.lower().startswith(("http://", "https://")):
        raise ValueError(f"Canvas 地址必须以 http:// 或 https:// 开头，收到：{base!r}")
    return base.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随跳转：Canvas 用 302 把人踢去统一认证，跟过去只会拿到登录页 HTML。"""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class CanvasClient:
    """最小只读客户端：访问令牌或 Cookie 认证 + 分页 + 三类资源。

    优先用个人访问令牌（`Authorization: Bearer`）：新版 Chrome/Edge 的 Cookie 是
    v20 应用级加密，自动读取这条路在浏览器更新后就走不通了，令牌才是长期方案。
    """

    def __init__(self, base_url: str, *, cookie: str = "", token: str = "", timeout: float = 15.0, logger: Any = None) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise ValueError("Canvas 地址不能为空")
        self.cookie = str(cookie or "").strip()
        self.token = str(token or "").strip()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.logger = logger or logging.getLogger(__name__)
        self._opener = urllib.request.build_opener(_NoRedirect())

    # -- 底层 -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        # 令牌优先：Cookie 模式下不叠加 Bearer，过期的令牌会盖掉有效的浏览器会话。
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        elif self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _fetch(self, url: str) -> tuple[int, str, dict[str, str]]:
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                headers = {str(k): str(v) for k, v in response.headers.items()}
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise CanvasAuthError("Canvas 会话已失效（被重定向到登录页），请重新登录后更新 Cookie") from exc
            if exc.code in (401, 403):
                raise CanvasAuthError("Canvas 登录已失效或无权访问（HTTP %s）" % exc.code) from exc
            raise CanvasHTTPError("Canvas HTTP %s：%s" % (exc.code, url), status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CanvasError("Canvas 请求失败：%s（%s）" % (url, exc)) from exc
        return status, body, headers

    @staticmethod
    def _next_link(link_header: str) -> str:
        for part in str(link_header or "").split(","):
            segments = part.split(";")
            if len(segments) >= 2 and 'rel="next"' in segments[1]:
                return segments[0].strip().strip("<>")
        return ""

    def _fetch_with_retry(self, url: str) -> tuple[int, str, dict[str, str]]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self._fetch(url)
            except DeadlineAuthError:
                raise
            except CanvasHTTPError as exc:
                last_error = exc
                if exc.status == 429 or 500 <= exc.status < 600:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise
            except CanvasError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise
        raise CanvasError("Canvas 请求重试后仍失败：%s（%s）" % (url, last_error))

    def api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET 一个 Canvas API 路径，自动跟随分页；列表接口返回合并后的列表。"""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        origin = urllib.parse.urlsplit(self.base_url).netloc
        items: list[Any] = []
        page = 0
        while url and page < 40:
            page += 1
            if urllib.parse.urlsplit(url).netloc != origin:
                raise CanvasError("分页链接跳到了其他域名，已阻止：%s" % url)
            _status, body, headers = self._fetch_with_retry(url)
            text = body.strip()
            data = json.loads(text) if text else []
            if not isinstance(data, list):
                return data
            items.extend(data)
            url = self._next_link(headers.get("Link", ""))
        return items

    # -- 资源 -------------------------------------------------------------

    def profile(self) -> dict[str, Any]:
        payload = self.api_get("/api/v1/users/self/profile")
        return payload if isinstance(payload, dict) else {}

    def courses(self) -> list[dict[str, Any]]:
        payload = self.api_get(
            "/api/v1/courses",
            {"enrollment_state": "active", "per_page": 100, "include[]": ["term"]},
        )
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def assignments(self, course_id: str) -> list[dict[str, Any]]:
        payload = self.api_get(
            "/api/v1/courses/%s/assignments" % course_id,
            {"per_page": 100, "include[]": ["submission"]},
        )
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def discussion_topics(self, course_id: str, *, only_announcements: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": 50 if only_announcements else 100}
        if only_announcements:
            params["only_announcements"] = "true"
        payload = self.api_get("/api/v1/courses/%s/discussion_topics" % course_id, params)
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def discussion(self, course_id: str, topic_id: str) -> dict[str, Any]:
        payload = self.api_get("/api/v1/courses/%s/discussion_topics/%s" % (course_id, topic_id))
        return payload if isinstance(payload, dict) else {}

    def validate_login(self) -> None:
        """能读到自己的 profile 就算登录有效。"""
        if not self.token and not self.cookie:
            raise CanvasAuthError("Canvas 登录信息未配置")
        self.profile()


# -- 纯解析：从公告正文里认出截止时间 -------------------------------------


def parse_dt(value: Any) -> dt.datetime | None:
    from noticehub.domain.time import to_naive_local

    return to_naive_local(value)


def clean_course_name(raw: str) -> str:
    """课程名末尾常带教务课程号（如「某课程 10220403」），显示时去掉。"""
    text = str(raw or "").strip()
    return re.sub(r"\s+\d{4,}\s*$", "", text).strip() or text


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(raw or ""))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text).replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def find_relative_times(text: str) -> list[str]:
    """找出正文里的相对时间词，只保留带动作词的句子片段。"""
    actions = ("提交", "交", "截止", "携带", "前", "上传", "发送")
    found: list[str] = []
    for match in RELATIVE_PAT.finditer(str(text or "")):
        snippet = re.sub(r"\s+", " ", text[max(0, match.start() - 18):match.end() + 18]).strip()
        if not any(action in snippet for action in actions):
            continue
        if snippet not in found:
            found.append(snippet)
    return found[:2]


def resolve_relative_times(text: str, base: dt.datetime) -> list[tuple[dt.datetime, str, str]]:
    """把「明天」「下周三」「月底」换算成绝对时间；基准是发布时间，不是现在。"""
    resolved: list[tuple[dt.datetime, str, str]] = []
    seen: set[str] = set()

    def nearby_clock(start: int, end: int) -> tuple[int, int]:
        match = TIME_PAT.search(text[end:end + 20])
        if match:
            hour, minute = int(match.group("H")), int(match.group("M"))
            if hour <= 23 and minute <= 59:
                return hour, minute
        return 23, 59

    def add(target: dt.datetime, word: str, start: int, end: int) -> None:
        hour, minute = nearby_clock(start, end)
        due = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
        key = due.strftime("%Y-%m-%d %H:%M")
        if key in seen:
            return
        seen.add(key)
        snippet = re.sub(r"\s+", " ", text[max(0, start - 22):end + 22]).strip()
        resolved.append((due, snippet, word))

    for match in RELATIVE_PAT.finditer(text):
        word = match.group(0)
        target: dt.datetime | None = None
        if word in ("今天", "今日"):
            target = base
        elif word == "明天":
            target = base + dt.timedelta(days=1)
        elif word == "后天":
            target = base + dt.timedelta(days=2)
        elif word in ("月底", "月末"):
            first_of_next = (base.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            target = first_of_next - dt.timedelta(days=1)
        elif word == "月初":
            target = base.replace(day=1)
        elif word.startswith(("本周", "这周")):
            tail = word[-1]
            if tail in _WEEKDAY:
                target = base + dt.timedelta(days=_WEEKDAY[tail] - base.weekday())
        elif word.startswith("下周"):
            tail = word[-1]
            if tail in _WEEKDAY:
                next_monday = base + dt.timedelta(days=7 - base.weekday())
                target = next_monday + dt.timedelta(days=_WEEKDAY[tail])
        if target is not None:
            add(target, word, match.start(), match.end())
    return resolved


def extract_deadlines(text: str, now: dt.datetime, *, lookback: int = YEAR_UNKNOWN_ROLLFORWARD_DAYS) -> list[tuple[dt.datetime, str]]:
    """从一段中文正文里找出「截止」类日期时间。

    只看日期附近（前 45 / 后 30 字）出现截止关键词的匹配，避免把正文里
    无关的日期当成 DDL；没写时间的按当天 23:59 处理。
    """
    found: list[tuple[dt.datetime, str]] = []
    seen: set[str] = set()
    text = re.sub(r"https?://\S+", " ", str(text or ""))
    for match in DATE_PAT.finditer(text):
        month, day = int(match.group("m")), int(match.group("d"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        context = text[max(0, match.start() - 45):match.end() + 30].lower()
        if not any(hint in context for hint in DEADLINE_HINTS):
            continue
        hour, minute = 23, 59
        clock = TIME_PAT.search(text[match.end():match.end() + 24])
        if clock:
            hour, minute = int(clock.group("H")), int(clock.group("M"))
            if hour > 23 or minute > 59:
                hour, minute = 23, 59
        year = int(match.group("y")) if match.group("y") else now.year
        try:
            due = dt.datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        if due < now - dt.timedelta(days=lookback):
            try:
                due = due.replace(year=due.year + 1)
            except ValueError:
                continue
        key = due.strftime("%Y-%m-%d %H:%M")
        if key in seen:
            continue
        seen.add(key)
        snippet = re.sub(r"\s+", " ", text[max(0, match.start() - 30):match.end() + 30]).strip()
        found.append((due, snippet))
    return found


def _is_submitted(submission: Any) -> bool:
    """作业是否已交：submitted_at / 状态 / 免交，任一命中都算。"""
    if not isinstance(submission, dict):
        return False
    state = str(submission.get("workflow_state") or "")
    return bool(
        submission.get("submitted_at")
        or state in ("submitted", "graded", "pending_review")
        or submission.get("excused")
    )


def due_of_assignment(assignment: dict[str, Any]) -> dt.datetime | None:
    """老师常只填「可供使用的截止日期」(lock_at)，界面上照样算截止，两个都认。"""
    return parse_dt(assignment.get("due_at")) or parse_dt(assignment.get("lock_at"))


def _due_text(due: dt.datetime) -> str:
    return due.strftime("%Y-%m-%d %H:%M")


def to_record(
    course_id: str,
    course_name: str,
    item_key: str,
    title: str,
    due: dt.datetime,
    *,
    kind: str = "作业",
    url: str = "",
    body: str = "",
    event: str = "WORK_DUE",
    received_at: str | None = None,
) -> dict[str, str]:
    """一条 DDL → 济务台认的入站记录；msg_id 稳定，重复抓取不会重复建待办。"""
    stamp = received_at or iso(now_local())
    lines = [f"{course_name} · {title}".strip(" ·"), "截止时间：" + _due_text(due), "来源：" + kind]
    cleaned = re.sub(r"\s+", " ", str(body or "")).strip()
    if cleaned:
        lines.append(cleaned[:200])
    if url:
        lines.append("链接：" + url)
    return {
        "msg_id": f"canvas:{course_id}:{item_key}",
        "source": "canvas",
        "event": event,
        "group_id": str(course_id),
        "group_name": str(course_name),
        "sender_id": "",
        "sender_name": "Canvas",
        "ts": stamp,
        "received_at": stamp,
        "content": "\n".join(lines),
        # 内部字段：本轮认出来的截止时间文本，用来比对改期；投递前会被 pop 掉。
        "_due": _due_text(due),
    }


def canvas_status(settings: Any, store: Any = None) -> dict[str, Any]:
    """给网页用的同步状态；不含任何凭据。"""
    payload = source_status(
        "canvas",
        settings,
        store,
        label="Canvas",
        banner=CANVAS_COOKIE_EXPIRED,
        login_url=str(getattr(settings, "canvas_base_url", "") or DEFAULT_BASE_URL),
    )
    payload["base_url"] = str(getattr(settings, "canvas_base_url", "") or DEFAULT_BASE_URL)
    payload["has_token"] = bool(getattr(settings, "canvas_token", ""))
    return payload


class CanvasReader(DeadlineSyncReader):
    """低频只读轮询：作业 + 公告/讨论正文里的 DDL，一起变成入站记录。"""

    source = "canvas"
    label = "Canvas"
    banner = CANVAS_COOKIE_EXPIRED
    interval_key = "canvas_scan_interval_seconds"
    default_interval = POLL_INTERVAL_SECONDS

    # -- 客户端 -----------------------------------------------------------

    def has_credentials(self) -> bool:
        return bool(self._cookie() or str(getattr(self.settings, "canvas_token", "") or ""))

    def build_client(self) -> CanvasClient:
        base = resolve_base_url(getattr(self.settings, "canvas_base_url", ""))
        return CanvasClient(
            base,
            cookie=self._cookie(),
            token=str(getattr(self.settings, "canvas_token", "") or ""),
            timeout=float(getattr(self.settings, "http_timeout", 15) or 15),
            logger=self.logger,
        )

    def validate_login(self, client: CanvasClient) -> None:
        client.validate_login()

    # -- 采集 -------------------------------------------------------------

    def _course_whitelist(self) -> set[str]:
        return {
            str(value).strip()
            for value in (getattr(self.settings, "canvas_course_whitelist", ()) or ())
            if str(value).strip()
        }

    def collect(self, client: CanvasClient) -> list[dict[str, Any]]:
        """扫一遍所有课程，返回本轮看到的全部 DDL 记录（已按 msg_id 去重）。"""
        now = now_local()
        horizon = now + dt.timedelta(days=HORIZON_DAYS)
        floor = now - dt.timedelta(days=OVERDUE_GRACE_DAYS)
        whitelist = self._course_whitelist()
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        def add(record: dict[str, Any]) -> None:
            if record["msg_id"] in seen_ids:
                return
            seen_ids.add(record["msg_id"])
            records.append(record)

        for course in client.courses():
            course_id = str(course.get("id") or "").strip()
            if not course_id:
                continue
            if whitelist and course_id not in whitelist:
                continue
            name = clean_course_name(course.get("name") or course.get("course_code") or course_id)

            try:
                assignments = client.assignments(course_id)
            except DeadlineAuthError:
                raise
            except CanvasError as exc:
                self.logger.warning("Canvas 跳过课程 %s 作业：%s", name, exc)
                assignments = []

            for assignment in assignments:
                due = due_of_assignment(assignment)
                if due is None or not (floor <= due <= horizon):
                    continue
                if _is_submitted(assignment.get("submission")):
                    continue
                add(to_record(
                    course_id,
                    name,
                    "a%s" % (assignment.get("id") or ""),
                    str(assignment.get("name") or "(未命名作业)").strip(),
                    due,
                    kind="作业",
                    url=str(assignment.get("html_url") or ""),
                    body=html_to_text(assignment.get("description") or ""),
                ))

            for record in self._announcement_records(client, course_id, name, now, horizon, floor):
                add(record)
        return records

    def _announcement_records(
        self,
        client: CanvasClient,
        course_id: str,
        course_name: str,
        now: dt.datetime,
        horizon: dt.datetime,
        floor: dt.datetime,
    ) -> list[dict[str, Any]]:
        """公告与讨论：正文里写了截止时间的，也当 DDL。"""
        topics: list[tuple[dict[str, Any], str]] = []
        try:
            topics.extend((row, "公告") for row in client.discussion_topics(course_id, only_announcements=True))
        except DeadlineAuthError:
            raise
        except CanvasError as exc:
            self.logger.warning("Canvas 跳过课程 %s 公告：%s", course_name, exc)
        try:
            topics.extend((row, "讨论") for row in client.discussion_topics(course_id))
        except DeadlineAuthError:
            raise
        except CanvasError as exc:
            self.logger.warning("Canvas 跳过课程 %s 讨论：%s", course_name, exc)

        wanted: list[tuple[dict[str, Any], str, dt.datetime | None]] = []
        for topic, kind in topics[:TOPIC_SCAN_LIMIT]:
            posted = parse_dt(topic.get("posted_at"))
            if posted and (now - posted).days > ANNOUNCEMENT_LOOKBACK_DAYS:
                continue
            wanted.append((topic, kind, posted))

        details: dict[str, dict[str, Any]] = {}
        if wanted:
            def grab(topic_id: str) -> tuple[str, dict[str, Any] | None]:
                try:
                    return topic_id, client.discussion(course_id, topic_id)
                except DeadlineAuthError:
                    raise
                except CanvasError:
                    return topic_id, None

            with ThreadPoolExecutor(max_workers=TOPIC_WORKERS, thread_name_prefix="canvas-topic") as pool:
                for topic_id, payload in pool.map(grab, [str(topic.get("id")) for topic, _kind, _posted in wanted]):
                    if payload is not None:
                        details[topic_id] = payload

        found: list[dict[str, Any]] = []
        for topic, kind, posted in wanted:
            detail = details.get(str(topic.get("id")))
            if not detail:
                continue
            text = html_to_text(detail.get("message") or "")
            if not text:
                continue
            title = str(topic.get("title") or detail.get("title") or "(无标题)").strip()
            url = str(topic.get("html_url") or "")
            absolute = extract_deadlines(text, now)
            absolute_times = [due for due, _snippet in absolute]
            for due, _snippet in absolute:
                if not (floor <= due <= horizon):
                    continue
                found.append(to_record(course_id, course_name, "t%s" % topic.get("id"), title, due, kind=kind, url=url, body=text))
            for due, _snippet, _word in resolve_relative_times(text, posted or now):
                if not (floor <= due <= horizon):
                    continue
                if any(abs((due - other).total_seconds()) < 86400 for other in absolute_times):
                    continue  # 同一公告已有绝对日期，不重复报
                found.append(to_record(course_id, course_name, "t%s" % topic.get("id"), title, due, kind=kind, url=url, body=text))
        return found


def main(argv: list[str] | None = None) -> int:
    """手工排障：把一轮抓取结果打印出来，不投递、不写库。"""
    import argparse

    parser = argparse.ArgumentParser(description="打印一轮 Canvas 抓取结果（只读）")
    parser.add_argument("--cookie-file", help="含 Cookie 头的 UTF-8 文件")
    parser.add_argument("--base", default="", help="Canvas 地址，默认用内置值")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cookie = ""
    if args.cookie_file:
        from pathlib import Path

        cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    reader = CanvasReader(_ProbeSettings(cookie, args.base), lambda record: True)
    for record in reader.collect(reader.build_client())[: max(1, args.limit)]:
        print(record["msg_id"], "|", record["content"].replace("\n", " / "))
    return 0


class _ProbeSettings:
    """给 main() 用的最小 Settings 替身：只有抓取要用的几个字段。"""

    canvas_cookie = ""
    canvas_base_url = DEFAULT_BASE_URL
    canvas_course_whitelist: tuple[str, ...] = ()
    canvas_scan_interval_seconds = POLL_INTERVAL_SECONDS
    http_timeout = 15

    def __init__(self, cookie: str = "", base: str = "") -> None:
        self.canvas_cookie = cookie
        self.canvas_base_url = base or DEFAULT_BASE_URL


if __name__ == "__main__":
    raise SystemExit(main())
