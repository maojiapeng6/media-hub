# -*- coding: utf-8 -*-
"""夸克 → 115 自动转存的零凭据参考实现（发布副本 / V1.49 同构）。

与线上 media-hub 版本的区别：**所有地址、令牌、路径均由调用方注入**，
本文件不含任何内网地址、Cookie、Token 或私人路径，可直接进入公开仓库。

典型用法（由部署者自己填）：
    svc = Quark2Five(
        ol_base="https://your-openlist.example",
        token_provider=lambda: os.environ["OPENLIST_ADMIN_TOKEN"],
        src="/quark_relay",
        cd2_dir="/mnt/cd2/115/target",
        state_path="/var/lib/app/q2f_state.json",
        tmp_dir="/var/tmp/q2f",
    )
    threading.Thread(target=svc.loop, daemon=True).start()

设计要点：
  1. 列目录必须 refresh=true —— OpenList 有目录缓存，新建目录不会立即出现。
  2. 秒传：115 只在云端已存在相同 SHA1 时成立；夸克与 115 哈希库不共享，
     因此本实现走「下载 → 落地」的可靠路径，不假设跨盘秒传。
  3. 按 (相对路径, 大小) 去重；状态落盘，进程重启后可继续未完成的条目。
"""
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.request


class Quark2Five(object):
    def __init__(self, ol_base, token_provider, src, cd2_dir, state_path,
                 tmp_dir, sleep_s=120, refresh=True, logger=None):
        self.ol_base = ol_base.rstrip("/")
        self.token_provider = token_provider
        self.src = src.rstrip("/")
        self.cd2_dir = cd2_dir
        self.state_path = state_path
        self.tmp_dir = tmp_dir
        self.sleep_s = sleep_s
        self.refresh = refresh
        self.logger = logger or (lambda *a: None)
        self.lock = threading.Lock()

    # ---------- OpenList 官方 REST API ----------
    def api(self, method, path, body=None, timeout=30):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.ol_base + path, data=data, method=method,
            headers={"Authorization": self.token_provider(),
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def list_dir(self, path):
        r = self.api("POST", "/api/fs/list",
                     {"path": path, "page": 1, "per_page": 500,
                      "refresh": bool(self.refresh)})
        if r.get("code") != 200:
            return None, str(r.get("message"))[:120]
        return (r.get("data") or {}).get("content") or [], ""

    def walk(self, rel=""):
        """递归列出源目录下所有文件。"""
        out = []
        items, err = self.list_dir(self.src + ("/" + rel if rel else ""))
        if items is None:
            return out, err
        for x in items:
            r = (rel + "/" if rel else "") + x["name"]
            if x.get("is_dir"):
                sub, _ = self.walk(r)
                out += sub
            else:
                out.append({"rel": r, "size": x.get("size") or 0,
                            "sha1": (x.get("hash_info") or {}).get("sha1", "")})
        return out, ""

    # ---------- 状态 ----------
    def state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict):
                raise ValueError()
        except Exception:
            s = {}
        s.setdefault("enabled", True)
        s.setdefault("items", {})
        s.setdefault("recent", [])
        return s

    def save(self, s):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    # ---------- 单文件下载 + 落地 ----------
    def fetch_one(self, item, url_rewriter=None):
        g = self.api("POST", "/api/fs/get",
                     {"path": self.src + "/" + item["rel"], "password": ""})
        if g.get("code") != 200:
            return False, "取直链失败: " + str(g.get("message"))[:80]
        raw = (g.get("data") or {}).get("raw_url") or ""
        if not raw:
            return False, "无直链"
        # 代理型驱动返回的 raw_url 可能指向容器内回环地址，由部署者替换。
        if url_rewriter:
            raw = url_rewriter(raw, self.ol_base)
        os.makedirs(self.tmp_dir, exist_ok=True)
        os.makedirs(self.cd2_dir, exist_ok=True)
        part = os.path.join(
            self.tmp_dir,
            "q2f_" + hashlib.md5(item["rel"].encode("utf-8")).hexdigest()[:12] + ".part")
        dst = os.path.join(self.cd2_dir, item["rel"])
        if os.path.exists(dst):
            try:
                cur = os.path.getsize(dst)
            except Exception:
                cur = -1
            if cur == item["size"]:
                return True, "已存在（跳过）"
            # 自愈：落地文件不完整（复制/上传被中断）→ 删除后重传
            try:
                os.remove(dst)
            except Exception as e:
                return False, "不完整文件无法移除: %s" % str(e)[:70]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            req = urllib.request.Request(raw, headers={"User-Agent": "quark2five/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as w:
                shutil.copyfileobj(r, w, 1024 * 1024)
            got = os.path.getsize(part)
            if item["size"] > 0 and got != item["size"]:
                os.remove(part)
                return False, "下载大小不符 %d/%d" % (got, item["size"])
            shutil.move(part, dst)
            try:
                final = os.path.getsize(dst)
            except Exception:
                final = -1
            if item["size"] > 0 and final != item["size"]:
                try:
                    os.remove(dst)
                except Exception:
                    pass
                return False, "落地大小不符 %d/%d（已移除，下轮自动重传）" % (final, item["size"])
            return True, "已投递落地目录"
        except Exception as e:
            try:
                os.remove(part)
            except Exception:
                pass
            return False, str(e)[:130]

    # ---------- 扫描 ----------
    def scan(self, force=False, url_rewriter=None):
        if not self.lock.acquire(False):
            return {"ok": True, "msg": "上一轮仍在进行中", "busy": True}
        try:
            s = self.state()
            if not s.get("enabled", True) and not force:
                return {"ok": True, "msg": "已停用", "enabled": False}
            files, err = self.walk()
            if err:
                s["last_msg"] = "列源失败: " + err
                self.save(s)
                return {"ok": False, "msg": s["last_msg"]}
            new = 0
            for f in files:
                key = "%s|%s" % (f["rel"], f["size"])
                old = s["items"].get(key)
                if old and old.get("status") in ("done", "skip", "running"):
                    continue
                s["items"][key] = dict(f, status="running", ts=time.time(), msg="")
                new += 1
            self.save(s)
            for key, it in list(s["items"].items()):
                if it.get("status") != "running":
                    continue
                ok, msg = self.fetch_one(it, url_rewriter)
                it["status"] = "done" if ok else "failed"
                it["msg"] = msg
                it["ts"] = time.time()
                s["recent"] = ([dict(rel=it["rel"], size=it["size"], ok=ok,
                                     msg=msg, ts=it["ts"])] + s["recent"])[:30]
                self.save(s)
            s["last_scan"] = time.time()
            s["last_msg"] = "本轮新增 %d 个，累计 %d 个" % (new, len(s["items"]))
            self.save(s)
            return {"ok": True, "new": new, "msg": s["last_msg"]}
        except Exception as e:
            self.logger("scan error: %s", e)
            return {"ok": False, "msg": str(e)[:150]}
        finally:
            self.lock.release()

    def status(self):
        s = self.state()
        items = list(s["items"].values())
        return {"ok": True, "enabled": s.get("enabled", True),
                "last_scan": s.get("last_scan", 0), "last_msg": s.get("last_msg", ""),
                "src": self.src, "total": len(items),
                "done": sum(1 for x in items if x.get("status") == "done"),
                "running": sum(1 for x in items if x.get("status") == "running"),
                "failed": sum(1 for x in items if x.get("status") == "failed"),
                "recent": s["recent"][:15]}

    def loop(self, delay=25):
        time.sleep(delay)
        while True:
            try:
                self.scan()
            except Exception as e:
                self.logger("loop error: %s", e)
            time.sleep(self.sleep_s)
