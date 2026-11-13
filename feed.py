# -*- coding: utf-8 -*-
"""行情快照数据源 —— 零依赖

三个接口：
  GET  /bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail?portfolioId=
       → 昵称 / AUM / 保证金 / 跟单数 / positionShow（是否公开持仓）
  GET  /bapi/futures/v1/friendly/future/copy-trade/lead-data/positions?portfolioId=&pageNumber=1&pageSize=100
       → 当前持仓（positionAmount=0 的是已平仓，需过滤）
  POST /bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history
       body {"portfolioId": <int>} → 最新操作记录（开多/平多/开空/平空）

点位来自合约行情：
  GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol=   → 标记价
  GET https://fapi.binance.com/fapi/v1/klines?symbol=&interval=1h&limit=40 → 算 ATR
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://www.binance.com"
FAPI = "https://fapi.binance.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""

_opener = None


def _op():
    global _opener
    if _opener is None:
        if PROXY:
            _opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        else:
            _opener = urllib.request.build_opener()
    return _opener


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _headers(pid: str) -> dict:
    return {
        "User-Agent": UA,
        "Referer": f"https://www.binance.com/zh-CN/copy-trading/lead-details/{pid}",
        "Origin": BASE,
        "Accept": "application/json, text/plain, */*",
        "clienttype": "web",
        "lang": "zh-CN",
    }


def _req(url: str, pid: str, body: dict | None = None, timeout: float = 15.0):
    h = _headers(pid)
    if body is not None:
        h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=h, method="POST")
    else:
        req = urllib.request.Request(url, headers=h)
    with _op().open(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------- 缓存 ----------------
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = float(os.environ.get("KOL_CACHE_TTL", "20"))


def profile(pid: str) -> dict:
    d = _req(f"{BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail?portfolioId={pid}", pid)
    return d.get("data") or {}


def holdings(pid: str) -> list[dict]:
    d = _req(f"{BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-data/positions"
             f"?portfolioId={pid}&pageNumber=1&pageSize=100", pid)
    return d.get("data") or []


def actions(pid: str, limit: int = 40) -> tuple[list[dict], int]:
    d = _req(f"{BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history",
             pid, body={"portfolioId": int(pid)})
    if (d or {}).get("code") != "000000":
        raise RuntimeError((d or {}).get("message") or "order-history 异常")
    data = d.get("data") or {}
    return (data.get("list") or [])[:limit], int(data.get("total") or 0)


# ---------------- 行情 ----------------
_MARK: dict[str, tuple[float, float, float]] = {}


def mark_atr(symbol: str) -> tuple[float, float]:
    now = time.time()
    hit = _MARK.get(symbol)
    if hit and now - hit[0] < 15:
        return hit[1], hit[2]
    mark = atr = 0.0
    try:
        with _op().open(urllib.request.Request(
                f"{FAPI}/fapi/v1/premiumIndex?symbol={symbol}",
                headers={"User-Agent": UA}), timeout=12) as r:
            mark = _f(json.loads(r.read()).get("markPrice"))
    except Exception:
        pass
    # 币安合约 API 在美国 IP 会返回 451，切 OKX
    if mark <= 0:
        try:
            mark, atr2 = _mark_atr_okx(symbol)
            if atr <= 0:
                atr = atr2
        except Exception:
            pass
    if atr <= 0:
        try:
            _, atr = _mark_atr_okx(symbol)
        except Exception:
            pass
    try:
        if atr > 0:
            raise StopIteration
        with _op().open(urllib.request.Request(
                f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval=1h&limit=40",
                headers={"User-Agent": UA}), timeout=12) as r:
            kl = json.loads(r.read())
        if isinstance(kl, list) and len(kl) > 15:
            trs = []
            for i in range(1, len(kl)):
                h, l, pc = _f(kl[i][2]), _f(kl[i][3]), _f(kl[i - 1][4])
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = sum(trs[-14:]) / 14
    except StopIteration:
        pass
    except Exception:
        pass
    _MARK[symbol] = (now, mark, atr)
    return mark, atr




OKX = "https://www.okx.com"


def _okx(path: str, timeout: float = 12.0):
    req = urllib.request.Request(OKX + path, headers={"User-Agent": UA, "Accept": "application/json"})
    with _op().open(req, timeout=timeout) as r:
        return json.loads(r.read())


def _okx_inst(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT"""
    if symbol.endswith("USDT"):
        return symbol[:-4] + "-USDT"
    if symbol.endswith("USDC"):
        return symbol[:-4] + "-USDC"
    return symbol


def _mark_atr_okx(symbol: str) -> tuple[float, float]:
    inst = _okx_inst(symbol)
    mark = atr = 0.0
    try:
        d = _okx(f"/api/v5/market/ticker?instId={inst}")
        rows = (d or {}).get("data") or []
        if rows:
            mark = _f(rows[0].get("last"))
    except Exception:
        pass
    try:
        d = _okx(f"/api/v5/market/candles?instId={inst}&bar=1H&limit=40")
        rows = (d or {}).get("data") or []
        # OKX 返回倒序：[ts, o, h, l, c, vol, ...]
        rows = list(reversed(rows))
        if len(rows) > 15:
            trs = []
            for i in range(1, len(rows)):
                h, l, pc = _f(rows[i][2]), _f(rows[i][3]), _f(rows[i - 1][4])
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = sum(trs[-14:]) / 14
    except Exception:
        pass
    return mark, atr

# ---------------- 动作映射 ----------------
ACTION = {
    ("BUY", "LONG"): "开多",
    ("SELL", "LONG"): "平多",
    ("SELL", "SHORT"): "开空",
    ("BUY", "SHORT"): "平空",
    ("BUY", "BOTH"): "开多",
    ("SELL", "BOTH"): "开空",
}


def normalize_orders(rows: list[dict]) -> list[dict]:
    out = []
    for x in rows:
        side = (x.get("side") or "").upper()
        pside = (x.get("positionSide") or "BOTH").upper()
        act = ACTION.get((side, pside), "未知")
        qty = _f(x.get("executedQty"))
        px = _f(x.get("avgPrice"))
        out.append({
            "symbol": x.get("symbol") or "",
            "action": act,
            "open": act.startswith("开"),
            "side": side,
            "qty": qty,
            "price": px,
            "notional": qty * px,
            "pnl": _f(x.get("totalPnl")),
            "ts": int(x.get("orderTime") or x.get("orderUpdateTime") or 0),
        })
    return out


def implied_positions(orders: list[dict], with_levels: bool = True) -> list[dict]:
    """用「开 − 平」反推持仓，并补现价/ATR/止损/目标"""
    agg: dict[str, dict] = {}
    for o in orders:
        a = agg.setdefault(o["symbol"], {
            "long_open": 0.0, "long_close": 0.0, "short_open": 0.0, "short_close": 0.0,
            "opens": [], "last_ts": 0, "last_action": "",
        })
        if o["action"] == "开多":
            a["long_open"] += o["qty"]
            a["opens"].append((o["qty"], o["price"]))
        elif o["action"] == "平多":
            a["long_close"] += o["qty"]
        elif o["action"] == "开空":
            a["short_open"] += o["qty"]
            a["opens"].append((o["qty"], o["price"]))
        elif o["action"] == "平空":
            a["short_close"] += o["qty"]
        if o["ts"] >= a["last_ts"]:
            a["last_ts"] = o["ts"]
            a["last_action"] = o["action"]

    out = []
    for sym, a in agg.items():
        nl = a["long_open"] - a["long_close"]
        ns = a["short_open"] - a["short_close"]
        if nl <= 1e-12 and ns <= 1e-12:
            continue
        side, net = ("LONG", nl) if nl >= ns else ("SHORT", ns)
        row = {"symbol": sym, "side": side, "net_qty": net,
               "last_action": a["last_action"], "last_ts": a["last_ts"]}
        if with_levels:
            mark, atr = mark_atr(sym)
            direction = 1 if side == "LONG" else -1
            tq = sum(q for q, _ in a["opens"])
            avg = (sum(q * p for q, p in a["opens"]) / tq) if tq > 0 else 0.0
            # 拿不到行情（地区封锁）时，用最后成交价当现价，用 1% 当波动率
            if mark <= 0 and a["opens"]:
                mark = a["opens"][-1][1]
            if atr <= 0 and mark > 0:
                atr = mark * 0.01
            row.update({"avg_price": avg, "mark": mark, "atr": atr,
                        "notional": net * mark if mark else 0.0})
            if avg and mark:
                row["pnl_pct"] = (mark / avg - 1) * 100 * direction
                row["upnl"] = (mark - avg) * net * direction
            else:
                row["pnl_pct"] = row["upnl"] = None
            if atr and avg:
                stop = avg - direction * atr * 1.5
                risk = abs(avg - stop)
                row.update({"stop": stop, "tp1": avg + direction * risk,
                            "tp2": avg + direction * risk * 2,
                            "tp3": avg + direction * risk * 3})
            else:
                row.update({"stop": None, "tp1": None, "tp2": None, "tp3": None})
        out.append(row)
    out.sort(key=lambda x: -x["last_ts"])
    return out


def snapshot(pid: str, alias: str = "", with_levels: bool = True) -> dict:
    """一个 KOL 的完整快照（带 20 秒缓存）"""
    now = time.time()
    hit = _CACHE.get(pid)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    out = {"id": pid, "alias": alias, "ok": False, "error": None, "updated": int(now)}
    try:
        d = profile(pid)
        out.update({
            "nickname": d.get("nickname") or alias or pid,
            "avatar": d.get("avatarUrl") or "",
            "aum": _f(d.get("aumAmount")),
            "margin": _f(d.get("marginBalance")),
            "copiers": d.get("currentCopyCount") or 0,
            "tags": d.get("tag") or [],
            "position_show": bool(d.get("positionShow")),
        })

        if out["position_show"]:
            poss = []
            for x in holdings(pid):
                amt = _f(x.get("positionAmount"))
                if abs(amt) < 1e-12:
                    continue
                lev = _f(x.get("leverage"))
                mark = _f(x.get("markPrice"))
                notional = abs(_f(x.get("notionalValue"))) or abs(amt) * mark
                side_raw = (x.get("positionSide") or "BOTH").upper()
                side = ("LONG" if amt > 0 else "SHORT") if side_raw == "BOTH" else side_raw
                poss.append({
                    "symbol": x.get("symbol"), "side": side, "amount": abs(amt),
                    "entry": _f(x.get("entryPrice")), "mark": mark, "leverage": lev,
                    "notional": notional, "upnl": _f(x.get("unrealizedProfit")),
                })
            out["positions"] = poss
        else:
            out["positions"] = []

        rows, total = actions(pid, limit=40)
        orders = normalize_orders(rows)
        out["orders"] = orders
        out["orders_total"] = total
        out["implied"] = implied_positions(orders, with_levels)
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"

    _CACHE[pid] = (now, out)
    return out


if __name__ == "__main__":
    import sys
    pid = sys.argv[1] if len(sys.argv) > 1 else "5075281354358777856"
    s = snapshot(pid)
    print(json.dumps(s, ensure_ascii=False, indent=2)[:1500])
