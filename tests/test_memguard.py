# -*- coding: utf-8 -*-
"""memguard 纯标准库单测：不联网、不读真实 /proc、不依赖任何凭据。"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "app", "memguard.py")
_spec = importlib.util.spec_from_file_location("memguard", _SRC)
mg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mg)

MEMINFO = """MemTotal:       32537904 kB
MemFree:         1520752 kB
MemAvailable:   11124364 kB
Buffers:          120000 kB
SwapTotal:       8388604 kB
SwapFree:          34412 kB
"""
PSI = """some avg10=0.00 avg60=0.01 avg300=0.26 total=48521111
full avg10=3.40 avg60=1.20 avg300=0.25 total=45582208
"""
VMSTAT = "nr_free_pages 1234\noom_kill 7\npswpin 12\n"
LOADAVG = "22.87 22.14 21.81 3/3585 535626"


def reader(mapping):
    return lambda p: mapping.get(p, "")


def files(**kw):
    d = {"/proc/meminfo": MEMINFO, "/proc/pressure/memory": PSI,
         "/proc/vmstat": VMSTAT, "/proc/loadavg": LOADAVG}
    d.update(kw)
    return reader(d)


def snap(level="crit", oom=7, top=None):
    return {"level": level, "oom_kill": oom, "mem": {"pct": 65.8, "avail_gb": 10.6, "total_gb": 31.0},
            "swap": {"pct": 99.6, "used_gb": 8.0, "total_gb": 8.0},
            "psi_full": 3.4, "load": "22.87 / 22.14 / 21.81",
            "top": top if top is not None else [{"name": "framebase", "gb": 1.02}],
            "advice": ["可考虑关闭 / 限流 <b>framebase</b>（占 1.02G）"]}


class ParseTests(unittest.TestCase):
    def test_meminfo(self):
        d = mg.parse_meminfo(MEMINFO)
        self.assertEqual(d["MemTotal"], 32537904)
        self.assertEqual(d["SwapFree"], 34412)

    def test_meminfo_tolerates_garbage(self):
        d = mg.parse_meminfo("no colon here\nMemTotal: nope\nSwapTotal: 8 kB\n")
        self.assertEqual(d.get("SwapTotal"), 8)
        # 无效行直接忽略，由 snapshot() 的 .get(key, 0) 兜底，绝不写入脏值
        self.assertNotIn("MemTotal", d)

    def test_psi(self):
        self.assertEqual(mg.parse_psi(PSI), {"some": 0.0, "full": 3.4})
        self.assertEqual(mg.parse_psi(""), {"some": 0.0, "full": 0.0})


class ConfigTests(unittest.TestCase):
    def test_defaults_and_clamp(self):
        c = mg.normalize_config({"crit_mem": 999, "warn_mem": -5, "cooldown_min": "30",
                                 "enabled": 0, "unknown": "x"})
        self.assertEqual(c["crit_mem"], 240)
        self.assertEqual(c["warn_mem"], 1)
        self.assertEqual(c["cooldown_min"], 30)
        self.assertFalse(c["enabled"])
        self.assertNotIn("unknown", c)

    def test_bad_types_keep_defaults(self):
        c = mg.normalize_config({"crit_mem": "oops", "enabled": None})
        self.assertEqual(c["crit_mem"], mg.DEFAULT_CONFIG["crit_mem"])
        self.assertFalse(c["enabled"])


class LevelTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(mg.level_of(50, 20, 0.1), "ok")

    def test_warn_by_each_signal(self):
        self.assertEqual(mg.level_of(85, 20, 0, ), "warn")
        self.assertEqual(mg.level_of(50, 65, 0), "warn")
        self.assertEqual(mg.level_of(50, 20, 2.5), "warn")

    def test_crit_priority(self):
        self.assertEqual(mg.level_of(95, 20, 0), "crit")
        self.assertEqual(mg.level_of(50, 90, 0), "crit")
        self.assertEqual(mg.level_of(50, 20, 9.0), "crit")
        self.assertEqual(mg.level_of(85, 95, 0), "crit")

    def test_custom_thresholds(self):
        self.assertEqual(mg.level_of(50, 20, 0, {"warn_mem": 40}), "warn")


class AdviceTests(unittest.TestCase):
    def test_excludes_core_and_small(self):
        top = [{"name": "emby-main", "gb": 4.0}, {"name": "media-hub", "gb": 1.2},
               {"name": "framebase", "gb": 0.98}, {"name": "tiny", "gb": 0.3}]
        a = mg.advice_for(top, swap_pct=50)
        self.assertTrue(any("framebase" in x for x in a))
        self.assertFalse(any("emby-main" in x for x in a))
        self.assertFalse(any("tiny" in x for x in a))

    def test_swap_crit_inserted_first(self):
        a = mg.advice_for([], swap_pct=95)
        self.assertIn("Swap 即将打满", a[0])


class SnapshotTests(unittest.TestCase):
    def test_values_and_level(self):
        d = mg.snapshot(read_file=files(), top_containers=lambda: [{"name": "framebase", "gb": 1.02}])
        self.assertTrue(d["ok"])
        self.assertEqual(d["level"], "crit")           # swap 99.6 + PSI full 3.4
        self.assertEqual(d["mem"]["total_gb"], 31.0)
        self.assertEqual(d["oom_kill"], 7)
        self.assertEqual(d["psi_full"], 3.4)
        self.assertTrue(d["load"].startswith("22.87"))
        self.assertEqual(d["top"][0]["name"], "framebase")
        self.assertTrue(d["advice"])

    def test_ok_snapshot_has_no_advice(self):
        d = mg.snapshot(read_file=files(**{"/proc/pressure/memory": "",
                                           "/proc/meminfo": "MemTotal: 10000000 kB\n"
                                                            "MemAvailable: 9000000 kB\n"
                                                            "SwapTotal: 8000000 kB\n"
                                                            "SwapFree: 7900000 kB\n"}))
        self.assertEqual(d["level"], "ok")
        self.assertEqual(d["advice"], [])

    def test_cache_and_force(self):
        cache = {"ts": 0.0, "data": None}
        calls = []

        def rd(p):
            calls.append(p)
            return files()(p)

        a = mg.snapshot(read_file=rd, cache=cache, now=1000.0)
        n = len(calls)
        b = mg.snapshot(read_file=rd, cache=cache, now=1005.0)
        self.assertIs(a, b)
        self.assertEqual(len(calls), n)
        c = mg.snapshot(read_file=rd, cache=cache, now=1006.0, force=True)
        self.assertIsNot(a, c)
        self.assertGreater(len(calls), n)

    def test_reader_failure_never_raises(self):
        d = mg.snapshot(read_file=lambda p: (_ for _ in ()).throw(OSError("nope")),
                        top_containers=lambda: (_ for _ in ()).throw(OSError("nope")))
        self.assertEqual(d["level"], "ok")
        self.assertEqual(d["top"], [])


class PushTests(unittest.TestCase):
    def test_disabled(self):
        st = {"last": 0.0, "level": "ok", "oom": 7}
        self.assertEqual(mg.should_push(st, snap(), {"enabled": False}, now=100), (False, "disabled"))

    def test_ok_resets_level(self):
        st = {"last": 100.0, "level": "crit", "oom": 7}
        self.assertEqual(mg.should_push(st, snap(level="ok"), now=120), (False, "ok"))
        self.assertEqual(st["level"], "ok")

    def test_escalate_then_throttle_then_cooldown(self):
        st = {"last": 0.0, "level": "ok", "oom": 7}
        self.assertEqual(mg.should_push(st, snap(), now=1000), (True, "escalate"))
        self.assertEqual(mg.should_push(st, snap(), now=1300), (False, "throttled"))
        self.assertEqual(mg.should_push(st, snap(), now=1000 + 15 * 60 + 1),
                         (True, "cooldown"))

    def test_new_oom_requires_baseline(self):
        st = {"last": 1000.0, "level": "crit", "oom": 0}
        # 首次计数未建立基线：不能仅因 OOM 推（但等级改变会推，故固定 level）
        self.assertEqual(mg.should_push(st, snap(oom=3), now=1001), (False, "throttled"))
        st2 = {"last": 1000.0, "level": "crit", "oom": 3}
        self.assertEqual(mg.should_push(st2, snap(oom=4), now=1002), (True, "oom"))

    def test_recovery_then_relapse_pushes_again(self):
        st = {"last": 1000.0, "level": "crit", "oom": 7}
        mg.should_push(st, snap(level="ok"), now=1010)
        self.assertEqual(mg.should_push(st, snap(), now=1020), (True, "escalate"))


class MessageTests(unittest.TestCase):
    def test_contains_metrics_and_no_secrets(self):
        t = mg.format_message(snap())
        self.assertIn("99.6%", t)
        self.assertIn("framebase", t)
        self.assertIn("建议现在做", t)
        self.assertNotIn("192.168", t)
        self.assertNotIn("api_key", t)

    def test_test_push_header(self):
        self.assertIn("测试推送", mg.format_message(snap(), test=True))


class MergePlaysTests(unittest.TestCase):
    def test_dedup_cross_server_and_order(self):
        p1 = {"rows": [
            {"ts": "2026-11-08 21:52:57", "item": "A", "server": "emby", "play_sec": 60},
            {"ts": "2026-11-08 20:00:00", "item": "B", "server": "emby", "play_sec": 30}]}
        p2 = {"rows": [
            {"ts": "2026-11-08 21:52:10", "item": "A", "server": "emby_adult"},
            {"ts": "2026-11-08 22:10:00", "item": "C", "server": "emby_adult"}]}
        out = mg.merge_userplays([p1, p2], days=365, limit=120)
        self.assertEqual(out["total"], 3)                     # 同分钟同片被合并
        self.assertEqual([r["item"] for r in out["rows"]], ["C", "A", "B"])
        self.assertEqual(out["rows"][1]["server"], "emby")   # 先到先得

    def test_limit_and_days_clamp(self):
        rows = [{"ts": "2026-11-08 21:%02d:00" % i, "item": "P%d" % i} for i in range(10)]
        out = mg.merge_userplays([{"rows": rows}], days=99999, limit=3)
        self.assertEqual(len(out["rows"]), 3)
        self.assertEqual(out["total"], 10)
        self.assertEqual(out["days"], 3650)

    def test_empty_is_not_error(self):
        out = mg.merge_userplays([], limit=120)
        self.assertFalse(out["ok"])
        self.assertEqual(out["rows"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
