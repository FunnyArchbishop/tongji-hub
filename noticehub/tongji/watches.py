"""「盯一个同济页面」：把任意需要登录的校内页面变成一条会自己更新的待办。

为什么不写死接口：同济各子系统的内部接口没有公开文档，写死一串猜出来的路径，
上线就是一堆 404。这里换成可配置的监看项——用户把教务通知页、学工通知页、
图书馆公告页的地址填进来，程序定时取一次正文，页面变了（且命中关键词）就产出一条
待办。等哪个接口被真机验证过，再在 `services.py` 里加成专用服务。

只读：只 GET，不提交表单、不点按钮。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

MAX_BODY_CHARS = 200_000
SNAPSHOT_CHARS = 6000


@dataclass
class Watch:
    """一条监看项。"""

    id: str
    label: str
    url: str
    keyword: str = ""
    enabled: bool = True
    note: str = ""

    @classmethod
    def from_mapping(cls, row: Any) -> "Watch | None":
        if not isinstance(row, dict):
            return None
        url = str(row.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return None
        label = str(row.get("label") or "").strip() or url
        watch_id = str(row.get("id") or "").strip() or _watch_id(url)
        return cls(
            id=watch_id,
            label=label,
            url=url,
            keyword=str(row.get("keyword") or "").strip(),
            enabled=bool(row.get("enabled", True)),
            note=str(row.get("note") or "").strip(),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "keyword": self.keyword,
            "enabled": self.enabled,
            "note": self.note,
        }


def _watch_id(url: str) -> str:
    return "w" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def parse_watches(raw: str) -> list[Watch]:
    """把库里存的 JSON 解析成监看项；坏一条就跳一条，不让整张表崩。"""
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("watches")
    if not isinstance(payload, list):
        return []
    result: list[Watch] = []
    seen: set[str] = set()
    for row in payload:
        watch = Watch.from_mapping(row)
        if watch is None or watch.id in seen:
            continue
        seen.add(watch.id)
        result.append(watch)
    return result


def dump_watches(watches: list[Watch]) -> str:
    return json.dumps([watch.to_mapping() for watch in watches], ensure_ascii=False, indent=2)


def html_to_text(raw: str) -> str:
    """HTML → 纯文本，够用来比对变化就行。"""
    text = str(raw or "")[:MAX_BODY_CHARS]
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fingerprint(text: str) -> str:
    """正文指纹：只取前若干字符的哈希，页面加个时间戳不会误报。"""
    normalized = re.sub(r"\s+", " ", text).strip()[:SNAPSHOT_CHARS]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def headline(text: str, limit: int = 60) -> str:
    """给待办用的标题：第一行有内容就用第一行。"""
    for line in str(text or "").splitlines():
        line = line.strip()
        if len(line) >= 4:
            return line[:limit]
    return ""


def matched_snippet(text: str, keyword: str, limit: int = 120) -> str:
    """命中关键词的那一小段上下文，用来当证据。"""
    if not keyword:
        return ""
    position = text.find(keyword)
    if position < 0:
        return ""
    start = max(0, position - 40)
    return re.sub(r"\s+", " ", text[start:position + len(keyword) + 40]).strip()[:limit]


def build_record(watch: Watch, text: str, *, first_seen: bool = False) -> dict[str, str]:
    """一次页面变化 → 一条入站记录。"""
    from noticehub.platform.timeutil import iso, now_local

    stamp = iso(now_local())
    title = headline(text) or watch.label
    snippet = matched_snippet(text, watch.keyword) if watch.keyword else ""
    lines = [
        f"{watch.label} · {title}",
        "来源：同济页面监看" + ("（首次建立基线）" if first_seen else "（页面有更新）"),
    ]
    if snippet:
        lines.append("命中：" + snippet)
    lines.append("链接：" + watch.url)
    return {
        "msg_id": f"tongji:{watch.id}:{fingerprint(text)[:12]}",
        "source": "tongji",
        "event": "PAGE_CHANGED",
        "group_id": watch.id,
        "group_name": watch.label,
        "sender_id": "",
        "sender_name": "同济",
        "ts": stamp,
        "received_at": stamp,
        "content": "\n".join(lines),
    }
