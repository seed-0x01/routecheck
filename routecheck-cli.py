#!/usr/bin/env python3
"""routecheck — quote one asset across every route and tell me which one is real.

Born from a real burn: on 2026-11-16 my SHIB position quoted 0.55 USDC via the
direct SHIB/USDC pool and 5.58 USDC via SHIB->WETH->USDC in the same block.
Ten times. Same tokens. The direct pool was just thin.

treasury.py quotes the pair you name. It does not tell you the pair you named is
the wrong one. This does: it walks every stable/ETH bridge to the target token
and ranks the realized output after gas, so a thin pool can't hide.

Read-only. No keys, no transactions. Quotes are free eth_calls.

  ./routecheck.py <SYM> --chain ethereum --amount 1445399 --to USDC
  ./routecheck.py SHIB --chain ethereum            # full route table
"""
import argparse
import json
import sys

sys.path.insert(0, "/opt/data/bin")
import treasury as T  # noqa: E402

# Route bridges: assets that most tokens have a deep pool against.
BRIDGES = ["WETH", "USDC", "USDT", "DAI"]


def quote_best(chain, tin, tout, amt):
    """Best fee-tier quote for one hop, or None."""
    best = None
    for fee in T.FEE_TIERS:
        try:
            q = T.quote_single(chain, tin[0], tout[0], amt, fee)
        except Exception:  # noqa: BLE001
            q = None
        if q and (best is None or q > best[1]):
            best = (fee, q)
    return best


def route_direct(chain, src, dst, amt):
    q = quote_best(chain, src, dst, amt)
    if not q:
        return None
    return {"path": [src[2], dst[2]], "fee": q[0], "out": q[1], "decimals": dst[1],
            "hops": 1}


def route_via(chain, src, dst, amt, bridge_sym):
    """src -> bridge -> dst. Returns realized dst units."""
    tbl = T.token_table(chain)
    if bridge_sym not in tbl:
        return None
    bridge = (tbl[bridge_sym][0], tbl[bridge_sym][1], bridge_sym)
    if src[0].lower() == bridge[0].lower() or dst[0].lower() == bridge[0].lower():
        return None
    h1 = quote_best(chain, src, bridge, amt)
    if not h1:
        return None
    h2 = quote_best(chain, bridge, dst, h1[1])
    if not h2:
        return None
    return {"path": [src[2], bridge_sym, dst[2]], "fee": h1[0],
            "out": h2[1], "decimals": dst[1], "hops": 2,
            "hop1_fee": h1[0], "hop2_fee": h2[0]}


def price_usdc(chain, token_sym, units, decimals, chain_usdc_per_eth):
    """Value any token in USDC terms via its best route to USDC."""
    tbl = T.token_table(chain)
    if token_sym == "USDC":
        return units / 10 ** decimals
    if token_sym in ("WETH", "ETH"):
        return units / 10 ** decimals * chain_usdc_per_eth
    src = (tbl[token_sym][0], tbl[token_sym][1], token_sym)
    dst = (tbl["USDC"][0], tbl["USDC"][1], "USDC")
    r = route_direct(chain, src, dst, units)
    if r:
        return r["out"] / 10 ** dst[1]
    r = route_via(chain, src, dst, units, "WETH")
    if r:
        return r["out"] / 10 ** dst[1]
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("symbol", help="token to route (e.g. SHIB)")
    p.add_argument("--chain", default="ethereum", choices=list(T.CHAINS))
    p.add_argument("--amount", type=float, default=None,
                   help="units to sell; default = whole wallet balance")
    p.add_argument("--to", default="USDC", help="destination token (default USDC)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    chain = a.chain
    mnem = T.load_key()
    owner = T.derive_address(mnem)

    # resolve source
    src = T.resolve(chain, a.symbol)
    if src is None:
        print("native ETH input not supported; use WETH", file=sys.stderr)
        return 2
    dst = T.resolve(chain, a.to)
    if dst is None:
        print("destination must be an ERC-20", file=sys.stderr)
        return 2

    # amount: explicit or whole balance
    if a.amount is None:
        raw = T.erc20_balance(chain, src[0], owner)
        if raw == 0:
            print(f"no {src[2]} balance on {chain}", file=sys.stderr)
            return 1
        amt = raw
    else:
        amt = T.to_units(a.amount, src[1])

    # ETH price for gas math
    tbl = T.token_table(chain)
    weth = (tbl["WETH"][0], tbl["WETH"][1], "WETH")
    usdc = (tbl["USDC"][0], tbl["USDC"][1], "USDC")
    q_eth = quote_best(chain, weth, usdc, 10 ** 18)
    eth_usdc = (q_eth[1] / 1e6) if q_eth else 0.0

    # gas
    blk = T.rpc(chain, "eth_getBlockByNumber", ["latest", False])
    base = int(blk["baseFeePerGas"], 16)
    swap_gas_eth = base * 2 * 150_000 / 1e18
    swap_gas_usd = swap_gas_eth * eth_usdc
    approve_gas_usd = base * 2 * 60_000 / 1e18 * eth_usdc

    routes = []
    d = route_direct(chain, src, dst, amt)
    if d:
        d["gas_usd"] = swap_gas_usd + (approve_gas_usd if src[2] != "WETH" else 0)
        d["net_usdc"] = d["out"] / 10 ** d["decimals"] - d["gas_usd"]
        d["gross_usdc"] = d["out"] / 10 ** d["decimals"]
        routes.append(d)

    for b in BRIDGES:
        if b == dst[2]:
            continue
        v = route_via(chain, src, dst, amt, b)
        if v:
            v["gas_usd"] = 2 * swap_gas_usd + (approve_gas_usd if src[2] != "WETH" else 0)
            v["net_usdc"] = v["out"] / 10 ** v["decimals"] - v["gas_usd"]
            v["gross_usdc"] = v["out"] / 10 ** v["decimals"]
            routes.append(v)

    if not routes:
        print("no route found", file=sys.stderr)
        return 1

    routes.sort(key=lambda r: r["net_usdc"], reverse=True)
    best, worst = routes[0], routes[-1]
    spread = (best["net_usdc"] / worst["net_usdc"]) if worst["net_usdc"] > 0 else float("inf")

    out = {
        "chain": chain, "asset": src[2], "amount_in": T.from_units(amt, src[1]),
        "target": dst[2], "eth_usd": round(eth_usdc, 2),
        "gas_swap_usd": round(swap_gas_usd, 4),
        "best": {"path": best["path"], "net_usdc": round(best["net_usdc"], 4)},
        "worst": {"path": worst["path"], "net_usdc": round(worst["net_usdc"], 4)},
        "spread_x": round(spread, 2),
        "routes": [{"path": " -> ".join(r["path"]), "gross_usdc": round(r["gross_usdc"], 4),
                    "gas_usd": round(r["gas_usd"], 4), "net_usdc": round(r["net_usdc"], 4),
                    "hops": r["hops"]} for r in routes],
    }
    if a.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"\n  {out['amount_in']} {out['asset']} on {chain} -> {out['target']}")
    print(f"  ETH ${eth_usdc:,.2f}   swap gas ${swap_gas_usd:.4f}\n")
    print(f"  {'route':<34} {'gross':>10} {'gas':>8} {'net':>10}")
    print(f"  {'-'*34} {'-'*10} {'-'*8} {'-'*10}")
    for r in out["routes"]:
        mark = " <<< BEST" if r is out["routes"][0] else ""
        print(f"  {' -> '.join(r['path'].split(' -> ')):<34} "
              f"{r['gross_usdc']:>10.4f} {r['gas_usd']:>8.4f} {r['net_usdc']:>10.4f}{mark}")
    print(f"\n  spread: {out['spread_x']}x between best and worst route")
    if out["spread_x"] > 1.5:
        print(f"  !! taking the worst route would have cost "
              f"${worst['net_usdc'] and (best['net_usdc']-worst['net_usdc']):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
