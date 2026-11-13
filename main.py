# -*- coding: utf-8 -*-
"""KOL 监控服务（可直接跑在免费虚拟主机上）

功能：
  · 后台线程轮询币安带单员的最新操作记录
  · 发现新开仓 → 拼上点位（均价/现价/止损/目标1-3）→ 推送到 QQ
  · 内置 HTTP 服务：
      GET  /                状态页（KOL 列表、疑似仓位、最近事件）
      GET  /healthz         健康检查（PaaS 用）
      GET  /api/state       当前快照（JSON）
      GET  /api/events      已捕获的事件（JSON）
      POST /trigger?token=  手动触发一次扫描
      POST /qq/webhook      QQ 官方机器人事件回调（抓 group_openid、回声测试）
  · 去重状态存 data/state.json

运行：
  python app.py            # 端口取 PORT 环境变量，默认 8080
配置：
  data/config.json（参考 config.example.json），或全用环境变量
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import feed
import dispatch

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CFG_FILE = DATA / "config.json"
STATE_FILE = DATA / "cache.json"
EVENT_FILE = DATA / "log.json"

PORT = int(os.environ.get("PORT", "8080"))

DEFAULT_CFG = {
    "kols": [{"id": "5075281354358777856", "alias": "熬鹰资本"},
             {"id": "5289761558741350656", "alias": "交易员刺客"}],
    "poll_seconds": 60,
    "push_on": ["open", "close"],
    "include_levels": True,
    "qq": {"type": "official", "appid": "", "secret": "", "target": "", "kind": "group",
           "onebot_api": "", "onebot_token": "", "onebot_target": 0, "onebot_kind": "private"},
    "pushplus_token": "",
    "mail": {"host": "", "port": 465, "user": "", "pass": "", "to": ""},
    "admin_token": "change-me-please",
    "qq_min_notional": 0,   # 大于 0 时，只有名义价值超过它的信号才发 QQ 官方机器人
    "dry_run": True,
}


def load_cfg() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if CFG_FILE.is_file():
        try:
            user = json.loads(CFG_FILE.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update({kk: vv for kk, vv in v.items() if not kk.startswith("_")})
                else:
                    cfg[k] = v
        except Exception as e:
            print("[cfg] read failed:", e)
    else:
        CFG_FILE.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[cfg] default config written:", CFG_FILE.name)

    # 环境变量覆盖
    env = os.environ
    if env.get("KOL_IDS"):
        cfg["kols"] = [{"id": x.strip(), "alias": ""} for x in env["KOL_IDS"].split(",") if x.strip()]
    if env.get("POLL_SECONDS"):
        cfg["poll_seconds"] = int(env["POLL_SECONDS"])
    if env.get("QQ_TYPE"):
        cfg["qq"]["type"] = env["QQ_TYPE"]
    if env.get("QQ_APPID"):
        cfg["qq"]["appid"] = env["QQ_APPID"]
    if env.get("QQ_SECRET"):
        cfg["qq"]["secret"] = env["QQ_SECRET"]
    if env.get("QQ_TARGET"):
        cfg["qq"]["target"] = env["QQ_TARGET"]
    if env.get("QQ_TARGET_KIND"):
        cfg["qq"]["kind"] = env["QQ_TARGET_KIND"]
    if env.get("QQ_ONEBOT_API"):
        cfg["qq"]["onebot_api"] = env["QQ_ONEBOT_API"]
    if env.get("QQ_ONEBOT_TOKEN"):
        cfg["qq"]["onebot_token"] = env["QQ_ONEBOT_TOKEN"]
    if env.get("QQ_ONEBOT_TARGET"):
        cfg["qq"]["onebot_target"] = int(env["QQ_ONEBOT_TARGET"])
    if env.get("PUSHPLUS_TOKEN"):
        cfg["pushplus_token"] = env["PUSHPLUS_TOKEN"]
    if env.get("ADMIN_TOKEN"):
        cfg["admin_token"] = env["ADMIN_TOKEN"]

    # ---- 中性环境变量别名（GitHub Secrets 里用这些，看不出业务） ----
    if env.get("ID_LIST"):
        cfg["kols"] = [{"id": x.strip(), "alias": ""} for x in env["ID_LIST"].split(",") if x.strip()]
    if env.get("NOTIFY_TYPE"):
        cfg["qq"]["type"] = env["NOTIFY_TYPE"]
    if env.get("NOTIFY_APP"):
        cfg["qq"]["appid"] = env["NOTIFY_APP"]
    if env.get("NOTIFY_SEC"):
        cfg["qq"]["secret"] = env["NOTIFY_SEC"]
    if env.get("NOTIFY_DEST"):
        cfg["qq"]["target"] = env["NOTIFY_DEST"]
    if env.get("NOTIFY_KIND"):
        cfg["qq"]["kind"] = env["NOTIFY_KIND"]
    if env.get("NOTIFY_ALT"):
        cfg["pushplus_token"] = env["NOTIFY_ALT"]
    if env.get("QQ_MIN"):
        cfg["qq_min_notional"] = float(env["QQ_MIN"])
    if env.get("MAIL_HOST"):
        cfg["mail"]["host"] = env["MAIL_HOST"]
    if env.get("MAIL_USER"):
        cfg["mail"]["user"] = env["MAIL_USER"]
    if env.get("MAIL_PASS"):
        cfg["mail"]["pass"] = env["MAIL_PASS"]
    if env.get("MAIL_TO"):
        cfg["mail"]["to"] = env["MAIL_TO"]
    if env.get("MAIL_PORT"):
        cfg["mail"]["port"] = int(env["MAIL_PORT"])
    if env.get("NOTIFY_TOKEN"):
        cfg["admin_token"] = env["NOTIFY_TOKEN"]
    if env.get("LIVE") in ("1", "true", "True"):
        cfg["dry_run"] = False

    if env.get("DRY_RUN") in ("0", "false", "False"):
        cfg["dry_run"] = False
    return cfg


# ---------------- 状态 ----------------
_lock = threading.Lock()
STATE = {"sent": {}, "last_check": 0, "last_error": None, "snapshot": [], "checks": 0}


def load_state() -> None:
    if STATE_FILE.is_file():
        try:
            STATE.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass


def save_state() -> None:
    sent = STATE.get("sent", {})
    if len(sent) > 1000:
        items = sorted(sent.items(), key=lambda kv: kv[1] if isinstance(kv[1], (int, float)) else 0)
        STATE["sent"] = dict(items[-1000:])
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")


def load_events() -> list:
    if EVENT_FILE.is_file():
        try:
            return json.loads(EVENT_FILE.read_text(encoding="utf-8")) or []
        except Exception:
            pass
    return []


def save_events(evs: list) -> None:
    EVENT_FILE.write_text(json.dumps(evs[:200], ensure_ascii=False, indent=2), encoding="utf-8")


EVENTS = load_events()


# ---------------- 消息格式 ----------------
def p(v, nd=4):
    if v is None:
        return "—"
    v = float(v)
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.{nd}f}"
    if v >= 0.01:
        return f"{v:.5f}"
    return f"{v:.8f}"


def build_subject(k: dict, ev: dict) -> str:
    """邮件主题：中性 + 一眼能看出动作和币种"""
    tag = {"开多": "L+", "开空": "S+", "平多": "L-", "平空": "S-"}.get(ev["action"], "?")
    return f"[{tag}] {ev['symbol']} {ev['qty']:,.4f} @ {p(ev['price'])}"


def build_message(k: dict, ev: dict) -> str:
    """邮件正文：开仓给点位，平仓给盈亏"""
    sym = ev["symbol"]
    side = "多" if ev["action"] in ("开多", "平多") else "空"
    mark, atr = 0.0, 0.0
    try:
        mark, atr = feed.mark_atr(sym)
    except Exception:
        pass
    if mark <= 0:
        mark = ev.get("price") or 0

    lines = [
        f"【{k.get('nickname') or k.get('alias') or k['id']}】",
        f"{ev['action']}  {sym}",
        f"数量 {ev['qty']:,.4f}    成交价 {p(ev['price'])}",
        f"名义价值 {ev['notional']:,.0f} U",
    ]

    if ev.get("open"):
        # ---- 开仓：点位 ----
        avg = ev["price"]
        lines.append("")
        lines.append("— 持仓点位 —")
        lines.append(f"开仓均价  {p(avg)}")
        lines.append(f"当前价格  {p(mark)}")
        if avg > 0 and mark > 0:
            chg = (mark / avg - 1) * 100 * (1 if side == "多" else -1)
            lines.append(f"浮动盈亏  {chg:+.2f}%")
        if atr > 0 and avg > 0:
            d = 1 if side == "多" else -1
            stop = avg - d * atr * 1.5
            risk = abs(avg - stop)
            lines.append("")
            lines.append(f"止损*     {p(stop)}")
            lines.append(f"目标1*    {p(avg + d * risk)}")
            lines.append(f"目标2*    {p(avg + d * risk * 2)}")
            lines.append(f"目标3*    {p(avg + d * risk * 3)}")
            lines.append(f"（ATR(1h)={p(atr)}，按 1.5×ATR 设止损，* 为系统推算非本人设置）")
        lines.append("")
        lines.append(f"方向：{side}   名义 {ev['notional']:,.0f} U")
    else:
        # ---- 平仓：已实现盈亏 ----
        pnl = ev.get("pnl") or 0.0
        lines.append("")
        lines.append("— 平仓结果 —")
        lines.append(f"平仓价    {p(ev['price'])}")
        lines.append(f"已实现盈亏  {pnl:+,.2f} U")
        if ev["notional"]:
            lines.append(f"收益率    {pnl / ev['notional'] * 100:+.2f}%（按名义）")
        lines.append("")
        lines.append(f"方向：{side}（{ev['action']}）")

    lines.append("")
    lines.append(f"https://www.binance.com/zh-CN/copy-trading/lead-details/{k['id']}")
    return "\n".join(lines)


def scan_once(cfg: dict) -> int:
    new_events = 0
    snapshots = []
    for item in cfg.get("kols", []):
        pid = str(item.get("id") or "").strip()
        if not pid:
            continue
        try:
            snap = feed.snapshot(pid, item.get("alias", ""), cfg.get("include_levels", True))
        except Exception as e:
            print("[sync] snapshot failed", pid, e)
            continue
        snapshots.append(snap)
        if not snap.get("ok"):
            print("[sync] fetch failed", pid, snap.get("error"))
            continue

        want_open = "open" in (cfg.get("push_on") or ["open"])
        want_close = "close" in (cfg.get("push_on") or [])
        for ev in snap.get("orders", []):
            if ev.get("open") and not want_open:
                continue
            if (not ev.get("open")) and not want_close:
                continue
            key = f"{pid}:{ev['ts']}"
            if key in STATE["sent"]:
                continue
            msg = build_message(snap, ev)
            # 通道选择：PushPlus 全发；QQ 官方机器人只发大额，省额度
            chans = ["mail"]
            try:
                dispatch.send(cfg, msg, channels=chans, subject=build_subject(snap, ev))
            except Exception as e:
                print("[dispatch] error", e)
            STATE["sent"][key] = int(time.time())
            EVENTS.insert(0, {"kol": snap.get("nickname"), "id": pid, **ev, "at": int(time.time())})
            new_events += 1
            time.sleep(0.4)

    with _lock:
        STATE["snapshot"] = snapshots
        STATE["last_check"] = int(time.time())
        STATE["checks"] += 1
    if new_events:
        save_state()
        save_events(EVENTS)
    else:
        save_state()
    print(f"[sync] sources={len(snapshots)} new={new_events}")
    return new_events


def worker():
    cfg = load_cfg()
    interval = max(20, int(cfg.get("poll_seconds") or 60))
    print(f"[loop] start interval={interval}s live={not cfg.get('dry_run')}")
    while True:
        try:
            scan_once(load_cfg())
        except Exception as e:
            STATE["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        time.sleep(interval)


# ---------------- HTTP ----------------
PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sync</title>
<style>
body{margin:0;background:#0b0e11;color:#e6edf3;font:13px/1.6 system-ui,"Microsoft YaHei",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:18px}
h1{font-size:16px;margin:0 0 4px}
.muted{color:#8b98a5;font-size:12px}
.card{background:#131920;border:1px solid #232c36;border-radius:10px;padding:12px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:6px 8px;border-bottom:1px solid #1a2129;text-align:right;white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:#8b98a5;font-weight:600}
.up{color:#e5484d}.down{color:#16a34a}.dim{color:#8b98a5}
.tag{border-radius:4px;padding:1px 7px;font-size:11px}
.l{background:#33191d;color:#ff9a9a}.s{background:#16301f;color:#7ee08f}
.olf{color:#c9a7ff}.clf{background:#2a2a20;color:#ffe08a}
</style></head><body><div class="wrap">
<h1>Sync Status</h1>
<div class="muted" id="meta">加载中…</div>
<div id="body"></div>
</div>
<script>
function f(v,d=2){ if(v===null||v===undefined||v==="") return "—"; const x=Number(v);
  return isFinite(x)? x.toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d}) : "—"; }
function pr(v){ if(!v) return "—"; v=Number(v);
  if(v>=1000) return v.toFixed(2); if(v>=1) return v.toFixed(4);
  if(v>=0.01) return v.toFixed(5); return v.toFixed(8); }
async function load(){
  const d = await (await fetch("/api/state")).json();
  const m = d.meta||{};
  document.getElementById("meta").innerHTML =
    `sources <b>${m.kols||0}</b> · 每 <b>${m.interval||"—"}s</b> 扫一次 · 上次 <b>${m.last_check||"—"}</b>`
    + ` · 累计新动作 <b>${m.events||0}</b> · dry_run=<b>${m.dry_run}</b>`
    + (m.last_error ? ` · <span class="up">${m.last_error}</span>` : "");
  const cards = (d.kols||[]).map(k=>{
    const rows = (k.implied||[]).map(i=>{
      const cls = i.side==="LONG"?"l":"s", lab = i.side==="LONG"?"多":"空";
      const pnlCls = (i.pnl_pct||0)>=0?"up":"down";
      return `<tr><td>${i.symbol}</td><td><span class="tag ${cls}">${lab}</span></td>
        <td>${f(i.net_qty,4)}</td><td>${pr(i.avg_price)}</td><td>${pr(i.mark)}</td>
        <td class="${pnlCls}">${i.pnl_pct==null?"—":(i.pnl_pct>=0?"+":"")+f(i.pnl_pct)+"%"}</td>
        <td>${f(i.notional,0)}</td><td>${pr(i.stop)}</td>
        <td>${pr(i.tp1)}</td><td class="dim">${pr(i.tp2)}</td><td class="dim">${pr(i.tp3)}</td></tr>`;
    }).join("");
    return `<div class="card"><b>${k.nickname||k.id}</b>
      <span class="muted">aum ${f(k.aum,0)} · margin ${f(k.margin,0)} · subs ${k.copiers||0}
      ${k.position_show?"":"· (derived)"}</span>
      <table><thead><tr><th>sym</th><th>side</th><th>qty</th><th>avg</th><th>last</th>
      <th>chg</th><th>notional</th><th>sl</th><th>t1</th><th>t2</th><th>t3</th></tr></thead>
      <tbody>${rows||'<tr><td colspan="11" class="dim">无持仓</td></tr>'}</tbody></table></div>`;
  }).join("");
  const evs = (d.events||[]).map(e=>{
    const t=new Date(e.ts).toLocaleString("zh-CN",{hour12:false});
    const AMAP={"开多":"L+","开空":"S+","平多":"L-","平空":"S-"};const cls = e.action==="开多"?"l":e.action==="开空"?"s":e.action==="平多"?"olf":"clf";
    return `<tr><td class="dim">${t}</td><td>${e.kol||""}</td>
      <td><span class="tag ${cls}">${AMAP[e.action]||e.action}</span></td><td>${e.symbol}</td>
      <td>${f(e.qty,4)}</td><td>${pr(e.price)}</td></tr>`;
  }).join("");
  document.getElementById("body").innerHTML = cards +
    `<div class="card"><b>Recent</b><table><thead><tr><th>time</th><th>src</th><th>act</th>
     <th>sym</th><th>qty</th><th>px</th></tr></thead>
     <tbody>${evs||'<tr><td colspan="6" class="dim">暂无</td></tr>'}</tbody></table></div>`;
}
load(); setInterval(load, 10000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        cfg = load_cfg()
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/healthz":
            return self._json({"ok": True, "ts": int(time.time())})
        if u.path == "/api/state":
            return self._json({
                "meta": {"kols": len(cfg.get("kols", [])),
                         "interval": cfg.get("poll_seconds"),
                         "last_check": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STATE["last_check"])) if STATE["last_check"] else None,
                         "events": len(EVENTS),
                         "dry_run": cfg.get("dry_run"),
                         "last_error": STATE.get("last_error")},
                "kols": STATE.get("snapshot") or [],
                "events": EVENTS[:30],
            })
        if u.path == "/api/events":
            return self._json({"events": EVENTS[:100]})
        if u.path == "/trigger":
            q = parse_qs(u.query)
            tok = (q.get("token") or [""])[0]
            if cfg.get("admin_token") and tok != cfg["admin_token"]:
                return self._json({"error": "token 不对"}, 403)
            n = scan_once(cfg)
            return self._json({"ok": True, "new_events": n})
        return self._json({"error": "not found"}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"

        if u.path == "/qq/webhook":
            # QQ 官方机器人事件回调：把 group_openid / user_openid 打出来，方便配 target
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}
            t = body.get("t")
            d = body.get("d") or {}
            print("[qq-webhook] type=", t, "payload=", json.dumps(d, ensure_ascii=False)[:600])
            if t == "GROUP_AT_MESSAGE_CREATE":
                print("[qq-webhook] ★ group_openid =", d.get("group_openid"))
            elif t == "C2C_MESSAGE_CREATE":
                print("[qq-webhook] ★ user_openid =", d.get("author", {}).get("user_openid"))
            return self._json({"op": 12})   # 12 = HTTP 回调 ACK
        if u.path == "/test-push":
            cfg = load_cfg()
            try:
                dispatch.send(cfg, "测试消息：KOL 监控已部署成功 " + time.strftime("%Y-%m-%d %H:%M:%S"))
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)


def main():
    load_state()
    import sys as _sys
    if "--test" in _sys.argv:
        cfg = load_cfg()
        cfg["dry_run"] = False
        text = ("测试消息：GitHub Actions → 推送通道\n"
                f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                "收到这条说明整条链路已通。")
        dispatch.send(cfg, text, channels=["mail", "pushplus"],
                      subject="Sync Test")
        print("[test] done")
        return
    if "--once" in _sys.argv:
        cfg = load_cfg()
        n = scan_once(cfg)
        print(f"[once] done, new={n}")
        return
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"[http] 监听 0.0.0.0:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
