# -*- coding: utf-8 -*-
"""推送：QQ 官方机器人 / QQ OneBot / PushPlus / 邮件

QQ 官方机器人要点（q.qq.com）：
  1) 拿 appid + secret（机器人 → 开发设置）
  2) 主动消息需要「目标 openid」，分三种：
       - 群：  POST https://api.sgroup.qq.com/v2/groups/{group_openid}/messages
       - 私聊：POST https://api.sgroup.qq.com/v2/users/{user_openid}/messages
       - 频道：POST https://api.sgroup.qq.com/channels/{channel_id}/messages
  3) openid 从机器人收到的事件里取（本项目的 /qq/webhook 会把它打印出来）
  4) token：POST https://bots.qq.com/app/getAppAccessToken  {appId, clientSecret}
"""
from __future__ import annotations

import json
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText

_token_cache: dict[str, tuple[str, float]] = {}


def _post(url: str, payload: dict, headers: dict | None = None, timeout: float = 15.0):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400].decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ---------------- QQ 官方机器人 ----------------
def qq_token(appid: str, secret: str) -> str:
    hit = _token_cache.get(appid)
    now = time.time()
    if hit and now < hit[1] - 60:
        return hit[0]
    st, body = _post("https://bots.qq.com/app/getAppAccessToken",
                     {"appId": str(appid), "clientSecret": str(secret)})
    if not (200 <= st < 300):
        raise RuntimeError(f"取 token 失败 HTTP {st} {body[:150]}")
    data = json.loads(body)
    token = data.get("access_token") or ""
    exp = int(data.get("expires_in") or 7200)
    if not token:
        raise RuntimeError(f"token 为空：{body[:150]}")
    _token_cache[appid] = (token, now + exp)
    return token


def send_qq_official(cfg: dict, text: str) -> tuple[bool, str]:
    appid = str(cfg.get("appid") or "").strip()
    secret = str(cfg.get("secret") or "").strip()
    target = str(cfg.get("target") or "").strip()
    kind = (cfg.get("kind") or "group").lower()
    if not (appid and secret and target):
        return False, "缺少 appid / secret / target"

    token = qq_token(appid, secret)
    headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": appid}
    seq = int(time.time()) % 100000

    if kind == "group":
        url = f"https://api.sgroup.qq.com/v2/groups/{target}/messages"
        payload = {"content": text[:1800], "msg_type": 0, "msg_seq": seq}
    elif kind in ("c2c", "private", "user"):
        url = f"https://api.sgroup.qq.com/v2/users/{target}/messages"
        payload = {"content": text[:1800], "msg_type": 0, "msg_seq": seq}
    else:  # channel
        url = f"https://api.sgroup.qq.com/channels/{target}/messages"
        payload = {"content": text[:1800]}

    st, body = _post(url, payload, headers)
    return 200 <= st < 300, f"HTTP {st} {body[:180]}"


# ---------------- QQ OneBot（NapCatQQ 等） ----------------
def send_onebot(cfg: dict, text: str) -> tuple[bool, str]:
    api = (cfg.get("onebot_api") or "").rstrip("/")
    target = cfg.get("onebot_target")
    kind = (cfg.get("onebot_kind") or "private").lower()
    if not (api and target):
        return False, "缺少 onebot_api / onebot_target"
    url = f"{api}/send_group_msg" if kind == "group" else f"{api}/send_private_msg"
    payload = ({"group_id": int(target), "message": text} if kind == "group"
               else {"user_id": int(target), "message": text})
    headers = {}
    tok = (cfg.get("onebot_token") or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    st, body = _post(url, payload, headers)
    return 200 <= st < 300, f"HTTP {st} {body[:150]}"


# ---------------- PushPlus / 邮件 ----------------
def send_pushplus(token: str, text: str) -> tuple[bool, str]:
    if not token:
        return False, "无 token"
    st, body = _post("https://www.pushplus.plus/send",
                     {"token": token, "title": "KOL 动作", "content": text, "template": "txt"})
    return 200 <= st < 300, f"HTTP {st} {body[:120]}"


def send_mail(cfg: dict, text: str, subject: str | None = None) -> tuple[bool, str]:
    host = cfg.get("host") or ""
    user = cfg.get("user") or ""
    pwd = cfg.get("pass") or ""
    to = cfg.get("to") or ""
    if not (host and user and pwd and to):
        return False, "配置不全"
    try:
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = subject or "Sync Alert"
        msg["From"], msg["To"] = user, to
        port = int(cfg.get("port") or 465)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as s:
                s.login(user, pwd); s.sendmail(user, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, pwd); s.sendmail(user, [to], msg.as_string())
        return True, "已发送"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------- 统一入口 ----------------
def send(cfg: dict, text: str, channels: list[str] | None = None,
         subject: str | None = None) -> dict:
    """channels 可指定只发某些通道：["qq"] / ["pushplus"] / ["mail"]"""
    if cfg.get("dry_run", True):
        print("[dry] \n" + text + "\n" + "-" * 50)
        return {"dry_run": True}

    want = channels if channels is not None else ["qq", "pushplus", "mail"]
    res = {}
    qq = cfg.get("qq") or {}
    if "qq" in want:
        if (qq.get("type") or "").lower() == "official":
            res["qq"] = send_qq_official(qq, text)
        else:
            res["qq"] = send_onebot(qq, text)

    if "pushplus" in want and cfg.get("pushplus_token"):
        res["pushplus"] = send_pushplus(cfg["pushplus_token"], text)
    if "mail" in want and (cfg.get("mail") or {}).get("host"):
        res["mail"] = send_mail(cfg["mail"], text, subject)

    for k, (ok, msg) in res.items():
        print(f"[dispatch] {k}: {'OK' if ok else 'FAIL'} {msg}")
    return res
