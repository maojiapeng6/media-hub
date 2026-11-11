# -*- coding: utf-8 -*-
"""PT 保号（发电）—— 站点配置 + 官方规则要点（单一数据源）

Web（media-hub:5678「PT保号」页）与 Bot（kfcgame）两端共用同一份配置；
运行时状态写 state.json，由 Bot 生成、Web 读取，避免两处漂移。

字段说明：
  id            站点短 id（state.json / 回调数据用）
  name/sym      展示名 / emoji
  home          站点首页（登录保活访问它）
  rules_url     官方规则页
  kind          站点类型（NexusPHP / M-Team 官方）
  need_own_115  True ⇒ 展示名带（括号）=「需自备 115 会员」
  req           保号硬性要求（全部来自官方规则页）
    login_days        未封存账号连续 N 天不登录 ⇒ 删/封号
    no_traffic_days   无流量（上传/下载都为 0）连续 N 天不登录 ⇒ 删号
    login_interval    保号系统每 N 天自动访问一次（≤删号期限，留足余量）
    login_mode        http=requests 访问即可；web=必须真实浏览器访问
    min_ratio         分享率安全线（低于会触发警告/封禁）
    hr_desc           H&R/做种要求原文
    newbie            新手考核要求（不足 30 天的新号）
  rules         官方规则要点（原文字数精简，数字原样保留）
  bound         账号绑定信息（cookie/passkey）是否已抓到
"""
from __future__ import annotations

SITES: list[dict] = [
    {
        "id": "zmpt",
        "name": "ZMTP·织梦",
        "sym": "🏯",
        "home": "https://zmpt.cc",
        "rules_url": "https://zmpt.cc/rules.php",
        "kind": "NexusPHP",
        "need_own_115": True,
        "req": {
            "login_days": 45,
            "sealed_login_days": 180,
            "no_traffic_days": 7,
            "login_interval": 20,
            "login_mode": "http",
            "min_ratio": 1.0,
            "hr_desc": "官种默认 H&R 考察：336 小时(14天)内做种满 72 小时 或 分享率>1；累计 10 条未达标封号；10000 魔力可消 1 条",
            "newbie": "新号无流量（上传/下载都 0）连续 7 天不登录即封号，注册后请尽快产生流量",
            # ★ 官方规则页 2026-10-17 实抓：邀请名额由站点按等级发放，侧栏「邀请[发送]:已用(可用)」
            "invite_level": "站点发放名额",
            "invite_rule": "发送邀请前必须查验三站低隐私；邀请人有义务介绍本站规则；"
                           "禁止公开场合邀请；禁止买卖邀请。名额由站点按等级/贡献发放",
            "invite_note": "当前侧栏名额 0(2) —— 有 2 个可用位，但 invite.php 返回无权限，"
                           "需等级/隐私达标后才能实际发送",
        },
        "rules": [
            "未封存账号连续 45 天不登录将被封禁帐号；封存账号连续 180 天不登录将被封禁",
            "没有流量的用户（上传/下载都为 0）连续 7 天不登录，账号将被封禁",
            "道馆训练家(IU)及以上封存账号后不会删除；传说训练家(NM)及以上永远保留",
            "站点已取消 HR，但官种默认 H&R 考察：336 小时内做种满 72 小时 或 分享率大于 1",
            "单种限速 100MB/s，第一次超速警告一个月，多次超速封禁账号",
            "种子发布者做种不足 24 小时将被警告甚至取消上传权限",
            "总体积大于 25GB 的种子自动成为免费（持续 2 天）",
        ],
    },
    {
        "id": "nicept",
        "name": "NicePT",
        "sym": "🎵",
        "home": "https://nicept.net",
        "rules_url": "https://www.nicept.net/rules.php",
        "kind": "NexusPHP",
        "need_own_115": True,
        "req": {
            "login_days": 30,
            "sealed_login_days": 200,
            "no_traffic_days": 7,
            "login_interval": 14,
            "login_mode": "http",
            "min_ratio": 1.0,
            "hr_desc": "下载后需做种（站点以辅本地种 + 免费热种刷上传为主）",
            "newbie": "新手考核2025 四项（任一不达标即不能邀请/有封号风险）："
                      "上传增量>200GB、下载增量>200GB、平均做种时间>200小时、魔力增量>30000",
            # ★ 官方规则页 + 侧栏「新手考核2025」2026-10-17 实抓。
            #   之前配的「300 时魔」是错的 —— 官方真实门槛是下面四项。
            "invite_level": "通过新手考核2025 后站点发放名额",
            "invite_rule": "被邀请者需有一定 PT 经验；邀请者需检验被邀请者其他站点数据"
                           "（习惯性吸血的不邀）；邀请者须指导被邀请者熟读本站规则；"
                           "被邀请者不能完成考核，邀请者受连带处罚；禁止买卖邀请、禁止公开发放",
            "invite_note": "侧栏名额 0(0) —— 当前无可用邀请位，必须先过新手考核四项",
            "newbie_items": {
                "upload_gb": 200, "download_gb": 200, "seed_hours": 200, "bonus": 30000
            },
        },
        "rules": [
            "未封存账号连续 30 天不登录将被封禁帐号；封存账号连续 200 天不登录删除",
            "没有流量的用户（上传/下载都为 0）连续 7 天不登录将被封禁",
            "新手考核2025 四项（任一不达标即不通过）：上传增量>200GB、下载增量>200GB、"
            "平均做种时间>200小时、魔力增量>30000；考核期 2026-10-12 ~ 2026-11-11",
            "邀请规则：被邀请者需有 PT 经验；邀请者检验被邀请者其他站数据（吸血的不邀）；"
            "被邀请者未完成考核则邀请者连带处罚；禁止买卖邀请；禁止公开发放邀请（发现即封号）",
            "严重违规封禁不适用分享率激励规则，也不接受捐赠解封",
        ],
    },
    {
        "id": "ptskit",
        "name": "PTSKIT·拾刻",
        "sym": "🪵",
        "home": "https://www.ptskit.org",
        "rules_url": "https://www.ptskit.org/rules.php",
        "kind": "NexusPHP",
        "need_own_115": True,
        "req": {
            "login_days": 30,
            "sealed_login_days": 400,
            "no_traffic_days": 7,
            "login_interval": 10,
            "login_mode": "http",
            "min_ratio": 1.0,
            "hr_desc": "下载后做种需保持一定时长；累计分享率建议大于 1",
            "newbie": "新号无流量（上传/下载都为 0）连续 7 天不登录即删除，请尽快产生流量",
            # ★ 官方规则页 2026-10-17 实抓（规则页无独立邀请章节 → 按 NexusPHP 等级发放）
            "invite_level": "站点发放名额",
            "invite_rule": "拾刻规则页未列独立邀请条款，邀请名额由站点按等级/贡献发放"
                           "（侧栏「邀请[发送]:已用(可用)」）；本站侧栏未显示名额 → 当前 0 可用",
            "invite_note": "魔力 20804 已是全站最高，但邀请受「等级 + 站点发放」限制，非魔力兑换",
        },
        "rules": [
            "未封存账号连续 30 天不登录将被删除帐号（全站最严，务必按期访问）",
            "封存账号连续 400 天不登录删除；无流量用户（上传/下载都 0）连续 7 天不登录删除",
            "Extreme User 及以上永远保留；Elite User 及以上封存后不会删除",
            "总体积大于 20GB 的种子自动免费；所有种子发布 1 个月后自动永久 2x 上传",
            "连续 5 次登录失败将导致 IP 被禁用",
        ],
    },
    {
        "id": "cyanbug",
        "name": "cyanbug·大青虫",
        "sym": "🐛",
        "home": "https://cyanbug.net",
        "rules_url": "https://cyanbug.net/rules.php",
        "kind": "NexusPHP",
        "need_own_115": True,
        "req": {
            "login_days": 150,
            "sealed_login_days": 400,
            "no_traffic_days": 15,
            "login_interval": 30,
            "login_mode": "http",
            "min_ratio": 1.0,
            "hr_desc": "普通下载需保持做种；上传者做种不足 24 小时将被警告",
            "newbie": "无流量（增量 0 且从未完成种子）连续 15 天不登录、或注册满 30 天将被删除",
            # ★ 官方规则页 2026-10-17 实抓：侧栏「邀请发送:1(2)」= 已用1/共2
            "invite_level": "站点发放名额",
            "invite_rule": "邀请名额由站点按等级发放（侧栏「邀请发送:已用(总)」）。"
                           "当前 1(2) —— 已发送 1 个、共 2 个名额",
            "invite_note": "大青虫是唯一已用掉邀请名额的站（已发 1）；等级 Elite User 起封存免删",
        },
        "rules": [
            "未封存账号连续 150 天不登录将被删除帐号；封存账号连续 400 天不登录删除",
            "没有流量的用户（上传/下载增量都为 0 且从未完成过任何种子）连续 15 天不登录，或者注册时间满 30 天将被删除",
            "Veteran User 及以上永远保留；Elite User 及以上封存后不会删除",
            "大于 20GB 的种子自动免费；发布 1 个月后永久 2x 上传",
        ],
    },
    {
        "id": "hdarea",
        "name": "HDArea",
        "sym": "🎬",
        "home": "https://hdarea.club",
        "rules_url": "https://hdarea.club/rules.php",
        "kind": "NexusPHP",
        "need_own_115": True,
        "req": {
            "login_days": 30,
            "sealed_login_days": 200,
            "no_traffic_days": 7,
            "login_interval": 14,
            "login_mode": "http",
            "min_ratio": 1.0,
            "hr_desc": "新手考核需从 4 种方式选 1；辅免费种子 + 辅本地种",
            "newbie": "注册后需从 4 个新手考核方式中选 1（时魔 B=1000 / L=700）",
            # ★ 官方规则页 2026-10-17 实抓：明确「达到 Crazy User 及以上，就可以发送邀请啦！」
            "invite_level": "Crazy User",
            "invite_level_cn": "Crazy User 及以上",
            "invite_rule": "官方：达到 Crazy User 及以上等级即可发送邀请。"
                           "升级需累积做种时间/上传/分享率（时魔考核）",
            "invite_note": "侧栏名额 0 —— 当前等级未达 Crazy User，故无邀请位；"
                           "刷魔力/做种升等级是解锁邀请的正路",
        },
        "rules": [
            "官方：达到 Crazy User 及以上等级，就可以发送邀请啦！",
            "新手从 4 个考核方式中选 1，辅十几个免费种子 + 辅本地种可轻松过（时魔 B=1000 / L=700）",
            "邀请名额随等级解锁 —— 当前 Crazy User 以下 → 侧栏 0 邀请位",
            "未封存账号连续 30 天不登录将被封禁（默认 NexusPHP 标准，待官方页二次确认）",
        ],
    },
    {
        "id": "mteam",
        "name": "M-Team·馒头",
        "sym": "🍙",
        "home": "https://kp.m-team.cc",
        "rules_url": "https://wiki.m-team.cc/zh-tw/account-rules",
        "kind": "M-Team",
        "need_own_115": True,
        "req": {
            "login_days": 40,
            "sealed_login_days": 90,
            "no_traffic_days": 5,
            "login_interval": 7,
            "login_mode": "web",
            "min_ratio": 0.6,
            "hr_desc": "M-Team 无 H&R 制度；靠做种获取魔力值，考核期需做种魔力>6000",
            "newbie": "新手考核：上传>30G、下载>15G、做种获得魔力值>6000（任一项不达标即禁号）",
            # ★ 官方 wiki 等级权限表（2026-10-17 实抓 wiki.m-team.cc/account-rules）
            "invite_level": "Elite User",
            "invite_level_cn": "知縣／Elite User",
            "invite_rule": "等级达 知縣/Elite User 才能发送邀请（升级需 8周 + 上传>400GB + 分享率>3）",
            "level_up": "Power User(捕頭)=4周/200GB/率>2；Elite User(知縣)=8周/400GB/率>3",
        },
        "rules": [
            "无流量用户（上传/下载都为 0）注册 5 天后将被删除账号",
            "未封存账号连续 40 天不登入将【立即删除】帐号（无宽限期）；封存账号连续 90 天不登入删除",
            "登入指用浏览器存取网站网页，单纯做种不算登入，第三方工具间接存取也不算登入",
            "府丞/Veteran User 及以上封存后永久保号；府尹/Extreme User 及以上永远保留",
            "注册未满 30 天：下载超过 10GB 且分享率低于 0.3 将立即被禁用（无宽限期）",
            "分享率警戒：>10GB 需>0.6；>50GB 需>0.7；>100GB 需>0.8；>200GB 需>0.9；>500GB 需>1.0",
            "单种上传上限最高 1Gbps，请勿超出",
            "新手考核三项（任一不达标即禁用）：上传>30G、下载>15G、做种获得魔力>6000",
            "等级权限（官方 wiki）：小卒/User 可发候选、兑换魔力；捕頭/Power User 同上；"
            "知縣/Elite User 起【可以发送邀请】",
        ],
    },
    {
        "id": "carpt",
        "name": "CarPT",
        "sym": "🚗",
        "home": "https://carpt.net",
        "rules_url": "https://carpt.net/rules.php",
        "kind": "NexusPHP",
        "need_own_115": True,
        "req": {
            "login_days": 90,
            "sealed_login_days": 150,
            "no_traffic_days": 14,
            "login_interval": 7,
            "login_mode": "http",
            "min_ratio": 1.0,
            "hr_desc": "H&R：考察中/未达标/20；做种时间未达或上传<10倍体积即考察中；"
                       "免除 1 条 HR 需消耗魔力；VIP/黄星期间下载免 HR",
            "newbie": "无流量（上传/下载都为 0）连续 14 天不登录即封号 —— 本站当前 0 流量，"
                      "最高优先级：尽快辅种产生上传/下载增量",
            # ★ 官方规则页 2026-10-17 实抓
            "invite_level": "Power User",
            "invite_level_cn": "Power User（站点按等级发放名额）",
            "invite_rule": "邀请名额由站点按等级发放（侧栏「邀请 [发送]: 已用(可用)」）；"
                           "禁买卖邀请、禁公开发放、每人仅一个账号",
            "level_up": "等级由站点按做种/上传/分享率综合评定；Elite User 及以上封存免封号，"
                        "Extreme User 及以上永久保留",
            # ★ CarPT 用 downhash 签名下载，qB 匿名拉 passkey URL 会被拒 →
            #   必须 bot 用 cookie 下载 .torrent 再把文件体推给 qB。
            "seed_via_cookie": True,
        },
        "rules": [
            "Extreme User 及以上等级用户不受定期访问限制，帐号永远保留",
            "Elite User 及以上等级用户封存账号后不会被封禁",
            "封存账号的用户连续 150 天不登录将被封禁帐号",
            "未封存账号的用户连续 90 天不登录将被封禁帐号",
            "没有流量的用户（上传/下载都为 0）连续 14 天不登录将被封禁帐号",
            "被封禁的账号如果连续 200 天不登录，将被从数据库彻底物理删除",
            "单种上传速度限制 100MB/s（超 100MB/s 系统自动封号，上传量减半解封）；"
            "非发布者上传超 25MB/s 按 3 倍扣罚汇报上传量",
            "总体积大于 20GB 的种子自动免费；蓝光原盘/每季第一集自动免费；"
            "所有种子发布 1 个月后自动永久 2x 上传",
            "认领达标标准：每月做种时间 ≥300 小时(12.5天) 或 上传量 ≥ 体积 5 倍；"
            "达标种子魔力奖励为正常的 2 倍",
            "不合格字幕/cue 直接删除并扣 100 魔力；举报奖励 50 魔力",
            "邀请规则：只发给明确有进站意向且现实中认识的人；禁止交易/交换/售卖邀请；"
            "每位用户仅允许一个账号",
        ],
    },
    {
        "id": "hdfans",
        "name": "HDFans",
        "sym": "💿",
        "home": "https://hdfans.org",
        "rules_url": "https://hdfans.org/rules.php",
        "kind": "NexusPHP",
        "need_own_115": True,
        "req": {
            "login_days": 90,
            "sealed_login_days": 180,
            "no_traffic_days": 15,
            "login_interval": 20,
            "login_mode": "http",
            "min_ratio": 1.0,
            "hr_desc": "无 H&R 制度；上传者做种不足 24 小时将被警告；"
                       "单种上传速度上限 200Mbps≈25MB/s（超限警告）；认领制："
                       "每月做种≥300h 或 上传≥体积 5 倍达标（达标魔力 2 倍）",
            "newbie": "无流量（上传/下载都为 0）连续 15 天不登录即删除，请尽快产生流量",
            "invite_level": "Veteran User",
            "invite_level_cn": "Veteran User 及以上封存免删",
            "invite_rule": "邀请名额由站点按等级发放；Veteran User 及以上封存账号不会被删",
            "invite_note": "Veteran User 及以上等级封存账号安全；其余按 90/180/15 天规则",
        },
        "rules": [
            "未封存账号连续 90 天不登录将被删除帐号；封存账号连续 180 天不登录删除",
            "没有流量的用户（上传/下载都为 0）连续 15 天不登录将被删除帐号",
            "Veteran User 及以上等级用户封存账号后不会被删除（安全）",
            "电视剧每季第一集自动免费；总体积大于 20GB 的种子自动免费；"
            "蓝光原盘/HDDVD 免费；限时促销 3 天（2x 上传无时限）",
            "单种上传速度最高 200Mbps（约 25MB/s），超限需自行限速",
            "上传者必须保证上传速度与做种时间，做种不足 24 小时将被警告",
            "违规种子不经提醒直接删除；不合格字幕/cue 扣魔力",
            "认领达标：每月做种≥300 小时 或 上传量≥体积 5 倍（达标魔力 2 倍）",
        ],
    },
    {
        "id": "cspt",
        "name": "CSPT",
        "sym": "🆕",
        "home": "https://cspt.top",
        "rules_url": "https://cspt.top/rules.php",
        "kind": "NexusPHP",
        "need_own_115": True,
        "req": {
            "login_days": 30,
            "sealed_login_days": 365,
            "no_traffic_days": 7,
            "login_interval": 15,
            "login_mode": "http",
            "min_ratio": 1.0,
            "hr_desc": "规则待核实：CSPT 为 2026-11 新接入站，请于站点规则页确认 H&R / 做种要求",
            "newbie": "新站接入，规则待核实；建议尽快产生上传/下载流量以避免无流量删号",
            "invite_level": "待核实",
            "invite_rule": "待核实：邀请名额由站点按等级发放",
            "invite_note": "CSPT 2026-11 新接入，规则以站点官方页面为准",
        },
        "rules": [
            "规则待核实：CSPT 为新接入 PT 站，请于 https://cspt.top/rules.php 核对登录/留存/邀请规则",
            "建议保持定期登录与做种，避免无流量账号被清理",
        ],
    },
]

SITES_BY_ID = {s["id"]: s for s in SITES}

# ── 价格与服务文案（用户指定口径，原文照录） ─────────────────────
PRICE_LINES = [
    "· 括号标注需自备 115 会员的注册码及续期码 —— 售价 ¥5",
    "· 无需自备 115 会员的注册码及续期码 —— 售价 ¥10/月",
]
VPOINT_HINT = "🎮 群游戏可赚 V 积分，到 /exchange 兑换「需自备 115 会员的注册码」。"
RULE_UNIFIED = "📌 保号规则（统一口径）：通过游戏兑换续期码完成保号。"
BRACKET_NOTE = "（括号）标注 = 需自备 115 会员"
