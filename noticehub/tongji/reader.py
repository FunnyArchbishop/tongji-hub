"""同济数据源：教务成绩、图书馆借阅到期、校内页面监看，三路一起进待办台。

底座复用 `ddl_sync.DeadlineSyncReader`（状态、线程、登录失效复核），
`collect()`/`scan_once()` 由本模块自己实现：三路来源各用自己的凭据，
其中「页面监看」首轮只建基线，不把当前页面当成新通知推一遍。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from noticehub.platform.timeutil import iso, now_local
from noticehub.qq.messages import TONGJI_SESSION_EXPIRED
from noticehub.receivers.ddl_sync import DeadlineSourceError, DeadlineSyncReader, source_status
from noticehub.tongji import calendar_feed, gpa as gpa_module, jwc, library
from noticehub.tongji import watches as watch_module
from noticehub.tongji.session import TongjiAuthError, TongjiError, TongjiSession

POLL_INTERVAL_SECONDS = 1800
WATCH_STATE_KEY = "tongji_watch_state"
GRADE_STATE_KEY = "tongji_grade_state"


def tongji_status(settings: Any, store: Any = None) -> dict[str, Any]:
    """给网页用的状态：不含任何凭据，只报「有没有配」。"""
    payload = source_status(
        "tongji",
        settings,
        store,
        label="同济",
        banner=TONGJI_SESSION_EXPIRED,
        login_url="https://iam.tongji.edu.cn/idp/authcenter/ActionAuthChain",
    )
    payload.update({
        "has_jwc_session": bool(getattr(settings, "tongji_jwc_session", "")),
        "has_library_login": bool(getattr(settings, "tongji_library_jwt", "")),
        "watches": len(load_watches(store)) if store is not None else 0,
        "cal_feed": str(getattr(settings, "tongji_cal_feed", "") or ""),
    })
    return payload


def load_watches(store: Any) -> list[watch_module.Watch]:
    if store is None or not callable(getattr(store, "meta_get", None)):
        return []
    try:
        return watch_module.parse_watches(store.meta_get("tongji_watches", "[]"))
    except Exception:  # noqa: BLE001 - 读坏了就当没有监看项
        logging.getLogger(__name__).warning("同济监看项读取失败", exc_info=True)
        return []


def save_watches(store: Any, watches: list[watch_module.Watch]) -> None:
    if store is None or not callable(getattr(store, "meta_set", None)):
        raise DeadlineSourceError("存储不可用，监看项没法保存")
    store.meta_set("tongji_watches", watch_module.dump_watches(watches))


class TongjiReader(DeadlineSyncReader):
    """低频只读：教务成绩 / 图书馆借阅 / 校内页面监看。"""

    source = "tongji"
    label = "同济"
    banner = TONGJI_SESSION_EXPIRED
    interval_key = "tongji_scan_interval_seconds"
    default_interval = POLL_INTERVAL_SECONDS
    # 成绩与借阅不需要「首轮基线」；监看项需要，所以单独在 scan_once 里处理。
    baseline_push_limit = 1000

    # -- 客户端 -----------------------------------------------------------

    def build_client(self) -> Any:
        """给状态复核用的客户端：优先教务会话，其次 iam Cookie。"""
        session_id = str(getattr(self.settings, "tongji_jwc_session", "") or "")
        if session_id:
            return jwc.JwcClient(session_id, timeout=float(getattr(self.settings, "http_timeout", 15) or 15), logger=self.logger)
        cookie = self._cookie()
        if not cookie:
            raise TongjiAuthError("同济登录信息未配置")
        return TongjiSession(cookie, timeout=float(getattr(self.settings, "http_timeout", 15) or 15), logger=self.logger)

    def validate_login(self, client: Any) -> None:
        if isinstance(client, jwc.JwcClient):
            client.validate_login()
            return
        watches = load_watches(self.store)
        url = next((watch.url for watch in watches if watch.enabled), "")
        if not url:
            raise TongjiAuthError("没有可用的同济地址来校验会话")
        client.get(url)

    def _timeout(self) -> float:
        return float(getattr(self.settings, "http_timeout", 15) or 15)

    # -- 三路采集 ---------------------------------------------------------

    def _grade_records(self) -> list[dict[str, Any]]:
        session_id = str(getattr(self.settings, "tongji_jwc_session", "") or "")
        uid = str(getattr(self.settings, "tongji_jwc_uid", "") or "")
        if not session_id or not uid:
            return []
        client = jwc.JwcClient(session_id, timeout=self._timeout(), logger=self.logger)
        try:
            user = client.session_user()
        except TongjiAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 拿不到用户信息不该拖垮成绩这一路
            self.logger.debug("教务会话信息读取失败，用配置里的学号：%s", exc)
            user = {}
        name = str((user or {}).get("name") or "")
        student_id = str((user or {}).get("uid") or uid)
        return jwc.grade_records(client.grades(student_id), student_name=name)

    def _loan_records(self) -> list[dict[str, Any]]:
        jwt = str(getattr(self.settings, "tongji_library_jwt", "") or "")
        if not jwt:
            return []
        header = str(getattr(self.settings, "tongji_library_jwt_header", "") or "")
        client = library.LibraryClient(jwt, header, timeout=self._timeout(), logger=self.logger)
        return library.loan_records(client.loans())

    def _watch_records(self, *, baseline: bool, state: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
        cookie = self._cookie()
        if not cookie:
            return [], state
        watches = [watch for watch in load_watches(self.store) if watch.enabled]
        if not watches:
            return [], state
        session = TongjiSession(cookie, timeout=self._timeout(), logger=self.logger)
        delivered: list[dict[str, Any]] = []
        for watch in watches:
            try:
                body = session.get_text(watch.url)
            except TongjiAuthError:
                raise
            except (TongjiError, DeadlineSourceError) as exc:
                self.logger.warning("同济监看项读取失败（%s）：%s", watch.label, exc)
                continue
            text = watch_module.html_to_text(body)
            if not text:
                continue
            if watch.keyword and watch.keyword not in text:
                continue
            digest = watch_module.fingerprint(text)
            if state.get(watch.id) == digest:
                continue
            state[watch.id] = digest
            if baseline:
                continue  # 首轮只建立基线
            delivered.append(watch_module.build_record(watch, text))
        return delivered, state

    def _load_watch_state(self) -> dict[str, str]:
        if self.store is None or not callable(getattr(self.store, "meta_get", None)):
            return {}
        try:
            state = json.loads(self.store.meta_get(WATCH_STATE_KEY, "{}"))
        except (ValueError, TypeError):
            return {}
        return {str(k): str(v) for k, v in state.items()} if isinstance(state, dict) else {}

    def _save_watch_state(self, state: dict[str, str]) -> None:
        if self.store is None or not callable(getattr(self.store, "meta_set", None)):
            return
        try:
            self.store.meta_set(WATCH_STATE_KEY, json.dumps(state, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 状态落库失败不影响本轮
            self.logger.debug("同济监看状态保存失败", exc_info=True)

    def collect(self, client: Any) -> list[dict[str, Any]]:
        """兼容底座接口：只回成绩与借阅（监看项在 scan_once 里单独处理）。"""
        records: list[dict[str, Any]] = []
        records.extend(self._grade_records())
        records.extend(self._loan_records())
        return records

    def scan_once(self) -> int:
        """三路各采一次；单路失败不拖垮其它两路。"""
        delivered = 0
        failures: list[Exception] = []

        for label, loader in (("教务成绩", self._grade_records), ("图书馆借阅", self._loan_records)):
            try:
                for record in loader():
                    if self.on_message(record):
                        delivered += 1
            except TongjiAuthError as exc:
                failures.append(exc)
            except Exception as exc:  # noqa: BLE001 - 一路坏不影响另一路
                self.logger.warning("同济%s读取失败：%s", label, exc)

        state = self._load_watch_state()
        baseline = not state
        try:
            records, state = self._watch_records(baseline=baseline, state=state)
            for record in records:
                if self.on_message(record):
                    delivered += 1
        except TongjiAuthError as exc:
            failures.append(exc)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("同济页面监看失败：%s", exc)
        self._save_watch_state(state)

        if failures:
            raise failures[0]
        return delivered

    def _run(self) -> None:
        interval = self._interval()
        while not self.stop_event.is_set():
            try:
                delivered = self.scan_once()
                self._set_status(ok=True, expired=False, error="", synced_at=iso(now_local()))
                self._last_logged_error = None
                if delivered:
                    self.logger.info("同济同步完成：%d 条已投递。", delivered)
            except Exception as exc:  # noqa: BLE001 - 轮询线程要活到下一轮
                self._record_failure(exc)
            self.stop_event.wait(interval)


# -- 课表 / 校历 / 绩点：给网页直接调用的入口 -----------------------------


def fetch_timetable_ics(settings: Any, *, logger: Any = None) -> tuple[str, int]:
    """拉当前学期课表并渲染成 ics；返回 (ics 正文, 课程节次数)。"""
    session_id = str(getattr(settings, "tongji_jwc_session", "") or "")
    if not session_id:
        raise TongjiAuthError("教务会话未配置（需要浏览器里的 sessionid）")
    client = jwc.JwcClient(
        session_id,
        timeout=float(getattr(settings, "http_timeout", 15) or 15),
        logger=logger or logging.getLogger(__name__),
    )
    term = client.current_term()
    calendar_id = term.get("id") or term.get("calendarId") or term.get("wid")
    if calendar_id is None:
        raise TongjiError("教务没有返回当前学期编号")
    rows = jwc.session_rows(client.timetable(calendar_id))
    events = jwc.expand_sessions(rows, jwc._term_start(term))
    return jwc.render_timetable_ics(events), len(events)


def fetch_calendar_feed(settings: Any) -> dict[str, Any]:
    """一网通办官方 ics → 规范化结果。"""
    feed_id = str(getattr(settings, "tongji_cal_feed", "") or "").strip()
    if not feed_id:
        raise TongjiError("还没有填一网通办的日历订阅编号")
    raw = calendar_feed.fetch_feed(
        feed_id,
        cookie=str(getattr(settings, "tongji_cookie", "") or ""),
        timeout=float(getattr(settings, "http_timeout", 15) or 15),
    )
    return calendar_feed.normalize(raw)


def compute_gpa(rows: list[Any], level: str = gpa_module.UNDERGRADUATE) -> dict[str, Any]:
    return gpa_module.compute_gpa(rows, level)
