#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 一键建仓 + 分类设置 + 首次/增量推送（零凭据入库）。

设计约束：
  * 脚本内绝不写死任何 Token / 用户名 / 仓库地址；Token 只从环境变量或 gh CLI 读取。
  * 全程幂等：仓库已存在 / remote 已配置 / Release 已发布，一律跳过而不是报错。
  * 推送前强制跑 privacy_scan，blocking != 0 直接中止。
  * 仅用 Python 标准库，Windows / Linux / macOS 均可直接运行。

用法：
  python scripts/bootstrap_github.py --dry-run          # 只看计划，不联网、不改动
  python scripts/bootstrap_github.py                    # 建仓 + 分类 + push + Release
  python scripts/bootstrap_github.py --no-push          # 只建仓和设置，不推代码
  python scripts/bootstrap_github.py --public           # 直接建公开仓（默认 Private）
  python scripts/bootstrap_github.py --owner my-org     # 建到组织下

Token 来源优先级：GITHUB_TOKEN > GH_TOKEN > GITHUB_PAT > `gh auth token`。
所需 scope：repo（私有仓）+ 可选 admin:repo_hook；Release 需要 contents:write。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"

REPO_NAME = "media-hub"
DESCRIPTION = (
    "自托管影音统一总控台：Emby / MoviePilot / qBittorrent / MediaTidy / "
    "CloudDrive2 多实例管理 + Telegram 机器人套件（零内置凭据）"
)
TOPICS = [
    "emby", "mediatidy", "moviepilot", "qbittorrent", "docker",
    "self-hosted", "nas", "flask", "media-center", "home-lab",
]

# Issue 分类标签（名称, 说明, 颜色）——与 .github/ISSUE_TEMPLATE 的 labels 对齐
LABELS = [
    ("bug", "缺陷", "d73a4a"),
    ("enhancement", "功能建议", "a2eeef"),
    ("triage", "待分类", "ededed"),
    ("app", "app/ 总控台", "1d76db"),
    ("bots", "bots/ 机器人套件", "5319e7"),
    ("deploy", "deploy/ 部署编排", "0e8a16"),
    ("docs", "docs/ 文档", "0075ca"),
    ("tools", "tools/ 仓库工具", "c5def5"),
    ("security", "安全", "b60205"),
    ("privacy", "隐私守门", "fbca04"),
]


def log(msg):
    print(msg, flush=True)


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s" % (" ".join(cmd), p.stderr.strip()))
    return p


def load_token():
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        val = os.environ.get(key, "").strip()
        if val:
            return val, "env:%s" % key
    try:
        p = run(["gh", "auth", "token"], check=False)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip(), "gh auth token"
    except FileNotFoundError:
        pass
    return "", ""


def api(method, path, token, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mediahub-bootstrap",
        "Authorization": "Bearer " + token,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        return exc.code, body
    except urllib.error.URLError as exc:
        raise RuntimeError("无法连接 GitHub API: %s" % exc.reason)


def guard_privacy():
    log("[1/6] 隐私守门 privacy_scan ...")
    py = sys.executable
    out = ROOT / "audit" / "privacy_findings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = run([py, "tools/privacy_scan.py", "--paths", ".",
             "--baseline", ".privacy-baseline.txt", "--exclude", "audit", ".githooks",
             "--out", str(out)], check=False)
    # --out 报告结构为 {"summary": {...}, "findings": [...]}；stdout 则直接是 summary
    report = {}
    try:
        raw = json.loads(out.read_text(encoding="utf-8"))
        report = raw.get("summary", raw) if isinstance(raw, dict) else {}
    except Exception:
        pass
    if "blocking" not in report:
        m = re.search(r"\{.*\}", p.stdout or "", re.S)
        if m:
            try:
                report = json.loads(m.group(0))
            except Exception:
                report = {}
    blocking = report.get("blocking")
    log("  blocking=%s / 扫描文件 %s / MED %s / exit=%s" % (
        blocking, report.get("files_scanned"),
        report.get("by_severity", {}).get("MED", 0), p.returncode))
    if blocking not in (0, None) or p.returncode != 0:
        raise RuntimeError("隐私守门未通过（CRIT/HIGH 命中或扫描器异常），禁止发布")
    if blocking is None:
        log("  ⚠️ 报告结构未识别，已按扫描器退出码 0 放行")
    log("  ✅ 通过")


def current_tag():
    p = run(["git", "describe", "--tags", "--abbrev=0"], check=False)
    return p.stdout.strip() if p.returncode == 0 else ""


def release_body(tag):
    path = ROOT / "CHANGELOG.md"
    if not tag or not path.exists():
        return "Release %s" % (tag or "initial")
    text = path.read_text(encoding="utf-8")
    ver = tag.lstrip("v")
    m = re.search(r"^## \[%s\][^\n]*\n(.*?)(?=^## \[|\Z)" % re.escape(ver),
                  text, re.S | re.M)
    return m.group(1).strip()[:6000] if m else "Release %s，详见 CHANGELOG.md。" % tag


def main():
    ap = argparse.ArgumentParser(description="GitHub 建仓 + 分类 + 一键发布")
    ap.add_argument("--repo", default=REPO_NAME, help="仓库名（默认 %s）" % REPO_NAME)
    ap.add_argument("--owner", default="", help="组织名；留空则取当前登录用户")
    ap.add_argument("--public", action="store_true", help="直接建 Public（默认 Private）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不联网不改动")
    ap.add_argument("--no-push", action="store_true", help="不推送代码与 tag")
    ap.add_argument("--skip-release", action="store_true", help="不创建 GitHub Release")
    ap.add_argument("--tag", default="", help="发布指定 tag（默认取最新 tag）")
    args = ap.parse_args()

    tag = args.tag or current_tag()
    log("=== MediaHub 发布助手 ===")
    log("仓库: %s | 可见性: %s | 最新 tag: %s" % (
        args.repo, "Public" if args.public else "Private", tag or "(无)"))
    log("Topics: %s" % ", ".join(TOPICS))

    if args.dry_run:
        log("\n[dry-run] 将依次执行：")
        log("  1. privacy_scan 守门（blocking=0 才继续）")
        log("  2. GET /user 取 owner → GET /repos/{owner}/%s（404 则 POST 建空仓）" % args.repo)
        log("  3. PATCH 简介 / PUT topics / POST issue labels（GitHub 侧三类分类）")
        log("  4. git remote add|set-url origin https://github.com/{owner}/%s.git" % args.repo)
        log("  5. git push -u origin HEAD:master --tags")
        log("  6. POST /releases（tag=%s，幂等）" % (tag or "latest"))
        log("  7. 回写 docs/publish-checklist.md 的 remote 状态（由维护者执行）")
        log("\n[dry-run] 未联网、未改动任何文件。")
        return 0

    guard_privacy()
    token, src = load_token()
    if not token:
        raise RuntimeError(
            "未找到 GitHub 授权。请任选其一后重跑：\n"
            "  a) 在 WorkBuddy 中启用 GitHub 连接器并授权\n"
            "  b) 设置环境变量 GITHUB_TOKEN=<PAT>\n"
            "  c) 安装并登录 gh CLI（gh auth login）")
    log("[2/6] 已取得授权（来源 %s，值不打印）" % src)

    if args.owner:
        owner = args.owner
        create_path = "/orgs/%s/repos" % owner
    else:
        code, me = api("GET", "/user", token)
        if code != 200:
            raise RuntimeError("获取登录用户失败 HTTP %s：%s" % (code, me.get("message", "")))
        owner = me.get("login", "")
        create_path = "/user/repos"
        log("  登录用户: %s" % owner)

    full = "%s/%s" % (owner, args.repo)
    code, repo = api("GET", "/repos/%s" % full, token)
    if code == 200:
        log("[3/6] 仓库已存在，跳过创建（private=%s）" % repo.get("private"))
    elif code == 404:
        payload = {
            "name": args.repo, "description": DESCRIPTION,
            "private": not args.public, "auto_init": False,
            "has_issues": True, "has_projects": False, "has_wiki": False,
        }
        code, repo = api("POST", create_path, token, payload)
        if code not in (201, 202):
            raise RuntimeError("建仓失败 HTTP %s：%s" % (code, repo.get("message", "")))
        log("[3/6] ✅ 已创建空仓 https://github.com/%s" % full)
    else:
        raise RuntimeError("查询仓库失败 HTTP %s：%s" % (code, repo.get("message", "")))

    log("[4/6] 设置分类（简介 / topics / issue labels）...")
    api("PATCH", "/repos/%s" % full, token, {"description": DESCRIPTION})
    code, _ = api("PUT", "/repos/%s/topics" % full, token, {"names": TOPICS})
    log("  topics HTTP %s（403 表示 token 缺 scope，可稍后在 About 手动补齐）" % code)
    made = 0
    for name, desc, color in LABELS:
        c, _ = api("POST", "/repos/%s/labels" % full, token,
                   {"name": name, "description": desc, "color": color})
        if c in (200, 201):
            made += 1
    log("  issue labels：新建 %d 个，其余已存在（分类体系共 %d 项）" % (made, len(LABELS)))

    remote_url = "https://github.com/%s.git" % full
    cur = run(["git", "remote", "get-url", "origin"], check=False)
    if cur.returncode != 0:
        run(["git", "remote", "add", "origin", remote_url])
        log("[5/6] 已配置 remote origin")
    elif cur.stdout.strip() != remote_url:
        run(["git", "remote", "set-url", "origin", remote_url])
        log("[5/6] 已更新 remote origin")
    else:
        log("[5/6] remote origin 已是目标地址")
    log("  %s" % remote_url)

    if not args.no_push:
        p = run(["git", "push", "-u", "origin", "HEAD:master", "--tags"], check=False)
        if p.returncode != 0:
            raise RuntimeError("push 失败：\n%s" % p.stderr.strip())
        log("  ✅ 已推送 master + 全部 tag")
    else:
        log("  --no-push：跳过推送")

    if tag and not args.skip_release and not args.no_push:
        code, _ = api("GET", "/repos/%s/releases/tags/%s" % (full, tag), token)
        if code == 200:
            log("[6/6] Release %s 已存在，跳过" % tag)
        else:
            code, rel = api("POST", "/repos/%s/releases" % full, token, {
                "tag_name": tag, "name": "MediaHub %s" % tag,
                "body": release_body(tag), "draft": False,
                "prerelease": ("rc" in tag or "beta" in tag),
            })
            log("[6/6] Release %s：HTTP %s" % (tag, code))

    log("\n✅ 完成：https://github.com/%s" % full)
    log("   后续每次版本更新：commit + tag 后直接 git push origin master --tags 即可。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log("\n❌ %s" % exc)
        sys.exit(1)
