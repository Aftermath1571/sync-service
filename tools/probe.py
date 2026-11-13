# -*- coding: utf-8 -*-
"""探针：检查当前环境能不能连币安 + QQ 官方 API"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
BASE = "https://www.binance.com"


def get(url, timeout=20.0):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()[:3000].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def post(url, body, pid, timeout=20.0):
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"https://www.binance.com/zh-CN/copy-trading/lead-details/{pid}",
        "clienttype": "web", "lang": "zh-CN"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()[:3000].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main():
    pid = sys.argv[1] if len(sys.argv) > 1 else "5075281354358777856"

    st, ip = get("https://api.ipify.org?format=json")
    print(f"[out ip] {ip[:120]}")

    st1, t1 = get(f"{BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail?portfolioId={pid}")
    ok1 = False
    try:
        j1 = json.loads(t1)
        ok1 = j1.get("code") == "000000"
        print(f"[detail ] HTTP {st1}  code={j1.get('code')}  nick={j1.get('data', {}).get('nickname')}")
    except Exception:
        print(f"[detail ] HTTP {st1}  BAD {t1[:80]!r}")

    st2, t2 = post(f"{BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history",
                   '{"portfolioId":%s}' % pid, pid)
    ok2 = False
    try:
        j2 = json.loads(t2)
        ok2 = j2.get("code") == "000000"
        n = len((j2.get("data") or {}).get("list") or [])
        print(f"[orders ] HTTP {st2}  code={j2.get('code')}  records={n}")
    except Exception:
        print(f"[orders ] HTTP {st2}  BAD {t2[:80]!r}")

    st3, _ = get("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT")
    print(f"[fapi   ] HTTP {st3}  {'OK' if st3 == 200 else 'BLOCKED'}")

    st4, t4 = get("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT")
    okx = "?"
    try:
        okx = (json.loads(t4).get("data") or [{}])[0].get("last")
    except Exception:
        pass
    print(f"[okx    ] HTTP {st4}  BTC={okx}")

    st5, _ = get("https://api.sgroup.qq.com/", timeout=15)
    print(f"[qq api ] HTTP {st5}  {'OK' if st5 in (200, 401, 403, 404) else 'BLOCKED'}")

    st6, _ = get("https://bots.qq.com/", timeout=15)
    print(f"[qq bots] HTTP {st6}  {'OK' if st6 in (200, 301, 302, 403, 404) else 'BLOCKED'}")

    print()
    print("✅ OK" if (ok1 and ok2) else "❌ BLOCKED")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
