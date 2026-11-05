"""qBittorrent Web API 客户端（基于标准库，cookie 自动维护）"""
import http.cookiejar
import json
import urllib.parse
import urllib.request
import urllib.error

import config


class QB:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def _call(self, path, data=None, retry=True, timeout=15):
        url = config.QB_URL + path
        hdr = {}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, encoding="utf-8").encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=hdr,
                                     method="POST" if data is not None else "GET")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code = e.code
            body_txt = e.read().decode("utf-8", "replace")[:200]
            if code in (401, 403) and retry:
                if self.login():
                    return self._call(path, data, retry=False, timeout=timeout)
            return code, body_txt
        except Exception as e:
            return -1, f"{type(e).__name__}: {e}"

    def login(self):
        st, _ = self._call("/api/v2/auth/login",
                           {"username": config.QB_USER, "password": config.QB_PASS},
                           retry=False)
        return st in (200, 204)

    def torrents(self, filter_=None):
        path = "/api/v2/torrents/info"
        if filter_:
            path += "?filter=" + urllib.parse.quote(filter_)
        st, txt = self._call(path)
        if st != 200:
            return []
        try:
            return json.loads(txt or "[]")
        except Exception:
            return []

    def trackers(self, h):
        st, txt = self._call("/api/v2/torrents/trackers?hash=" + h)
        if st != 200:
            return []
        try:
            return json.loads(txt or "[]")
        except Exception:
            return []

    def files(self, h):
        st, txt = self._call("/api/v2/torrents/files?hash=" + h)
        if st != 200:
            return []
        try:
            return json.loads(txt or "[]")
        except Exception:
            return []

    def add_tags(self, hashes, tags):
        return self._call("/api/v2/torrents/addTags",
                          {"hashes": "|".join(hashes), "tags": ",".join(tags)})[0]

    def remove_tags(self, hashes, tags):
        return self._call("/api/v2/torrents/removeTags",
                          {"hashes": "|".join(hashes), "tags": ",".join(tags)})[0]

    def stop(self, hashes):
        return self._call("/api/v2/torrents/stop", {"hashes": "|".join(hashes)})[0]

    def delete(self, hashes, with_files=False):
        return self._call("/api/v2/torrents/delete",
                          {"hashes": "|".join(hashes),
                           "deleteFiles": "true" if with_files else "false"})[0]

    def recheck(self, hashes):
        return self._call("/api/v2/torrents/recheck", {"hashes": "|".join(hashes)})[0]
