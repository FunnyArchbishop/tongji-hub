"""一网通办官方日历订阅（`https://all.tongji.edu.cn/cal/<id>`）的读取与规范化。

这是同济唯一一份官方 ics：配一次、管一学期，比抓任何页面都稳。已知缺陷是
**所有全天事件都写成 00:00–23:59 的「伪全天」**，手机日历上会显示成一条盖住整天的条，
所以要在这里把它们规范成真正的全天事件（`VALUE=DATE`）。

只读：只 GET 那一个订阅地址，不碰一网通办其它接口。
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.error
import urllib.request
from typing import Any

from noticehub.tongji.session import TongjiAuthError, TongjiError

FEED_BASE = "https://all.tongji.edu.cn/cal/"
FEED_BASE_ENV = "NOTICEHUB_TONGJI_CAL_BASE"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
# 订阅 id 只允许字母数字与短横，避免把任意 URL 拼进来。
FEED_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def resolve_feed_base() -> str:
    import os

    override = str(os.environ.get(FEED_BASE_ENV) or "").strip()
    if not override:
        return FEED_BASE
    if not override.lower().startswith(("http://", "https://")):
        raise ValueError(f"{FEED_BASE_ENV} 必须以 http:// 或 https:// 开头，收到：{override!r}")
    return override.rstrip("/") + "/"


def feed_url(feed_id: str, *, cookie: str = "") -> str:
    text = str(feed_id or "").strip()
    if not FEED_ID_PATTERN.match(text):
        raise ValueError("日历订阅编号只能是字母、数字、下划线或短横")
    return resolve_feed_base() + text


def fetch_feed(feed_id: str, *, cookie: str = "", timeout: float = 15.0) -> str:
    """取一份官方 ics 原文。需要登录态时把 Cookie 一起带上。"""
    url = feed_url(feed_id)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/calendar, text/plain, */*"}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise TongjiAuthError("日历订阅需要登录：%s（HTTP %s）" % (url, exc.code)) from exc
        if exc.code == 404:
            raise TongjiError("日历订阅不存在（HTTP 404）：%s" % url) from exc
        raise TongjiError("日历订阅拉取失败：HTTP %s" % exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TongjiError("日历订阅拉取失败：%s" % exc) from exc
    if "BEGIN:VCALENDAR" not in body:
        raise TongjiError("拉回来的不是 ics（可能被重定向到登录页）")
    return body


def unfold(raw: str) -> list[str]:
    """ics 的行折叠：以空格或制表符开头的行接上一行。"""
    lines: list[str] = []
    for line in str(raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return [line for line in lines if line != ""]


def _parse_dt(value: str) -> dt.datetime | None:
    text = str(value or "").strip().rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _is_midnight_pair(start: str, end: str) -> bool:
    """官方订阅的「伪全天」：00:00:00 开始、23:59:59 结束。"""
    begin = _parse_dt(start)
    finish = _parse_dt(end)
    if begin is None or finish is None:
        return False
    return (begin.hour, begin.minute, begin.second) == (0, 0, 0) and (finish.hour, finish.minute) == (23, 59)


def normalize(raw: str, *, calendar_name: str = "同济 · 一网通办") -> dict[str, Any]:
    """把官方 ics 拆成事件并规范化伪全天；返回统计与规范化后的 ics 正文。"""
    lines = unfold(raw)
    events: list[dict[str, Any]] = []
    current: dict[str, str] | None = None
    for line in lines:
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        name = key.split(";")[0].upper()
        current[name] = value.strip()
        current["_" + name + "_PARAMS"] = key
    normalized: list[dict[str, Any]] = []
    pseudo = 0
    for event in events:
        start_raw = event.get("DTSTART", "")
        end_raw = event.get("DTEND", "")
        if _is_midnight_pair(start_raw, end_raw):
            pseudo += 1
            begin = _parse_dt(start_raw)
            end_date = (_parse_dt(end_raw) or begin) + dt.timedelta(days=1)
            normalized.append({
                "summary": event.get("SUMMARY", ""),
                "start": begin.strftime("%Y%m%d") if begin else start_raw,
                "end": end_date.strftime("%Y%m%d"),
                "all_day": True,
                "location": event.get("LOCATION", ""),
                "description": event.get("DESCRIPTION", ""),
                "uid": event.get("UID", ""),
            })
            continue
        normalized.append({
            "summary": event.get("SUMMARY", ""),
            "start": start_raw,
            "end": end_raw,
            "all_day": "VALUE=DATE" in event.get("_DTSTART_PARAMS", "").upper(),
            "location": event.get("LOCATION", ""),
            "description": event.get("DESCRIPTION", ""),
            "uid": event.get("UID", ""),
        })
    return {
        "events": normalized,
        "total": len(events),
        "pseudo_all_day": pseudo,
        "ics": render_ics(normalized, calendar_name=calendar_name),
    }


def _fold(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def render_ics(events: list[dict[str, Any]], *, calendar_name: str = "同济 · 一网通办") -> str:
    """规范化后重新渲染；全天事件用 VALUE=DATE，不再用 00:00–23:59 冒充。"""
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TongjiHub//TongjiCalendar//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + _fold(calendar_name),
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for index, event in enumerate(events):
        out.append("BEGIN:VEVENT")
        uid = event.get("uid") or "tongji-cal-%s-%d@noticehub" % (str(event.get("start") or "")[:8], index)
        out.append("UID:" + _fold(uid))
        out.append("DTSTAMP:" + stamp)
        if event.get("all_day"):
            out.append("DTSTART;VALUE=DATE:" + str(event.get("start") or "")[:8])
            out.append("DTEND;VALUE=DATE:" + str(event.get("end") or "")[:8])
        else:
            out.append("DTSTART;TZID=Asia/Shanghai:" + str(event.get("start") or ""))
            out.append("DTEND;TZID=Asia/Shanghai:" + str(event.get("end") or ""))
        out.append("SUMMARY:" + _fold(event.get("summary") or "(无标题)"))
        if event.get("location"):
            out.append("LOCATION:" + _fold(event["location"]))
        if event.get("description"):
            out.append("DESCRIPTION:" + _fold(event["description"]))
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"
