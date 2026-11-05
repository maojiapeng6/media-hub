"""全局配置与世界观常量 —— KFC 修仙 bot"""
from __future__ import annotations

import json
import os

_CFG_PATH = os.environ.get("KFCGAME_CONFIG", "/app/config.json")

with open(_CFG_PATH, encoding="utf-8") as f:
    CFG: dict = json.load(f)

# ---------------- 基础 ----------------
BOT_TOKEN: str = CFG["bot_token"]
BOT_NAME: str = CFG.get("bot_name", "KFCGAME_bot")
API_ID: int = CFG["api_id"]
API_HASH: str = CFG["api_hash"]
OWNER: int = CFG["owner"]
ADMINS: list[int] = CFG.get("admins", [])
GROUP: list[int] = [int(i) for i in CFG.get("group", [])]
GROUP_USERNAME: str = CFG.get("group_username", "")
CHANEL: str = CFG.get("chanel", "")
SERVER_NAME: str = CFG.get("server_name", "KFC 影音")

# Pyrogram 的 get_peer_type 只认 32 位频道 ID，新版超群的 ID 会超出范围被判定非法。
# 因此首次必须先按 username 解析一次把 peer 写进本地缓存，之后用数字 ID 才正常。
GROUP_USERNAMES: list[str] = [u for u in [GROUP_USERNAME] if u]
CHANEL_USERNAME: str = CHANEL

PROXY: dict = {k: v for k, v in CFG.get("proxy", {}).items() if v not in ("", None)}

DB = CFG["db"]
GAME_DB: str = DB["game_db"]
SHARED_DB: str = DB["shared_db"]

EMBY = CFG.get("emby", {})
MP = CFG.get("moviepilot", {})
QB = CFG.get("qb", {})
MT = CFG.get("mt", {})
CD2 = CFG.get("cd2", {})
G = CFG.get("game", {})
RANK_PUSH = CFG.get("rank_push", {})

CURRENCY: str = G.get("currency", "灵石")
PREFIX = ["/", "!"]

# ---------------- 境界体系 ----------------
# (等级序号, 名称, 突破所需修为, 每日打坐基础收益, 可解锁描述)
REALMS = [
    (1,  "炼气期", 0,      5,   "初入仙途，尚是凡骨"),
    (2,  "筑基期", 120,    9,   "筑得道基，可御轻风"),
    (3,  "金丹期", 360,    14,  "金丹已成，寿元大增"),
    (4,  "元婴期", 900,    21,  "元婴出窍，神识外放"),
    (5,  "化神期", 2100,   30,  "化神之境，言出法随"),
    (6,  "炼虚期", 4800,   42,  "炼虚成真，以身合道"),
    (7,  "合体期", 10500,  57,  "法体合一，气机连绵"),
    (8,  "大乘期", 22000,  75,  "大乘将成，只差一步"),
    (9,  "渡劫期", 45000,  96,  "天劫将至，退无可退"),
    (10, "仙人",   90000,  120, "飞升成仙，与天同寿"),
]
REALM_NAMES = [r[1] for r in REALMS]
MAX_REALM = len(REALMS)


def realm_by_level(lv: int) -> tuple:
    """按等级取境界信息，越界则取最高"""
    lv = max(1, min(int(lv or 1), MAX_REALM))
    return REALMS[lv - 1]


# ---------------- 门派 ----------------
# key: (名称, emoji, 说明, 加成类型, 加值)
SECTS = {
    "shushan": ("蜀山剑宗", "🗡", "剑修一脉，斗法伤害 +25%", "duel", 0.25),
    "kunlun":  ("昆仑丹宗", "⚗", "丹修一脉，打坐收益 +25%", "sign", 0.25),
    "xuantian":("玄天器宗", "🛡", "器修一脉，被夺灵石概率 -30%", "defend", 0.30),
    "xuehai":  ("血海魔宗", "🩸", "魔道一脉，夺取成功率 +20%", "rob", 0.20),
}
SECT_KEYS = list(SECTS.keys())

# ---------------- 坊市 ----------------
# key: (名称, emoji, 价格, 效果说明, 类型)
SHOP_ITEMS = {
    "dan_xiaohuan": ("小还丹", "💊", 30,  "立得 40~80 修为", "cultivation"),
    "dan_dahuan":   ("大还丹", "🧪", 120, "立得 200~400 修为", "cultivation"),
    "fu_ling":      ("聚灵符", "🔮", 60,  "下次打坐收益翻倍（可叠加1次）", "buff_sign"),
    "dan_hushen":   ("护身丹", "🧿", 80,  "12 小时内免疫一次夺取", "shield"),
    "yu_jian":      ("御剑符", "🌪", 100, "斗法必定先手（下次斗法必胜）", "buff_duel"),
    "ling_yu":      ("灵玉",   "💎", 200, "永久：打坐收益 +10%", "perm_sign"),
}

# ---------------- 兑换阁（用 V积分换真实的 Emby 权益） ----------------
# key: (名称, emoji, 原价, 说明, 码种类, 最低境界等级, 是否暂停兑换)
#   最低境界 = 兑换门槛；达不到境界的人连看都不会显示在可兑换列表里（但会标注需求）
#   暂停兑换 = 暂未开放（如白名单），面板只展示「敬请期待」，点击会被拒绝
REDEEM_ITEMS = {
    "invite":    ("注册码",    "🎟", 250, "兑换一枚 Emby 注册码（新道友入场券）", "invite",    1, False),
    "renew30":   ("30 天续期", "🔑", 50,  "给自己的账号延长 30 天",        "renew",     1, False),
    "renew90":   ("90 天续期", "🗝", 120, "给自己的账号延长 90 天",        "renew",     1, False),
    "renew365":  ("365 天续期","💠", 400, "给自己的账号延长 365 天",       "renew",     1, False),
    "whitelist": ("白名单",    "🛡", 300, "加入白名单，免验证直连影库",    "whitelist", 1, True),
}

# ---------------- 货币关系（灵石 与 V积分 彻底解耦） ----------------
# 🪙 灵石：游戏内货币，存在 kfc_cultivator.ls。
#    打坐 /sign、闭关 /cultivate、每日任务 /tasks、发抢红包 /hb、群里发言 都能拿到。
#    用途：坊市买丹药符箓、发红包、求片。 **不能** 直接换码。
# 💎 V积分：embyboss.emby.us，就是 RT bot 里的那个「V」积分，1:1 互通，
#    任意一边消耗都会同步扣减。用途：**只有 V积分 能兑换注册码 / 续期码**。
# 汇率：灵石 按 LS_PER_V 的比例兑换成 V积分（只有这一个方向，不可逆）。
LS_PER_V  = 50   # 50 灵石 = 1 V积分
SPEAK_LS  = 2    # 群里每条发言给的灵石
SPEAK_CAP = 20   # 每天最多按前 20 条发言发放灵石（防刷屏刷币）

# 境界折扣：段位越高，同一件东西花的 V积分越少。
# 已收敛：仙人最高也只到 8.2 折，避免「太容易兑换」。
REALM_DISCOUNT = {1: 1.00, 2: 0.98, 3: 0.96, 4: 0.94, 5: 0.92,
                  6: 0.90, 7: 0.88, 8: 0.86, 9: 0.84, 10: 0.82}


# ═════════════════ V积分获取渠道（★ 唯一权威口径，所有规则文案都从这里出） ═════════════
#   数值来源：watch_iv.py（观影结算）、embyboss config（签到 [1,3]）、
#   本文件 LS_PER_V / REDEEM_ITEMS / REALM_DISCOUNT。改数值改这里+对应模块。
WATCH_MIN = 10          # 当日观影满 10 分钟才有结算资格
WATCH_PER = 30          # 每 30 分钟观影 = 1 V积分
WATCH_CAP = 0           # ★ 2026-11-05 用户拍板：每日上限**不限**（0=无上限，一直看一直赚）
CHECKIN_IV = (1, 3)     # Boss「🎯 签到」每日随机 1~3 V积分（与打坐互斥，一天一次）


def vpoints_rules_md() -> list[str]:
    """「如何获取 V积分 + 能花在哪」标准文案（Markdown 粗体），多处复用。"""
    return [
        "**💎 怎么获得 V积分**（各渠道汇总）：",
        f"　① **签到**｜Boss 机器人点「🎯 签到」，每天 **{CHECKIN_IV[0]}~{CHECKIN_IV[1]}** 分"
        "（Game 打坐与 Boss 签到互斥，一天一次）",
        f"　② **观影自动结算**｜每天 23:30 由 Game 机器人统一自动核算（全群唯一结算源）："
        f"当日观影满 {WATCH_MIN} 分钟起算，**每 {WATCH_PER} 分钟 = 1 分，上不封顶**"
        f"（≈追一集 45 分钟剧 = 1 分，一直看一直赚）",
        f"　③ **灵石兑换**｜{LS_PER_V} 灵石 = 1 分，/duihuan 随时换"
        "（灵石靠打坐/闭关/任务/群发言/红包攒）",
        "　④ **群主派送**｜群主 /pay 直接转（活动奖励等）",
        "",
        "**🎟 V积分能换什么**（原价，境界越高折扣越大）：",
    ] + [
        f"　· {it[1]} {it[0]}｜**{it[2]}** 分"
        for it in (REDEEM_ITEMS[k] for k in ("invite", "renew30", "renew90", "renew365"))
    ] + [
        "　· 🛡 白名单｜⏳ 暂未开放",
        "",
        f"**📈 段位折扣**：{REALM_DISCOUNT[10] * 10:.1f} 折封顶（仙人），"
        "升级路线见 /duanwei。",
        "**⏱ 攒分参考**：认真追剧一天几小时就有 10+ 分，"
        "一个月轻松 300+ 分 ≈ 一枚注册码（250）。",
    ]


def realm_level(realm) -> int:
    return max(1, min(int(realm or 1), MAX_REALM))


def price_for(realm, base: int) -> int:
    """按境界打折后的实付价"""
    return max(1, int(round(int(base) * REALM_DISCOUNT.get(realm_level(realm), 1.0))))


def discount_text(realm) -> str:
    d = REALM_DISCOUNT.get(realm_level(realm), 1.0)
    return "无折扣" if d >= 1.0 else f"{d * 10:.1f} 折"


# ---------------- 段位 / 货币 说明（兑换面板「📖 说明」按钮 + /duanwei） ----------------
def duanwei_help() -> str:
    """段位折扣表 + 货币关系（灵石 ↔ V积分 汇率）的说明文案。

    按用户要求：**不设任何购买门槛**，任何段位都能买；段位只决定折扣力度。
    """
    lines = [
        "📖 **段位 · 货币 说明**",
        "━━━━━━━━━━━━━━━━━━",
        "",
        "**① 段位折扣（境界越高，兑换越便宜）**",
        "_不设购买门槛：任何段位都能购买，段位只决定优惠力度_",
    ]
    for lv in range(1, MAX_REALM + 1):
        _, nm, _need, _gain, _flavor = realm_by_level(lv)
        d = REALM_DISCOUNT[lv]
        mark = " 👑" if lv == MAX_REALM else ""
        lines.append(f"　{lv:>2} 重 {nm}　→　{'无折扣' if d >= 1 else f'{d * 10:.1f} 折'}{mark}")
    lines += [
        "",
        "**② 两种货币（用途不同，别搞混）**",
        "　🪙 **灵石**：修仙玩法货币。",
        "　　获得：打坐 `/sign`、闭关 `/cultivate`、每日任务 `/tasks`、",
        "　　　　　发/抢红包 `/hb`、以及 **在群里发言**。",
        "　　用途：坊市 `/shop` 买丹药符箓、发红包、求片 `/qp`。",
        "　💎 **V积分**（就是 RT bot 里的那个 **V** 积分）：",
        "　　与 RT·V积分 **1:1 互通**，任意一边消耗都会同步扣减。",
        "　　用途：**只有 V积分 能兑换「注册码 / 续期码」**。",
        "",
        *vpoints_rules_md(),   # ★ V积分获取渠道+用途（唯一权威口径）
        "",
        f"**③ 汇率：{LS_PER_V} 灵石 = 1 V积分**",
        "　　灵石 **不能** 直接换码。先 `/duihuan` 把灵石按比例换成 V积分，",
        "　　再去 `/exchange` 兑换阁换注册码 / 续期码。",
        "　　_零头（不足 1 V积分 的部分）留在灵石里，不会丢失_",
        "",
        "**④ 等价参考（原价，未计段位折扣）**",
    ]
    for key in ("invite", "renew30", "renew90", "renew365"):
        it = REDEEM_ITEMS[key]
        lines.append(f"　{it[1]} {it[0]}｜{it[2]} V积分 ≈ {it[2] * LS_PER_V:,} 灵石")
    lines += [
        "　🛡 白名单｜⏳ 暂未开放兑换",
        "",
        "💡 想更便宜？继续 `/cultivate` 提升境界，折扣自动变大；",
        "💡 想更快攒够？多发言、多做任务拿灵石，再 `/duihuan` 换成 V积分。",
    ]
    return "\n".join(lines)


# ---------------- 每日任务 ----------------
DAILY_TASKS = {
    "sign":    ("完成一次打坐", 3),
    "train":   ("闭关修炼 2 次", 6),
    "rob":     ("夺取他人灵石 1 次", 8),
    "duel":    ("斗法 1 次", 8),
    "visit":   ("拜访 2 位道友", 4),
    "shop":    ("在坊市消费一次", 5),
}

# ---------------- 成就 ----------------
ACHIEVEMENTS = {
    "first_step": ("初入仙途", "完成角色创建"),
    "realm_3":    ("金丹有成", "境界达到金丹期"),
    "realm_5":    ("化神之姿", "境界达到化神期"),
    "realm_10":   ("飞升成仙", "境界达到仙人"),
    "rich_1k":    ("富甲一方", "累计持有灵石达到 1000"),
    "rob_master": ("盗亦有道", "成功夺取 10 次"),
    "duel_master":("斗法宗师", "斗法获胜 10 次"),
    "sign_7":     ("持之以恒", "连续打坐 7 天"),
    "sign_30":    ("道心坚定", "连续打坐 30 天"),
}
