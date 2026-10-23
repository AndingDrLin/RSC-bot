"""第三轮：protection 端点、成功撤单、close_only 真实拒绝原因、全量 state 时延。"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from rscclient import Client  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
OUT = {}


def show(tag, st, js, dt=None):
    print(f"  {tag:<28} http={st} {json.dumps(js, ensure_ascii=False)[:240]}")
    OUT[tag] = {"http": st, "body": js, "seconds": round(dt, 2) if dt else None}


def main():
    c = Client(os.environ["RSC_EMAIL"], os.environ["RSC_PASSWORD"])
    c.login()

    t0 = time.time()
    st, s, dt = c.state()
    print(f"full state: {st} {dt:.1f}s {len(json.dumps(s))/1048576:.2f}MB")
    OUT["full_state"] = {"http": st, "seconds": round(dt, 2)}
    insts = {i["id"]: i for i in s["instruments"]}
    held = {p["instrument_id"] for p in s["positions"]}

    # A) protection：带 request_id
    pos = next((p for p in s["positions"] if "NVDA" in p["symbol"]), None)
    if pos:
        px = float(pos["average"])
        show("A1 set tp/sl", *c.api(
            f"/rsc/positions/{pos['id']}/protection.json",
            {"take_profit": f"{px*1.08:.2f}", "stop_loss": f"{px*0.96:.2f}",
             "request_id": "probe-" + str(int(time.time()))}))
        show("A2 tp below breakeven", *c.api(
            f"/rsc/positions/{pos['id']}/protection.json",
            {"take_profit": f"{px*0.5:.2f}", "stop_loss": f"{px*0.96:.2f}",
             "request_id": "probe-" + str(int(time.time()))}))
        show("A3 sl beyond liquidation", *c.api(
            f"/rsc/positions/{pos['id']}/protection.json",
            {"take_profit": f"{px*1.08:.2f}", "stop_loss": "1.00",
             "request_id": "probe-" + str(int(time.time()))}))
        show("A4 clear", *c.api(
            f"/rsc/positions/{pos['id']}/protection.json",
            {"take_profit": "", "stop_loss": "",
             "request_id": "probe-" + str(int(time.time()))}))

    # B) 成功撤单：找一个开市、非延迟、未持仓的美股
    cand = next((i for i in insts.values()
                 if i["category"] == "us" and i["id"] not in held
                 and not i.get("market_closed") and (i.get("quote") or {}).get("delay_seconds") == 0), None)
    if cand:
        st, j, _ = c.order(cand["id"], "long", str(cand["minimum"]), "1")
        show(f"B1 order {cand['symbol']}", st, j)
        oid = j.get("order_id")
        if oid:
            show("B2 cancel immediately", *c.cancel(oid))
            show("B3 inspect order", *c.state("market"))
            OUT["B3_inspect"] = {"note": "见 orders 中该单状态"}

    # C) close_only：先 refresh 再看拒绝原因
    co = next((i for i in insts.values() if i.get("close_only")), None)
    if co:
        show(f"C0 refresh {co['symbol']}", *c.api(f"/rsc/instruments/{co['id']}/refresh.json", {}))
        time.sleep(2)
        show(f"C1 open close_only {co['symbol']}", *c.order(co["id"], "long", str(co["minimum"]), "1"))

    # D) 待成交上限：同一品种连发 2 笔（US 股 immediate，若首笔即成交则第二笔是加仓）
    if cand:
        st, j, _ = c.order(cand["id"], "long", str(cand["minimum"]), "1")
        show("D2 same instrument 2nd", st, j)

    json.dump(OUT, open("probes/results3.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nwallet:", s["wallet"]["balance"], "-> re-check next run")


if __name__ == "__main__":
    main()
