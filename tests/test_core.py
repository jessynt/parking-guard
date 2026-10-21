import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import (  # noqa: E402
    Config,
    ParkingWatcher,
    distance_m,
    format_duration,
)
from notify import BarkNotifier  # noqa: E402

T0 = 1_700_000_000.0  # 固定时间戳，便于断言
LAT = 31.230416
LON = 121.473701
M_PER_DEG_LAT = 111_320.0


def offset_lat(lat: float, meters: float) -> float:
    return lat + meters / M_PER_DEG_LAT


def make_cfg(**overrides) -> Config:
    params = dict(
        lat=LAT,
        lon=LON,
        place="测试停车场",
        enter_radius_m=80.0,
        exit_radius_m=150.0,
        confirm_seconds=90.0,
        discount_minutes=180.0,
        warn_minutes=(30.0, 10.0, 5.0),
        max_session_hours=14.0,
        tz="Asia/Shanghai",
    )
    params.update(overrides)
    return Config(**params)


def enter(w: ParkingWatcher, now: float = T0, shift: str = "P") -> None:
    """把车开进围栏并熬过确认时间。"""
    w.observe(now, LAT, LON, shift)
    w.observe(now + 91, LAT, LON, shift)


class DistanceTest(unittest.TestCase):
    def test_one_degree_latitude(self) -> None:
        self.assertAlmostEqual(distance_m(0, 0, 1, 0), 111_195, delta=500)

    def test_small_offset(self) -> None:
        d = distance_m(LAT, LON, offset_lat(LAT, 100), LON)
        self.assertAlmostEqual(d, 100, delta=2)


class DurationTest(unittest.TestCase):
    def test_formats(self) -> None:
        self.assertEqual(format_duration(0), "0分")
        self.assertEqual(format_duration(59), "0分")
        self.assertEqual(format_duration(60), "1分")
        self.assertEqual(format_duration(3600), "1小时")
        self.assertEqual(format_duration(3600 + 25 * 60), "1小时25分")
        self.assertEqual(format_duration(2 * 3600 + 58 * 60 + 30), "2小时58分")


class EntryTest(unittest.TestCase):
    def test_entry_needs_confirmation(self) -> None:
        w = ParkingWatcher(make_cfg())
        self.assertEqual(w.observe(T0, LAT, LON, "P"), [])
        self.assertEqual(w.state, "PENDING")
        self.assertEqual(w.observe(T0 + 60, LAT, LON, "P"), [])
        events = w.observe(T0 + 90, LAT, LON, "P")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "entered")
        self.assertTrue(w.inside)
        self.assertEqual(w.entry_ts, T0)  # 入场时间取首次进入围栏的时刻

    def test_entry_confirmed_by_tick_without_new_position(self) -> None:
        """车辆停稳后 TeslaMate 可能不再发位置，确认必须由 tick 推进。"""
        w = ParkingWatcher(make_cfg())
        self.assertEqual(w.observe(T0, LAT, LON, "P"), [])
        self.assertEqual(w.state, "PENDING")
        self.assertEqual(w.tick(T0 + 89), [])
        self.assertEqual(w.state, "PENDING")
        events = w.tick(T0 + 90)
        self.assertEqual([e.kind for e in events], ["entered"])
        self.assertTrue(w.inside)
        self.assertEqual(w.entry_ts, T0)

    def test_tick_entry_then_warn_ladder(self) -> None:
        w = ParkingWatcher(make_cfg())
        w.observe(T0, LAT, LON, "P")
        self.assertEqual([e.kind for e in w.tick(T0 + 95)], ["entered"])
        self.assertEqual([e.kind for e in w.tick(T0 + 9000)], ["warn30"])
        self.assertEqual([e.kind for e in w.tick(T0 + 10200)], ["warn10"])

    def test_tick_confirms_with_last_known_jittered_fix(self) -> None:
        w = ParkingWatcher(make_cfg())
        w.observe(T0, LAT, LON, "P")
        w.observe(T0 + 60, offset_lat(LAT, 120), LON, "P")  # 80~150m 之间，未离开
        events = w.tick(T0 + 95)
        self.assertEqual([e.kind for e in events], ["entered"])
        self.assertEqual(w.entry_ts, T0)

    def test_status_text(self) -> None:
        w = ParkingWatcher(make_cfg())
        self.assertEqual(w.status_text(T0), "场外")
        w.observe(T0, LAT, LON, "P")
        self.assertIn("候选确认中", w.status_text(T0 + 30))
        w.tick(T0 + 90)
        self.assertIn("场内", w.status_text(T0 + 3600))
        self.assertIn("优惠还剩", w.status_text(T0 + 3600))

    def test_driving_through_does_not_start(self) -> None:
        w = ParkingWatcher(make_cfg())
        self.assertEqual(w.observe(T0, LAT, LON, "D"), [])
        self.assertEqual(w.state, "OUTSIDE")

    def test_leaving_park_gear_cancels_confirmation(self) -> None:
        w = ParkingWatcher(make_cfg())
        w.observe(T0, LAT, LON, "P")
        w.observe(T0 + 30, LAT, LON, "D")
        self.assertEqual(w.state, "OUTSIDE")

    def test_messages_use_discount_wording(self) -> None:
        w = ParkingWatcher(make_cfg())
        enter(w)
        events = w.tick(T0 + 9_000)
        self.assertIn("优惠", events[0].body)
        self.assertNotIn("免费", events[0].body)

    def test_pass_through_leaves_pending(self) -> None:
        w = ParkingWatcher(make_cfg())
        w.observe(T0, LAT, LON, "P")
        self.assertEqual(w.state, "PENDING")
        w.observe(T0 + 20, offset_lat(LAT, 300), LON, "P")  # 只是路过
        self.assertEqual(w.state, "OUTSIDE")
        self.assertFalse(w.inside)

    def test_jitter_between_radii_keeps_candidate(self) -> None:
        w = ParkingWatcher(make_cfg())
        w.observe(T0, LAT, LON, "P")
        w.observe(T0 + 30, offset_lat(LAT, 110), LON, "P")  # 80~150m 之间抖动
        self.assertEqual(w.state, "PENDING")
        events = w.observe(T0 + 95, LAT, LON, "P")
        self.assertEqual([e.kind for e in events], ["entered"])
        self.assertEqual(w.entry_ts, T0)

    def test_jitter_after_entry_does_not_exit(self) -> None:
        w = ParkingWatcher(make_cfg())
        enter(w)
        for i, metres in enumerate((90, 120, 99, 140, 80)):
            self.assertEqual(w.observe(T0 + 200 + i * 30, offset_lat(LAT, metres), LON, "P"), [])
        self.assertTrue(w.inside)

    def test_bad_fix_is_ignored(self) -> None:
        w = ParkingWatcher(make_cfg())
        self.assertEqual(w.observe(T0, None, None, "P"), [])
        self.assertEqual(w.observe(T0, 0.0, 0.0, "P"), [])
        self.assertEqual(w.observe(T0, 999, 999, "P"), [])


class WarnTest(unittest.TestCase):
    def test_warn_ladder(self) -> None:
        w = ParkingWatcher(make_cfg())
        enter(w)
        self.assertEqual(w.tick(T0 + 8000), [])  # 剩 46 分钟
        e1 = w.tick(T0 + 9000)  # 剩 30 分钟
        self.assertEqual([e.kind for e in e1], ["warn30"])
        self.assertEqual(w.tick(T0 + 9010), [])  # 不重复
        e2 = w.tick(T0 + 10200)  # 剩 10 分钟
        self.assertEqual([e.kind for e in e2], ["warn10"])
        e3 = w.tick(T0 + 10500)  # 剩 5 分钟，最后一档
        self.assertEqual([e.kind for e in e3], ["warn5"])
        self.assertEqual(e3[0].level, "critical")
        self.assertIn("最后提醒", e3[0].title)
        self.assertEqual(w.tick(T0 + 10801), [])  # 到点后不再提醒
        self.assertEqual(w.tick(T0 + 20000), [])

    def test_restart_after_deadline_is_silent(self) -> None:
        w = ParkingWatcher(make_cfg())
        enter(w)
        self.assertEqual(w.tick(T0 + 20000), [])  # 已经超时，不再补发
        self.assertEqual(w.fired, {30.0, 10.0, 5.0})

    def test_no_events_when_outside(self) -> None:
        w = ParkingWatcher(make_cfg())
        self.assertEqual(w.tick(T0 + 99999), [])


class ExitTest(unittest.TestCase):
    def test_exit_reports_duration(self) -> None:
        w = ParkingWatcher(make_cfg())
        enter(w)
        events = w.observe(T0 + 3600, offset_lat(LAT, 400), LON, "D")
        self.assertEqual([e.kind for e in events], ["exited"])
        self.assertIn("1小时", events[0].body)
        self.assertFalse(w.inside)
        self.assertIsNone(w.entry_ts)
        self.assertEqual(w.tick(T0 + 7200), [])

    def test_exit_after_overstay(self) -> None:
        w = ParkingWatcher(make_cfg())
        enter(w)
        events = w.observe(T0 + 4 * 3600, offset_lat(LAT, 400), LON, "D")
        self.assertEqual([e.kind for e in events], ["exited"])
        self.assertIn("超出优惠", events[0].title)


class PersistenceTest(unittest.TestCase):
    def test_roundtrip_keeps_timer(self) -> None:
        w = ParkingWatcher(make_cfg())
        enter(w)
        w.tick(T0 + 9000)  # warn30 已发
        snapshot = w.to_dict()

        w2 = ParkingWatcher(make_cfg())
        self.assertTrue(w2.restore(snapshot))
        self.assertTrue(w2.inside)
        self.assertEqual(w2.entry_ts, T0)
        self.assertEqual(w2.fired, {30.0})
        self.assertEqual(w2.tick(T0 + 9010), [])  # 不会重复提醒
        self.assertEqual([e.kind for e in w2.tick(T0 + 10200)], ["warn10"])

    def test_outside_snapshot(self) -> None:
        w = ParkingWatcher(make_cfg())
        w2 = ParkingWatcher(make_cfg())
        self.assertFalse(w2.restore(w.to_dict()))
        self.assertFalse(w2.inside)

    def test_corrupt_snapshot(self) -> None:
        w = ParkingWatcher(make_cfg())
        self.assertFalse(w.restore({}))
        self.assertFalse(w.restore({"version": 1, "state": "INSIDE"}))
        self.assertFalse(w.inside)


class BarkTest(unittest.TestCase):
    def test_url_encoding_and_params(self) -> None:
        n = BarkNotifier("https://api.day.app/", "KEY123", group="停车场", sound="alarm", level="timeSensitive")
        url = n.build_url("已进入停车场", "优惠 180 分钟，21:30 到期")
        self.assertTrue(url.startswith("https://api.day.app/KEY123/"))
        self.assertNotIn("已进入", url)  # 中文被转义
        self.assertIn("%E5%81%9C", url)
        self.assertIn("group=%E5%81%9C%E8%BD%A6%E5%9C%BA", url)
        self.assertIn("level=timeSensitive", url)
        self.assertIn("isArchive=1", url)

    def test_critical_adds_volume(self) -> None:
        n = BarkNotifier("https://api.day.app", "k")
        url = n.build_url("t", "b", level="critical")
        self.assertIn("level=critical", url)
        self.assertIn("volume=10", url)

    def test_extra_query_passthrough(self) -> None:
        n = BarkNotifier("https://bark.example.com", "k", extra_query="call=1&icon=https://x/y.png")
        url = n.build_url("a", "b")
        self.assertIn("call=1", url)
        self.assertIn("icon=", url)

    def test_missing_key_fails_fast(self) -> None:
        n = BarkNotifier("https://api.day.app", "")
        from core import Event

        self.assertFalse(n.send(Event("t", "t", "b")))


if __name__ == "__main__":
    unittest.main()
