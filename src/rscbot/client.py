"""RSC 客户端：会话、限频、重试、结构化日志。

设计约束（来自 docs/01）：
- 全站 30 req/min，必须客户端先限，不能等服务端 429
- 所有写操作必须带 request_id（幂等键）
- 会话 30 天，但 _forum_session 会滚动，需要能自动重登
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

log = logging.getLogger("rsc.client")

BASE = os.environ.get("RSC_BASE_URL", "https://river-side.cc").rstrip("/")
UA = os.environ.get(
    "RSC_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
PER_MINUTE = int(os.environ.get("RSC_PER_MINUTE", "25"))
TIMEOUT = float(os.environ.get("RSC_TIMEOUT_S", "60"))


class RateLimiter:
    """固定窗口令牌桶，线程安全。默认 25/min（留 5 次余量给突发）。"""

    def __init__(self, per_minute: int = PER_MINUTE):
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._window_start = time.time()
        self._used = 0

    def acquire(self, priority: int = 1) -> None:
        while True:
            with self._lock:
                now = time.time()
                if now - self._window_start >= 60:
                    self._window_start = now
                    self._used = 0
                if self._used < self.per_minute:
                    self._used += 1
                    return
                wait = 60 - (now - self._window_start) + 0.05
            log.warning("rate limit hit, sleeping %.1fs", wait)
            time.sleep(max(wait, 0.05))


class RSCError(Exception):
    def __init__(self, status: int, body: dict, path: str):
        self.status = status
        self.body = body
        self.code = body.get("error_code") or (
            "MISSING_REQUEST_ID" if status == 400 and "request_id" in str(body) else None
        )
        super().__init__(f"{path} -> {status} {self.code or body.get('errors')}")

    @property
    def retryable(self) -> bool:
        # quote_stale / market_closed / position_conflict 都不是"重试就好"的错
        return self.status >= 500 or self.status == 429


class Client:
    def __init__(self, email: str, password: str, per_minute: int = PER_MINUTE,
                 timeout: float = TIMEOUT):
        self.email = email
        self.password = password
        self.timeout = timeout
        self.limiter = RateLimiter(per_minute)
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self._csrf: str | None = None
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "errors": 0, "relogins": 0}

    # ── 底层 ────────────────────────────────────────────
    def _raw(self, path: str, data: dict | None, headers: dict | None, timeout=None):
        body = None if data is None else urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            BASE + path, data=body, method="POST" if data is not None else "GET"
        )
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json")
        req.add_header("X-Requested-With", "XMLHttpRequest")
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        t0 = time.time()
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, raw, time.time() - t0
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), time.time() - t0

    def _parse(self, raw: str) -> dict:
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw[:500]}

    def csrf(self, force: bool = False) -> str:
        if self._csrf is None or force:
            self.limiter.acquire(0)
            st, raw, _ = self._raw("/session/csrf.json", None, None)
            self._csrf = self._parse(raw)["csrf"]
        return self._csrf

    # ── 会话 ────────────────────────────────────────────
    def login(self) -> dict:
        st, raw, dt = self._raw(
            "/session",
            {"login": self.email, "password": self.password},
            {"X-CSRF-Token": self.csrf()},
        )
        js = self._parse(raw)
        if st != 200 or "user" not in js:
            raise RSCError(st, js, "/session")
        log.info("login ok user=%s id=%s %.2fs", js["user"].get("username"),
                 js["user"].get("id"), dt)
        return js["user"]

    def _ensure_session(self) -> None:
        with self._lock:
            if self._csrf is None:
                self.login()

    # ── 通用请求 ────────────────────────────────────────
    def request(self, path: str, payload: dict | None = None, priority: int = 1,
                retries: int = 2) -> dict:
        self._ensure_session()
        last: Exception | None = None
        for attempt in range(retries + 1):
            self.limiter.acquire(priority)
            headers = {} if payload is None else {"X-CSRF-Token": self.csrf()}
            st, raw, dt = self._raw(path, payload, headers)
            self.stats["requests"] += 1
            js = self._parse(raw)
            if st == 200:
                return js
            if st in (401, 403):
                log.warning("session expired (%s), relogin", st)
                self._csrf = None
                self.stats["relogins"] += 1
                try:
                    self.login()
                except Exception as e:  # noqa: BLE001
                    last = e
                continue
            err = RSCError(st, js, path)
            self.stats["errors"] += 1
            if not err.retryable or attempt == retries:
                raise err
            last = err
            time.sleep([0.5, 1.5][min(attempt, 1)])
        raise last or RSCError(500, {}, path)

    # ── 业务封装 ────────────────────────────────────────
    def state(self, section: str | None = None, priority: int = 1) -> dict:
        q = f"?section={section}" if section else ""
        return self.request("/rsc/state.json" + q, priority=priority)

    def history(self, instrument_id: int, rng: str = "1d") -> dict:
        return self.request(f"/rsc/instruments/{instrument_id}/history.json?range={rng}")

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex

    def place_order(self, instrument_id: int, side: str, quantity, leverage,
                    take_profit="", stop_loss="", high_risk=None,
                    request_id: str | None = None) -> dict:
        payload = {
            "instrument_id": instrument_id,
            "side": side,
            "quantity": str(quantity),
            "leverage": str(leverage),
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "request_id": request_id or self.new_request_id(),
        }
        if high_risk is not None:
            payload["high_risk"] = "true" if high_risk else "false"
        return self.request("/rsc/orders.json", payload, priority=0)

    def cancel_order(self, order_id: int, request_id: str | None = None) -> dict:
        return self.request(
            f"/rsc/orders/{order_id}/cancel.json",
            {"request_id": request_id or self.new_request_id()},
            priority=0,
        )

    def protect(self, position_id: int, take_profit="", stop_loss="") -> dict:
        return self.request(
            f"/rsc/positions/{position_id}/protection.json",
            {"take_profit": str(take_profit), "stop_loss": str(stop_loss),
             "request_id": self.new_request_id()},
            priority=0,
        )

    def refresh(self, instrument_id: int) -> dict:
        return self.request(f"/rsc/instruments/{instrument_id}/refresh.json", {},
                            priority=3)
