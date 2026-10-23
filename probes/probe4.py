"""第四轮：crypto 上的 protection 全链路 + FX 上的成功撤单。"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from rscclient import Client  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
OUT = {}


def show(tag, st, js, dt=None):
    print(f"  {tag:<30} http={st} {json.dumps(js, ensure_ascii=False)[:230]}")
    OUT[tag] = {"http": st, "body": js, "seconds": round(dt, 2) if dt else None}


def rid():
    return "probe-" + str(int(time.time() * 1000))


def main():
    c = Client(os.environ["RSC_EMAIL"], os.environ["RSC_PASSWORD"])
    c.login()
    st, s, dt = c.state()
    insts = {i["symbol"]: i for i in s["instruments"]}
    held = {p["instrument_id"] for p in s["positions"]}
    btc = insts["CRYPTO:BTC/USD"]
    qmin = str(btc["minimum"])
    print(f"BTC price {(btc.get('quote') or {}).get('price')} min={qmin}")

    # 1) 成功撤单：FX:EUR（forex_confirmation，报价轮到才成交）
    fx = next((i for i in insts.values()
               if i["category"] == "forex" and i["id"] not in held
               and not i.get("market_closed")), None)
    if fx:
        st, j, dt = c.api("/rsc/orders.json", {
            "instrument_id": fx["id"], "side": "long",
            "quantity": str(fx["minimum"]), "leverage": "1", "request_id": rid()})
        show(f"F1 order {fx['symbol']}", st, j, dt)
        oid = j.get("order_id")
        if oid:
            time.sleep(0.5)
            show("F2 cancel within window", *c.cancel(oid))
            show("F3 cancel again", *c.cancel(oid))

    # 2) crypto：下单即带止盈止损
    px = float(btc["quote"]["price"])
    st, j, dt = c.api("/rsc/orders.json", {
        "instrument_id": btc["id"], "side": "long", "quantity": qmin,
        "leverage": "1",
        "take_profit": f"{px*1.05:.2f}", "stop_loss": f"{px*0.97:.2f}",
        "request_id": rid()})
    show("P1 order with tp/sl", st, j, dt)
    oid = j.get("order_id")

    time.sleep(2)
    st, s2, _ = c.state("market")
    pos = next((p for p in s2.get("positions", []) if p["instrument_id"] == btc["id"]), None)
    if pos:
        print(f"  position #{pos['id']} qty={pos['quantity']} avg={pos['average']} "
              f"tp={pos.get('take_profit')} sl={pos.get('stop_loss')} "
              f"liq={pos.get('liquidation')} be={pos.get('break_even')}")
        show("P2 set tp/sl (valid)", *c.api(
            f"/rsc/positions/{pos['id']}/protection.json",
            {"take_profit": f"{px*1.06:.2f}", "stop_loss": f"{px*0.96:.2f}",
             "request_id": rid()}))
        show("P3 tp below breakeven", *c.api(
            f"/rsc/positions/{pos['id']}/protection.json",
            {"take_profit": f"{px*0.90:.2f}", "stop_loss": f"{px*0.96:.2f}",
             "request_id": rid()}))
        show("P4 sl beyond liquidation", *c.api(
            f"/rsc/positions/{pos['id']}/protection.json",
            {"take_profit": f"{px*1.06:.2f}", "stop_loss": f"{px*0.10:.2f}",
             "request_id": rid()}))
        show("P5 clear tp/sl", *c.api(
            f"/rsc/positions/{pos['id']}/protection.json",
            {"take_profit": "", "stop_loss": "", "request_id": rid()}))
        show("P6 close", *c.order(btc["id"], "close", pos["quantity"], str(pos["leverage"])))

    json.dump(OUT, open("probes/results4.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\ndone")


if __name__ == "__main__":
    main()
