#!/usr/bin/env python3
"""networth — my true liquid position across every chain.

The host's vitals oracle (/etc/organism/vitals.py) reads ONLY Base. It is
mounted read-only, so I cannot fix it — but I can stop being lied to by it.

On 2026-11-16 vitals.json reported 14.22 USDC while 5.57 USDC sat on Ethereum
L1 in a SHIB position the oracle had never seen. A $5.27 blind spot, 27% of my
actual net worth, invisible to the one file I am told to trust for survival.

This computes the honest number: every chain, every token, valued in USDC
through the deepest available route (not the first pool named).

  networth.py            # human table
  networth.py --json     # machine
"""
import json
import sys

sys.path.insert(0, "/opt/data/bin")
import treasury as T  # noqa: E402


def value_token(chain, sym, units, decimals, eth_usdc, tbl):
    """USDC value of a holding, routed for the best price."""
    if sym == "USDC":
        return units / 10 ** decimals
    if sym in ("WETH", "ETH"):
        return units / 10 ** decimals * eth_usdc
    if sym not in tbl:
        return None
    src = (tbl[sym][0], tbl[sym][1], sym)
    dst = (tbl["USDC"][0], tbl["USDC"][1], "USDC")

    def best(tin, tout, amt):
        b = None
        for fee in T.FEE_TIERS:
            try:
                q = T.quote_single(chain, tin[0], tout[0], amt, fee)
            except Exception:  # noqa: BLE001
                q = None
            if q and (b is None or q > b[1]):
                b = (fee, q)
        return b

    d = best(src, dst, units)
    if d:
        return d[1] / 10 ** dst[1]
    weth = (tbl["WETH"][0], tbl["WETH"][1], "WETH")
    h1 = best(src, weth, units)
    if not h1:
        return None
    h2 = best(weth, dst, h1[1])
    if not h2:
        return None
    return h2[1] / 10 ** dst[1]


def main():
    as_json = "--json" in sys.argv
    mnem = T.load_key()
    owner = T.derive_address(mnem)

    chains = {}
    total = 0.0
    eth_usdc = {}
    for ch in T.CHAINS:
        tbl = T.token_table(ch)
        try:
            q = None
            for fee in T.FEE_TIERS:
                try:
                    r = T.quote_single(ch, tbl["WETH"][0], tbl["USDC"][0], 10 ** 18, fee)
                except Exception:  # noqa: BLE001
                    r = None
                if r and (q is None or r > q):
                    q = r
            eth_usdc[ch] = (q / 1e6) if q else 0.0
        except Exception:  # noqa: BLE001
            eth_usdc[ch] = 0.0

    for ch in T.CHAINS:
        tbl = T.token_table(ch)
        entry = {"tokens": {}, "subtotal_usdc": 0.0}
        # native ETH
        try:
            wei = int(T.rpc(ch, "eth_getBalance", [owner, "latest"]), 16)
            if wei > 0:
                v = wei / 1e18 * eth_usdc[ch]
                entry["tokens"]["ETH"] = {
                    "amount": T.from_units(wei, 18), "usdc": round(v, 6)}
                entry["subtotal_usdc"] += v
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)[:80]
        for sym, (addr, dec) in sorted(tbl.items()):
            try:
                raw = T.erc20_balance(ch, addr, owner)
            except Exception:  # noqa: BLE001
                continue
            if raw <= 0:
                continue
            v = value_token(ch, sym, raw, dec, eth_usdc[ch], tbl)
            if v is None:
                continue
            entry["tokens"][sym] = {"amount": T.from_units(raw, dec), "usdc": round(v, 6)}
            entry["subtotal_usdc"] += v
        entry["subtotal_usdc"] = round(entry["subtotal_usdc"], 6)
        chains[ch] = entry
        total += entry["subtotal_usdc"]

    # compare against the host oracle
    oracle = None
    try:
        with open("/opt/data/readonly/vitals.json", encoding="utf-8") as f:
            oracle = json.load(f)
    except Exception:  # noqa: BLE001
        pass

    out = {
        "address": owner,
        "total_usdc": round(total, 6),
        "by_chain": chains,
        "eth_usd": {k: round(v, 2) for k, v in eth_usdc.items()},
        "oracle_usdc_base": oracle.get("usdc_base") if oracle else None,
        "oracle_undervalues_by": (round(total - oracle["usdc_base"], 6)
                                  if oracle and oracle.get("usdc_base") is not None else None),
        "oracle_blind_chains": [c for c in T.CHAINS if c != "base"],
    }
    if as_json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"\n  {owner}\n")
    for ch, e in out["by_chain"].items():
        print(f"  {T.CHAINS[ch]['label']}  (ETH ${out['eth_usd'][ch]:,.2f})")
        for sym, d in e["tokens"].items():
            print(f"    {sym:<8} {d['amount']:>22}  ${d['usdc']:>10,.4f}")
        print(f"    {'':<8} {'subtotal':>22}  ${e['subtotal_usdc']:>10,.4f}\n")
    print(f"  {'TOTAL':<8} {'':>22}  ${out['total_usdc']:>10,.4f}")
    if out["oracle_undervalues_by"] is not None:
        d = out["oracle_undervalues_by"]
        print(f"\n  host vitals.json (Base only): ${out['oracle_usdc_base']:,.4f}")
        if abs(d) >= 0.01:
            print(f"  !! oracle is off by ${d:+,.4f} — it cannot see "
                  f"{', '.join(out['oracle_blind_chains'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
