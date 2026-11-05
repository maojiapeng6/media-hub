#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开源发布隐私扫描器（守门基线）
用途：发布前扫描源码中的所有敏感信息，输出结构化清单。
每次 push 前必须运行，退出码非 0 = 存在泄露，禁止发布。

★ 本文件将公开。因此规则里**绝不内联任何真实秘密片段**。
  个人已知凭据用「哈希清单」比对：本地 .privacy-hashes.txt（已被 .gitignore 拦截）
  每行一个 sha256 前 16 位；扫描时对行内 token 计算哈希查表，命中即 CRIT。
"""
import re, sys, os, json, argparse, hashlib

RULES = [
    # (id, 严重级, 描述, 正则)
    ("PRIV-IP",   "HIGH",   "内网 IP 192.168.x.x",                r"192\.168\.\d+\.\d+"),
    ("PRIV-IP2",  "MED",    "常见局域网网段 10.x/172.16-31",       r"\b10\.\d+\.\d+\.\d+|\b172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"),
    ("PRIV-TOKEN","CRIT",   "Telegram bot token 形态",             r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"),
    ("PRIV-APIHASH","CRIT", "TG api_hash 形态(32位hex)",           r"\b[0-9a-f]{32}\b"),
    ("PRIV-EMBYKEY","CRIT", "Emby/api key 32hex 赋值",             r"api_?key['\"]?\s*[:=]\s*['\"][0-9a-f]{32}"),
    ("PRIV-EMAIL","HIGH",   "个人邮箱(常见QQ/163)",                r"\b\d{8,12}@(?:qq|163|126)\.com\b"),
    ("PRIV-UID",  "MED",    "Telegram numeric UID(10位)",          r"\b8[0-9]{9,10}\b"),
    ("PRIV-DOMAIN","HIGH",  "作者自建域名",                        r"ccwu\.cc|z1000\.cc\.cd"),
    ("PRIV-115",  "CRIT",   "115 cookie / uptoken / eid",          r"UPTOKEN|COOKIE115|_cookie.*=.*['\"][A-Za-z0-9%]{40,}|115cookie"),
    ("PRIV-SECRET","CRIT",  "generic secret=赋值长串",             r"(?:secret|token|passwd|password|api_?key|access_?key)['\"]?\s*[:=]\s*['\"][^'\"$\{{<][^'\"]{12,}['\"]"),
    ("PRIV-STRM", "MED",    "本机 strm/挂载绝对路径",              r"/vol1/1000|/CloudNAS/115open"),
    ("PRIV-PORTMAP","LOW",  "本机端口写死(结合IP才是泄露)",         r"192\.168\.10\.110:\d+"),
]

# token 候选：够长的"像密码"串（含字母且长度>=6）
_TOKEN_RE = re.compile(r"[A-Za-z0-9!@#$%^&*.+=_\-]{6,}")


# 白名单：示例/占位/掩码，不算泄露
WHITELIST_PAT = re.compile(
    r"(your|xxx|placeholder|example|changeme|<.*>|\{\{.*\}\}|REDACTED|MASKED|"
    r"255\.255\.255\.255|host\.docker\.internal|127\.0\.0\.1|localhost|0\.0\.0\.0|"
    r"5701:AAEvAHzsg30|123456|MYSQL|template)", re.I)

def load_hashlist(paths):
    """读个人秘密哈希清单（本地文件，绝不入库）。每行 = sha256(secret) 前16位。"""
    h = set()
    for p in paths:
        if p and os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#"):
                    h.add(line[:16].lower())
    return h

def tok16(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]

def scan_file(path, rules, hashlist):
    findings = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return findings, str(e)
    for ln, text in enumerate(lines, 1):
        for rid, sev, desc, pat in rules:
            for m in re.finditer(pat, text):
                ctx = text.strip()
                # 整行白名单命中则跳过
                if WHITELIST_PAT.search(m.group(0)) or (len(ctx) < 200 and WHITELIST_PAT.search(ctx) and rid in ("PRIV-SECRET","PRIV-APIHASH","PRIV-EMBYKEY","PRIV-115")):
                    continue
                findings.append({"file": path, "line": ln, "rule": rid, "sev": sev,
                                 "desc": desc, "match": m.group(0)[:48],
                                 "ctx": ctx[:160]})
        # 哈希比对：个人已知秘密（明文不落地本工具）
        if hashlist:
            for m in _TOKEN_RE.finditer(text):
                t = m.group(0)
                if tok16(t) in hashlist:
                    findings.append({"file": path, "line": ln, "rule": "PRIV-HASH", "sev": "CRIT",
                                     "desc": "命中个人秘密哈希清单", "match": "<hashed-secret>",
                                     "ctx": text.strip()[:120]})
    return findings, None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", nargs="+", required=True)
    ap.add_argument("--out", default="audit/privacy_findings.json")
    ap.add_argument("--ext", default=".py,.html,.js,.json,.yml,.yaml,.env,.conf,.ini,.md,.sh,.txt")
    ap.add_argument("--exclude", nargs="*", default=["audit"], help="按路径片段排除目录/文件")
    ap.add_argument("--baseline", default=None, help="白名单文件：每行 file子串::rule，命中则视为已审核安全项")
    ap.add_argument("--hashlist", default=None, help="个人秘密哈希清单(本地,勿入库)；缺省找同级 .privacy-hashes.txt")
    args = ap.parse_args()
    exts = tuple(args.ext.split(","))
    excl = [e.strip("/\\") for e in args.exclude]

    hl_path = args.hashlist
    if not hl_path:
        cand = os.path.join(os.path.dirname(__file__), "..", ".privacy-hashes.txt")
        hl_path = cand if os.path.exists(cand) else None
    hashlist = load_hashlist([hl_path])

    # 载入 baseline：(path_substring, rule) 对
    baseline = []
    bl_path = args.baseline
    if not bl_path:
        cand = os.path.join(os.path.dirname(__file__), "..", ".privacy-baseline.txt")
        bl_path = cand if os.path.exists(cand) else None
    if bl_path and os.path.exists(bl_path):
        for line in open(bl_path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "::" not in line:
                continue
            ps, rl = line.split("::", 1)
            baseline.append((ps.strip().replace("\\", "/"), rl.strip()))

    def in_baseline(path, rule):
        norm = path.replace("\\", "/")
        return any(ps in norm and (rl == "*" or rl == rule) for ps, rl in baseline)

    def is_excluded(path):
        norm = path.replace("\\", "/")
        return any(seg in norm for seg in excl)

    all_files = []
    for p in args.paths:
        if os.path.isfile(p):
            all_files.append(p)
        elif os.path.isdir(p):
            for root, dirs, fs in os.walk(p):
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".venv", "venv", "audit")]
                for fn in fs:
                    if fn.endswith(exts):
                        fp = os.path.join(root, fn)
                        if not is_excluded(fp):
                            all_files.append(fp)

    rules = [(r if r[3] else r) for r in RULES]
    results = []
    errors = []
    baselined = 0
    for f in all_files:
        found, err = scan_file(f, rules, hashlist)
        if err: errors.append({"file": f, "err": err})
        for item in found:
            if in_baseline(item["file"], item["rule"]):
                baselined += 1
            else:
                results.append(item)

    # 汇总
    by_rule, by_sev = {}, {}
    for r in results:
        by_rule[r["rule"]] = by_rule.get(r["rule"], 0) + 1
        by_sev[r["sev"]] = by_sev.get(r["sev"], 0) + 1

    summary = {
        "files_scanned": len(all_files),
        "total_findings": len(results),
        "by_severity": by_sev,
        "by_rule": by_rule,
        "blocking": by_sev.get("CRIT", 0) + by_sev.get("HIGH", 0),
        "errors": errors,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "findings": results}, fh, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # 按文件聚合 blocking（CRIT+HIGH）计数，定位待办重灾区
    by_file = {}
    for r in results:
        if r["sev"] in ("CRIT", "HIGH"):
            by_file[r["file"]] = by_file.get(r["file"], 0) + 1
    if by_file:
        print("\n===== 按文件 blocking 计数（CRIT+HIGH）=====")
        for f, n in sorted(by_file.items(), key=lambda x: -x[1])[:20]:
            print(f"{n:4d}  {f}")
    # 打印 CRIT/HIGH 前 40 条
    print("\n===== CRIT/HIGH 采样 =====")
    shown = 0
    for r in results:
        if r["sev"] in ("CRIT", "HIGH") and shown < 40:
            print(f"[{r['sev']}] {r['rule']:12s} {r['file']}:{r['line']}  {r['match']}  | {r['ctx'][:90]}")
            shown += 1
    # 退出码：CRIT>0 => 2, HIGH>0 => 1, 否则 0
    sys.exit(2 if by_sev.get("CRIT", 0) else (1 if by_sev.get("HIGH", 0) else 0))

if __name__ == "__main__":
    main()
