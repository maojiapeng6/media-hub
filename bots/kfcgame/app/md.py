"""Markdown 富文本渲染：绕开 Pyrogram 解析器的 emoji 偏移 bug。

★ 为什么需要这个模块（2026-10-09 实测）
─────────────────────────────────────
Pyrogram 的 parser 解析完会做 `utils.remove_surrogates(parser.text)`，
把 emoji 从代理对压回单字符，**但实体(entities)的 offset/length 没有同步回退**。
结果：凡是 emoji 出现在 `**粗体**` 前面，那条粗体及其之后所有实体的 offset 都
整体前移 1 个字符。实测对照：

    'X **播放** 中'          → Bold 覆盖 '播放'      ✅
    '🎬 **Emby 在线播报**'    → Bold 覆盖 'mby 在线播报' ❌ 偏 1
    '👥 人数｜**12** 人▶️ …'   → Bold 覆盖 '2 ' 和 ' '   ❌ 完全错位

⇒ 表现就是「加粗好像没生效」，还会顺带把后面的字吞掉一格。
Markdown 和 HTML 两条路都中招（Markdown 内部也调 html.parse）。

★ 本模块的解法
自己按 Telegram 规则算offset —— 扫一遍文本，标记从最终 message 里剔除，
边拼纯文本边用**UTF-16 code unit** 记录每个实体的位置。
emoji 在 UTF-16 里占 2 个单位，正好和协议对齐 ⇒ 再也不会错位。
"""
from __future__ import annotations

import re
from typing import Any

# (正则, 实体类型)。顺序即优先级，长标记放前面。
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"```(.+?)```", re.S), "pre"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), "bold"),
    (re.compile(r"__(.+?)__", re.S), "underline"),
    (re.compile(r"~~(.+?)~~", re.S), "strike"),
    (re.compile(r"\|\|(.+?)\|\|", re.S), "spoiler"),
    (re.compile(r"`(.+?)`", re.S), "code"),
    (re.compile(r"\[(.+?)\]\(([^()\s]+?)\)"), "link"),
    # 单星号/单下划线斜体：要求两侧不是同类符号，避免吃掉 ** 和 __
    (re.compile(r"(?<![\*\w])\*([^*\n]+?)\*(?!\*)"), "italic"),
    (re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])"), "italic"),
]

_MENTION = re.compile(r"\Atg://user\?id=(\d+)\Z")

# ══════════════════════════════════════════════════════════════════
# ★★★HTML 分支（2026-10-11 修`<a>` 裸露的真正根因）
#
# 事故经过：本模块的 FixedParser.parse(text, mode) 一开始**完全忽略了 mode**，
#   无论调用方传HTML 还是 Markdown，一律走上面的 Markdown 解析器。
#   而 request.py / mptask.py 全模块用 `parse_mode=ParseMode.HTML` +真HTML 文案
#   （`<a href="tg://user?id=…">名字</a>`、`<b>…</b>`）——
#   ⇒ 这些标签被 Markdown 解析器当普通字符，**原样裸露给用户**。
#   （之前排查时一直怀疑"发送点漏了 parse_mode"，其实参数全带对了，
#     错的是这个自写解析器吞掉了 mode。）
#
# 现在按 mode 分流：HTML 走 build_html()，Markdown 走 build()。
# ══════════════════════════════════════════════════════════════════

# HTML 标签 → 实体类型。未列出的标签一律当普通文本保留（不吞）。
_HTML_TAGS: dict[str, str] = {
    "b": "bold", "strong": "bold",
    "i": "italic", "em": "italic",
    "u": "underline", "ins": "underline",
    "s": "strike", "strike": "strike", "del": "strike",
    "code": "code", "pre": "pre",
    "blockquote": "blockquote",
    "tg-spoiler": "spoiler",
    "a": "link",
}
# 自闭合/无内容标签，剥掉即可，不产生实体
_HTML_VOID = {"br"}

_TAG_RE = re.compile(
    r"<(\s*/?\s*)([a-zA-Z][a-zA-Z0-9-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(\s*/?\s*)>")
_ESC_RE = re.compile(r"&(amp|lt|gt|quot|apos|#\d+);")
_ESC_MAP = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def _unescape(s: str) -> str:
    def rep(m: re.Match) -> str:
        g = m.group(1)
        if g.startswith("#"):
            try:
                return chr(int(g[1:]))
            except Exception:
                return m.group(0)
        return _ESC_MAP.get(g, m.group(0))
    return _ESC_RE.sub(rep, s)


def build_html(text: str) -> tuple[str, list[dict[str, Any]]]:
    """HTML 文本 → (纯文本, entities)。

    自己扫标签、按 **UTF-16 code unit** 记 offset（与 build() 同一套算法），
    所以 HTML 路径同样免疫 emoji 代理对错位。

    容错原则（宁可少加样式，也不能让用户看到标签）：
      · 未闭合的标签 → 开标签起就算实体，收尾处不报错
      · 多余的闭标签 → 直接丢弃，不当成文本吐出去
      · 未知标签     → 原样保留为文本（TG 也不认）
    """
    src = str(text or "")
    out: list[str] = []
    ents: list[dict[str, Any]] = []
    u16 = 0
    # 栈：[tag, kind, start_u16, url]
    stack: list[list] = []
    pos = 0

    def emit(chunk: str) -> None:
        nonlocal u16
        if chunk:
            out.append(chunk)
            u16 += u16_len(chunk)

    while pos < len(src):
        m = _TAG_RE.search(src, pos)
        if m is None:
            emit(_unescape(src[pos:]))
            break
        emit(_unescape(src[pos:m.start()]))
        closing = bool(m.group(1).strip())
        tag = m.group(2).lower()
        attrs = m.group(3)
        pos = m.end()

        if tag in _HTML_VOID:
            continue
        kind = _HTML_TAGS.get(tag)
        if kind is None:                      # 未知标签：原样吐出去
            emit(m.group(0))
            continue
        if closing:
            # 找最近的同标签开标签（容错：中间夹了别的标签也能正确闭合）
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    item = stack.pop(i)
                    ln = u16 - item[2]
                    if ln > 0:
                        rec: dict[str, Any] = {"type": item[1],
                                               "offset": item[2], "length": ln}
                        if item[1] == "link":
                            url = item[3]
                            mm = _MENTION.match(url)
                            if mm:
                                rec["type"] = "mention"
                                rec["user_id"] = int(mm.group(1))
                            else:
                                # ★ 必须是 text_url：_RAW 里注册的是这个名字，
                                #   写成 "link" 会被下面的 `cls is None` 丢弃，
                                #   表现为「TMDB 链接变成不可点的纯文字」。
                                rec["type"] = "text_url"
                                rec["url"] = url
                        ents.append(rec)
                    break
                #闭标签与栈顶不匹配 → 把栈顶未闭合的收尾掉，避免 entity 一直开着
        else:
            url = ""
            if kind == "link":
                hm = re.search(r'href\s*=\s*"([^"]*)"', attrs) or \
                    re.search(r"href\s*=\s*'([^']*)'", attrs)
                url = _unescape(hm.group(1)) if hm else ""
            stack.append([tag, kind, u16, url])

    # 收尾：把还开着的标签就地闭合（长度到文本末尾）
    while stack:
        item = stack.pop()
        ln = u16 - item[2]
        if ln > 0:
            rec = {"type": item[1], "offset": item[2], "length": ln}
            if item[1] == "link":
                url = item[3]
                mm = _MENTION.match(url)
                if mm:
                    rec["type"] = "mention"
                    rec["user_id"] = int(mm.group(1))
                else:
                    rec["type"] = "text_url"          # 同上：必须是 text_url
                    rec["url"] = url
            ents.append(rec)

    ents.sort(key=lambda e: e["offset"])
    return "".join(out), ents


def u16_len(s: str) -> int:
    """UTF-16 code unit 长度 —— Telegram 实体 offset/length 用的就是这个单位。
    Python str 按码点计数（一个 emoji = 1），但 BMP 外字符在 UTF-16 里占 2。"""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def build(text: str) -> tuple[str, list[dict[str, Any]]]:
    """markdown 文本 → (纯文本, entities)。

    entities 每项是 dict：``{"type","offset","length"[,"url"|"user_id"]}``，
    可直接传给 ``client.send_message(..., entities=...)``。
    """
    out: list[str] = []          # 最终纯文本的片段
    ents: list[dict[str, Any]] = []
    pos = 0                      # 当前扫描位置（原始 text 的下标）
    u16 = 0                      # 已产出文本的 UTF-16 长度

    def emit(chunk: str) -> None:
        nonlocal u16
        if chunk:
            out.append(chunk)
            u16 += u16_len(chunk)

    while pos < len(text):
        best = None              # (起始下标, 正则, 类型, group)
        for rx, kind in _RULES:
            m = rx.search(text, pos)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), rx, kind, m)
        if best is None:
            emit(text[pos:])
            break

        start, rx, kind, m = best
        emit(text[pos:start])                # 标记之前的普通文本

        body = m.group(1)
        if kind == "bold":
            # ★ **粗体 + 超链接** 的组合写法：**[片名](url)**
            #   不特判的话，bold 规则会先吃掉整段（body = "[片名](url)"），
            #   链接规则再也匹配不到 ⇒ 用户看到的是字面量「[片名](url)」。
            #   这里拆开处理：只吐链接文字，同时发 bold + link 两个实体。
            lm = re.fullmatch(r"\[([^\]\n]+)\]\(([^()\s]+)\)", body)
            if lm:
                link_txt, url = lm.group(1), lm.group(2)
                u0 = u16
                ln = u16_len(link_txt)
                mt = _MENTION.match(url)
                if mt:
                    ents.append({"type": "mention", "user_id": int(mt.group(1)),
                                 "offset": u0, "length": ln})
                else:
                    ents.append({"type": "text_url", "url": url,
                                 "offset": u0, "length": ln})
                ents.append({"type": "bold", "offset": u0, "length": ln})
                emit(link_txt)
                pos = m.end()
                continue
        rec: dict[str, Any] = {"type": kind,
                               "offset": u16,
                               "length": u16_len(body)}
        if kind == "link":
            url = m.group(2)
            mt = _MENTION.match(url)
            if mt:      # TG 提及：点了直接进对方主页
                rec["type"] = "mention"
                rec["user_id"] = int(mt.group(1))
            else:
                rec["type"] = "text_url"
                rec["url"] = url
        ents.append(rec)
        emit(body)                            # 只把内容留下，标记丢弃
        pos = m.end()

    return "".join(out), ents


# ── Pyrogram 补丁：把 client.parser 换掉 ────────────────────────────
# 所有发送方法（send_message / edit_text / send_photo 的 caption 等）
# 都会走 `await self.parser.parse(text, mode)` 并把返回的
# {"message":..., "entities":...} 展开进请求。替换这一个对象，
# 就能一次性修好全站所有文案，**调用点一行都不用改**。
#
# ★ 必须在 Client 构造后、开始收发消息前安装。
# ★ 返回的 entities 用 pyrogram 的 raw 类型，和原版 parser 接口一致。

from pyrogram import raw

_RAW = {
    "bold": raw.types.MessageEntityBold,
    "italic": raw.types.MessageEntityItalic,
    "underline": raw.types.MessageEntityUnderline,
    "strike": raw.types.MessageEntityStrike,
    "code": raw.types.MessageEntityCode,
    "pre": raw.types.MessageEntityPre,
    "spoiler": raw.types.MessageEntitySpoiler,
    "blockquote": raw.types.MessageEntityBlockquote,
    "text_url": raw.types.MessageEntityTextUrl,
    "mention": raw.types.InputMessageEntityMentionName,
}


def _norm_mode(mode, client_mode) -> str:
    """把 mode 归一成 'html' / 'markdown'。

    Pyrogram 调用时传的可能是枚举、字符串，也可能是 None（表示用
    client.parse_mode）。统一在这里定好，避免 parse() 里各处判不一致。
    """
    m = mode if mode is not None else client_mode
    v = getattr(m, "value", m)                # ParseMode.HTML.value == "html"
    s = str(v or "").lower()
    if s in ("html", "htm"):
        return "html"
    return "markdown"


class FixedParser:
    """替代 Pyrogram 原 Parser —— 自己算 UTF-16 offset，绕开代理对回退 bug。

    ★★必须按 mode 分流（2026-10-11 修`<a>` 裸露）
    原版这里 `mode` 参数被完全忽略，导致 parse_mode=HTML 的文案被当Markdown 解析，
    `<a href>`/`<b>` 原样裸露。现在 HTML 走 build_html()、Markdown 走 build()。
    """

    def __init__(self, client) -> None:
        self.client = client

    async def parse(self, text: str, mode=None) -> dict[str, Any]:
        kind = _norm_mode(mode, getattr(self.client, "parse_mode", None))
        if kind == "html":
            bare, marks = build_html(str(text or ""))
        else:
            bare, marks = build(str(text or ""))
        entities: list[Any] = []
        for m in marks:
            kind_e = m["type"]
            off, ln = m["offset"], m["length"]
            if ln <= 0:
                continue
            # TG 提及：优先做成「点击直接进主页」的 mention 实体；
            # 万一 peer 解析不了，退化成普通文本链接 —— 宁可点开网页，
            # 也不能整条链接消失（TG ID 本身仍是可点的）。
            if kind_e == "mention":
                try:
                    peer = await self.client.resolve_peer(m["user_id"])
                except Exception:
                    entities.append(raw.types.MessageEntityTextUrl(
                        offset=off, length=ln,
                        url=f"tg://user?id={m['user_id']}"))
                    continue
                entities.append(raw.types.InputMessageEntityMentionName(
                    offset=off, length=ln, user_id=peer))
                continue
            cls = _RAW.get(kind_e)
            if cls is None:
                continue
            kwargs: dict[str, Any] = {"offset": off, "length": ln}
            if kind_e == "text_url":
                kwargs["url"] = m.get("url", "")
            elif kind_e == "pre":
                kwargs["language"] = ""
            entities.append(cls(**kwargs))
        entities.sort(key=lambda e: e.offset)
        return {"message": bare, "entities": entities or None}

    @staticmethod
    def unparse(text: str, entities: list) -> str:
        return text


def install(client) -> None:
    """给 client 装上修正版 parser。幂等，可重复调用。"""
    if isinstance(getattr(client, "parser", None), FixedParser):
        return
    client.parser = FixedParser(client)