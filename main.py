#!/usr/bin/env python3
"""TeslaMate MQTT 接入和 Bark 通知入口。"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt

from core import Config, Event, ParkingWatcher, distance_m
from notify import BarkNotifier

log = logging.getLogger("parking-guard")

TRUTHY = {"1", "true", "yes", "on", "y"}


def env_value(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None

def env_str(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = env_value(name) or default
    if value is None or (required and not value):
        raise SystemExit(f"缺少环境变量 {name}")
    return value


def env_float(name: str, default: float, *fallbacks: str) -> float:
    raw = env_value(name, *fallbacks)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"环境变量 {name}={raw!r} 不是数字")


def env_bool(name: str, default: bool) -> bool:
    raw = env_value(name)
    if raw is None:
        return default
    return raw.lower() in TRUTHY


def env_float_list(name: str, default: Tuple[float, ...]) -> Tuple[float, ...]:
    raw = env_value(name)
    if raw is None:
        return default
    try:
        return tuple(float(item) for item in raw.replace("，", ",").split(",") if item.strip())
    except ValueError:
        raise SystemExit(f"环境变量 {name}={raw!r} 解析失败，应为 30,10,5 这样的列表")


def build_config() -> Tuple[Config, Dict[str, Any]]:
    try:
        cfg = Config(
            lat=env_float("PARK_LAT", 0.0),
            lon=env_float("PARK_LON", 0.0),
            place=env_str("PARK_PLACE", "停车场"),
            enter_radius_m=env_float("PARK_RADIUS_ENTER", 80.0),
            exit_radius_m=env_float("PARK_RADIUS_EXIT", 150.0),
            confirm_seconds=env_float("PARK_CONFIRM_SECONDS", 90.0),
            require_park=env_bool("PARK_REQUIRE_P", True),
            accept_unknown_shift=env_bool("PARK_ACCEPT_UNKNOWN_SHIFT", True),
            discount_minutes=env_float("DISCOUNT_MINUTES", 180.0, "FREE_MINUTES"),
            warn_minutes=env_float_list("WARN_MINUTES", (30.0, 10.0, 5.0)),
            exit_buffer_minutes=env_float("EXIT_BUFFER_MINUTES", 10.0),
            max_session_hours=env_float("MAX_SESSION_HOURS", 14.0),
            tz=env_str("TZ", "Asia/Shanghai"),
        )
    except ValueError as exc:
        raise SystemExit(f"配置有误: {exc}") from exc
    if cfg.lat == 0.0 and cfg.lon == 0.0:
        raise SystemExit("请设置 PARK_LAT / PARK_LON（停车场坐标）")

    runtime = {
        "mqtt_host": env_str("MQTT_HOST", required=True),
        "mqtt_port": int(env_float("MQTT_PORT", 1883)),
        "mqtt_username": env_str("MQTT_USERNAME", ""),
        "mqtt_password": env_str("MQTT_PASSWORD", ""),
        "mqtt_tls": env_bool("MQTT_TLS", False),
        "mqtt_tls_insecure": env_bool("MQTT_TLS_INSECURE", False),
        "mqtt_prefix": env_str("MQTT_PREFIX", "teslamate/cars").rstrip("/"),
        "car_id": env_str("CAR_ID", "1"),
        "state_file": Path(env_str("STATE_FILE", "/data/parking_state.json")),
        "tick_seconds": max(5.0, env_float("TICK_SECONDS", 15.0)),
        "status_log_seconds": max(0.0, env_float("STATUS_LOG_SECONDS", 300.0)),
        "bark_server": env_str("BARK_SERVER", "https://api.day.app"),
        "bark_key": env_str("BARK_KEY", ""),
        "bark_group": env_str("BARK_GROUP", "停车场"),
        "bark_sound": env_str("BARK_SOUND", "alarm"),
        "bark_level": env_str("BARK_LEVEL", "timeSensitive"),
        "bark_extra": env_str("BARK_EXTRA_QUERY", ""),
    }
    return cfg, runtime


def load_state(path: Path, watcher: ParkingWatcher, now: float) -> bool:
    if not path.exists():
        log.debug("状态文件不存在：%s", path)
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("状态文件不可读，忽略: %s", exc)
        return False
    log.debug("读取状态文件：%s（%s）", path, data)
    restored = watcher.restore(data)
    if not restored and watcher.state == "PENDING":
        watcher.reset()
    if restored and watcher.entry_ts is not None:
        age_h = (now - watcher.entry_ts) / 3600.0
        limit = watcher.cfg.max_session_hours
        if age_h < 0 or (limit > 0 and age_h > limit):
            log.warning("恢复的会话已存在 %.1f 小时，视为过期，重新开始", age_h)
            watcher.reset()
            return False
        remaining = watcher.remaining_seconds(now) or 0.0
        log.info(
            "恢复进行中的会话：入场 %.0f 秒前，剩余 %.0f 分钟，已发提醒 %s",
            now - watcher.entry_ts,
            remaining / 60.0,
            sorted(watcher.fired),
        )
    return restored


def save_state(path: Path, watcher: ParkingWatcher) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(watcher.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        log.debug(
            "保存状态：%s（state=%s entry_ts=%s pending_since=%s fired=%s）",
            path, watcher.state, watcher.entry_ts, watcher.pending_since, sorted(watcher.fired),
        )
    except OSError as exc:
        log.warning("状态保存失败: %s", exc)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg, rt = build_config()
    now = time.time()
    watcher = ParkingWatcher(cfg)
    rmasked = "***" if rt["bark_key"] else "(未配置)"
    log.info(
        "启动：%s (%.6f, %.6f) 半径 %gm/%gm，优惠 %g 分钟，提醒 %s 分钟，MQTT %s:%s car=%s，Bark %s %s",
        cfg.place, cfg.lat, cfg.lon, cfg.enter_radius_m, cfg.exit_radius_m,
        cfg.discount_minutes, list(cfg.warn_minutes), rt["mqtt_host"], rt["mqtt_port"],
        rt["car_id"], rt["bark_server"], rmasked,
    )
    log.info(
        "入场判定：连续 %.0f 秒；必须 P 挡=%s；未知挡位按停车=%s；状态文件 %s",
        cfg.confirm_seconds, cfg.require_park, cfg.accept_unknown_shift, rt["state_file"],
    )
    log.debug(
        "运行参数：tick=%.0fs status_log=%.0fs mqtt_user=%r tls=%s "
        "bark_group=%r level=%r sound=%r",
        rt["tick_seconds"], rt["status_log_seconds"], rt["mqtt_username"] or None,
        rt["mqtt_tls"], rt["bark_group"], rt["bark_level"], rt["bark_sound"],
    )

    load_state(rt["state_file"], watcher, now)

    notifier = BarkNotifier(
        server=rt["bark_server"],
        device_key=rt["bark_key"],
        group=rt["bark_group"],
        sound=rt["bark_sound"],
        level=rt["bark_level"],
        extra_query=rt["bark_extra"],
    )

    outbox: "queue.Queue[Event]" = queue.Queue(maxsize=200)
    stop = threading.Event()
    lock = threading.Lock()

    def worker() -> None:
        while not stop.is_set():
            try:
                event = outbox.get(timeout=1.0)
            except queue.Empty:
                continue
            log.debug("推送事件 [%s]，队列剩余 %d", event.kind, outbox.qsize())
            try:
                notifier.send(event)
            except Exception as exc:  # noqa: BLE001 - 推送失败不影响计时
                log.exception("推送异常: %s", exc)
            finally:
                outbox.task_done()

    # 位置缓存：两类主题分开到达，需要拼在一起判定。
    # fix_live 表示经纬度来自实时上报（而不是 retained 快照），避免用旧坐标开场。
    car: Dict[str, Any] = {
        "lat": None, "lon": None, "shift": None, "state": "", "fix_live": False,
        "first_fix_logged": False,
    }

    topics = [
        f"{rt['mqtt_prefix']}/{rt['car_id']}/{suffix}"
        for suffix in ("latitude", "longitude", "location", "shift_state", "state")
    ]

    def on_connect(client, userdata, flags, rc, properties=None) -> None:  # noqa: ANN001
        log.debug("on_connect：rc=%r flags=%r properties=%r", rc, flags, properties)
        if int(getattr(rc, "value", rc)) == 0:
            log.info("MQTT 已连接，订阅 %s", ", ".join(topics))
            result = client.subscribe([(t, 0) for t in topics])
            log.debug("订阅请求已发送：%r", result)
        else:
            log.error("MQTT 连接被拒绝 rc=%s（检查用户名/密码）", rc)

    def on_disconnect(client, userdata, *args) -> None:  # noqa: ANN001
        log.warning("MQTT 断开，paho 将自动重连（%r）", args)

    def handle_message(client, userdata, msg) -> None:  # noqa: ANN001
        suffix = msg.topic.rsplit("/", 1)[-1]
        payload = msg.payload.decode("utf-8", "replace").strip()
        retained = bool(msg.retain)
        log.debug(
            "MQTT 收到 %s（retain=%s qos=%s）：%s",
            msg.topic, retained, getattr(msg, "qos", "?"), shorten(payload),
        )
        events: List[Event] = []
        with lock:
            before_state = car["state"]
            should_judge = update_car(car, suffix, payload, retained)
            log.debug(
                "缓存：lat=%r lon=%r shift=%r state=%r fix_live=%s",
                car["lat"], car["lon"], car["shift"], car["state"], car["fix_live"],
            )
            if suffix == "state" and not retained and payload != before_state:
                log.info("车辆状态: %s", payload)
            if retained:
                # TeslaMate 的所有主题都用 retain 发布。订阅时收到的 retained
                # 快照只用于补齐缓存，不能当成“刚刚发生”的位置来开场。
                log.info("MQTT 保留值（仅缓存，不判定）：%s = %s", suffix, payload or "(空)")
                return
            if not should_judge:
                log.debug("消息不参与位置判定：suffix=%s", suffix)
                return

            has_fix = car["lat"] is not None and car["lon"] is not None
            if not has_fix:
                log.debug("等待另一侧坐标：lat=%r lon=%r", car["lat"], car["lon"])
                return
            if not car["fix_live"]:
                log.debug("坐标仅来自 retained 快照，等待实时位置后再判定")
                return

            before = watcher.state
            dist = distance_m(car["lat"], car["lon"], cfg.lat, cfg.lon)
            shift = car["shift"] or "未知"
            events = watcher.observe(time.time(), car["lat"], car["lon"], car["shift"])
            after = watcher.state
            log.debug(
                "位置判定完成：状态 %s -> %s，产生 %d 个事件", before, after, len(events)
            )
            if not car["first_fix_logged"]:
                log.info("收到实时位置：距停车点 %.0fm，挡位 %s", dist, shift)
                car["first_fix_logged"] = True
            if before == "OUTSIDE" and after == "PENDING":
                log.info(
                    "进入 %.0fm 围栏：距停车点 %.0fm，挡位 %s，等待 %.0f 秒确认",
                    cfg.enter_radius_m, dist, shift, cfg.confirm_seconds,
                )
            elif before == "PENDING" and after == "OUTSIDE":
                log.info("候选取消：距停车点 %.0fm，挡位 %s", dist, shift)
            elif after == "OUTSIDE":
                log.debug("位置更新：距停车点 %.0fm，挡位 %s", dist, shift)
            if events:
                save_state(rt["state_file"], watcher)
        for event in events:
            log.info("事件 %s", event)
            put_latest(outbox, event)

    def on_message(client, userdata, msg) -> None:  # noqa: ANN001
        """包一层异常处理：单条消息出错不能打断 paho 的网络循环。"""
        try:
            handle_message(client, userdata, msg)
        except Exception:  # noqa: BLE001
            log.exception("处理 MQTT 消息失败：%s", msg.topic)

    client = make_client(f"parking-guard-{rt['car_id']}")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    if log.isEnabledFor(logging.DEBUG) and hasattr(client, "enable_logger"):
        # DEBUG 时打开 paho 协议日志：CONNECT/SUBSCRIBE/PING、QoS、重连原因
        client.enable_logger(log)
    if rt["mqtt_username"]:
        client.username_pw_set(rt["mqtt_username"], rt["mqtt_password"] or None)
    if rt["mqtt_tls"]:
        import ssl

        client.tls_set(
            cert_reqs=ssl.CERT_NONE if rt["mqtt_tls_insecure"] else ssl.CERT_REQUIRED
        )
        client.tls_insecure_set(bool(rt["mqtt_tls_insecure"]))

    def shutdown(signum, frame) -> None:  # noqa: ANN001
        log.info("收到信号 %s，退出", signum)
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threading.Thread(target=worker, name="bark-worker", daemon=True).start()

    try:
        client.connect(rt["mqtt_host"], rt["mqtt_port"], keepalive=60)
    except Exception as exc:  # noqa: BLE001
        log.error("MQTT 首次连接失败（会持续重试）: %s", exc)
    client.loop_start()

    last_periodic_save = time.time()
    last_status_log = time.time()
    while not stop.is_set():
        current = time.time()
        status: Optional[
            Tuple[str, Optional[Tuple[float, float]], bool, Optional[str], Optional[float]]
        ] = None
        with lock:
            events = watcher.tick(current)
            if events:
                save_state(rt["state_file"], watcher)
            fix = watcher.last_fix
            if fix is None and car["lat"] is not None and car["lon"] is not None:
                fix = (car["lat"], car["lon"])
            if log.isEnabledFor(logging.DEBUG):
                remaining = watcher.remaining_seconds(current)
                log.debug(
                    "tick：state=%s 距停车点=%s 剩余=%s pending_since=%s fired=%s 队列=%d",
                    watcher.state,
                    f"{distance_m(fix[0], fix[1], cfg.lat, cfg.lon):.0f}m" if fix else "无位置",
                    f"{remaining / 60:.1f}分" if remaining is not None else "-",
                    watcher.pending_since,
                    sorted(watcher.fired),
                    outbox.qsize(),
                )
            if rt["status_log_seconds"] > 0 and current - last_status_log >= rt["status_log_seconds"]:
                last_status_log = current
                status = (
                    watcher.status_text(current),
                    fix,
                    car["fix_live"],
                    car["shift"],
                    (current - watcher.last_fix_ts) if watcher.last_fix_ts else None,
                )
        for event in events:
            log.info("事件 %s", event)
            put_latest(outbox, event)
        if status is not None:
            text, fix, fix_live, shift, fix_age = status
            if fix is None:
                log.warning(
                    "状态：%s（仍未收到经纬度；检查 CAR_ID / MQTT_PREFIX 与 TeslaMate 的 MQTT 发布）",
                    text,
                )
            elif not fix_live:
                log.info(
                    "状态：%s，距停车点 %.0fm（仅收到 retained 快照，等待实时位置）",
                    text,
                    distance_m(fix[0], fix[1], cfg.lat, cfg.lon),
                )
            else:
                age_note = (
                    f"，位置已 {fix_age / 60:.0f} 分钟未更新"
                    if fix_age is not None and fix_age >= 600
                    else ""
                )
                log.info(
                    "状态：%s，距停车点 %.0fm，挡位 %s%s",
                    text,
                    distance_m(fix[0], fix[1], cfg.lat, cfg.lon),
                    shift or "未知",
                    age_note,
                )
        if current - last_periodic_save >= 300:
            with lock:
                save_state(rt["state_file"], watcher)
            last_periodic_save = current
        stop.wait(rt["tick_seconds"])

    client.loop_stop()
    try:
        client.disconnect()
    except Exception:  # noqa: BLE001
        pass
    with lock:
        save_state(rt["state_file"], watcher)
    log.info("已退出")
    return 0


def to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def shorten(value: str, limit: int = 200) -> str:
    """日志里截断长 payload，避免一行刷屏。"""
    return value if len(value) <= limit else f"{value[:limit]}...（共 {len(value)} 字符）"


def update_car(car: Dict[str, Any], suffix: str, payload: str, retained: bool) -> bool:
    """更新车辆缓存，返回这条消息是否应触发位置判定。

    retained 消息只补齐缓存：TeslaMate 用 retain 发布所有主题，订阅时收到的
    快照可能是几小时前的坐标，不能按“当前时间”开场。
    """
    if suffix == "latitude":
        car["lat"] = to_float(payload)
    elif suffix == "longitude":
        car["lon"] = to_float(payload)
    elif suffix == "location":
        # TeslaMate 新版把坐标合并成 JSON；旧版仍单独发 latitude/longitude。
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        car["lat"] = to_float(data.get("latitude"))
        car["lon"] = to_float(data.get("longitude"))
    elif suffix == "shift_state":
        car["shift"] = payload.upper() if payload else None
    elif suffix == "state":
        car["state"] = payload
    else:
        return False
    if retained:
        return False
    if suffix in {"latitude", "longitude", "location"}:
        car["fix_live"] = True
    return suffix in {"latitude", "longitude", "location", "shift_state"}


def put_latest(box: "queue.Queue[Event]", event: Event) -> None:
    """入队；队列满时丢弃最旧的一条，保证最新提醒不丢。"""
    try:
        box.put_nowait(event)
    except queue.Full:
        log.warning("通知队列已满，丢弃最旧事件以放入 [%s]（size=%d）", event.kind, box.qsize())
        try:
            box.get_nowait()
            box.task_done()
        except queue.Empty:
            pass
        try:
            box.put_nowait(event)
        except queue.Full:
            log.error("通知队列仍满，事件 [%s] 未能入队", event.kind)


def make_client(client_id: str) -> "mqtt.Client":
    """兼容 paho-mqtt 1.x / 2.x。"""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)


if __name__ == "__main__":
    sys.exit(main())
