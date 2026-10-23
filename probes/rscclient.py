"""最小 RSC 客户端（仅标准库）。M1 会用正式实现替换它。"""

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("RSC_BASE_URL", "https://river-side.cc")
UA = os.environ.get("RSC_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")


class Client:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self._csrf = None

    def _open(self, path, data=None, headers=None, timeout=60):
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
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace"), time.time() - t0
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), time.time() - t0

    def csrf(self, force=False):
        if self._csrf is None or force:
            st, raw, _ = self._open("/session/csrf.json")
            self._csrf = json.loads(raw)["csrf"]
        return self._csrf

    def login(self):
        st, raw, dt = self._open(
            "/session",
            {"login": self.email, "password": self.password},
            {"X-CSRF-Token": self.csrf()},
        )
        js = json.loads(raw) if raw.lstrip().startswith("{") else {}
        if st != 200 or "user" not in js:
            raise RuntimeError(f"login failed {st}: {raw[:300]}")
        return js["user"]

    def api(self, path, payload=None, timeout=60):
        h = {} if payload is None else {"X-CSRF-Token": self.csrf()}
        st, raw, dt = self._open(path, payload, h, timeout)
        try:
            js = json.loads(raw)
        except Exception:
            js = {"_raw": raw[:400]}
        return st, js, dt

    # 便捷封装
    def state(self, section=None, timeout=60):
        q = f"?section={section}" if section else ""
        return self.api("/rsc/state.json" + q, timeout=timeout)

    def order(self, instrument_id, side, quantity, leverage, **extra):
        p = {
            "instrument_id": instrument_id,
            "side": side,
            "quantity": quantity,
            "leverage": leverage,
            "request_id": str(__import__("uuid").uuid4()),
        }
        p.update(extra)
        return self.api("/rsc/orders.json", p)

    def cancel(self, order_id):
        return self.api(
            f"/rsc/orders/{order_id}/cancel.json",
            {"request_id": str(__import__("uuid").uuid4())},
        )
