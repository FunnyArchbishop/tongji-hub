"""图书馆借阅到期（`webpac.tongji.edu.cn`）。

凭据是 webpac 自己的 `jwt` + `jwtHeader`（浏览器 sessionStorage 里那两项），
和统一认证、Canvas 都不是一套。接口来自 tongji-online 的 webpac 参考文档：

* `POST /find/loanInfo/loanList` —— 本人借阅，**上限 100 条**
* `POST /find/unify/search` —— 馆藏检索（公开）

到期提醒只认 `normReturnDate`（`returnDate` 是历史归还日期，用错了会天天误报）。
只读：不调续借 `/find/lendbook/reNew`。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from noticehub.tongji.session import TongjiAuthError, TongjiError, _NoRedirect

BASE_URL = "https://webpac.tongji.edu.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
BASE_ENV = "NOTICEHUB_TONGJI_LIBRARY_BASE"
LOAN_LIMIT = 100


def resolve_base() -> str:
    import os

    override = str(os.environ.get(BASE_ENV) or "").strip()
    if not override:
        return BASE_URL
    if not override.lower().startswith(("http://", "https://")):
        raise ValueError(f"{BASE_ENV} 必须以 http:// 或 https:// 开头，收到：{override!r}")
    return override.rstrip("/")


class LibraryClient:
    """最小只读客户端：jwt + jwtHeader 两个头打过去。"""

    def __init__(self, jwt: str, jwt_header: str = "", *, base: str = "", timeout: float = 15.0, logger: Any = None) -> None:
        self.jwt = str(jwt or "").strip()
        self.jwt_header = str(jwt_header or "").strip() or "Authorization"
        self.base = (base or resolve_base()).rstrip("/")
        self.timeout = float(timeout)
        self.logger = logger or logging.getLogger(__name__)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": USER_AGENT,
            "Referer": self.base + "/s12/",
        }
        if self.jwt:
            headers[self.jwt_header] = self.jwt
        return headers

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        if not self.jwt:
            raise TongjiAuthError("图书馆登录信息未配置（需要 webpac 的 jwt）")
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, headers=self._headers(), method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise TongjiAuthError("图书馆登录已失效（被重定向到登录页）") from exc
            if exc.code in (401, 403):
                raise TongjiAuthError("图书馆登录已失效（HTTP %s）" % exc.code) from exc
            raise TongjiError("图书馆 HTTP %s：%s" % (exc.code, path)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TongjiError("图书馆请求失败：%s（%s）" % (path, exc)) from exc
        try:
            payload = json.loads(text or "{}")
        except (ValueError, TypeError) as exc:
            if "login" in text[:400].lower():
                raise TongjiAuthError("图书馆返回登录页，会话已失效") from exc
            raise TongjiError("图书馆响应不是 JSON：%s" % path) from exc
        return payload

    # -- 资源 -------------------------------------------------------------

    def userinfo(self) -> dict[str, Any]:
        """`GET /oga/userinfo` 只是探活；POST 探更稳，用借阅列表第一页代替。"""
        payload = self.post("/find/loanInfo/loanList", {"page": 1, "rows": 1})
        if isinstance(payload, dict) and str(payload.get("code") or "") not in ("", "0", "200"):
            raise TongjiAuthError("图书馆登录已失效：%s" % (payload.get("msg") or payload.get("message") or ""))
        return payload if isinstance(payload, dict) else {}

    def validate_login(self) -> dict[str, Any]:
        return self.userinfo()

    def loans(self) -> list[dict[str, Any]]:
        payload = self.post("/find/loanInfo/loanList", {"page": 1, "rows": LOAN_LIMIT})
        rows: Any = payload
        if isinstance(payload, dict):
            rows = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("list")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def parse_date(value: Any) -> dt.date | None:
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
    match = re.match(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def due_of(loan: dict[str, Any]) -> dt.date | None:
    """应还日期：只认 normReturnDate（returnDate 是历史归还日期）。"""
    return parse_date(loan.get("normReturnDate") or loan.get("dueDate") or loan.get("shouldReturnDate"))


def loan_records(loans: list[dict[str, Any]], *, now: dt.date | None = None, warn_days: int = 7) -> list[dict[str, str]]:
    """快到期/已逾期的借阅 → 记录；每条书一个稳定 msg_id。"""
    from noticehub.platform.timeutil import iso, now_local

    today = now or now_local().date()
    stamp = iso(now_local())
    records: list[dict[str, str]] = []
    for loan in loans:
        title = str(loan.get("title") or loan.get("bookName") or loan.get("name") or "").strip()
        due = due_of(loan)
        if not title or due is None:
            continue
        days_left = (due - today).days
        if days_left > warn_days:
            continue
        state = "已逾期 %d 天" % (-days_left) if days_left < 0 else ("今天到期" if days_left == 0 else "还有 %d 天到期" % days_left)
        lines = [
            f"图书馆还书：{title}",
            f"应还日期：{due.isoformat()}（{state}）",
            "来源：同济图书馆",
        ]
        author = str(loan.get("author") or "").strip()
        if author:
            lines.insert(1, "作者：" + author)
        records.append({
            "msg_id": "tongji:loan:%s:%s" % (title, due.isoformat()),
            "source": "tongji",
            "event": "BOOK_DUE",
            "group_id": "library",
            "group_name": "同济图书馆",
            "sender_id": "",
            "sender_name": "同济图书馆",
            "ts": stamp,
            "received_at": stamp,
            "content": "\n".join(lines),
            "_due": due.isoformat(),
        })
    return records
