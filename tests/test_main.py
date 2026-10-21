"""main.py 的配置解析 / 状态落盘测试。

本机没装 paho-mqtt 也能跑：先注入一个最小桩模块。
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "paho.mqtt.client" not in sys.modules:
    _paho = types.ModuleType("paho")
    _paho.__path__ = []
    _mqtt = types.ModuleType("paho.mqtt")
    _mqtt.__path__ = []
    _client = types.ModuleType("paho.mqtt.client")

    class _StubClient:  # 只满足 import 阶段
        def __init__(self, *args, **kwargs):
            pass

    class _CallbackAPIVersion:
        VERSION2 = 2

    _client.Client = _StubClient
    _client.CallbackAPIVersion = _CallbackAPIVersion
    _client.MQTTv311 = 4
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _mqtt
    sys.modules["paho.mqtt.client"] = _client

import main  # noqa: E402
from core import Config, ParkingWatcher  # noqa: E402


BASE_ENV = {
    "PARK_LAT": "31.230416",
    "PARK_LON": "121.473701",
    "PARK_PLACE": "测试停车场",
    "DISCOUNT_MINUTES": "180",
    "WARN_MINUTES": "30,10,5",
    "MQTT_HOST": "mqtt.test",
    "MQTT_PORT": "1883",
    "BARK_KEY": "abc123",
}


class ConfigTest(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        with mock.patch.dict(os.environ, BASE_ENV, clear=True):
            cfg, rt = main.build_config()
        self.assertAlmostEqual(cfg.lat, 31.230416)
        self.assertEqual(cfg.discount_minutes, 180)
        self.assertEqual(cfg.warn_minutes, (30.0, 10.0, 5.0))
        self.assertEqual(rt["mqtt_port"], 1883)
        self.assertEqual(rt["bark_key"], "abc123")
        self.assertEqual(rt["tick_seconds"], 15)
        self.assertEqual(rt["status_log_seconds"], 300)

    def test_missing_coordinates_exits(self) -> None:
        env = dict(BASE_ENV)
        env.pop("PARK_LAT")
        env.pop("PARK_LON")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                main.build_config()

    def test_missing_mqtt_host_exits(self) -> None:
        env = dict(BASE_ENV)
        env.pop("MQTT_HOST")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                main.build_config()

    def test_bad_number_exits(self) -> None:
        env = dict(BASE_ENV, DISCOUNT_MINUTES="三小时")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                main.build_config()

    def test_warn_must_be_less_than_discount(self) -> None:
        env = dict(BASE_ENV, WARN_MINUTES="200,10", DISCOUNT_MINUTES="180")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                main.build_config()

    def test_env_bool_variants(self) -> None:
        for raw in ("1", "true", "YES", "on"):
            with mock.patch.dict(os.environ, {"X_FLAG": raw}, clear=True):
                self.assertTrue(main.env_bool("X_FLAG", False))
        with mock.patch.dict(os.environ, {"X_FLAG": "0"}, clear=True):
            self.assertFalse(main.env_bool("X_FLAG", True))

    def test_old_duration_name_is_supported(self) -> None:
        env = dict(BASE_ENV)
        env.pop("DISCOUNT_MINUTES")
        env["FREE_MINUTES"] = "120"
        with mock.patch.dict(os.environ, env, clear=True):
            cfg, _ = main.build_config()
        self.assertEqual(cfg.discount_minutes, 120)


class RetainedMessageTest(unittest.TestCase):
    def new_car(self) -> dict:
        return {"lat": None, "lon": None, "shift": None, "state": "", "fix_live": False}

    def test_retained_snapshot_only_caches(self) -> None:
        car = self.new_car()
        self.assertFalse(main.update_car(car, "latitude", "31.230416", True))
        self.assertFalse(main.update_car(car, "longitude", "121.473701", True))
        self.assertFalse(main.update_car(car, "shift_state", "p", True))
        self.assertEqual(car["lat"], 31.230416)
        self.assertEqual(car["lon"], 121.473701)
        self.assertEqual(car["shift"], "P")
        self.assertFalse(car["fix_live"])  # 旧快照不能当作实时位置开场

    def test_live_position_triggers_judgement(self) -> None:
        car = self.new_car()
        main.update_car(car, "latitude", "31.230416", True)
        main.update_car(car, "longitude", "121.473701", True)
        self.assertTrue(main.update_car(car, "latitude", "31.230417", False))
        self.assertTrue(car["fix_live"])
        self.assertTrue(main.update_car(car, "shift_state", "P", False))

    def test_location_json_topic(self) -> None:
        car = self.new_car()
        payload = '{"latitude":31.230416,"longitude":121.473701}'
        self.assertFalse(main.update_car(car, "location", payload, True))
        self.assertFalse(car["fix_live"])
        self.assertTrue(main.update_car(car, "location", payload, False))
        self.assertAlmostEqual(car["lat"], 31.230416)
        self.assertAlmostEqual(car["lon"], 121.473701)
        self.assertTrue(car["fix_live"])

    def test_location_bad_payload_is_ignored(self) -> None:
        car = self.new_car()
        self.assertFalse(main.update_car(car, "location", "not json", False))
        self.assertIsNone(car["lat"])

    def test_state_message_does_not_trigger_position_judgement(self) -> None:
        car = self.new_car()
        self.assertFalse(main.update_car(car, "state", "online", False))
        self.assertEqual(car["state"], "online")


class StateFileTest(unittest.TestCase):
    def test_save_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "parking_state.json"
            watcher = ParkingWatcher(
                Config(lat=31.230416, lon=121.473701, discount_minutes=180, tz="Asia/Shanghai")
            )
            watcher.observe(1_700_000_000, 31.230416, 121.473701, "P")
            watcher.observe(1_700_000_100, 31.230416, 121.473701, "P")
            main.save_state(path, watcher)
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "INSIDE")

            fresh = ParkingWatcher(
                Config(lat=31.230416, lon=121.473701, discount_minutes=180, tz="Asia/Shanghai")
            )
            self.assertTrue(main.load_state(path, fresh, 1_700_000_200))
            self.assertTrue(fresh.inside)
            self.assertEqual(fresh.entry_ts, 1_700_000_000)

    def test_missing_file_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            watcher = ParkingWatcher(Config(lat=1.0, lon=2.0))
            self.assertFalse(main.load_state(Path(tmp) / "nope.json", watcher, 0.0))

    def test_corrupt_file_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            watcher = ParkingWatcher(Config(lat=1.0, lon=2.0))
            self.assertFalse(main.load_state(path, watcher, 0.0))

    def test_stale_session_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "state": "INSIDE",
                        "entry_ts": 1_000_000,
                        "fired": [],
                    }
                ),
                encoding="utf-8",
            )
            watcher = ParkingWatcher(Config(lat=1.0, lon=2.0, max_session_hours=14))
            self.assertFalse(main.load_state(path, watcher, 1_000_000 + 3600 * 48))
            self.assertFalse(watcher.inside)


class QueueTest(unittest.TestCase):
    def test_put_latest_drops_oldest(self) -> None:
        import queue

        from core import Event

        box: "queue.Queue[Event]" = queue.Queue(maxsize=2)
        for i in range(5):
            main.put_latest(box, Event("k", f"t{i}", "b"))
        self.assertEqual(box.qsize(), 2)
        self.assertEqual(box.get().title, "t3")


if __name__ == "__main__":
    unittest.main()
