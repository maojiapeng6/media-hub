"""零凭据参考实现：NAS 内存预警（面板指标 + TG 推送节流 + 用户观影清单合并）。

本模块不绑定任何服务器地址 / API Key / 内网路径 / Bot Token：所有外部调用通过
回调注入，纯标准库即可单元测试（tests/test_memguard.py）。

线上 media-hub 的等价实现是 app.py 的 ``mg_snapshot`` / ``mem_guard_loop`` /
``emby_userplays``，行为契约一致：

1. **采集**：``/proc/meminfo``（MemTotal / MemAvailable / SwapTotal / SwapFree）、
   ``/proc/pressure/memory``（avg10 some/full）、``/proc/vmstat``（oom_kill）、
   ``/proc/loadavg``；容器占用 Top 由调用方注入 Docker Engine stats 回调。
   ⚠ 容器内的 /proc/meminfo 反映的是**宿主机**真值（cgroup v1 环境实测一致），
   若部署在 cgroup v2 + 内存命名空间隔离的环境，应改读
   ``/sys/fs/cgroup/memory.current`` 与 ``memory.max``。
2. **分级**：内存占用 / Swap 占用 / PSI full 任一越「危险」阈值 → crit，
   任一越「注意」阈值 → warn，否则 ok。
3. **推送节流**（绝不刷屏）：仅「等级跃迁」「冷却到期」「新增 OOM 计数」三种情况推；
   恢复正常不推；开关关闭不推；进程刚启动、OOM 基线尚未建立时不误推。
4. **观影清单**：多台媒体服务器分别取播放明细，按「时间(分钟) + 片名」去重，
   按时间倒序合并，返回统一的 rows 结构给前端弹层。

红线：**只读**。本模块不停止任何进程、不清理内存、不修改任何服务配置，
只提供「该关点什么」的建议与推送，实际处置永远由人决定。
"""
from __future__ import annotations

import time

DEFAULT_CONFIG = {
    "enabled": True,
    "warn_swap": 60,
    "crit_swap": 80,
    "warn_mem": 82,
    "crit_mem": 91,
    "cooldown_min": 15,
}
# 核心服务不进入「可考虑关闭」建议，避免误伤线上链路
CORE_SERVICES = frozenset({
    "media-hub", "emby", "emby-main", "emby-adult", "mediatidy",
    "clouddrive2", "mysql", "moviepilot", "qbittorrent",
})
_LIMIT_KEYS = ("warn_swap", "crit_swap", "warn_mem", "crit_mem", "cooldown_min")


def normalize_config(raw=None, base=None):
    """合并用户配置并夹到安全区间；未知键忽略，类型错误保留默认。"""
    cfg = dict(DEFAULT_CONFIG if base is None else base)
    if not isinstance(raw, dict):
        return cfg
    if "enabled" in raw:
        cfg["enabled"] = bool(raw["enabled"])
    for k in _LIMIT_KEYS:
        if k in raw:
            try:
                cfg[k] = max(1, min(240, int(raw[k])))
            except (TypeError, ValueError):
                pass
    return cfg


def parse_meminfo(text):
    """``/proc/meminfo`` 文本 → {key: kB}；缺行返回 0，不抛异常。"""
    out = {}
    for line in (text or "").splitlines():
        p = line.split(":", 1)
        if len(p) != 2:
            continue
        try:
            out[p[0]] = int(p[1].strip().split()[0])
        except (ValueError, IndexError):
            continue
    return out


def parse_psi(text):
    """``/proc/pressure/memory`` → {some: avg10, full: avg10}。"""
    out = {"some": 0.0, "full": 0.0}
    for line in (text or "").splitlines():
        p = line.split()
        if not p or p[0] not in ("some", "full"):
            continue
        for seg in p[1:]:
            if seg.startswith("avg10="):
                try:
                    out[p[0]] = float(seg.split("=", 1)[1])
                except ValueError:
                    pass
    return out


def _pct(used, total):
    return round(used / total * 100, 1) if total else 0.0


def level_of(mem_pct, swap_pct, psi_full, cfg=None):
    """三级判定：crit 优先于 warn；阈值均可在面板调整。"""
    c = normalize_config(cfg)
    if (swap_pct >= c["crit_swap"] or mem_pct >= c["crit_mem"] or psi_full >= 8):
        return "crit"
    if (swap_pct >= c["warn_swap"] or mem_pct >= c["warn_mem"] or psi_full >= 2):
        return "warn"
    return "ok"


def advice_for(top, cfg=None, core=None, swap_pct=0):
    """生成「可考虑关闭 / 限流」建议：只列非核心且占用 >= 0.6G 的容器。"""
    core = CORE_SERVICES if core is None else core
    c = normalize_config(cfg)
    rows = []
    for t in (top or [])[:5]:
        name, gb = str(t.get("name") or ""), float(t.get("gb") or 0)
        if gb >= 0.6 and name not in core:
            rows.append("可考虑关闭 / 限流 <b>%s</b>（占 %.2fG）" % (name, gb))
    rows.append("优先检查：闲置虚拟机、qBittorrent 大批量下载、动态封面渲染类定时任务")
    if swap_pct >= c["crit_swap"]:
        rows.insert(0, "<b>Swap 即将打满</b>——再涨会触发整机 OOM，服务会突然连不上")
    return rows


def snapshot(read_file=None, top_containers=None, cfg=None, now=None, force=False,
             cache=None):
    """组装与线上 ``/api/memguard/status`` 同构的快照。

    :param read_file: 回调 ``(path) -> str``，读不到应抛异常或返回空串。
    :param top_containers: 回调 ``() -> [{'name': str, 'gb': float}, ...]``。
    :param cache: 可选 dict ``{'ts': float, 'data': dict}``，命中 15s 内直接复用
        （面板读缓存，后台线程用 ``force=True`` 强制重采样）。
    """
    t = time.time() if now is None else now
    if not force and cache and cache.get("data") and t - cache.get("ts", 0) < 15:
        return cache["data"]
    rd = read_file or (lambda p: "")
    try:
        mi = parse_meminfo(rd("/proc/meminfo"))
    except Exception:
        mi = {}
    try:
        psi = parse_psi(rd("/proc/pressure/memory"))
    except Exception:
        psi = {"some": 0.0, "full": 0.0}
    total, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
    swt, swf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
    mem_pct = _pct(total - avail, total)
    sw_pct = _pct(swt - swf, swt)
    oom = 0
    try:
        for ln in (rd("/proc/vmstat") or "").splitlines():
            if ln.startswith("oom_kill "):
                oom = int(ln.split()[1])
                break
    except Exception:
        pass
    try:
        load = " / ".join((rd("/proc/loadavg") or "").split()[:3])
    except Exception:
        load = ""
    try:
        top = list(top_containers() or []) if top_containers else []
    except Exception:
        top = []
    c = normalize_config(cfg)
    lv = level_of(mem_pct, sw_pct, psi.get("full", 0.0), c)
    d = {
        "ok": True, "ts": time.strftime("%F %T"), "level": lv,
        "mem": {"total_gb": round(total / 1048576.0, 1),
                "avail_gb": round(avail / 1048576.0, 1), "pct": mem_pct},
        "swap": {"total_gb": round(swt / 1048576.0, 1),
                 "used_gb": round((swt - swf) / 1048576.0, 1), "pct": sw_pct},
        "psi_some": psi.get("some", 0.0), "psi_full": psi.get("full", 0.0),
        "oom_kill": oom, "load": load, "top": top,
        "advice": advice_for(top, c, swap_pct=sw_pct) if lv != "ok" else [],
        "cfg": c,
    }
    if cache is not None:
        cache.update({"ts": t, "data": d})
    return d


def should_push(state, snap, cfg=None, now=None):
    """推送节流：返回 ``(是否推送, 原因)``；调用方负责用结果更新 state。

    state 结构（原地修改）：``{'last': float, 'level': str, 'oom': int}``。
    规则：关闭 → 不推；ok → 不推且把 level 复位；等级跃迁 → 推；
    冷却到期 → 推；OOM 计数新增（且基线已建立）→ 推。
    """
    c = normalize_config(cfg)
    st = state if isinstance(state, dict) else {"last": 0.0, "level": "ok", "oom": 0}
    t = time.time() if now is None else now
    lv = str(snap.get("level") or "ok")
    oom = int(snap.get("oom_kill") or 0)
    prev_oom = int(st.get("oom") or 0)
    prev_lv = str(st.get("level") or "ok")
    if not c["enabled"]:
        st.update({"level": lv, "oom": oom})
        return False, "disabled"
    if lv == "ok":
        st.update({"level": "ok", "oom": oom})
        return False, "ok"
    escalate = lv != prev_lv
    repeat = (t - float(st.get("last") or 0)) > float(c["cooldown_min"]) * 60
    oom_new = oom > prev_oom and prev_oom > 0
    reason = "escalate" if escalate else ("oom" if oom_new else ("cooldown" if repeat else ""))
    need = bool(reason)
    if need:
        st["last"] = t
    st.update({"level": lv, "oom": oom})
    return need, reason or "throttled"


def format_message(d, test=False):
    """TG HTML 文案（不含任何凭据、地址或个人信息，只有指标与建议）。"""
    lv, tag = d.get("level"), ""
    tag = "🔴 危险" if lv == "crit" else ("🟡 注意" if lv == "warn" else "🟢 正常")
    head = "🧪 <b>内存预警 · 测试推送</b>" if test else "🚨 <b>NAS 内存预警 · %s</b>" % tag
    mem, sw = d.get("mem") or {}, d.get("swap") or {}
    lines = [head, "",
             "内存 <b>%.1f%%</b>（可用 %.1fG / %.1fG）" % (
                 mem.get("pct", 0), mem.get("avail_gb", 0), mem.get("total_gb", 0)),
             "Swap <b>%.1f%%</b>（已用 %.1fG / %.1fG）" % (
                 sw.get("pct", 0), sw.get("used_gb", 0), sw.get("total_gb", 0)),
             "IO 等待 full %.1f · OOM 累计 %s 次 · load %s" % (
                 d.get("psi_full") or 0, d.get("oom_kill") or 0, d.get("load") or "-")]
    if d.get("top"):
        lines += ["", "占用 Top：" + " / ".join(
            "%s %.2fG" % (t.get("name", "?"), t.get("gb", 0)) for t in d["top"][:4])]
    if d.get("advice"):
        lines += ["", "<b>建议现在做：</b>"] + ["· " + a for a in d["advice"][:4]]
    lines += ["", "面板 → 仪表盘 → 🛡 内存预警，可查看明细 / 调整阈值 / 关闭推送"]
    return "\n".join(lines)


_ROW_FIELDS = ("ts", "item", "item_id", "type", "img", "server", "server_name",
               "play_sec", "client", "device", "tmdb", "year", "quality", "series")


def merge_userplays(parts, days=365, limit=120):
    """合并多台媒体服务器的某用户播放明细（与 ``/api/emby/userplays`` 同构）。

    :param parts: 可迭代的 ``{'rows': [...]}``（各服务器分别查询的结果）。
    去重键 = ``(时间前 16 位, 片名)``：同一分钟内同一片的重复上报只留一条。
    """
    try:
        days = max(1, min(3650, int(days or 365)))
        limit = max(1, min(300, int(limit or 120)))
    except (TypeError, ValueError):
        days, limit = 365, 120
    seen, rows = set(), []
    for part in parts or []:
        for x in (part or {}).get("rows") or []:
            if not isinstance(x, dict):
                continue
            k = (str(x.get("ts") or "")[:16], str(x.get("item") or ""))
            if k in seen:
                continue
            seen.add(k)
            rows.append({f: x.get(f) for f in _ROW_FIELDS})
    rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return {"ok": bool(rows), "rows": rows[:limit], "total": len(rows), "days": days}
