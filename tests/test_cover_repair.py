# -*- coding: utf-8 -*-
"""cover_repair 纯标准库单元测试：全部外部调用用 fake 注入，不联网、不依赖凭据。"""
import base64
import importlib.util
import os
import unittest

# 按文件加载，绝不改 sys.path：openrepo/app 目录里同时存在 app.py，
# 把它加入 sys.path 会破坏既有测试里的 `from app.multi_instance import ...`。
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "app", "cover_repair.py")
_spec = importlib.util.spec_from_file_location("cover_repair", _SRC)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)

KEY = "test_key_placeholder"


def item(iid, name="某片", typ="Movie", year="2024", tmdb="123", primary=None):
    it = {"Id": iid, "Name": name, "Type": typ, "ProductionYear": year,
          "Path": "/media/" + name, "ProviderIds": {"Tmdb": tmdb},
          "ImageTags": {}}
    if primary:
        it["ImageTags"]["Primary"] = primary
    return it


class ScanTests(unittest.TestCase):
    def test_skips_items_with_primary(self):
        rows = [item("1", primary="tag1"), item("2"), item("3", typ="Series")]
        out = cr.scan_missing(lambda s, n, sc, lib: {"Items": rows,
                                                     "TotalRecordCount": 3})
        self.assertTrue(out["ok"])
        self.assertEqual(out["scanned"], 3)
        self.assertEqual(out["missing"], 2)
        self.assertEqual([x["id"] for x in out["items"]], ["2", "3"])

    def test_pagination(self):
        pages = [[item("1"), item("2", primary="x")], [item("3")], []]

        def fetch(start, n, scope, lib):
            k = start // 2
            return {"Items": pages[k] if k < len(pages) else [],
                    "TotalRecordCount": 3}

        out = cr.scan_missing(fetch, page_size=2, limit=10)
        self.assertEqual(out["scanned"], 3)
        self.assertEqual(out["missing"], 2)

    def test_limit_respected(self):
        out = cr.scan_missing(
            lambda s, n, sc, lib: {"Items": [item(str(s + i)) for i in range(n)],
                                   "TotalRecordCount": 100000},
            limit=50, page_size=25)
        self.assertEqual(out["scanned"], 50)
        self.assertEqual(out["missing"], 50)

    def test_fetch_error_is_contained(self):
        def bad(*a):
            raise RuntimeError("emby down")

        out = cr.scan_missing(bad)
        self.assertFalse(out["ok"])
        self.assertIn("emby down", out["err"])


class PosterTests(unittest.TestCase):
    def test_tmdb_id_same_kind(self):
        def fake(url):
            self.assertIn("/movie/123?", url)
            return {"title": "某片", "poster_path": "/a.jpg"}

        po, match, title = cr.find_poster(
            {"tmdb": "123", "type": "Movie", "name": "某片", "year": "2024"},
            fake, api_key=KEY)
        self.assertEqual(po, "/a.jpg")
        self.assertIn("精确匹配 (movie)", match)
        self.assertEqual(title, "某片")

    def test_never_cross_kind(self):
        """🔴 红线：movie 无海报时绝不能取 tv/{同ID} 的海报。"""
        calls = []

        def fake(url):
            calls.append(url)
            if "/movie/292542?" in url:
                return {"title": "某电影"}          # 该电影无 poster
            if "/movie/292542/images" in url:
                return {"posters": []}
            if "/tv/292542" in url:
                return {"name": "完全不同的剧", "poster_path": "/wrong.jpg"}
            return {"results": []}

        po, match, _ = cr.find_poster(
            {"tmdb": "292542", "type": "Movie",
             "name": "某电影", "year": "2011"}, fake, api_key=KEY)
        self.assertEqual(po, "")
        self.assertIn("不跨类型", match)
        self.assertFalse(any("/tv/292542" in u for u in calls))

    def test_multilanguage_images_fallback(self):
        def fake(url):
            if "/movie/123/images" in url:
                return {"posters": [{"file_path": "/zh.jpg"}]}
            return {"title": "某片"}

        po, match, _ = cr.find_poster(
            {"tmdb": "123", "type": "Movie", "name": "某片"}, fake, api_key=KEY)
        self.assertEqual(po, "/zh.jpg")
        self.assertIn("多语言海报集", match)

    def test_strict_title_match(self):
        def fake(url):
            return {"results": [
                {"media_type": "movie", "title": "另一个名字",
                 "release_date": "2024-01-01", "poster_path": "/wrong.jpg"},
                {"media_type": "movie", "title": "某片",
                 "release_date": "2024-05-01", "poster_path": "/right.jpg"}]}

        po, match, _ = cr.find_poster(
            {"tmdb": "", "type": "Movie", "name": "某片", "year": "2024"},
            fake, api_key=KEY)
        self.assertEqual(po, "/right.jpg")
        self.assertIn("片名", match)

    def test_loose_matching_is_opt_in(self):
        def fake(url):
            return {"results": [{"media_type": "movie", "title": "不相关",
                                 "release_date": "1999-01-01",
                                 "poster_path": "/loose.jpg"}]}

        strict, _, _ = cr.find_poster(
            {"tmdb": "", "type": "Movie", "name": "某片", "year": "2024"},
            fake, api_key=KEY)
        loose, match, _ = cr.find_poster(
            {"tmdb": "", "type": "Movie", "name": "某片", "year": "2024"},
            fake, loose=True, api_key=KEY)
        self.assertEqual(strict, "")
        self.assertEqual(loose, "/loose.jpg")
        self.assertIn("宽松", match)

    def test_missing_key(self):
        po, match, _ = cr.find_poster({"tmdb": "1"}, lambda u: {}, api_key="")
        self.assertEqual(po, "")
        self.assertIn("未配置", match)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.uploads = []

        def tmdb(url):
            return {"title": "某片", "poster_path": "/a.jpg"}

        def fetch(po, size):
            return b"\xff\xd8\xff" + b"x" * 5000

        def upload(iid, body, ctype):
            self.uploads.append((iid, body, ctype))
            return True, "ok"

        self.tmdb, self.fetch, self.upload = tmdb, fetch, upload
        self.items = [{"id": "1", "name": "某片", "type": "Movie",
                       "year": "2024", "tmdb": "123"}]

    def test_encode_for_emby(self):
        body, ctype = cr.encode_for_emby(b"abc")
        self.assertEqual(base64.b64decode(body), b"abc")
        self.assertEqual(ctype, "application/octet-stream")

    def test_dry_run_never_uploads(self):
        out = cr.repair(self.items, self.tmdb, self.fetch, self.upload,
                        dry=True, api_key=KEY)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(out["fixed"], 0)
        self.assertEqual(self.uploads, [])

    def test_real_repair_writes_and_counts(self):
        out = cr.repair(self.items, self.tmdb, self.fetch, self.upload,
                        api_key=KEY)
        self.assertEqual(out["fixed"], 1)
        self.assertEqual(out["failed"], 0)
        self.assertEqual(len(self.uploads), 1)
        iid, body, ctype = self.uploads[0]
        self.assertEqual(iid, "1")
        self.assertEqual(ctype, "application/octet-stream")
        self.assertEqual(base64.b64decode(body)[:3], b"\xff\xd8\xff")

    def test_no_poster_marks_reason_not_fake_success(self):
        out = cr.repair(self.items, lambda u: ({"title": "某片"}
                                               if "/movie/123?" in u else {"posters": []}),
                        self.fetch, self.upload, api_key=KEY)
        self.assertEqual(out["fixed"], 0)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(self.uploads, [])
        self.assertIn("不跨类型", out["details"][0]["msg"])

    def test_upload_failure_counted(self):
        out = cr.repair(self.items, self.tmdb, self.fetch,
                        lambda i, b, c: (False, "Emby HTTP 500"), api_key=KEY)
        self.assertEqual(out["fixed"], 0)
        self.assertEqual(out["failed"], 1)
        self.assertIn("500", out["details"][0]["msg"])

    def test_empty_items(self):
        out = cr.repair([], self.tmdb, self.fetch, self.upload, api_key=KEY)
        self.assertEqual(out["total"], 0)
        self.assertIn("请先扫描", out["msg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
