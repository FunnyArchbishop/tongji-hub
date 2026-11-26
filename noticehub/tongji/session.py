"""同济校园系统的只读会话。

同济各子系统（教务、门户、图书馆、校园网…）都挂在统一身份认证后面，登录一次
拿到的 Cookie 可以在子系统之间复用，所以这里只做三件事：带 Cookie 发请求、
把登录失效认出来、把响应尽量解析成 JSON。

约定（和超星 / Canvas 那两个源一致）：
* 只读，不发任何写操作；
* 一次只打一个主机，不跨域跟随跳转（302 一律当登录失效，别把 Cookie 送给认证域）；
* 失败抛 TongjiAuthError / TongjiError，由上层决定要不要标记「过期」。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from noticehub.receivers.ddl_sync import DeadlineAuthError, DeadlineSourceError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
# 登录页 / 统一认证的特征：命中就当会话失效，而不是把 HTML 当数据去解析。
LOGIN_MARKERS = (
    "ActionAuthChain",
    "请登录",
    "统一身份认证",
    "KEYCLOAK",
    "login",
)


class TongjiError(DeadlineSourceError):
    """同济校园系统可预期的失败。"""


class TongjiAuthError(DeadlineAuthError):
    """统一身份认证会话缺失或已失效。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随跳转：跟过去只会拿到认证页，还会把 Cookie 泄露给认证域。"""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class TongjiSession:
    """一套 Cookie 打所有同济子系统。"""

    def __init__(self, cookie: str, *, timeout: float = 15.0, logger: Any = None) -> None:
        self.cookie = str(cookie or "").strip()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.logger = logger or logging.getLogger(__name__)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _headers(self, referer: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if referer:
            headers["Referer"] = referer
        return headers

    def get(self, url: str, params: dict[str, Any] | None = None, *, referer: str = "") -> tuple[str, dict[str, str]]:
        """GET 一个地址，返回 (正文, 响应头)；登录失效抛 TongjiAuthError。"""
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(url, headers=self._headers(referer), method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                headers = {str(k): str(v) for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise TongjiAuthError("统一身份认证会话已失效（被重定向到登录页）：%s" % url) from exc
            if exc.code in (401, 403):
                raise TongjiAuthError("无权访问或会话已失效（HTTP %s）：%s" % (exc.code, url)) from exc
            raise TongjiError("HTTP %s：%s" % (exc.code, url)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TongjiError("请求失败：%s（%s）" % (url, exc)) from exc
        if self._looks_like_login(body):
            raise TongjiAuthError("拿回来的是登录页，说明会话已失效：%s" % url)
        return body, headers

    @staticmethod
    def _looks_like_login(body: str) -> bool:
        head = body[:4000]
        if head.lstrip().startswith(("{", "[")):
            return False  # JSON 一律不判死，交给调用方
        return any(marker in head for marker in LOGIN_MARKERS)

    def get_json(self, url: str, params: dict[str, Any] | None = None, *, referer: str = "") -> Any:
        body, _headers = self.get(url, params, referer=referer)
        text = body.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            # 有些接口会包一层 JSONP：callback({...})
            match = re.match(r"^[\w$.]+\((.*)\)\s*;?$", text, re.S)
            if match:
                try:
                    return json.loads(match.group(1))
                except (ValueError, TypeError):
                    pass
            raise TongjiError("响应不是 JSON：%s" % url)

    def get_text(self, url: str, params: dict[str, Any] | None = None, *, referer: str = "") -> str:
        return self.get(url, params, referer=referer)[0]

    def validate(self, url: str, *, referer: str = "") -> bool:
        """打一个需要登录的地址：能拿到非登录页就算有效。"""
        if not self.cookie:
            return False
        try:
            self.get(url, referer=referer)
        except TongjiAuthError:
            return False
        except TongjiError:
            return False
        return True
