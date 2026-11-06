# -*- coding: utf-8 -*-
"""实例注册表单测：零凭据、零网络、纯标准库。

运行：python -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

from app.multi_instance import InstanceRegistry, MASK


# 全部为占位值，不含任何真实地址或凭据。
CFG = {
    "emby_key": "your_api_key_here",
    "emby_key2": "your_second_key_here",
}
CONTAINERS = {
    "emby": {"name": "Main", "addr": "host.docker.internal", "port": 8096,
             "cname": "emby-main"},
    "emby_adult": {"name": "Second", "addr": "host.docker.internal", "port": 8896,
                   "cname": "emby-second"},
}
EXTRA = {
    "id": "emby_third", "name": "Third", "short": "third",
    "addr": "127.0.0.1", "port": 8097, "api_key": "PLACEHOLDER_THIRD_KEY",
    "cname": "", "pr_db_path": "/config/data/playback_reporting.db",
    "enabled": True,
}


class RegistryTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.reg = InstanceRegistry(self.path, cfg=CFG, containers=CONTAINERS)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_01_builtin_fold(self):
        """老配置两个内置键自动折算，旧值一字不改。"""
        rows = self.reg.all()
        self.assertEqual([r["id"] for r in rows], ["emby", "emby_adult"])
        self.assertEqual(rows[0]["api_key"], CFG["emby_key"])
        self.assertEqual(rows[1]["api_key"], CFG["emby_key2"])
        self.assertTrue(all(r["builtin"] for r in rows))
        self.assertEqual(CFG["emby_key"], "your_api_key_here")

    def test_02_add_third_instance(self):
        saved = self.reg.save_extra([EXTRA])
        self.assertEqual(len(saved), 1)
        rows = self.reg.all()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["id"], "emby_third")
        self.assertFalse(rows[2]["builtin"])
        self.assertEqual(self.reg.pairs()[2], ("emby_third", "Third"))

    def test_03_public_never_leaks_key(self):
        self.reg.save_extra([EXTRA])
        pub = self.reg.public()
        self.assertEqual(len(pub), 3)
        for p in pub:
            self.assertNotIn("api_key", p)
            self.assertIn("has_key", p)
        self.assertTrue(pub[2]["has_key"])
        self.assertNotIn("PLACEHOLDER_THIRD_KEY", json.dumps(pub))

    def test_04_extra_public_masked(self):
        self.reg.save_extra([EXTRA])
        ex = self.reg.extra_public()
        self.assertEqual(ex[0]["api_key"], MASK)
        self.assertTrue(ex[0]["has_key"])
        self.assertNotIn("PLACEHOLDER_THIRD_KEY", json.dumps(ex))

    def test_05_masked_resave_keeps_original_key(self):
        """编辑表单回传打码值，不得把真实 key 覆盖成星号。"""
        self.reg.save_extra([EXTRA])
        masked = dict(self.reg.extra_public()[0])
        masked.pop("has_key", None)
        self.reg.save_extra([masked])
        self.assertEqual(self.reg.key("emby_third"), "PLACEHOLDER_THIRD_KEY")

    def test_06_empty_key_resave_keeps_original(self):
        self.reg.save_extra([EXTRA])
        blank = dict(self.reg.extra_public()[0])
        blank.pop("has_key", None)
        blank["api_key"] = ""
        self.reg.save_extra([blank])
        self.assertEqual(self.reg.key("emby_third"), "PLACEHOLDER_THIRD_KEY")

    def test_07_invalid_ids_rejected(self):
        bad = [
            {"id": "emby", "api_key": "x"},                 # 内置 id 不可覆盖
            {"id": "emby_adult", "api_key": "x"},
            {"id": "!!!", "api_key": "x"},                  # 清洗后为空 → 自动生成
            {"id": "emby_third", "api_key": "dup"},         # 同 id 去重
            {"id": "emby_third", "api_key": "dup"},
        ]
        saved = self.reg.save_extra(bad)
        ids = [x["id"] for x in saved]
        self.assertNotIn("emby", ids)
        self.assertNotIn("emby_adult", ids)
        self.assertEqual(len([i for i in ids if i == "emby_third"]), 1)
        self.assertEqual(len(self.reg.all()), 4)  # 2 内置 + 2 新增（含自动 id）

    def test_08_conf_and_key_for_extra(self):
        self.reg.save_extra([EXTRA])
        c = self.reg.conf("emby_third")
        self.assertEqual(c["port"], 8097)
        self.assertEqual(c["addr"], "127.0.0.1")
        self.assertEqual(self.reg.key("emby_third"), "PLACEHOLDER_THIRD_KEY")
        self.assertIsNone(self.reg.conf("no_such_instance"))

    def test_09_db_paths_per_instance(self):
        self.reg.save_extra([EXTRA])
        paths = self.reg.db_paths()
        self.assertIn("third", paths)
        self.assertEqual(paths["third"], EXTRA["pr_db_path"])

    def test_10_delete_and_corrupt_file(self):
        self.reg.save_extra([EXTRA])
        self.reg.delete_extra("emby_third")
        self.assertEqual(len(self.reg.all()), 2)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(self.reg.extra(), [])


if __name__ == "__main__":
    unittest.main()
