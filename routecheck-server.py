#!/usr/bin/env python3
"""Tallow — routecheck server.

Serves the public web UI and a JSON API that quotes one asset across every
Uniswap V3 route, ranks them after gas, and reports the spread.

Read-only for quotes. Since paidgate, GET /paid/swapcheck loads the signing
key ONCE PER SETTLED PAYMENT to emit an EIP-191-signed read-only report; it
never signs a transaction or an x402 payment and never exposes the key. Every
other route stays key-free.
"""
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, "/opt/data/bin")
import treasury as T  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

# The Courtyard front door (resident decision, 2026-11-20). The atlas bundle is
# a self-contained reader shipped in seed-0x01/town; it reads published JSON and
# never bakes a score into HTML. It is served from /atlas/ and published at /.
ATLAS_DIR = os.path.join(HERE, "atlas")
ATLAS_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".svg": "image/svg+xml",
}
from presence import handle as presence_handle  # noqa: E402
import paidgate  # noqa: E402 — x402 v2 paywalled /paid/swapcheck route

CACHE = {}
CACHE_TTL = 30  # seconds; quotes move, but not that fast


def parse_amount(amt, decimals, unit):
    """Turn an API `amount` into raw base units, with an explicit unit contract.

    This is the fix for mentor issue soul#7. Before this, `amount` was always
    interpreted as a HUMAN quantity: `amount=1000000000000000000` meant
    a quintillion WETH, not 1 WETH in raw base units. The quote was real but
    the caller's mental model was not, and the response gave no signal that
    the input had been read that way. Both interpretations are now explicit.
    """
    if unit not in ("human", "raw"):
        raise ValueError("unit must be 'human' (default) or 'raw' (base units)")
    s = str(amt).strip()
    if unit == "raw":
        if not s.isdigit():
            raise ValueError("amount must be a non-negative integer when unit=raw")
        return int(s)
    return T.to_units(s, decimals)


def do_quote(chain, src_sym, dst_sym, amount_units, src_dec, dst_dec):
    """Core routing logic. Read-only. Returns a dict."""
    key = (chain, src_sym, dst_sym, amount_units)
    now = time.time()
    if key in CACHE and now - CACHE[key][0] < CACHE_TTL:
        return CACHE[key][1]

    src = T.resolve(chain, src_sym)
    dst = T.resolve(chain, dst_sym)
    if src is None or dst is None:
        raise ValueError("both tokens must be ERC-20s")

    tbl = T.token_table(chain)
    weth = (tbl["WETH"][0], tbl["WETH"][1], "WETH")
    usdc = (tbl["USDC"][0], tbl["USDC"][1], "USDC")

    def best_hop(tin, tout, amt):
        b = None
        for fee in T.FEE_TIERS:
            try:
                q = T.quote_single(chain, tin[0], tout[0], amt, fee)
            except Exception:  # noqa: BLE001
                q = None
            if q and (b is None or q > b[1]):
                b = (fee, q)
        return b

    q_eth = best_hop(weth, usdc, 10 ** 18)
    eth_usdc = (q_eth[1] / 1e6) if q_eth else 0.0

    blk = T.rpc(chain, "eth_getBlockByNumber", ["latest", False])
    base = int(blk["baseFeePerGas"], 16)
    swap_gas = base * 2 * 150_000 / 1e18 * eth_usdc
    approve_gas = base * 2 * 60_000 / 1e18 * eth_usdc

    routes = []

    d = best_hop(src, dst, amount_units)
    if d:
        gross = d[1] / 10 ** dst[1]
        gas = swap_gas + (approve_gas if src_sym.upper() != "WETH" else 0)
        routes.append({"path": [src[2], dst[2]], "fee": d[0], "gross": gross,
                       "gas": gas, "net": gross - gas, "hops": 1})

    for b in ("WETH", "USDC", "USDT", "DAI"):
        if b not in tbl or b == dst[2].upper():
            continue
        br = (tbl[b][0], tbl[b][1], b)
        if src[0].lower() == br[0].lower() or dst[0].lower() == br[0].lower():
            continue
        h1 = best_hop(src, br, amount_units)
        if not h1:
            continue
        h2 = best_hop(br, dst, h1[1])
        if not h2:
            continue
        gross = h2[1] / 10 ** dst[1]
        gas = 2 * swap_gas + (approve_gas if src_sym.upper() != "WETH" else 0)
        routes.append({"path": [src[2], b, dst[2]], "fee": h1[0], "gross": gross,
                       "gas": gas, "net": gross - gas, "hops": 2})

    if not routes:
        raise ValueError("no route found")

    routes.sort(key=lambda r: r["net"], reverse=True)
    best, worst = routes[0], routes[-1]
    spread = (best["net"] / worst["net"]) if worst["net"] > 0 and len(routes) > 1 else None

    out = {
        "chain": chain, "asset": src[2], "target": dst[2],
        "amount_in": T.from_units(amount_units, src[1]),
        "eth_usd": round(eth_usdc, 2),
        "swap_gas_usd": round(swap_gas, 4),
        "routes": [{"path": " -> ".join(r["path"]), "fee": r["fee"],
                    "gross": round(r["gross"], 4), "gas": round(r["gas"], 4),
                    "net": round(r["net"], 4), "hops": r["hops"]} for r in routes],
        "best": {"path": " -> ".join(best["path"]), "gross": round(best["gross"], 4),
                 "net": round(best["net"], 4)},
        "worst": {"path": " -> ".join(worst["path"]), "gross": round(worst["gross"], 4),
                  "net": round(worst["net"], 4)},
        "spread_x": round(spread, 2) if spread else None,
        "cost_of_wrong_route": round(best["net"] - worst["net"], 4),
        "block": int(blk["number"], 16),
        "ts": int(time.time()),
    }
    CACHE[key] = (now, out)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "Tallow/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        """CORS preflight for x402 clients.

        A browser sending the custom PAYMENT-SIGNATURE header must preflight
        first. Without this, BaseHTTPRequestHandler answers 501 and a wallet
        client can never reach /paid/swapcheck, even though the GET handler is
        correct. Allow only read methods; never widen this to writes.
        """
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, PAYMENT-SIGNATURE, X-PAYMENT, "
                         "PAYMENT-REQUIRED, PAYMENT-RESPONSE")
        self.send_header("Access-Control-Expose-Headers",
                         "PAYMENT-REQUIRED, PAYMENT-RESPONSE")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path

        if presence_handle(self, p, "GET"):
            return

        if paidgate.handle(self, "GET", p):
            return

        # The Courtyard front door. The atlas reader is published at / and
        # /index.html; routecheck (the old artifact) stays at /routecheck.html.
        # The reader's assets are relative (./styles.css, ./data/*.json), so a
        # <base href="/atlas/"> is injected when serving from / — otherwise the
        # browser resolves them to /styles.css etc. and the page renders broken.
        if p in ("/", "/index.html"):
            try:
                with open(os.path.join(ATLAS_DIR, "index.html"), "r",
                          encoding="utf-8") as f:
                    html = f.read()
                if "<base " not in html:
                    html = html.replace("<head>",
                                        '<head>\n<base href="/atlas/" />', 1)
                # Name comes from the published world.json, not a baked string:
                # Tallow renamed it in commit 91a3e4. A raw GET must carry the
                # current name too, not only the browser after app.js runs.
                try:
                    with open(os.path.join(ATLAS_DIR, "data", "world.json"),
                              "r", encoding="utf-8") as wf:
                        site_name = json.load(wf)["meta"]["siteName"]
                    html = re.sub(r"<title>.*?</title>",
                                  "<title>%s — town reader</title>" % site_name,
                                  html, count=1)
                except (OSError, KeyError, ValueError):
                    pass  # leave the bundled title if the source is unreadable
                self._send(200, html, "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "the atlas bundle is not installed", "text/plain")
            return

        if p == "/routecheck.html":
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "not found", "text/plain")
            return

        # Static files inside the atlas bundle. Restrict to a fixed suffix set,
        # resolve without symlinks, and require the result to stay inside the
        # bundle directory — never let a reader walk out with ../ or a symlink.
        if p.startswith("/atlas/"):
            rel = p[len("/atlas/"):]
            ext = os.path.splitext(rel)[1].lower()
            if not rel or ".." in rel.split("/") or ext not in ATLAS_MIME:
                self._send(404, "not found", "text/plain")
                return
            base = os.path.realpath(ATLAS_DIR) + os.sep
            target = os.path.realpath(os.path.join(ATLAS_DIR, rel))
            if not target.startswith(base):
                self._send(404, "not found", "text/plain")
                return
            try:
                with open(target, "rb") as f:
                    self._send(200, f.read(), ATLAS_MIME[ext])
            except (FileNotFoundError, IsADirectoryError):
                self._send(404, "not found", "text/plain")
            return

        # Static sibling files (e.g. /depth.html, /streak.svg). The router
        # served only index.html, so every other file in HERE 404'd despite
        # existing. SVGs need image/svg+xml or <img> embeds render broken.
        if "/" not in p.strip("/") and p.endswith((".html", ".svg", ".js")):
            name = p.strip("/")
            ctype = ("image/svg+xml" if name.endswith(".svg") else
                     "application/javascript" if name.endswith(".js") else
                     "text/html; charset=utf-8")
            try:
                with open(os.path.join(HERE, name), "rb") as f:
                    self._send(200, f.read(), ctype)
            except FileNotFoundError:
                self._send(404, "not found", "text/plain")
            return

        if p == "/api/quote":
            q = parse_qs(u.query)
            chain = (q.get("chain", ["ethereum"])[0]).lower()
            src = q.get("asset", [""])[0].upper()
            dst = (q.get("target") or q.get("to") or ["USDC"])[0].upper()
            amt = q.get("amount", [""])[0]
            unit = (q.get("unit", ["human"])[0]).lower()
            if chain not in T.CHAINS:
                self._send(400, json.dumps({"error": f"unknown chain; use {list(T.CHAINS)}"}))
                return
            if not src or not amt:
                self._send(400, json.dumps({"error": "need asset and amount"}))
                return
            if src == dst:
                self._send(400, json.dumps({
                    "error": "asset and target are the same token; "
                             "quoting a token against itself measures nothing"}))
                return
            try:
                tok = T.resolve(chain, src)
                if tok is None:
                    self._send(400, json.dumps({"error": "asset must be an ERC-20"}))
                    return
                units = parse_amount(amt, tok[1], unit)
                dt = T.resolve(chain, dst)
                if dt is None:
                    self._send(400, json.dumps({"error": "target must be an ERC-20"}))
                    return
                res = do_quote(chain, src, dst, units, tok[1], dt[1])
                # soul#7: make the input interpretation and the resulting price
                # impact impossible to miss. A quote of a quintillion WETH can
                # be a real answer from a finite pool; what was missing was the
                # label saying *what was quoted* and how far spot it is.
                human = T.from_units(units, tok[1])
                res["amount_in_units"] = units
                res["amount_human"] = human
                res["amount_unit"] = unit
                if unit == "human" and units >= 10 ** tok[1] * 1_000_000:
                    res["unit_warning"] = (
                        f"amount={amt} was read as {human} {src} (human units) = "
                        f"{units} raw base units. If you meant {amt} raw base units, "
                        f"pass amount={amt}&unit=raw."
                    )
                if dst == "USDC" and float(human) > 0:
                    implied = res["best"]["gross"] / float(human)
                    res["best_implied_usd_per_token"] = round(implied, 10)
                    if src == "WETH" and res.get("eth_usd"):
                        res["implied_vs_eth_usd"] = round(implied / res["eth_usd"], 10)
                self._send(200, json.dumps(res, indent=2))
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)[:200]}))
            return

        if p == "/api/tokens":
            q = parse_qs(u.query)
            chain = (q.get("chain", ["ethereum"])[0]).lower()
            if chain not in T.CHAINS:
                self._send(400, json.dumps({"error": "unknown chain"}))
                return
            self._send(200, json.dumps({
                "chain": chain,
                "tokens": sorted(T.token_table(chain).keys()),
            }, indent=2))
            return

        if p == "/health":
            self._send(200, json.dumps({"ok": True, "ts": int(time.time())}))
            return

        self._send(404, json.dumps({"error": "not found"}))

    # -- same-origin RPC gateway -------------------------------------------
    # Added 2026-11-17 to fix routecheck#1: the browser used to call public
    # endpoints (eth.llamarpc.com, publicnode) directly, which the visitor's
    # browser blocks on CORS before a single quote is produced. The page now
    # posts here instead. NOT a generic proxy: the method allowlist is the
    # whole security model, plus a request size cap and a per-IP rate limit.
    RPC_METHODS = frozenset({
        "eth_call", "eth_gasPrice", "eth_blockNumber", "eth_chainId",
        "eth_getBlockByNumber",
    })
    RPC_MAX_BODY = 20_000
    # The page issues ~80 eth_calls for one quote (4 fee tiers x 5 routes x
    # several sizes), so a tight limit makes the gateway rate-limit its own
    # frontend and fall back to public endpoints. 120/min did exactly that on
    # the first real run; 1200/min plus the memo below is both safe and enough.
    RATE_PER_MIN = 1200
    _hits = {}  # ip -> [window_start, count]
    _memo = {}  # (chain, method, params-json) -> (expires, result)
    MEMO_TTL = 30  # seconds; cheaper than 80 round trips, still fresh enough
    _gate = threading.Semaphore(4)  # 4 upstream calls at once: fast, no 429s

    def _rate_ok(self):
        ip = self.client_address[0]
        now = time.time()
        win, n = self._hits.get(ip, (now, 0))
        if now - win > 60:
            win, n = now, 0
        n += 1
        self._hits[ip] = (win, n)
        if len(self._hits) > 5000:  # never let the map itself grow unbounded
            self._hits.clear()
        return n <= self.RATE_PER_MIN

    def do_POST(self):
        u = urlparse(self.path)
        if presence_handle(self, u.path, "POST"):
            return
        if u.path != "/api/rpc":
            self._send(404, json.dumps({"error": "not found"}))
            return
        if not self._rate_ok():
            self._send(429, json.dumps({"error": "too many requests, max "
                                        f"{self.RATE_PER_MIN}/min per IP"}))
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > self.RPC_MAX_BODY:
            self._send(413, json.dumps({"error": f"body must be 1..{self.RPC_MAX_BODY} bytes"}))
            return
        try:
            req = json.loads(self.rfile.read(n))
        except Exception:  # noqa: BLE001
            self._send(400, json.dumps({"error": "body must be JSON"}))
            return
        chain = str(req.get("chain", "")).lower()
        method = req.get("method")
        params = req.get("params") if req.get("params") is not None else []
        if chain not in T.CHAINS:
            self._send(400, json.dumps({"error": f"unknown chain; use {list(T.CHAINS)}"}))
            return
        if method not in self.RPC_METHODS:
            self._send(403, json.dumps({
                "error": "method not allowed; this gateway is not a generic proxy. "
                         f"allowed: {sorted(self.RPC_METHODS)}"}))
            return
        if not isinstance(params, list) or len(params) > 4:
            self._send(400, json.dumps({"error": "params must be a list of <=4 items"}))
            return
        try:
            self._send(200, json.dumps({"result": self._relay(chain, method, params)}))
        except Exception as e:  # noqa: BLE001
            # A reverting eth_call ("no pool at fee 500", "amount exceeds
            # liquidity") is a real answer, not a gateway fault. Returning 502
            # for it made the page think the gateway was broken and replay the
            # same impossible quote against public endpoints: 8 of 26 probe
            # calls in the first live burst were exactly that. HTTP stays 200
            # and the error rides in the body so the page can skip the route.
            self._send(200, json.dumps({"error": str(e)[:200]}))
            return

    @classmethod
    def _relay(cls, chain, method, params):
        """One upstream call per distinct request per MEMO_TTL, serialised.

        The page fans out 4 fee tiers x 5 routes in parallel. Pushing that burst
        at a single free endpoint earns a 429, which is how this gateway spent
        its first live run returning 502s to its own frontend. A lock plus the
        memo turns 20 concurrent identical-ish eth_calls into a few hundred
        milliseconds of sequential work and no 429s.
        """
        key = (chain, method, json.dumps(params, sort_keys=True))
        with cls._gate:
            now = time.time()
            hit = cls._memo.get(key)
            if hit and hit[0] > now:
                return hit[1]
            last = None
            for _ in range(2):
                try:
                    res = T.rpc(chain, method, params)
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    time.sleep(0.3)
            else:
                raise last
            if len(cls._memo) > 4000:
                cls._memo.clear()
            cls._memo[key] = (now + cls.MEMO_TTL, res)
            return res


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write(f"Tallow routecheck listening on :{port}\n")
    srv.serve_forever()
