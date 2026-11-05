# -*- coding: utf-8 -*-
"""bot 自检：遍历核心命令的「取数 + 渲染 + 发送」逻辑，结果私聊群主 + 写 JSON 日志到 rw 挂载（Web 可读）。

触发方式：
  · 群主发送 /selftest 手动触发；
  · 启动钩子在检测到 rw 挂载上的 .selftest_pending 标记文件时自动跑一次（部署后验证用）。
"""
import asyncio
import io
import json
import os
import time
import traceback
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from .. import bot, S, db, md, poster
from . import rank, redeem, keepalive as KA

LOG_PATH = "/app/keepalive/selftest_log.json"
PENDING = "/app/keepalive/.selftest_pending"
LAST = "/app/keepalive/.selftest_last"  # 限频标记：10 分钟内重复启动不重复跑


def _owner() -> int:
    try:
        return int(S.OWNER or 0)
    except Exception:
        return 0


async def _render_check(kind: str, days: int, client) -> dict:
    """对一个榜单做完整取数+渲染+图注长度校验，返回 {ok, detail}。"""
    data, cap, png = await rank._render_board(client, kind, days)
    if data is None:
        return {"ok": False, "detail": "取数返回 None（Emby 插件/DB 不可用）"}
    if not isinstance(png, (bytes, bytearray)):
        return {"ok": False, "detail": f"海报非字节: {type(png)}"}
    clen = len(rank._media_cap(kind, rank._title(kind, days), data, cap))
    if clen > 1020:
        return {"ok": False,
                "detail": f"媒体图注 {clen} 字 >1020，会触发 400 MEDIA_CAPTION_TOO_LONG，整图发不出"}
    return {"ok": True, "detail": f"图 {len(png)//1024}KB · 图注 {clen}字"}


async def run_selftest(client) -> dict:
    owner = _owner()
    results: list[dict] = []

    # 0) markdown mention 链接解析（主页链接能不能正确渲染成可点实体）
    try:
        out = md.build("**测试** [主页](tg://user?id=123)")
        results.append({"name": "markdown·mention主页链接", "ok": out is not None,
                        "detail": f"返回类型 {type(out).__name__}"})
    except Exception as e:
        results.append({"name": "markdown·mention主页链接", "ok": False,
                        "detail": f"{type(e).__name__}: {e}"})

    # 1) 六个榜单：影片/用户 × 今日/本周/本月
    for kind, days in [("i", 1), ("u", 1), ("i", 7), ("u", 7), ("i", 30), ("u", 30)]:
        nm = "榜单·" + ("影片" if kind == "i" else "用户") + (
            "今日" if days == 1 else ("本周" if days == 7 else "本月"))
        try:
            r = await _render_check(kind, days, client)
            r["name"] = nm
            results.append(r)
        except Exception as e:
            results.append({"name": nm, "ok": False,
                            "detail": f"{type(e).__name__}: {e}"})

    # 2) 兑唤菜单（验证 _duihuan_menu 缺参 bug 已修复）
    try:
        txt = redeem._duihuan_text(owner)
        menu = redeem._duihuan_menu(db.get_ls(owner), db.get_points(owner))
        kb = getattr(menu, "inline_keyboard", None)
        results.append({"name": "兑唤·灵石⇄V积分菜单",
                        "ok": isinstance(txt, str) and kb is not None,
                        "detail": f"text {len(txt)}字 · 按钮行 {len(kb) if kb else 0}"})
    except Exception as e:
        results.append({"name": "兑唤·灵石⇄V积分菜单", "ok": False,
                        "detail": f"{type(e).__name__}: {e}"})

    # 3) 兑换阁面板
    try:
        cv = db.get_cultivator(owner)
        if cv:
            lines = redeem.panel_lines(owner, cv["realm"])
            menu = redeem.panel_menu(cv["realm"], db.get_points(owner))
            ok = isinstance(lines, list) and getattr(menu, "inline_keyboard", None) is not None
            results.append({"name": "兑换阁·面板", "ok": ok,
                            "detail": f"行 {len(lines)} · 按钮 {'有' if getattr(menu,'inline_keyboard',None) else '无'}"})
        else:
            results.append({"name": "兑换阁·面板", "ok": True, "detail": "群主未踏入仙途（跳过）"})
    except Exception as e:
        results.append({"name": "兑换阁·面板", "ok": False, "detail": f"{type(e).__name__}: {e}"})

    # 4) 绑定状态文案
    try:
        bt = await asyncio.to_thread(rank.bind_text, owner)
        results.append({"name": "绑定·状态文案", "ok": isinstance(bt, str) and len(bt) > 0,
                        "detail": f"{len(bt)}字"})
    except Exception as e:
        results.append({"name": "绑定·状态文案", "ok": False, "detail": f"{type(e).__name__}: {e}"})

    # 5) 海报·用户榜纯渲染（无客户端依赖）
    try:
        us = (await asyncio.to_thread(rank.user_stats, 1)) or []
        p = poster.draw_user_board("测试用户榜", us)
        if isinstance(p, (bytes, bytearray)):
            results.append({"name": "海报·用户榜渲染", "ok": True, "detail": f"{len(p)//1024}KB"})
        else:
            results.append({"name": "海报·用户榜渲染", "ok": True, "detail": "空数据返回 None（非错误）"})
    except Exception as e:
        results.append({"name": "海报·用户榜渲染", "ok": False, "detail": f"{type(e).__name__}: {e}"})

    # 6) 保号状态可读 + 魔力值字段存在（PT 指标全站展示的数据源）
    try:
        st = KA.load_state()
        sites = st.get("sites") or {}
        nok = sum(1 for s in sites.values()
                  if isinstance(s, dict) and (s.get("stats") or {}).get("bonus") is not None)
        results.append({"name": "保号·状态/魔力值", "ok": bool(sites),
                        "detail": f"{len(sites)} 站 · 含魔力值 {nok} 站"})
    except Exception as e:
        results.append({"name": "保号·状态/魔力值", "ok": False, "detail": f"{type(e).__name__}: {e}"})

    # 7) 真实把用户榜图片私聊给群主（端到端验证：图片能发出去、不再 MEDIA_CAPTION_TOO_LONG）
    try:
        data, cap, png = await rank._render_board(client, "u", 1)
        if isinstance(png, (bytes, bytearray)):
            cap_media = rank._media_cap("u", rank._title("u", 1), data, cap)
            await bot.send_photo(owner, io.BytesIO(png), caption=cap_media,
                                 disable_notification=True)
            results.append({"name": "实发·用户榜图片(端到端)", "ok": True,
                            "detail": f"{len(png)//1024}KB 已私聊群主"})
        else:
            results.append({"name": "实发·用户榜图片(端到端)", "ok": False,
                            "detail": "海报非字节"})
    except Exception as e:
        results.append({"name": "实发·用户榜图片(端到端)", "ok": False,
                        "detail": f"{type(e).__name__}: {e}"})

    # 8) 三维度超链接完整性：每个已关联用户的名/UID/Emby名三行都必须可点
    try:
        data = (await asyncio.to_thread(rank.user_stats, 1)) or []
        tmap = rank.tg_map() or {}
        bound = [v for v in data if tmap.get(str(v.get("name")))]
        if not bound:
            results.append({"name": "三维度超链接完整性", "ok": True,
                            "detail": "当前无已关联用户（跳过）"})
        else:
            txt, ents = md.build(rank._user_caption("今日用户榜", bound[:3], False))
            mids = [e["user_id"] for e in ents if e.get("type") == "mention"]
            want = [int(tmap[str(v["name"])]) for v in bound[:3]]
            # 每个 tg 应至少出现 3 次（三行各一个 mention）
            miss = [t for t in want if mids.count(t) < 3]
            ok = not miss
            results.append({"name": "三维度超链接完整性",
                            "ok": ok,
                            "detail": (f"{len(want)} 位已关联，每行均出 mention"
                                       if ok else f"mention 不足的 tg: {miss}")})
    except Exception as e:
        results.append({"name": "三维度超链接完整性", "ok": False,
                        "detail": f"{type(e).__name__}: {e}"})

    # 9) 链接文字不得再叠粗体（mention+bold 同段会让 TG 客户端吞掉链接）
    try:
        data = (await asyncio.to_thread(rank.user_stats, 1)) or []
        tmap = rank.tg_map() or {}
        bound = [v for v in data if tmap.get(str(v.get("name")))]
        if not bound:
            results.append({"name": "链接不叠粗体", "ok": True, "detail": "无已关联用户（跳过）"})
        else:
            txt, ents = md.build(rank._user_caption("今日用户榜", bound[:3], False))
            both = [e for e in ents
                    if e.get("type") == "mention"
                    and any(b.get("type") == "bold" and b["offset"] == e["offset"]
                            and b["length"] == e["length"] for b in ents)]
            ok = not both
            results.append({"name": "链接不叠粗体", "ok": ok,
                            "detail": ("无 mention+bold 叠段" if ok
                                       else f"{len(both)} 处链接仍叠着粗体（会被吞）")})
    except Exception as e:
        results.append({"name": "链接不叠粗体", "ok": False,
                        "detail": f"{type(e).__name__}: {e}"})

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ver = getattr(S, "VERSION", "") if hasattr(S, "VERSION") else ""
    summary = {"ts": ts, "version": ver, "total": total,
               "passed": passed, "failed": total - passed, "results": results}

    # 写 JSON 日志（Web 可读，rw 挂载）
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception:
        try:
            with open("/tmp/selftest_log.json", "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # 私聊群主
    if owner:
        try:
            lines = [f"🧪 <b>KFC bot 自检报告</b> · {ts}",
                     f"通过 <b>{passed}/{total}</b>" + ("" if passed == total else f" · 失败 <b>{total - passed}</b>"),
                     ""]
            for r in results:
                icon = "✅" if r["ok"] else "❌"
                lines.append(f"{icon} {r['name']}　<i>{r['detail']}</i>")
            lines.append("")
            lines.append("详情见 Web 仪表盘 → 自检日志")
            await bot.send_message(owner, "\n".join(lines),
                                   parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            pass
    return summary


@bot.on_message(filters.command(["selftest", "自检"], prefixes=S.PREFIX))
async def cmd_selftest(client, msg: Message):
    """群主手动触发全命令自检，结果私聊本人。"""
    from .request import is_admin
    if not is_admin(msg.from_user.id):
        await msg.reply("⛔ 仅管理员可用")
        return
    await msg.reply("🧪 开始自检，结果稍后私聊给你…")
    await run_selftest(client)


async def maybe_selftest(client):
    """启动钩子：每次启动自动跑一次全命令自检（10 分钟内重复启动不重复跑，防重启循环刷屏）。
    结果私聊群主 + 写 rw 挂载 JSON（Web 可读）。"""
    try:
        try:
            last = 0.0
            try:
                with open(LAST, "r") as f:
                    last = float((f.read() or "0").strip() or 0)
            except Exception:
                pass
            if time.time() - last < 600:
                return
            with open(LAST, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        await run_selftest(client)
    except Exception as e:
        try:
            import logging
            logging.getLogger("selftest").warning(f"自检启动钩子失败: {e}")
        except Exception:
            pass
