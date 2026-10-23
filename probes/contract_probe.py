#!/usr/bin/env python3
"""RSC 交易契约补测（M0-T3）。

目的：把 docs/01 里"推测"的行为换成"实测错误码"，产出 tests/fixtures/errors.json。
只用标准库；凭据从环境变量读，不落盘。

用法：
    set -a; . ./.env; set +a
    python probes/contract_probe.py

所有写操作都记入 results.json，结束时尝试平掉本次新开仓位。
"""

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.environ.get("RSC_BASE_URL", "https://river-side.cc")
EMAIL = os.environ["RSC_EMAIL"]
PASSWORD = os.environ["RSC_PASSWORD"]
UA = os.environ.get("RSC_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

CJ = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CJ))
RESULTS = []


def _open(path, data=None, headers=None, timeout=30):
    """data is None -> GET; data == {} -> POST with empty form (still sends CSRF)."""
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
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0


_csrf = {"v": None}


def csrf(force=False):
    if _csrf["v"] is None or force:
        st, raw, _ = _open("/session/csrf.json")
        _csrf["v"] = json.loads(raw)["csrf"]
    return _csrf["v"]


def api(path, payload=None):
    h = {} if payload is None else {"X-CSRF-Token": csrf()}
    st, raw, dt = _open(path, payload, h)
    try:
        js = json.loads(raw)
    except Exception:
        js = {"_raw": raw[:400]}
    return st, js, dt


def case(name, expect, st, js, note=""):
    got = js.get("error_code") or js.get("status") or (
        "OK" if st == 200 else f"http{st}"
    )
    RESULTS.append({"case": name, "expect": expect, "http": st, "got": got,
                    "body": js, "note": note})
    print(f"  {name:<26} http={st}  got={got!r:<28} expect={expect}")


def main():
    print("== login ==")
    st, raw, dt = _open("/session", {"login": EMAIL, "password": PASSWORD},
                        {"X-CSRF-Token": csrf()})
    js = json.loads(raw) if raw.strip().startswith("{") else {}
    if st != 200 or "user" not in js:
        print(f"LOGIN FAILED {st} {raw[:300]}")
        return 1
    print(f"  ok user={js['user'].get('username')} id={js['user'].get('id')} {dt:.2f}s")

    print("== state ==")
    st, s, dt = api("/rsc/state.json")
    print(f"  {st} {len(json.dumps(s))/1024/1024:.2f}MB {dt:.2f}s")
    with open("probes/state_snapshot.json", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    insts = s["instruments"]
    wallet0 = s["wallet"]["balance"]
    pos0 = {p["instrument_id"] for p in s.get("positions", [])}
    print(f"  wallet={wallet0} positions={len(pos0)} orders={len(s.get('orders', []))}")

    def pick(pred):
        return next((i for i in insts if pred(i)), None)

    btc = pick(lambda i: i["category"] == "crypto" and "BTC" in i["symbol"])
    fx = pick(lambda i: i["category"] == "forex" and i.get("execution_mode") == "forex_confirmation")
    co = pick(lambda i: i.get("close_only"))
    closed = pick(lambda i: i.get("market_closed") and i["category"] != "crypto")
    print(f"  btc={btc['symbol']}#{btc['id']} fx={fx['symbol'] if fx else None} "
          f"close_only={co['symbol'] if co else None} closed={closed['symbol'] if closed else None}")

    qmin = str(btc["minimum"])
    lev = str(btc["max_leverage"])

    print("== C1 幂等 ==")
    rid = str(uuid.uuid4())
    p = {"instrument_id": btc["id"], "side": "long", "quantity": qmin,
         "leverage": "1", "request_id": rid}
    st1, j1, _ = api("/rsc/orders.json", p)
    st2, j2, _ = api("/rsc/orders.json", p)
    case("C1a first order", "accepted", st1, j1)
    case("C1b replay same rid", "replayed=true same id", st2, j2,
         note=f"id1={j1.get('order_id')} id2={j2.get('order_id')}")

    print("== C2 杠杆越界 ==")
    st, j, _ = api("/rsc/orders.json", {"instrument_id": btc["id"], "side": "long",
                                        "quantity": qmin, "leverage": str(int(lev) + 1),
                                        "request_id": str(uuid.uuid4())})
    case("C2 leverage=max+1", "rejected", st, j)

    print("== C3 数量不符 step ==")
    st, j, _ = api("/rsc/orders.json", {"instrument_id": btc["id"], "side": "long",
                                        "quantity": "0.0000005", "leverage": "1",
                                        "request_id": str(uuid.uuid4())})
    case("C3 qty below minimum", "rejected", st, j)

    print("== C4 闭市 ==")
    if closed:
        st, j, _ = api("/rsc/orders.json", {"instrument_id": closed["id"], "side": "long",
                                            "quantity": str(closed["minimum"]), "leverage": "1",
                                            "request_id": str(uuid.uuid4())})
        case("C4 market closed", "rejected", st, j)

    print("== C5 close_only 开仓 ==")
    if co:
        st, j, _ = api("/rsc/orders.json", {"instrument_id": co["id"], "side": "long",
                                            "quantity": str(co["minimum"]), "leverage": "1",
                                            "request_id": str(uuid.uuid4())})
        case("C5 close_only open", "rejected", st, j)

    print("== C6 撤单 ==")
    if fx:
        st, j, _ = api("/rsc/orders.json", {"instrument_id": fx["id"], "side": "long",
                                            "quantity": str(fx["minimum"]), "leverage": "1",
                                            "request_id": str(uuid.uuid4())})
        case("C6a fx order", "pending", st, j)
        oid = j.get("order_id")
        if oid:
            st, j2b, _ = api(f"/rsc/orders/{oid}/cancel.json",
                             {"request_id": str(uuid.uuid4())})
            case("C6b cancel in window", "cancelled", st, j2b)
            st, j2c, _ = api(f"/rsc/orders/{oid}/cancel.json",
                             {"request_id": str(uuid.uuid4())})
            case("C6c cancel again", "rejected", st, j2c)
        st, j2d, _ = api(f"/rsc/orders/{oid}/cancel.json", {})
        case("C6d cancel no rid", "400 missing param", st, j2d)

    print("== C7 双向 / 换杠杆（基于 C1 的多头）==")
    st, j, _ = api("/rsc/orders.json", {"instrument_id": btc["id"], "side": "short",
                                        "quantity": qmin, "leverage": "1",
                                        "request_id": str(uuid.uuid4())})
    case("C7a opposite side", "rejected", st, j)
    st, j, _ = api("/rsc/orders.json", {"instrument_id": btc["id"], "side": "long",
                                        "quantity": qmin, "leverage": "2",
                                        "request_id": str(uuid.uuid4())})
    case("C7b add diff leverage", "rejected", st, j)

    print("== C8 止盈止损 ==")
    st, s2, _ = api("/rsc/state.json?section=market")
    pos = next((p for p in s2.get("positions", []) if p["instrument_id"] == btc["id"]), None)
    if pos:
        px = float(pos["average"])
        st, j, _ = api(f"/rsc/positions/{pos['id']}/protection.json",
                       {"take_profit": f"{px*1.05:.2f}", "stop_loss": f"{px*0.97:.2f}"})
        case("C8a set tp/sl", "ok", st, j)
        st, j, _ = api(f"/rsc/positions/{pos['id']}/protection.json",
                       {"take_profit": f"{px*0.10:.2f}", "stop_loss": f"{px*0.50:.2f}"})
        case("C8b tp < breakeven", "rejected", st, j)
        st, j, _ = api(f"/rsc/positions/{pos['id']}/protection.json",
                       {"take_profit": "", "stop_loss": ""})
        case("C8c clear tp/sl", "ok", st, j)

    print("== C9 强制刷新 ==")
    st, j, _ = api(f"/rsc/instruments/{fx['id'] if fx else btc['id']}/refresh.json", {})
    case("C9 refresh quote", "ok", st, j)

    print("== cleanup ==")
    st, s3, _ = api("/rsc/state.json?section=market")
    for p in s3.get("positions", []):
        if p["instrument_id"] == btc["id"]:
            st, j, _ = api("/rsc/orders.json", {
                "instrument_id": btc["id"], "side": "close", "quantity": p["quantity"],
                "leverage": str(p["leverage"]), "request_id": str(uuid.uuid4())})
            case("C10 close position", "ok", st, j)
    st, s4, _ = api("/rsc/state.json?section=wallet")
    print(f"  wallet {wallet0} -> {s4['wallet']['balance']}")

    with open("probes/results.json", "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=1)
    print(f"\n{len(RESULTS)} cases -> probes/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
