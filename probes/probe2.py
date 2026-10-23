"""第二轮补测：解释资金变动 + 补 C5/C6/C8 的真实错误码。"""

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(__file__))
from rscclient import Client  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def main():
    c = Client(os.environ["RSC_EMAIL"], os.environ["RSC_PASSWORD"])
    u = c.login()
    print("login", u["username"], u["id"])

    # 1) 账本：解释 608.0738 -> 607.9921 的 0.0817 去哪了
    st, h, _ = c.api("/rsc/history.json?per_page=12&page=0")
    print("\n== history ==", st)
    for e in h.get("entries", [])[:12]:
        print(f"  {e.get('created_at','')[:19]} {e.get('operation'):<14} "
              f"amt={e.get('amount')} bal_after={e.get('balance_after')} "
              f"{e.get('journal_id') or ''} {str(e.get('details'))[:80]}")

    # 2) 订单 5586/5587 的真实成交明细
    st, s, _ = c.state("market")
    for o in s.get("orders", []):
        if o["id"] in (5586, 5587):
            d = o.get("details") or {}
            print(f"\n  order {o['id']} {o['side']} {o['symbol']} qty={o['quantity']} "
                  f"lev={o['leverage']} status={o['status']}")
            print(f"    price={d.get('price')} ref={d.get('reference_price')} "
                  f"fee={d.get('fee')} pnl={d.get('pnl')} gross={d.get('gross')}")
    print("  wallet now:", s["wallet"]["balance"])

    # 3) C8 重测：protection 需要 request_id
    pos = next((p for p in s["positions"] if p["symbol"] == "NVDA"), None)
    if pos:
        px = float(pos["average"])
        rid = str(uuid.uuid4())
        st, j, _ = c.api(f"/rsc/positions/{pos['id']}/protection.json",
                         {"take_profit": f"{px*1.10:.2f}",
                          "stop_loss": f"{px*0.95:.2f}", "request_id": rid})
        print(f"\n  C8a tp/sl+rid -> {st} {json.dumps(j, ensure_ascii=False)[:200]}")
        st, j, _ = c.api(f"/rsc/positions/{pos['id']}/protection.json",
                         {"take_profit": f"{px*0.50:.2f}",
                          "stop_loss": f"{px*0.95:.2f}", "request_id": str(uuid.uuid4())})
        print(f"  C8b tp<breakeven -> {st} {json.dumps(j, ensure_ascii=False)[:200]}")
        st, j, _ = c.api(f"/rsc/positions/{pos['id']}/protection.json",
                         {"take_profit": "", "stop_loss": "",
                          "request_id": str(uuid.uuid4())})
        print(f"  C8c clear -> {st} {json.dumps(j, ensure_ascii=False)[:200]}")

    # 4) C6 重测：找一个无持仓、开市的 forex 对，下单后立刻撤
    held = {p["instrument_id"] for p in s["positions"]}
    fx = next((i for i in s["instruments"]
               if i["category"] == "forex" and i["id"] not in held
               and not i.get("market_closed")), None)
    if fx:
        st, j, _ = c.order(fx["id"], "long", str(fx["minimum"]), "1")
        print(f"\n  C6a {fx['symbol']} order -> {st} {json.dumps(j, ensure_ascii=False)[:160]}")
        oid = j.get("order_id")
        if oid:
            st2, j2, _ = c.cancel(oid)
            print(f"  C6b cancel -> {st2} {json.dumps(j2, ensure_ascii=False)[:160]}")
            st3, j3, _ = c.cancel(oid)
            print(f"  C6c cancel again -> {st3} {json.dumps(j3, ensure_ascii=False)[:160]}")

    # 5) C5 重测：close_only 先 refresh 再开仓
    co = next((i for i in s["instruments"] if i.get("close_only")), None)
    if co:
        st, j, _ = c.api(f"/rsc/instruments/{co['id']}/refresh.json", {})
        print(f"\n  C5a refresh {co['symbol']} -> {st}")
        time.sleep(1.5)
        st, j, _ = c.order(co["id"], "long", str(co["minimum"]), "1")
        print(f"  C5b open close_only -> {st} {json.dumps(j, ensure_ascii=False)[:220]}")

    # 6) 待成交上限（R05）：同一品种连发 2 笔会怎样
    print("\n  R05 probe: 用 FX 品种连发 2 笔（第 2 笔应 conflict 或 pending 上限）")
    print("  -> 已由 C6a 的 position_conflict 间接覆盖，跳过以免刷单")


if __name__ == "__main__":
    main()
