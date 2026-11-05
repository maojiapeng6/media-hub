"""界面：按钮与文案"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KM

from . import settings as S


# ---------------------------------------------------------------- 主菜单
# 设计原则：**点一下就执行**。
#   · 无需额外输入的动作（打坐 / 闭关 / 突破 / 日常 / 成就 …）点完立刻出结果；
#   · 需要选择的动作（坊市 / 兑换阁 / 榜单 …）点完进入「二级选择界面」；
#   · 唯一必须自己打字的是「取道号 / 求片」这类本身就有内容的输入。
def main_menu() -> KM:
    return KM([
        [B("🧘 打坐", "sign"), B("📖 闭关", "train"), B("⚡ 突破", "tupo")],
        [B("👤 我的道途", "me"), B("🏯 坊市", "shop"), B("📜 日常", "tasks")],
        [B("🎟 兑换阁", "ex"), B("💱 换 V积分", "dh"), B("🏅 成就", "ach")],
        [B("🔮 天机榜", "rank"), B("📊 观看榜", "board"), B("📡 在线播报", "now")],
        [B("🔍 求片", "qpnew"), B("📡 下载进度", "dlpanel"), B("📋 我的求片", "qpmine")],
        [B("📺 我的账号", "account"), B("🔗 绑定", "bind"), B("❓ 帮助", "help")],
        [B("❌ 关闭", "close")],
    ])


def start_hub_text() -> str:
    """/start 双面板选择页文案"""
    return "\n".join([
        f"🌌 **欢迎来到 {S.SERVER_NAME}**", divider(),
        "请选择要进入的面板：",
        "",
        "📺 **EMBY 面板**｜开号 / 兑换码 / 线路 / 媒体库 / 统计 / 重置 / 删除 / 绑115",
        "🧘 **修仙面板**｜打坐闭关 / 兑换阁 / 求片 / 排行榜",
    ])


def start_hub_menu() -> KM:
    """/start 双面板选择按钮"""
    return KM([
        [B("📺 EMBY 面板", "hub:emby"), B("🧘 修仙面板", "hub:game")],
    ])


def after_menu(extra: list[tuple[str, str]] | None = None) -> KM:
    """执行结果页的底部按钮：可带若干「接着做」的快捷动作，末尾恒有返回主菜单"""
    rows, cur = [], []
    for label, data in (extra or []):
        cur.append(B(label, data))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([B("↩️ 返回主菜单", "menu")])
    return KM(rows)


def rank_pick_menu() -> KM:
    """天机榜二级界面"""
    return KM([
        [B("🌌 境界榜", "rank:realm"), B("💰 财富榜", "rank:rich")],
        [B("📊 我的排名", "rank:mine")],
        [B("↩️ 返回主菜单", "menu")],
    ])


def board_pick_menu() -> KM:
    """观看榜二级界面：影片 / 用户 × 今日 / 本周 / 本月"""
    return KM([
        [B("🎬 今日影片榜", "rk:i:1"), B("👥 今日用户榜", "rk:u:1")],
        [B("🎬 本周影片榜", "rk:i:7"), B("👥 本周用户榜", "rk:u:7")],
        [B("🎬 本月影片榜", "rk:i:30"), B("👥 本月用户榜", "rk:u:30")],
        [B("📺 我的观看统计", "rk:mine")],
        [B("↩️ 返回主菜单", "menu")],
    ])


def board_pick_text() -> str:
    return ("📊 **观看榜**\n" + divider() + "\n"
            "🎬 影片榜｜哪部片最火\n"
            "👥 用户榜｜谁看得最多\n\n"
            "_下面每个按钮都能直接点，点完自动出榜_")


def sect_menu() -> KM:
    rows = [[B(f"{v[1]} {v[0]}", f"sect:{k}")] for k, v in S.SECTS.items()]
    rows.append([B("❌ 取消", "close")])
    return KM(rows)


def shop_menu(points: int) -> KM:
    rows = []
    for k, (name, emo, price, desc, _t) in S.SHOP_ITEMS.items():
        rows.append([B(f"{emo} {name} · {price}{S.CURRENCY}", f"buy:{k}")])
    rows.append([B("🎟 兑换阁（注册码 / 续期 / 白名单）", "ex:panel")])
    rows.append([B(f"💱 灵石换 V积分（{S.LS_PER_V}:1）", "dh:panel")])
    rows.append([B(f"💰 余额 {points} {S.CURRENCY}", "me")])
    rows.append([B("↩️ 返回", "menu")])
    return KM(rows)


def shop_lines() -> list[str]:
    return [f"🏯 **坊市**　{divider()}"] + [
        f"{emo} **{name}**｜`{price}` {S.CURRENCY}\n　　_{desc}_"
        for name, emo, price, desc, _t in S.SHOP_ITEMS.values()
    ] + [
        divider(),
        "🎟 **兑换阁**：注册码 / 续期码 / 白名单（只花 💎V积分）",
        "　不设购买门槛：任何段位都能买，段位越高折扣越大（仙人最高 8.2 折）",
        "　灵石不能换码，先用 `/duihuan` 按比例换成 V积分",
        "　点下方按钮进入 `/exchange`",
    ]


def back_menu() -> KM:
    return KM([[B("↩️ 返回主菜单", "menu")]])


def close_menu() -> KM:
    return KM([[B("❌ 关闭", "close")]])


# ---------------------------------------------------------------- 文案
def divider() -> str:
    return "━━━━━━━━━━━━━━━━━━"


# ───────────────────────────────────────── 勾选态字形（全站统一）
# ★ 用户 2026-10-11 要求：**勾上必须是绿色 ✅**。
#   原来用 ☑（灰色 ballot box），在 Telegram 里灰扑扑的，「感觉跟没点到一样」。
#   所以：已勾选 = ✅（绿色），未勾选 = ⬜（空框）。
ON = "✅"
OFF = "⬜"


def mark(flag) -> str:
    """勾选态 → 字形。任何勾选界面（PT 任务面板 / 权限面板 / 选集）都走这里。"""
    return ON if flag else OFF


# Markdown 转义字符：用户名/道号是用户可控内容，一旦混进这些字符会让
# 整条消息解析失败（Telegram 直接报 Can't parse entities），用户就"没反应"了
_MD_CHARS = "\\`*_{}[]()#+-.!|~>"

# ★ 但 `[文字](链接)` 这种可点链接必须活下来 —— 它上面的 [ ] ( ) 正好都在
#   _MD_CHARS 里，直接剔除会把 `[爸爸](tg://user?id=123456789)` 拆成
#   `爸爸tg://user?id=123456789` ⇒ **超链接裸露成一串裸 URL**。
#   做法：先把链接抠出来换成占位符，剔完字符再原样放回去。
_LINK_RE = re.compile(r"\[([^\]\n]{1,64})\]\(([^)\s]{1,300})\)")


def safe(text: str | None) -> str:
    """把用户可控文本里的 Markdown 元字符剔除，避免解析失败导致消息发不出去

    ★ 会**完整保留** `[文字](链接)` 形式的可点链接（含 tg://user?id= 主页链接）。
    """
    if not text:
        return ""
    out = str(text)
    links: list[str] = []

    def _keep(m):
        links.append(m.group(0))
        return f"\x00L{len(links) - 1}\x00"

    out = _LINK_RE.sub(_keep, out)
    for ch in _MD_CHARS:
        out = out.replace(ch, "")
    for i, l in enumerate(links):
        out = out.replace(f"\x00L{i}\x00", l)
    return out


# ★ 2026-11-04：兑换码专用展示。safe() 会把 '-' 和 '_' 当 Markdown 转义符删掉，
#   导致 BRAND-123456789-Register-XXXX 显示成连号 BRAND123456789RegisterXXXX（用户
#   投诉「中间没有杠」）。码一律放进 `...` 反引号里，只需剔除反引号/换行即可，
#   必须完整保留 '-' 与 '_'。
def code(text: str | None) -> str:
    """兑换码等「原样展示、保留 - 和 _」的文本清洗（放进反引号内使用）。"""
    if not text:
        return ""
    return str(text).replace("`", "").replace("\r", "").replace("\n", "")


# ───────────────────────────────────────── 超链接 / TMDB
# ★ 全站统一（用户 2026-10-11 要求）：
#   · 影片名一律做成**可点的 TMDB 超链接**，通知里不再出现纯文本片名；
#   · 用户名一律做成指向 TG 主页的超链接，且 TG 用户名前面**不带 @**。
TMDB_BASE = "https://www.themoviedb.org"


def esc(s) -> str:
    """HTML 转义（只处理 & < > 三个字符，与 request._esc 同款）"""
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _cut(s: str, n: int) -> str:
    """截断，但不把 &lt; 这类实体砍成半成品（宁可少留一个字符）"""
    s = str(s or "")
    if len(s) <= n:
        return s
    out = s[:n]
    i = out.rfind("&")
    if i >= 0 and ";" not in out[i:] and len(out) - i < 6:
        out = out[:i]
    return out


_TAG_STRIP_RE = re.compile(r"<[^>]*>")


def visible_len(s) -> int:
    """Telegram 的长度限制算的是**解析后的纯文本**长度 —— HTML 标签不计。

    实测：8 条任务的 PT 面板 raw 长度 1507，可见长度只有 945。
    直接 `s[:1024]` 会白扔掉 400 多个字符的 <a href> 标签开销，
    表现为「一页 8 条只显示 5 条，最后一行还被砍半」。
    """
    return len(_TAG_STRIP_RE.sub("", str(s or "")))


def clip_html(text, limit: int = 1024) -> str:
    """按 Telegram 的 1024 上限安全截断（HTML 文案专用）

    两条规矩：
      · 只统计**可见文本**，标签不算 ⇒ 同样的 1024 能多放好几行；
      · **整行**取舍，绝不切断标签、也不把一行砍成半截 ——
        否则用户会看到裸奔的 `<a href="https://...`。
    """
    s = str(text or "")
    if visible_len(s) <= limit:
        return s
    out: list[str] = []
    used = 0
    for line in s.split("\n"):
        v = visible_len(line)
        if out and used + v + 1 > limit:
            break
        out.append(line)
        used += v + 1
    if not out:                       # 极端：单行就超长 → 退化为按可见长度硬截
        out = [_cut(s.split("\n")[0], limit)]
    return "\n".join(out)


def tmdb_url(tid, mtype: str = "") -> str:
    """TMDB 详情页：电视剧走 /tv/，其余（电影 / 类型未知）走 /movie/"""
    t = str(tid or "").strip()
    if not t or not t.isdigit():
        return ""
    return f"{TMDB_BASE}/{'tv' if str(mtype or '') == '电视剧' else 'movie'}/{t}"


def tmdb_search_url(title) -> str:
    """没有 TMDB ID 时的兜底：跳 TMDB 搜索页 —— 总之片名永远可点"""
    q = str(title or "").strip()
    return f"{TMDB_BASE}/search?query={quote(q)}" if q else ""


def link_html(text, url: str, n: int = 0) -> str:
    """HTML 超链接（文字自动转义）；没有 url 就退回纯文本"""
    t = esc(text)
    if n:
        t = _cut(t, n)
    return f'<a href="{esc(url)}">{t}</a>' if url else t


def link_md(text, url: str, n: int = 0) -> str:
    """Markdown 超链接（文字自动转义）；没有 url 就退回纯文本"""
    t = safe(text)
    if n:
        t = _cut(t, n)
    return f"[{t}]({url})" if url else t


def title_link(title, tid="", mtype: str = "", n: int = 0,
               mode: str = "html") -> str:
    """片名 → **可点击的 TMDB 超链接**

    有 TMDB ID 直达详情页；没有就退到 TMDB 搜索页（片名仍然可点）。
    mode: html（request / reqmgr / mptask）或 md（inbox 播报）。
    """
    raw = str(title or "").strip()
    url = tmdb_url(tid, mtype) or tmdb_search_url(raw)
    if mode == "html":
        return link_html(raw, url, n)
    # ★ 用户 2026-10-12 要求：影片名一律**粗体 + 超链接**。
    #   md 模式下写成 **[片名](url)**，由 md.build() 特判拆成 bold + link 两个实体。
    return f"**{link_md(raw, url, n)}**"


# ───────────────────────────────────────── 身份展示（Emby 名 + TG 名 · 两行）
def tg_home(uid) -> str:
    """TG 主页链接 tg://user?id=xxx —— 点一下直达对方主页"""
    try:
        u = int(uid)
    except Exception:
        return ""
    return f"tg://user?id={u}" if u else ""


def user_card(uid, tg_name: str = "", tg_username: str = "",
              emby_name: str = "", label: str = "", icon: str = "👤",
              mode: str = "html", indent: str = "　　") -> list[str]:
    """★ 三行标准身份卡（用户 2026-10-15 定稿）——所有个人相关播报统一走这里：

      第一行：**实时用户名**（粗体，超链接 → tg://user?id=UID）
      第二行：**UID 数字**（粗体，超链接 → 同上；UID 永不变，定位最准）
      第三行：**Emby 用户名**（粗体，超链接 → 同上）

    为什么三行都链到 uid：改名了主页还是他；播报时上层先刷新 tg_name
    （签到/续期/求片场景都会带最新 first_name），保证"实时用户名"。
    TG ID 缺失（用户已注销）返回空列表，由调用方决定显示。
    """
    url = tg_home(uid)
    if not url:
        return []
    tg_txt = (str(tg_name or "").strip() or str(tg_username or "").strip()
              or "未知用户")
    emby_txt = str(emby_name or "").strip() or "未绑定"
    # ★ 2026-10-15 用户要求：三行必须带**文字标签**（用户名/UID/Emby用户名），
    #   不能光秃秃出三个值——否则读者分不清哪行是谁。
    head = f"{icon} {label}" if label else f"{icon}"
    if mode == "html":
        return [f'{head}｜用户名：<b><a href="{esc(url)}">{_cut(esc(tg_txt), 24)}</a></b>',
                f'{indent}UID：<b><a href="{esc(url)}">{_esc_num(uid)}</a></b>',
                f'{indent}Emby用户名：<b><a href="{esc(url)}">{_cut(esc(emby_txt), 24)}</a></b>']
    # 本引擎 md 解析器特判 **[..](..)** → 粗体+超链接合体（md.py:222）
    return [f"{head}｜用户名：**[{_cut(safe(tg_txt), 24)}]({url})**",
            f"{indent}UID：**[{uid}]({url})**",
            f"{indent}Emby用户名：**[{_cut(safe(emby_txt), 24)}]({url})**"]


def _esc_num(v) -> str:
    try:
        return str(int(v))
    except Exception:
        return esc(str(v or ""))


# Emby 用户名查询缓存：每次播报都查库太浪费，10 分钟足够新鲜
_EMBY_NC: dict[int, tuple[float, str | None]] = {}
_EMBY_NC_TTL = 600


def emby_name_of(tg) -> str | None:
    """查某个 TG 用户绑定的 Emby 用户名。查不到返回 None（调用方显示「未绑定」）

    两条来源（与 rank._myview 同口径）：
      ① kfc_bind —— 群友自己 /bind 认领的（主要来源，最可靠）
      ② EmbyBoss 的 emby.name —— 几乎全空，仅作兜底
    """
    try:
        uid = int(tg)
    except Exception:
        return None
    if not uid:
        return None
    now = time.time()
    hit = _EMBY_NC.get(uid)
    if hit and now - hit[0] < _EMBY_NC_TTL:
        return hit[1]
    name = None
    try:
        from . import db
        name = db.my_bind(uid)
        if not name:
            from sqlalchemy import text as _sql
            with db._engine.connect() as c:
                r = c.execute(_sql(
                    f"SELECT name FROM `{db.S.SHARED_DB}`.emby WHERE tg = :tg"),
                    {"tg": uid}).first()
            if r and r[0] and r[0] != "?":
                name = str(r[0])
    except Exception:
        name = name or None
    _EMBY_NC[uid] = (now, name)
    if len(_EMBY_NC) > 500:                       # 别让缓存无限涨
        _EMBY_NC.pop(next(iter(_EMBY_NC)), None)
    return name


def bar(pct, width: int = 14, fill: str = "█", empty: str = "░") -> str:
    """进度条 —— 全站同款（█ 实心 + ░ 空心）"""
    try:
        p = max(0.0, min(100.0, float(pct or 0)))
    except Exception:
        p = 0.0
    n = int(round(p / 100 * width))
    return fill * n + empty * (width - n)


# ───────────────────────────────────────── 中文序数
# 榜单统一写「第一名 / 第二名」，不再出现 01 02 03 这种机械序号；
# 而且第 1~10 名都是「第 + 一个汉字 + 名」，字数完全一致，右侧内容天然对齐。
_CN_DIGIT = "零一二三四五六七八九"


def cn_no(i: int) -> str:
    """阿拉伯数字 -> 中文序数词：1→一　10→十　11→十一　21→二十一"""
    try:
        n = int(i)
    except Exception:
        return str(i)
    if n < 0:
        return str(n)
    if n < 10:
        return _CN_DIGIT[n]
    if n < 20:
        return "十" + (_CN_DIGIT[n % 10] if n % 10 else "")
    if n < 100:
        t, o = divmod(n, 10)
        return _CN_DIGIT[t] + "十" + (_CN_DIGIT[o] if o else "")
    if n == 100:
        return "一百"
    return str(n)


_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def rank_tag(i: int) -> str:
    """榜单名次抬头：前三金银铜，其后统一奖牌 + 中文名次"""
    return f"{_MEDALS.get(i, '🏅')} 第{cn_no(i)}名"


def cultivator_card(cv: dict, ls: int, us: int, rank: int | None,
                    is_owner: bool = False) -> str:
    lv = cv["realm"]
    _, rname, need, gain, flavor = S.realm_by_level(lv)
    sect_name, sect_emo = S.SECTS[cv["sect"]][0], S.SECTS[cv["sect"]][1]
    nxt = S.realm_by_level(lv + 1) if lv < S.MAX_REALM else None
    prog = ""
    if nxt:
        base = S.realm_by_level(lv)[2]
        span = max(nxt[2] - base, 1)
        cur = max(cv["cultivation"] - base, 0)
        bar_n = min(int(cur / span * 10), 10)
        prog = f"`{'█' * bar_n}{'░' * (10 - bar_n)}` {cur}/{span}"
    else:
        prog = "`██████████` 已至巅峰"
    return (
        f"{sect_emo} **{cv['daohao']}**\n"
        f"{divider()}\n"
        f"🏯 门派｜**{sect_name}**\n"
        f"🌌 境界｜**{rname}**（第 **{lv}** 重）\n"
        f"📈 修为｜**{cv['cultivation']}**\n"
        f"　　{prog}\n"
        f"🪙 灵石｜**{'∞ 无限' if is_owner else ls}**\n"
        f"💎 V积分｜**{'∞ 无限' if is_owner else us}**（与 RT 1:1）\n"
        f"🔮 天机排名｜第 **{cn_no(rank) if rank else '-'}** 名\n"
        f"{divider()}\n"
        f"📜 _{flavor}_\n"
        f"🧘 打坐基础收益｜**{gain} {S.CURRENCY}**"
        + (f"　🔥 连续 **{cv['sign_streak']}** 天" if cv["sign_streak"] else "")
    )


_HELP_BASE = f"""🌌 **{S.SERVER_NAME} · 修仙录**
{divider()}
**🎛 主菜单：点一下就执行**
`/start` 或 `/me` 都会弹出主菜单，**所有按钮都是直接生效的**：
　🧘 打坐　📖 闭关　⚡ 突破　→　点了立刻结算，不用再打命令
　👤 我的道途　📜 日常　🏅 成就　→　点了立刻出结果
　📡 在线播报　📡 下载进度　→　点了立刻出一条带进度条的实时面板
　🏯 坊市　🎟 兑换阁　💱 换 V积分　🔮 天机榜　📊 观看榜
　　→　这五个是「二级选择界面」，点进去再选一次即可
> 唯一需要自己打字的是 `/name 道号` 和 `/qp 片名`
> （这两样本身就有内容要输入）。

**基础**
`/start` 踏入仙途 / 打开主菜单
`/choose` 选择门派（一生一次，可 `/choose 1`）
`/name` 取一个道号
`/me` 查看自己的道途
`/help` 本帮助

**修行**
`/sign` 每日打坐，领 {S.CURRENCY}
`/cultivate` 闭关修炼，涨修为
`/breakthrough` 冲击更高境界
`/tasks` 每日任务
`/achievements` 我的成就

**坊市 / 兑换阁**
`/shop` 坊市：花 🪙{S.CURRENCY} 买丹药 / 符箓
`/exchange` 兑换阁：**注册码 · 续期码**（只花 💎V积分）
> 不设购买门槛：任何段位都能买，段位只决定折扣（仙人最高 8.2 折）。
> 兑换成功后，码会**私信**发给你。
`/duihuan` 💱 把 🪙灵石 按 **{S.LS_PER_V}:1** 换成 💎V积分
`/duanwei` 📖 段位折扣表 · 货币关系 说明
`/mycodes` 查看我领到过的码

**货币速记**
　🪙 **灵石**：修仙玩法币。打坐 / 闭关 / 任务 / 红包 / **群里发言** 都能拿，
　　用于坊市买丹药符箓、发红包、求片。 **不能** 直接换码。
　💎 **V积分**（就是 RT bot 里的那个 **V** 积分）：与 RT bot **1:1 互通**，
　　同一数值在任意一边消耗都会同步扣减。**只有它能换注册码 / 续期码**。
　💱 汇率：**{S.LS_PER_V} 灵石 = 1 V积分**（用 `/duihuan`，零头不丢）

**社交**
`/rank` 天机榜（可看境界榜 / 财富榜）
`/myrank` 我的排名
`/visit` 拜访道友
`/rob` 夺取他人 {S.CURRENCY}（有风险）
`/duel` 斗法论道

**奖励**
`/claim` 领取周榜 / 月榜奖励
`/hb 总额 份数` 在群里发拼手气红包

**观看榜（群友都能查，不用管权限）**
`/dayrank` 今日播放榜
`/weekrank` 本周播放榜
`/monthrank` 本月播放榜
`/top` 榜单总入口（🎬影片/👥用户 × 日周月，**全是可点按钮**）
`/myview` 我自己的观看时长与群内排名
`/now` 📡 在线播报：谁在看什么，**带进度条**
> 榜单消息里每个按钮都能点，点一下自动换榜，不用重新发命令。

**求片与下载（群友都能用）**
`/qp 片名` 搜索 → 出来的候选**每条都是按钮，点哪条就订哪条**
`/mp 片名 [年份]` 直接订阅最匹配的一条
`/myreqs` 我的求片申请与审批状态
`/myreq` 📋 **我**求的片 + 每一部现在到哪一步了
`/dl` 📡 实时下载进度（进度条 / 速度 / 剩余时间）
`/ptreport` 📊 PT 日报：今日求片数 + 入库成功数
`/今日求片` 看群里今天所有人在求什么
`/ptstatus` 🔗 整条自动化链路的状态
> 群里只播报「群友求片」的进度；自己在 MoviePilot 订阅的下载**不刷群**，
> 完成后只私聊通知。

**社交与赠送**
`/give @某人 100` 🎁 把**自己的** 🪙灵石送给别人
> 也可以**回复对方的消息**发 `/give 100`，更省事。"""

# ★ 管理段：按权限动态生成（用户 2026-10-10 拍板「群主专属的命令，
#   管理员也看不到，只有群主能看到」）。
#   以前这里是一段写死的静态文字，混在 HELP_TEXT 里 ⇒ 任何群友 /help
#   都能看到 /admin /mpsearch /qb 这些群主专属命令的名字。
_ADMIN_DOC = {
    "bindctl":  ["🔄 `/syncbind` 自动关联 Emby↔TG",
                 "📢 `/callbind` 群里喊大家认领名字",
                 "📋 `/unbound` 未关联 TG 的名单"],
    "reqaudit": ["🛎 `/reqs` 待审核求片（含通过 / 驳回）",
                "🗂 `/reqmgr` 求片管理台：按状态改，**驳回后可反悔同意**",
                "📅 `/reqmgr today` 今日总求片：逐条随时改状态"],
    "ptctl":    ["🛠 `/ptasks` 订阅 / 下载任务面板",
                 "❌ `/ptdel` 批量取消任务",
                 "🧹 `/ptclean` 清理无种源下载卡片",
                 "🌐 `/syncsite` 同步 MP 站点目录"],
}
_OWNER_DOC = {
    "admin": ["👑 `/admin` 管理员与权限面板：增删管理员、**逐项勾选**授权",
              "🩺 `/gaps 30` **查漏补缺**：逐季逐集比对 TMDB，找出缺的季/集，"
              "私聊你勾选后一键补下"],
    "mp":    ["🔎 `/mpsearch 片名` 搜索并直接下载",
              "📺 `/mpsubs` 订阅管理",
              "🌐 `/mpsites` 站点管理",
              "🧩 `/mpplugin` 插件 / 下载器状态"],
    "qb":    ["🧲 `/qb` 种子增删 / 限速 / 暂停恢复"],
    "emby":  ["📺 `/emby` 媒体库刷新 / 在线会话"],
    "mt":    ["🗂 `/mt` 整理任务 / 方案 / strm"],
    "cd2":   ["☁️ `/cd2` 115 云盘挂载与容量"],
}


def help_text(uid=None) -> str:
    """按这个人实际拥有的权限拼帮助文案。

    ·群友   → 只有 _HELP_BASE，完全看不到任何管理命令
    · 管理员 → 额外看到**自己被勾选**的模块（看不到没授权的，更看不到群主的）
    · 群主   → 全部
    """
    base = _HELP_BASE
    try:
        from .modules import perm          # 延迟导入：perm 也依赖 ui
        lv = perm.level_of(uid)
        if lv == perm.PUBLIC:
            return base
        rows: list[str] = []
        mine = perm.grants_of(uid)
        for key, items in _ADMIN_DOC.items():
            if lv == perm.OWNER_ONLY or key in mine:
                rows += items
        if lv == perm.OWNER_ONLY:
            for items in _OWNER_DOC.values():
                rows += items
        if not rows:
            return base
        title = ("👑 **管理命令（仅群主）**" if lv == perm.OWNER_ONLY
                 else "🛠 **管理命令（你已获授权）**")
        return f"{base}\n\n{divider()}\n{title}\n" + "\n".join(rows) + \
               f"\n> 命令菜单是**按人下发**的：没权限的命令你根本看不到。"
    except Exception:
        return base


# 兼容旧调用点（无参时给「公开视角」）
HELP_TEXT = _HELP_BASE
