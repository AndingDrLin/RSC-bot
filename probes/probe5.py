"""第五轮：补完 P2–P6（BTC 持仓的止盈止损校验与平仓）。"""

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
    OUT[tag] = {"http": st, "body": js}


def rid():
    return "probe-" + str(int(time.time() * 1000))


def main():
    c = Client(os.environ["RSC_EMAIL"], os.environ["RSC_PASSWORD"])
    c.login()
    st, s, _ = c.state("market")
    for o in s.get("orders", []):
        if o["id"] == 5590:
            print(f"  order 5590 {o['status']} {o.get('details')}")
    pos = next((p for p in s.get("positions", []) if "BTC" in p["symbol"]), None)
    if not pos:
        print("no BTC position; nothing to do")
        return
    px = float(pos["average"])
    print(f"  pos #{pos['id']} qty={pos['quantity']} avg={px} lev={pos['leverage']} "
          f"tp={pos.get('take_profit')} sl={pos.get('stop_loss')} "
          f"liq={pos.get('liquidation')} be={pos.get('break_even')}")

    show("P2 set tp/sl (valid)", *c.api(
        f"/rsc/positions/{pos['id']}/protection.json",
        {"take_profit": f"{px*1.06:.2f}", "stop_loss": f"{px*0.96:.2f}", "request_id": rid()}))
    show("P3 tp below breakeven", *c.api(
        f"/rsc/positions/{pos['id']}/protection.json",
        {"take_profit": f"{px*0.90:.2f}", "stop_loss": f"{px*0.96:.2f}", "request_id": rid()}))
    show("P4 sl beyond liquidation", *c.api(
        f"/rsc/positions/{pos['id']}/protection.json",
        {"take_profit": f"{px*1.06:.2f}", "stop_loss": f"{px*0.10:.2f}", "request_id": rid()}))
    show("P5 clear tp/sl", *c.api(
        f"/rsc/positions/{pos['id']}/protection.json",
        {"take_profit": "", "stop_loss": "", "request_id": rid()}))
    st, s2, _ = c.state("market")
    p2 = next((p for p in s2.get("positions", []) if p["id"] == pos["id"]), None)
    print("  after clear:", {k: p2.get(k) for k in ("take_profit", "stop_loss")} if p2 else None)
    show("P6 close", *c.order(pos["instrument_id"], "close", pos["quantity"], str(pos["leverage"])))
    json.dump(OUT, open("probes/results5.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
