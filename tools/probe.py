# -*- coding: utf-8 -*-
"""连通性测试：检查当前环境的网络出口是否可用

用法：
    python tools/reach_test.py                     # 测默认 KOL
    python tools/reach_test.py 5289761558741350656  # 指定 KOL

输出示例：
    [出口 IP] 20.1.2.3 (Azure)
    [detail ] HTTP 200  code=000000  nickname=熬鹰资本
    [orders ] HTTP 200  code=000000  记录 10 条
    [fapi   ] HTTP 200  BTCUSDT = 69350.1
    ✅ 这台机器可以使用
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
BASE = "https://www.binance.com"


def get(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()[:4000].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def post(url: str, body: str, pid: str, timeout: float = 20.0):
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"https://www.binance.com/zh-CN/copy-trading/lead-details/{pid}",
        "clienttype": "web",
        "lang": "zh-CN",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()[:4000].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main():
    pid = sys.argv[1] if len(sys.argv) > 1 else "5075281354358777856"

    # 出口 IP
    st, ip = get("https://api.ipify.org?format=json")
    print(f"[出口 IP] {ip[:120]}")

    # 详情
    st1, t1 = get(f"{BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail?portfolioId={pid}")
    ok1 = False
    try:
        j1 = json.loads(t1)
        ok1 = j1.get("code") == "000000"
        print(f"[detail ] HTTP {st1}  code={j1.get('code')}  nickname={j1.get('data', {}).get('nickname')}")
    except Exception:
        print(f"[detail ] HTTP {st1}  ❌ 非 JSON: {t1[:100]!r}")

    # 操作记录（注意 portfolioId 必须是精确整数）
    st2, t2 = post(f"{BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history",
                   '{"portfolioId":%s}' % pid, pid)
    ok2 = False
    try:
        j2 = json.loads(t2)
        ok2 = j2.get("code") == "000000"
        n = len((j2.get("data") or {}).get("list") or [])
        print(f"[orders ] HTTP {st2}  code={j2.get('code')}  记录 {n} 条")
    except Exception:
        print(f"[orders ] HTTP {st2}  ❌ 非 JSON: {t2[:100]!r}")

    # 合约行情
    st3, t3 = get("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT")
    ok3 = False
    try:
        j3 = json.loads(t3)
        ok3 = j3.get("symbol") == "BTCUSDT"
        print(f"[fapi   ] HTTP {st3}  BTCUSDT = {j3.get('price')}")
    except Exception:
        print(f"[fapi   ] HTTP {st3}  ❌ 非 JSON: {t3[:100]!r}")

    print()
    if ok1 and ok2:
        print("✅ OK")
        return 0
    print("❌ BLOCKED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
