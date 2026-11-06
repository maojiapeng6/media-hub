# -*- coding: utf-8 -*-
"""media-hub · 媒体服务器实例注册表（参考实现）

设计目标（商业化多实例）：
  1. **N 实例**：用户可自助新增任意台媒体服务器，不需要改代码、不需要重启。
  2. **零变更升级**：老配置里成对的内置键自动折算成前两个实例，绝不改写/重置用户已存值。
  3. **凭据不外泄**：`public()` / `extra_public()` 永不下发明文 api_key；
     编辑表单回传打码值 ``********`` 时，后端保留原值，不破坏凭据。
  4. **一处注册、全模块派生**：实时观看 / 总览统计 / 播放记录 / 排行榜 /
     来源徽标 / 图片扫描 / 测速 / 动作分发，一律遍历 ``all()``。

本模块只依赖 Python 标准库，不含任何真实地址、端口或凭据，可独立单测
（见 ``tests/test_multi_instance.py``）。接入现有 Flask 单体时，把
``cfg`` / ``containers`` 换成项目自己的配置读取函数即可。

注意：这是**发布副本的参考实现**。线上 media-hub 已按同样语义以锚点增量
方式内联落地（见 ``docs/multi-instance-runtime.md``）；两者数据模型与 API 契约一致。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

MASK = "********"
BUILTIN_IDS = ("emby", "emby_adult")

# 各人挂载点不同，路径一律可配置；这里只给结构性占位，不含任何真实路径。
DEFAULT_PR_DB = {
    "main": "",
    "adult": "",
}


class InstanceRegistry:
    """媒体服务器实例注册表。

    :param config_path: 新增实例的独立 JSON 落盘路径（与主配置隔离，零变更升级）。
    :param cfg: 返回主配置 dict 的可调用对象（或 dict 本身）。
    :param containers: 返回容器/服务注册表 dict 的可调用对象（或 dict 本身）。
    :param env_key_fallback: 主实例 api_key 的环境变量兜底（可留空）。
    :param cache_ttl: 新增实例文件读取缓存秒数；保存后自动失效。
    """

    def __init__(
        self,
        config_path: str,
        cfg: Any = None,
        containers: Any = None,
        env_key_fallback: str = "",
        cache_ttl: float = 3.0,
    ) -> None:
        self.path = config_path
        self._cfg = cfg or {}
        self._containers = containers or {}
        self.env_key_fallback = env_key_fallback
        self.cache_ttl = cache_ttl
        self._cache_t = 0.0
        self._cache_v: List[dict] = []

    # ---------- 基础读取 ----------
    def _read(self, src: Any) -> dict:
        return dict(src() if callable(src) else (src or {}))

    def cfg(self) -> dict:
        return self._read(self._cfg)

    def containers(self) -> dict:
        return self._read(self._containers)

    # ---------- 内置实例（向后兼容折算） ----------
    def builtins(self) -> List[dict]:
        """老配置的两个内置键 → 注册表前两项。不修改、不迁移、不删除任何旧值。"""
        c = self.cfg()
        svc = self.containers()
        spec = (
            ("emby", "main", "emby", "emby_key", "playback_reporting.db"),
            ("emby_adult", "adult", "emby_adult", "emby_key2", None),
        )
        out = []
        for which, short, ck, key_field, dbname in spec:
            conf = svc.get(ck) or {}
            key = c.get(key_field) or ""
            if which == BUILTIN_IDS[0] and not key:
                key = self.env_key_fallback
            out.append({
                "id": which,
                "short": short,
                "name": conf.get("name") or which,
                "label": conf.get("name") or which,
                "cname": conf.get("cname") or "",
                "addr": conf.get("addr") or "",
                "port": conf.get("port") or 0,
                "api_key": key,
                "pr_db_path": (c.get(short + "_pr_db_path") or "").strip()
                              or DEFAULT_PR_DB.get(short, ""),
                "dbname": dbname,
                "builtin": True,
                "enabled": True,
            })
        return out

    # ---------- 用户新增实例 ----------
    def extra(self) -> List[dict]:
        now = time.time()
        if self._cache_v and now - self._cache_t < self.cache_ttl:
            return list(self._cache_v)
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            items = d.get("instances") if isinstance(d, dict) else d
            out = [x for x in (items or [])
                   if isinstance(x, dict) and str(x.get("id") or "").strip()
                   and x.get("enabled", True) is not False]
        except Exception:
            out = []
        self._cache_t = now
        self._cache_v = out
        return list(out)

    def save_extra(self, items: List[dict]) -> List[dict]:
        """保存新增实例。打码值/空值 → 保留该 id 原有的 api_key（编辑不破坏凭据）。"""
        old = {str(x.get("id")): x for x in self.extra()}
        clean: List[dict] = []
        seen = set()
        for raw in (items or []):
            if not isinstance(raw, dict):
                continue
            e = dict(raw)
            e["id"] = re.sub(r"[^A-Za-z0-9_-]", "", str(e.get("id") or ""))[:48]
            if not e["id"]:
                e["id"] = "emby_x" + str(int(time.time() * 1000))[-7:]
            if e["id"] in seen or e["id"] in BUILTIN_IDS:
                continue
            seen.add(e["id"])
            e.setdefault("name", e["id"])
            e["short"] = re.sub(r"[^A-Za-z0-9_-]", "",
                                str(e.get("short") or e["id"]))[:32] or e["id"]
            try:
                e["port"] = int(float(e.get("port") or 0))
            except Exception:
                e["port"] = 0
            k = str(e.get("api_key") or "")
            if (not k) or set(k) == {"*"}:
                k = str((old.get(e["id"]) or {}).get("api_key") or "")
            e["api_key"] = k
            e["enabled"] = e.get("enabled", True) is not False
            clean.append(e)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"instances": clean}, f, ensure_ascii=False, indent=2)
        self._cache_t = 0.0
        self._cache_v = []
        return clean

    def delete_extra(self, which: str) -> List[dict]:
        keep = [x for x in self.extra() if str(x.get("id")) != which]
        return self.save_extra(keep)

    # ---------- 统一视图 ----------
    def all(self) -> List[dict]:
        """全部生效实例。所有功能模块只应遍历本方法，禁止硬编码实例名。"""
        out = list(self.builtins())
        for e in self.extra():
            out.append({
                "id": e["id"],
                "short": e.get("short") or e["id"],
                "name": e.get("name") or e["id"],
                "label": e.get("name") or e["id"],
                "cname": e.get("cname") or "",
                "addr": e.get("addr") or "",
                "port": e.get("port") or 0,
                "api_key": e.get("api_key") or "",
                "pr_db_path": e.get("pr_db_path") or "",
                "dbname": None,
                "builtin": False,
                "enabled": True,
            })
        return [x for x in out if x.get("enabled", True)]

    def ids(self) -> List[str]:
        return [x["id"] for x in self.all()]

    def pairs(self) -> List[tuple]:
        return [(x["id"], x["label"]) for x in self.all()]

    def conf(self, which: str) -> Optional[dict]:
        """按 id 取连接配置：内置走服务注册表，新增走本注册表。"""
        c = self.containers().get(which)
        if c:
            return c
        for e in self.all():
            if e["id"] == which and not e.get("builtin"):
                return {"name": e["name"], "cname": e["cname"],
                        "addr": e["addr"], "port": e["port"], "group": "media"}
        return None

    def key(self, which: str) -> str:
        for e in self.all():
            if e["id"] == which:
                return e.get("api_key") or ""
        return ""

    def db_paths(self) -> Dict[str, str]:
        """每个 short 一份播放记录库路径；新增实例未填则交给活动日志兜底。"""
        out = {}
        for e in self.all():
            if e.get("pr_db_path"):
                out[e["short"]] = e["pr_db_path"]
        return out

    # ---------- 对外（脱敏） ----------
    @staticmethod
    def _public_one(e: dict) -> dict:
        return {
            "id": e["id"], "short": e["short"], "name": e["name"],
            "label": e["label"], "cname": e["cname"], "addr": e["addr"],
            "port": e["port"], "builtin": bool(e["builtin"]),
            "has_key": bool(e.get("api_key")),
            "pr_db_path": e.get("pr_db_path") or "",
            "enabled": bool(e.get("enabled", True)),
        }

    def public(self) -> List[dict]:
        """给前端的实例清单：api_key 永不下发。"""
        return [self._public_one(e) for e in self.all()]

    def extra_public(self) -> List[dict]:
        """给实例管理表单的新增实例清单：api_key 打码，避免明文落 DOM。"""
        out = []
        for e in self.extra():
            x = dict(e)
            x["has_key"] = bool(x.get("api_key"))
            if x.get("api_key"):
                x["api_key"] = MASK
            out.append(x)
        return out
