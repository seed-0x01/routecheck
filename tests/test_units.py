#!/usr/bin/env python3
"""Fixtures for the unit-contract fix (soul#7).

Pure conversion fixtures need no network. The live fixtures start a local copy
of routecheck-server.py and quote against Ethereum mainnet, so they prove
behavior rather than syntax. Set TREASURY_PATH if treasury.py is not on the path.

    python3 tests/test_units.py
"""
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("TREASURY_PATH", "/opt/data/bin"))
spec = importlib.util.spec_from_file_location(
    "rc_server", os.path.join(ROOT, "routecheck-server.py"))
assert spec is not None and spec.loader is not None
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += ok
    FAIL += (not ok)
    print(("PASS " if ok else "FAIL ") + name + " | got=" + repr(got) + " want=" + repr(want))


check("human WETH 1 -> 1e18 raw", R.parse_amount("1", 18, "human"), 10**18)
check("human WETH 1e18 -> 1e36 raw", R.parse_amount("1000000000000000000", 18, "human"), 10**36)
check("raw WETH 1e18 -> 1e18 raw", R.parse_amount("1000000000000000000", 18, "raw"), 10**18)
check("human USDC 1.5 -> 1500000 raw", R.parse_amount("1.5", 6, "human"), 1_500_000)
check("human WETH 0.0001 -> 1e14 raw", R.parse_amount("0.0001", 18, "human"), 10**14)
for bad in (("1.5", 18, "raw"), ("-1", 18, "raw"), ("1", 18, "weird")):
    try:
        R.parse_amount(*bad)
        check("reject %r" % (bad,), "accepted", "ValueError")
    except ValueError:
        check("reject %r" % (bad,), "ValueError", "ValueError")

srv = ThreadingHTTPServer(("127.0.0.1", 8099), R.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def get(qs):
    url = "http://127.0.0.1:8099/api/quote?chain=ethereum&asset=WETH&target=USDC&" + qs
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


try:
    one = get("amount=1")
    raw = get("amount=1000000000000000000&unit=raw")
    huge = get("amount=1000000000000000000")
    print("--- live fixtures (fixed server on 8099, real mainnet RPC) ---")
    check("1 WETH human gross ~= 1 WETH raw gross",
          abs(one["best"]["gross"] - raw["best"]["gross"]) < 1.0, True)
    check("unit_warning absent for 1 WETH", "unit_warning" in one, False)
    check("unit_warning present for 1e18 human", "unit_warning" in huge, True)
    check("raw interpretation echoed", raw["amount_unit"], "raw")
    check("raw amount_human is 1 WETH", raw["amount_human"], "1.0")
    check("huge implied price << spot", huge["implied_vs_eth_usd"] < 1e-6, True)
    check("1 WETH implied price ~= spot", abs(one["implied_vs_eth_usd"] - 1.0) < 0.05, True)
    print("SPOT eth_usd=%s | 1 WETH implied=%s | 1e18 human implied=%s"
          % (huge["eth_usd"], one["best_implied_usd_per_token"],
             huge["best_implied_usd_per_token"]))
finally:
    srv.shutdown()

print("\n%d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
