"""带截止时间的数据源共用底座：状态上报、首次基线、改期检测、轮询线程。

Canvas 和课堂派两个抓取器的差别只有「怎么连、怎么解析」；状态怎么写、第一次
扫到的东西怎么处理、截止时间变了怎么办、线程什么时候停，全部收在这里，
免得两边各写一份然后慢慢分叉。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from noticehub.platform.timeutil import iso, now_local

# 首次建基线时最多推这么多条，避免一门课几十个历史作业把待办台刷爆。
BASELINE_PUSH_LIMIT = 60


class DeadlineSourceError(RuntimeError):
    """数据源可预期的失败（网络、响应形状不对等）。"""


class DeadlineAuthError(DeadlineSourceError):
    """登录态缺失或已失效。"""


class DeadlineSyncReader:
    """只读轮询器底座；子类只实现 build_client / collect / validate_login。"""

    #: 记录里的 source 字段，同时是状态与状态的存储键前缀。
    source: str = ""
    #: 用户看到的名字，用在日志和提示里。
    label: str = ""
    #: 登录失效时给用户看的那句话。
    banner: str = ""
    #: Settings 上的扫描间隔字段名。
    interval_key: str = ""
    default_interval: int = 1800
    baseline_push_limit: int = BASELINE_PUSH_LIMIT
    #: 间隔下限：抓学校平台别太勤。
    min_interval: int = 300

    def __init__(
        self,
        settings: Any,
        on_message: Callable[[dict[str, Any]], bool],
        logger: Any = None,
        store: Any = None,
    ) -> None:
        self.settings = settings
        self.on_message = on_message
        self.logger = logger or logging.getLogger(__name__)
        self.store = store
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._last_logged_error: tuple[str, str] | None = None
        self._status_lock = threading.Lock()
        # 锁挂在 settings 上：网页线程读状态时用的是同一把锁，和超星那边一致。
        setattr(self.settings, f"{self.source}_status_lock", self._status_lock)
        saved: dict[str, Any] = {}
        if store is not None and callable(getattr(store, "meta_get", None)):
            try:
                saved = json.loads(store.meta_get(self._status_meta_key, "{}"))
            except (ValueError, TypeError):
                saved = {}
        setattr(self.settings, self._status_attr, {
            "ok": False,
            "expired": False,
            "last_sync_at": "",
            "last_error": "",
            **(saved if isinstance(saved, dict) else {}),
        })

    # -- 命名 -------------------------------------------------------------

    @property
    def _status_attr(self) -> str:
        return f"{self.source}_status"

    @property
    def _status_meta_key(self) -> str:
        return f"{self.source}_status"

    @property
    def _state_meta_key(self) -> str:
        return f"{self.source}_sync_state"

    @property
    def _cookie_attr(self) -> str:
        return f"{self.source}_cookie"

    def _cookie(self) -> str:
        return str(getattr(self.settings, self._cookie_attr, "") or "")

    def has_credentials(self) -> bool:
        """有没有可用的登录信息；Canvas 还认访问令牌，所以子类可以覆盖。"""
        return bool(self._cookie())

    def _interval(self) -> int:
        raw = getattr(self.settings, self.interval_key, self.default_interval)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = self.default_interval
        return max(self.min_interval, value)

    # -- 状态 -------------------------------------------------------------

    def _status(self) -> dict[str, Any]:
        return dict(getattr(self.settings, self._status_attr, {}) or {})

    def _set_status(self, *, ok: bool, expired: bool = False, error: str = "", synced_at: str | None = None) -> None:
        with self._status_lock:
            status = self._status()
            status.update({"ok": ok, "expired": expired, "last_error": error})
            if synced_at is not None:
                status["last_sync_at"] = synced_at
            setattr(self.settings, self._status_attr, status)
        if self.store is not None and callable(getattr(self.store, "meta_set", None)):
            try:
                self.store.meta_set(self._status_meta_key, json.dumps(status, ensure_ascii=False))
            except Exception:  # noqa: BLE001 - 状态持久化失败不影响抓取
                self.logger.debug("%s 状态持久化失败", self.label, exc_info=True)

    def _record_failure(self, exc: Exception) -> None:
        """登录类失败先复核一次，避免把「某个子站要重新登录」误判成整体过期。"""
        expired, message = False, str(exc)
        if isinstance(exc, DeadlineAuthError):
            verdict = self._login_still_valid()
            if verdict is None:
                expired = bool(self._status().get("expired"))
                message = f"{self.label}登录复核失败（未能确认是否过期）：{message}"
            elif verdict:
                message = f"部分 {self.label} 接口返回登录页，登录复核仍有效（未标记为过期）"
            else:
                expired, message = True, f"{self.label}登录已过期（需重新登录）"
        cookie = self._cookie()
        if cookie:
            message = message.replace(cookie, "<redacted>")
        self._set_status(ok=False, expired=expired, error=message)
        key = (type(exc).__name__, message)
        if key != self._last_logged_error:
            if expired:
                self.logger.warning("%s登录已过期，需重新登录后更新 Cookie", self.label)
            else:
                self.logger.warning("%s 读取失败：%s", self.label, message)
            self._last_logged_error = key

    def _login_still_valid(self) -> bool | None:
        """None 表示复核本身失败、无法判定；调用方据此保留原状态。"""
        if not self.has_credentials():
            return False
        try:
            client = self.build_client()
        except DeadlineAuthError:
            return False
        except (DeadlineSourceError, ValueError) as exc:
            self.logger.debug("%s 登录复核失败，保留原状态：%s", self.label, exc)
            return None
        try:
            self.validate_login(client)
        except DeadlineAuthError:
            return False
        except (DeadlineSourceError, ValueError) as exc:
            self.logger.debug("%s 登录复核失败，保留原状态：%s", self.label, exc)
            return None
        return True

    # -- 同步状态（记录上次看到的截止时间，用来识别改期） -------------------

    def _load_state(self) -> dict[str, str]:
        if self.store is None or not callable(getattr(self.store, "meta_get", None)):
            return {}
        try:
            state = json.loads(self.store.meta_get(self._state_meta_key, "{}"))
        except (ValueError, TypeError):
            return {}
        due = state.get("due") if isinstance(state, dict) else None
        return {str(key): str(value) for key, value in due.items()} if isinstance(due, dict) else {}

    def _save_state(self, known: dict[str, str]) -> None:
        if self.store is None or not callable(getattr(self.store, "meta_set", None)):
            return
        try:
            self.store.meta_set(self._state_meta_key, json.dumps({"due": known}, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 状态落库失败不影响本轮投递
            self.logger.debug("%s 同步状态保存失败", self.label, exc_info=True)

    # -- 投递 -------------------------------------------------------------

    def _rescheduled(self, record: dict[str, Any], previous: str, due_text: str) -> dict[str, Any]:
        """改期：同一条 DDL 换了截止时间，发一条新记录（旧待办仍在，别悄悄改别人的截止日）。"""
        updated = dict(record)
        updated["msg_id"] = f"{record['msg_id']}@{due_text}"
        updated["event"] = "WORK_RESCHEDULED"
        updated["content"] = f"改期：{record['content']}\n原截止时间：{previous}"
        return updated

    def scan_once(self) -> int:
        """扫一次并投递，返回本轮新投递的条数；失败向上抛，由 _run 记状态。"""
        client = self.build_client()
        collected = self.collect(client)
        known = self._load_state()
        baseline = not known
        delivered = 0
        for record in collected:
            key = str(record.get("msg_id") or "")
            if not key:
                continue
            due_text = str(record.pop("_due", "") or "")
            previous = known.get(key)
            if previous == due_text:
                continue
            if previous is None:
                if baseline and delivered >= self.baseline_push_limit:
                    known[key] = due_text  # 记下但这次不推，避免首次把历史作业全倒进待办
                    continue
                out = record
            else:
                out = self._rescheduled(record, previous, due_text)
            try:
                self.on_message(out)
            except Exception:  # noqa: BLE001 - 单条投递失败不能中断整轮
                self.logger.exception("%s 记录投递失败：%s", self.label, key)
                continue
            known[key] = due_text
            delivered += 1
        self._save_state(known)
        return delivered

    # -- 线程 -------------------------------------------------------------

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name=f"{self.source}-reader", daemon=True)
        self.thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        interval = self._interval()
        while not self.stop_event.is_set():
            try:
                delivered = self.scan_once()
                self._set_status(ok=True, expired=False, error="", synced_at=iso(now_local()))
                self._last_logged_error = None
                if delivered:
                    self.logger.info("%s 同步完成：%d 条 DDL 已投递。", self.label, delivered)
            except Exception as exc:  # noqa: BLE001 - 轮询线程必须活到下一轮
                self._record_failure(exc)
            self.stop_event.wait(interval)

    # -- 子类必须实现 -----------------------------------------------------

    def build_client(self) -> Any:
        raise NotImplementedError

    def collect(self, client: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def validate_login(self, client: Any) -> None:
        raise NotImplementedError


def source_status(source: str, settings: Any, store: Any = None, *, label: str = "", banner: str = "", login_url: str = "") -> dict[str, Any]:
    """给网页用的同步状态；不含任何凭据。"""
    status_attr = f"{source}_status"
    lock = getattr(settings, f"{status_attr}_lock", None)
    if lock:
        with lock:
            state = dict(getattr(settings, status_attr, {}) or {})
    else:
        state = dict(getattr(settings, status_attr, {}) or {})
    if not state and store is not None and callable(getattr(store, "meta_get", None)):
        try:
            state = json.loads(store.meta_get(status_attr, "{}"))
        except (ValueError, TypeError):
            state = {}
    expired = bool(state.get("expired"))
    return {
        "ok": bool(state.get("ok", False)),
        "expired": expired,
        "last_sync_at": str(state.get("last_sync_at") or ""),
        "last_error": str(state.get("last_error") or ""),
        "has_cookie": bool(getattr(settings, f"{source}_cookie", "")),
        "banner_text": banner if expired else "",
        "login_url": login_url if expired else "",
    }
