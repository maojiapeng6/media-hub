# -*- coding: utf-8 -*-
"""RT（ResourceTool）面板 API 客户端。

★ 2026-10-23 用户要求：「游戏机器人或 boss 开号可以登录 RT 管理面板」。
  RT 暴露了 HTTP API（:29999），`POST /api/admin/emby-users` 一次就把
  **RT 面板账号 + Emby 账号** 同时建好（返回 emby_user_id），
  所以 kfcgame 开号直接走它，天然实现三端互通，不用重复调 Emby API。

实测要点（2026-10-23）：
  · 登录：POST /api/auth/login {username,password} → {token}
  · 建号：POST /api/admin/emby-users，**必填** emby302_config_id / policy_template_id，
    否则会误报「参数错误，密码至少 6 位」
  · 删号：DELETE /api/admin/emby-users/{id}
  · 鉴权头：Authorization: Bearer <token>
"""
from __future__ import annotations

import logging
import secrets
import string
import threading
import time
from datetime import datetime, timedelta

import requests

from .. import settings as S

logger = logging.getLogger(__name__)

_TK = {"token": "", "ts": 0.0}
_LOCK = threading.Lock()
_TTL = 6 * 3600          # token 缓存 6 小时


def _cfg() -> dict:
    return (S.CFG.get("rt") or {})


def enabled() -> bool:
    c = _cfg()
    return bool(c.get("url") and c.get("user") and c.get("pass"))


def _base() -> str:
    return str(_cfg().get("url") or "").rstrip("/")


def _proxies():
    """kfcgame 的 config.proxy 可能是字符串，也可能是 {http,https} 字典。
    ★ 2026-11-05：目标是局域网地址时**不走代理**（RT 在 10.110 本机，
      经 Clash 代理回环可能连不通/变慢）。"""
    import re as _re
    base = _base()
    if _re.search(r"://(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", base):
        return None
    p = S.CFG.get("proxy")
    if not p:
        return None
    if isinstance(p, dict):
        return p or None
    return {"http": str(p), "https": str(p)}


def _login() -> str:
    """取 token（带 6 小时缓存）。"""
    now = time.time()
    with _LOCK:
        if _TK["token"] and now - _TK["ts"] < _TTL:
            return _TK["token"]
    c = _cfg()
    r = requests.post(_base() + "/api/auth/login",
                      json={"username": c.get("user"), "password": c.get("pass")},
                      proxies=_proxies(), timeout=20)
    j = r.json() if r.content else {}
    tk = (j or {}).get("token") or ""
    if not tk:
        raise RuntimeError("RT 登录失败：%s" % ((j or {}).get("message") or r.status_code))
    with _LOCK:
        _TK["token"] = tk
        _TK["ts"] = time.time()
    return tk


def _req(method: str, path: str, **kw):
    tk = _login()
    h = {"Authorization": "Bearer " + tk, "Accept": "application/json"}
    r = requests.request(method, _base() + path, headers=h,
                         proxies=_proxies(), timeout=40, **kw)
    if r.status_code == 401:          # token 过期 → 重登一次
        with _LOCK:
            _TK["token"] = ""
        tk = _login()
        h["Authorization"] = "Bearer " + tk
        r = requests.request(method, _base() + path, headers=h,
                             proxies=_proxies(), timeout=40, **kw)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:200]}
    return r.status_code, j


def gen_password(n: int = 8) -> str:
    """生成不易混淆的随机密码（去掉 0O1lI）。"""
    alphabet = "".join(c for c in (string.ascii_letters + string.digits)
                       if c not in "0O1lI")
    return "".join(secrets.choice(alphabet) for _ in range(n))


def find_user(tg_user_id: int) -> dict | None:
    """按 TG ID 找 RT 用户（找不到返回 None）。"""
    try:
        st, j = _req("GET", "/api/admin/emby-users",
                     params={"page": 1, "page_size": 200,
                             "config_id": int(_cfg().get("config_id") or 1)})
        if st != 200:
            return None
        for u in (j.get("data") or []):
            if int(u.get("tg_user_id") or 0) == int(tg_user_id):
                return u
    except Exception as e:
        logger.debug(f"RT find_user 失败: {e}")
    return None


def create_user(tg_user_id: int, username: str, password: str,
                days: int = 30, allowed_modes: str = "shared") -> dict:
    """在 RT 建号（同时会建 Emby 账号）。

    返回 {"ok":bool, "emby_user_id":str, "username":str, "password":str, "msg":str}
    """
    c = _cfg()
    body = {
        "username": username,
        "password": password,
        "emby302_config_id": int(c.get("config_id") or 1),
        "policy_template_id": int(c.get("policy_template_id") or 1),
        "user_mode": allowed_modes,
        "allowed_modes": allowed_modes,
        "status": "active",
        "cookie115": "",
        "pan115_config_id": 0,
        "cache_path": c.get("cache_path") or "/ResourceTool",
        "transfer_quota_on": False,
        "expiry_date": (datetime.now() + timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S"),
        "is_whitelist": False,
        "is_admin": False,
        "bypass302": False,
        "remark": "KFC bot 开号",
        "recycle_code": "",
        "emby_user_id": "",
        "max_devices": int(c.get("max_devices") or 2),
        "tg_user_id": int(tg_user_id),
    }
    try:
        st, j = _req("POST", "/api/admin/emby-users", json=body)
    except Exception as e:
        return {"ok": False, "msg": "RT 接口异常：%s" % str(e)[:80]}
    if st not in (200, 201):
        return {"ok": False, "msg": "RT 建号失败：%s" % (j.get("message") or st)}
    d = j.get("data") or {}
    eid = str(d.get("emby_user_id") or "")
    if not eid:
        return {"ok": False, "msg": "RT 返回无 emby_user_id"}
    logger.info(f"RT 建号成功 tg={tg_user_id} user={username} embyid={eid}")
    return {"ok": True, "emby_user_id": eid, "username": username,
            "password": password, "id": d.get("id"),
            "expiry_date": d.get("expiry_date"), "msg": "ok"}


def renew_user(tg_user_id: int, days: int) -> dict:
    """给 RT 用户续期（按 TG ID）。"""
    u = find_user(tg_user_id)
    if not u:
        return {"ok": False, "msg": "RT 里没有该 TG 的用户"}
    try:
        st, j = _req("POST", "/api/admin/emby-users/renew",
                     json={"ids": [u.get("id")], "days": int(days)})
    except Exception as e:
        return {"ok": False, "msg": "RT 续期异常：%s" % str(e)[:80]}
    if st != 200:
        return {"ok": False, "msg": "RT 续期失败：%s" % (j.get("message") or st)}
    return {"ok": True, "msg": "RT 已续 %d 天" % days}


def set_cookie115(tg_user_id: int, cookie: str) -> dict:
    """给 RT 用户写 115 cookie（用于扫码绑定后的落库）。"""
    u = find_user(tg_user_id)
    if not u:
        return {"ok": False, "msg": "RT 里没有该 TG 的用户"}
    body = {k: u.get(k) for k in (
        "username", "emby302_config_id", "policy_template_id", "user_mode",
        "allowed_modes", "status", "cache_path", "transfer_quota_on",
        "is_whitelist", "is_admin", "bypass302", "remark", "recycle_code",
        "emby_user_id", "max_devices", "tg_user_id")}
    body["cookie115"] = cookie
    body["pan115_config_id"] = 0
    try:
        st, j = _req("PUT", "/api/admin/emby-users/%s" % u.get("id"), json=body)
    except Exception as e:
        return {"ok": False, "msg": "RT 更新异常：%s" % str(e)[:80]}
    if st != 200:
        return {"ok": False, "msg": "RT 更新失败：%s" % (j.get("message") or st)}
    return {"ok": True, "msg": "已绑定 115 cookie"}
