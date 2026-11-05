"""MediaTidy API 客户端：登录拿 token + 云端列目录（用于上传后校验）"""
import json
import time
import urllib.parse
import urllib.request
import urllib.error

import config


class MT:
    def __init__(self):
        self.token = None
        self.token_at = 0

    def _req(self, path, data=None, auth=True, timeout=20):
        url = config.MT_URL + path
        hdr = {"Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode()
            hdr["Content-Type"] = "application/json"
        if auth and self.token:
            hdr["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=body, headers=hdr,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:300]
        except Exception as e:
            return -1, f"{type(e).__name__}: {e}"

    def login(self, force=False):
        if self.token and not force and time.time() - self.token_at < 3600:
            return True
        st, txt = self._req("/api/auth/login",
                            {"Username": config.MT_USER, "Password": config.MT_PASS},
                            auth=False)
        if st != 200:
            return False
        try:
            self.token = json.loads(txt)["data"]["token"]
            self.token_at = time.time()
            return True
        except Exception:
            return False

    def list_dir(self, cloud_path, retry=True):
        """列出云端目录，返回 [{name, path, size, isDir}]；失败返回 None"""
        if not self.login():
            return None
        p = urllib.parse.quote(cloud_path)
        st, txt = self._req(f"/api/drive/files?accountId={config.MT_ACCOUNT_ID}&path={p}")
        if st in (401, 403) and retry:
            self.login(force=True)
            st, txt = self._req(f"/api/drive/files?accountId={config.MT_ACCOUNT_ID}&path={p}")
        if st != 200:
            return None
        try:
            d = json.loads(txt).get("data")
            return d if isinstance(d, list) else []
        except Exception:
            return None

    def walk(self, cloud_path, limit=800):
        """递归统计云端目录：返回 (文件数, 总字节)；超限返回 None"""
        total, count = 0, 0
        stack = [cloud_path]
        seen = 0
        while stack:
            cur = stack.pop()
            seen += 1
            if seen > limit:
                return None
            items = self.list_dir(cur)
            if items is None:
                return None
            for it in items:
                if it.get("isDir"):
                    stack.append(it.get("path") or (cur.rstrip("/") + "/" + it["name"]))
                else:
                    count += 1
                    total += int(it.get("size") or 0)
        return count, total
