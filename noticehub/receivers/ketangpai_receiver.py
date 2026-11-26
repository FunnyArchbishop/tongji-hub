"""课堂派只读抓取器：统一待办里的作业/考试/公告 → 入站记录。

接口面来自对前端 bundle 与两个开源实现的静态分析（见 `docs/wiki/config.md`）：

* 接口主机 `https://openapiv5.ketangpai.com/`（前端 `baseURL`，不是 www 那个域名）
* 鉴权是请求头 `token`（同值存在 cookie `token` 里），密码登录不需要验证码
* 首选 `POST /Futurev2/Todo/getTodoList`：一个接口给全课程名、标题、类型、截止时间
* 截止时间 `endtime` 是 **Unix 秒**；`0` 或 `""` 表示不限
* 兜底 `POST /FutureV2/CourseMeans/getCourseContent`（contenttype 4=作业 / 6=考试 / 7=公告）

只读：不发提交、不签到、不改任何状态。
"""
from __future__ import annotations

import datetime as dt
import html as html_module
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

from noticehub.platform.endpoints import KETANGPAI_BASE_URL
from noticehub.platform.timeutil import iso, now_local, to_naive_local
from noticehub.qq.messages import KETANGPAI_COOKIE_EXPIRED
from noticehub.receivers.ddl_sync import (
    DeadlineAuthError,
    DeadlineSourceError,
    DeadlineSyncReader,
    source_status,
)

# 前端 axios 的 baseURL。openapiv5 是当前版本；平台升级过主机名（openapiv100 关过），
# 所以留了环境变量口子，只给排障和 E2E 用。
API_BASE = "https://openapiv5.ketangpai.com"
API_BASE_ENV = "NOTICEHUB_KETANGPAI_API"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
POLL_INTERVAL_SECONDS = 1800
HORIZON_DAYS = 120
OVERDUE_GRACE_DAYS = 3
TODO_PAGE_SIZE = 50
TODO_MAX_PAGES = 6
# contenttype 语义（前端图标映射表）：4=作业 6=考试 7=公告。其余不动。
CONTENT_TYPE_LABELS = {"4": "作业", "6": "考试", "7": "公告"}
CONTENT_TYPE_ORDER = ("4", "6", "7")
# 待办里算「还没做完」的状态：timestate 1=将于开始 2=提交截止 3=已结束。
FINISHED_TIMESTATES = {"3"}
# 已提交/已完成的取值，出现这些就不再当待办。
FINISHED_FLAGS = {"1", "2", "true", "True", "已提交", "已完成"}


class KetangpaiError(DeadlineSourceError):
    """课堂派客户端可预期的失败。"""


class KetangpaiAuthError(DeadlineAuthError):
    """token 缺失或已失效。"""


def resolve_api_base() -> str:
    override = str(os.environ.get(API_BASE_ENV) or "").strip()
    if not override:
        return API_BASE
    if not override.lower().startswith(("http://", "https://")):
        raise ValueError(f"{API_BASE_ENV} 必须以 http:// 或 https:// 开头，收到：{override!r}")
    return override.rstrip("/")


def extract_token(cookie: str) -> str:
    """从 Cookie 头里抠出 token；用户直接粘 token 也认。"""
    raw = str(cookie or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    for part in raw.split(";"):
        name, separator, value = part.partition("=")
        if separator and name.strip().lower() == "token":
            return value.strip()
    # 没有分号、也没有 token= 前缀：当作裸 token。
    if ";" not in raw and "=" not in raw:
        return raw
    return ""


def parse_endtime(value: Any) -> dt.datetime | None:
    """endtime → 本地 naive 时间。0 / 空串 / 非法值都当「不限」。

    平台给的是 Unix **秒**；万一以后改成毫秒，按量级识别，不猜。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not re.fullmatch(r"-?\d+(\.\d+)?", text):
            # 极少数接口会直接给日期字符串，能解析就用。
            return to_naive_local(text)
        number = float(text)
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    if number <= 0:
        return None
    if number > 1e11:  # 毫秒
        number /= 1000.0
    try:
        return dt.datetime.fromtimestamp(number)
    except (OverflowError, OSError, ValueError):
        return None


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(raw or ""))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


class KetangpaiClient:
    """最小只读客户端：token 头 + JSON POST。"""

    def __init__(self, token: str, *, base: str = "", timeout: float = 15.0, logger: Any = None) -> None:
        self.token = str(token or "").strip()
        self.base = (base or resolve_api_base()).rstrip("/")
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.logger = logger or logging.getLogger(__name__)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "token": self.token,
            "Origin": KETANGPAI_BASE_URL,
            "Referer": KETANGPAI_BASE_URL + "/",
            "User-Agent": USER_AGENT,
        }

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        """POST 一个接口，剥掉外层包装返回 data；登录失效抛 KetangpaiAuthError。"""
        payload = dict(body or {})
        # 前端拦截器每个请求都塞这个毫秒时间戳；不是签名，但不塞显得太不像浏览器。
        payload.setdefault("reqtimestamp", int(dt.datetime.now().timestamp() * 1000))
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise KetangpaiAuthError("课堂派登录已失效（HTTP %s）" % exc.code) from exc
            raise KetangpaiError("课堂派 HTTP %s：%s" % (exc.code, path)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise KetangpaiError("课堂派请求失败：%s（%s）" % (path, exc)) from exc
        return self._unwrap(text, path)

    @staticmethod
    def _unwrap(text: str, path: str) -> Any:
        """平台有两套包装：{status:1,data} 与 {code:10000,data,message}，都要认。"""
        try:
            payload = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise KetangpaiError("课堂派响应不是 JSON：%s" % path) from exc
        if not isinstance(payload, dict):
            return payload
        message = _first(payload.get("message"), payload.get("msg"), payload.get("info"))
        code = payload.get("code", payload.get("status"))
        auth_markers = ("未登录", "请登录", "登录失效", "登录已过期", "重新登录", "token")
        if str(code) == "20003" or any(marker in message for marker in auth_markers):
            raise KetangpaiAuthError("课堂派登录已失效：%s" % (message or code))
        for key in ("status", "code"):
            if key in payload:
                value = str(payload.get(key))
                # 成功标记只有这两种包装里的写法；0 不是成功标记，别放行。
                if value not in ("1", "10000", "200", "True", "true"):
                    raise KetangpaiError("课堂派接口返回失败（%s=%s）：%s" % (key, value, message or path))
                break
        if "data" in payload:
            return payload["data"]
        return payload

    # -- 资源 -------------------------------------------------------------

    @classmethod
    def login(cls, email: str, password: str, *, base: str = "", timeout: float = 15.0, logger: Any = None) -> str:
        """账号密码换 token（一次性）：密码只用这一次，调用方只该保存返回的 token。

        课堂派密码登录不需要验证码；`remember=1` 让 token 活 30 天。
        """
        client = cls("", base=base, timeout=timeout, logger=logger)
        payload = client.post(
            "/UserApi/login",
            {"email": str(email or "").strip(), "password": str(password or ""), "remember": "1", "source_type": 1},
        )
        token = ""
        if isinstance(payload, dict):
            token = _first(payload.get("token"), payload.get("xktptoken"), (payload.get("user") or {}).get("token") if isinstance(payload.get("user"), dict) else "")
        if not token:
            raise KetangpaiError("课堂派登录没有返回 token，账号或密码可能不对")
        return token

    def validate_login(self) -> None:
        """能拿到待办列表第一页就算登录有效。"""
        if not self.token:
            raise KetangpaiAuthError("课堂派 token 未配置")
        self.todo_list(page=1, limit=1)

    def todo_list(self, *, page: int = 1, limit: int = TODO_PAGE_SIZE, todo_type: str = "", class_id: str = "") -> list[dict[str, Any]]:
        payload = self.post(
            "/Futurev2/Todo/getTodoList",
            {"page": int(page), "limit": int(limit), "type": todo_type, "classid": class_id},
        )
        return _as_rows(payload)

    def todo_menus(self, class_id: str = "") -> list[dict[str, Any]]:
        payload = self.post("/Futurev2/Todo/getTodoMenuList", {"classid": class_id})
        return _as_rows(payload)

    def courses(self) -> list[dict[str, Any]]:
        payload = self.post("/CourseApi/semesterCourseList", {"semester": "", "term": "", "isstudy": "1"})
        rows: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for key in ("lists", "toplists"):
                value = payload.get(key)
                if isinstance(value, list):
                    rows.extend(row for row in value if isinstance(row, dict))
        elif isinstance(payload, list):
            rows.extend(row for row in payload if isinstance(row, dict))
        return rows

    def course_content(self, course_id: str, content_type: str, *, page: int = 1, limit: int = 50) -> list[dict[str, Any]]:
        payload = self.post(
            "/FutureV2/CourseMeans/getCourseContent",
            {
                "courseid": str(course_id),
                "contenttype": str(content_type),
                "dirid": 0,
                "lessonlink": [],
                "sort": [],
                "page": int(page),
                "limit": int(limit),
                "desc": 3,
                "courserole": 0,
                "vtr_type": "",
            },
        )
        return _as_rows(payload)


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """把各种可能的包裹形状压成 list[dict]。"""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("list", "lists", "data", "rows", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _is_finished(item: dict[str, Any]) -> bool:
    """已结束/已提交的条目不再当 DDL。"""
    timestate = _first(item.get("timestate"), item.get("timeState"))
    if timestate in FINISHED_TIMESTATES:
        return True
    for key in ("submitState", "handupState", "submittate", "finished"):
        value = item.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        if str(value).strip() in FINISHED_FLAGS:
            return True
    return False


def _course_of(item: dict[str, Any]) -> tuple[str, str]:
    course_id = _first(item.get("courseid"), item.get("courseId"), item.get("classid"))
    course_name = _first(item.get("coursename"), item.get("courseName"), item.get("classname"), course_id)
    return course_id, course_name


def _content_type_of(item: dict[str, Any]) -> str:
    raw = _first(item.get("contenttype"), item.get("contentType"))
    return raw or "0"


def to_record(item: dict[str, Any], due: dt.datetime, *, content_type: str = "") -> dict[str, str]:
    """一条课堂派待办 → 济务台认的入站记录。"""
    stamp = iso(now_local())
    course_id, course_name = _course_of(item)
    title = _first(item.get("contenttitle"), item.get("title"), item.get("name"), "(无标题)")
    kind = CONTENT_TYPE_LABELS.get(content_type or _content_type_of(item), "待办")
    lines = [f"{course_name} · {title}".strip(" ·"), "截止时间：" + due.strftime("%Y-%m-%d %H:%M"), "来源：" + kind]
    description = html_to_text(_first(item.get("contentdescription"), item.get("description"), item.get("content")))
    if description and description != title:
        lines.append(description[:200])
    item_id = _first(item.get("contentid"), item.get("contentId"), item.get("id"), title)
    return {
        "msg_id": f"ketangpai:{course_id}:{content_type or _content_type_of(item)}:{item_id}",
        "source": "ketangpai",
        "event": "WORK_DUE",
        "group_id": str(course_id),
        "group_name": str(course_name),
        "sender_id": "",
        "sender_name": "课堂派",
        "ts": stamp,
        "received_at": stamp,
        "content": "\n".join(lines),
        "_due": due.strftime("%Y-%m-%d %H:%M"),
    }


def ketangpai_status(settings: Any, store: Any = None) -> dict[str, Any]:
    """给网页用的同步状态；不含任何凭据。"""
    payload = source_status(
        "ketangpai",
        settings,
        store,
        label="课堂派",
        banner=KETANGPAI_COOKIE_EXPIRED,
        login_url=KETANGPAI_BASE_URL,
    )
    payload["api_base"] = resolve_api_base()
    return payload


class KetangpaiReader(DeadlineSyncReader):
    """低频只读轮询：统一待办为主，取不到时按课程兜底。"""

    source = "ketangpai"
    label = "课堂派"
    banner = KETANGPAI_COOKIE_EXPIRED
    interval_key = "ketangpai_scan_interval_seconds"
    default_interval = POLL_INTERVAL_SECONDS

    def build_client(self) -> KetangpaiClient:
        token = extract_token(self._cookie())
        if not token:
            raise KetangpaiAuthError("课堂派 token 未配置")
        return KetangpaiClient(
            token,
            timeout=float(getattr(self.settings, "http_timeout", 15) or 15),
            logger=self.logger,
        )

    def validate_login(self, client: KetangpaiClient) -> None:
        client.validate_login()

    def _course_whitelist(self) -> set[str]:
        return {
            str(value).strip()
            for value in (getattr(self.settings, "ketangpai_course_whitelist", ()) or ())
            if str(value).strip()
        }

    def collect(self, client: KetangpaiClient) -> list[dict[str, Any]]:
        now = now_local()
        horizon = now + dt.timedelta(days=HORIZON_DAYS)
        floor = now - dt.timedelta(days=OVERDUE_GRACE_DAYS)
        whitelist = self._course_whitelist()
        records: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(item: dict[str, Any], content_type: str = "") -> None:
            if _is_finished(item):
                return
            course_id, _course_name = _course_of(item)
            if whitelist and course_id not in whitelist:
                return
            due = parse_endtime(item.get("endtime") if item.get("endtime") is not None else item.get("deadline"))
            if due is None or not (floor <= due <= horizon):
                return
            record = to_record(item, due, content_type=content_type)
            if record["msg_id"] in seen:
                return
            seen.add(record["msg_id"])
            records.append(record)

        rows = self._todo_rows(client)
        for item in rows:
            add(item)

        if not rows:
            # 待办接口给空（或形状变了）时按课程兜底：作业/考试/公告各扫一遍。
            for item, content_type in self._course_rows(client):
                add(item, content_type)

        records.sort(key=lambda record: record["_due"])
        return records

    def _todo_rows(self, client: KetangpaiClient) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(1, TODO_MAX_PAGES + 1):
            batch = client.todo_list(page=page, limit=TODO_PAGE_SIZE)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < TODO_PAGE_SIZE:
                break
        return rows

    def _course_rows(self, client: KetangpaiClient) -> list[tuple[dict[str, Any], str]]:
        rows: list[tuple[dict[str, Any], str]] = []
        try:
            courses = client.courses()
        except KetangpaiAuthError:
            raise
        except KetangpaiError as exc:
            self.logger.warning("课堂派课程列表读取失败，本轮跳过兜底：%s", exc)
            return rows
        whitelist = self._course_whitelist()
        for course in courses:
            course_id, _name = _course_of(course)
            if not course_id or (whitelist and course_id not in whitelist):
                continue
            for content_type in CONTENT_TYPE_ORDER:
                try:
                    batch = client.course_content(course_id, content_type)
                except KetangpaiAuthError:
                    raise
                except KetangpaiError as exc:
                    self.logger.warning("课堂派课程 %s 内容(%s)读取失败：%s", course_id, content_type, exc)
                    continue
                rows.extend((item, content_type) for item in batch)
        return rows


def main(argv: list[str] | None = None) -> int:
    """手工排障：把一轮抓取结果打印出来，不投递、不写库。"""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="打印一轮课堂派抓取结果（只读）")
    parser.add_argument("--cookie-file", help="含 Cookie 头（或裸 token）的 UTF-8 文件")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip() if args.cookie_file else ""
    reader = KetangpaiReader(_ProbeSettings(cookie), lambda record: True)
    for record in reader.collect(reader.build_client())[: max(1, args.limit)]:
        print(record["msg_id"], "|", record["content"].replace("\n", " / "))
    return 0


class _ProbeSettings:
    """给 main() 用的最小 Settings 替身：只有抓取要用的几个字段。"""

    ketangpai_cookie = ""
    ketangpai_course_whitelist: tuple[str, ...] = ()
    ketangpai_scan_interval_seconds = POLL_INTERVAL_SECONDS
    http_timeout = 15

    def __init__(self, cookie: str = "") -> None:
        self.ketangpai_cookie = cookie


if __name__ == "__main__":
    raise SystemExit(main())
