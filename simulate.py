#!/usr/bin/env python3
"""本地加速模拟一段停车过程。

    python simulate.py                    # 只打印事件
    BARK_KEY=xxx python simulate.py       # 同时真的推送到手机
    SPEED=120 python simulate.py          # 加速倍数（默认 600，200 分钟约 20 秒跑完）
    EXIT_AFTER_MIN=170 python simulate.py # 改出场时间，验证「提前离场」文案

坐标默认取一组演示值，可用 PARK_LAT / PARK_LON 覆盖。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

from core import Config, Event, ParkingWatcher
from notify import BarkNotifier

if hasattr(sys.stdout, "reconfigure"):
    try:  # Windows 控制台默认 GBK，中文会乱码
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

STEP_SECONDS = 15.0


def env_float(name: str, default: float, *fallbacks: str) -> float:
    raw = next((os.getenv(key) for key in (name, *fallbacks) if os.getenv(key, "").strip()), None)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        raise SystemExit(f"{name}={raw!r} 不是数字")


def env_warns() -> tuple[float, ...]:
    raw = os.getenv("WARN_MINUTES", "30,10,5")
    return tuple(float(item) for item in raw.split(",") if item.strip())


def main() -> int:
    lat = env_float("PARK_LAT", 31.230416)
    lon = env_float("PARK_LON", 121.473701)
    exit_after_min = env_float("EXIT_AFTER_MIN", 200.0)
    speed = max(0.0, env_float("SPEED", 600.0))
    key = os.getenv("BARK_KEY", "").strip()

    cfg = Config(
        lat=lat,
        lon=lon,
        place=os.getenv("PARK_PLACE", "演示停车场"),
        enter_radius_m=env_float("PARK_RADIUS_ENTER", 80.0),
        exit_radius_m=env_float("PARK_RADIUS_EXIT", 150.0),
        confirm_seconds=env_float("PARK_CONFIRM_SECONDS", 90.0),
        discount_minutes=env_float("DISCOUNT_MINUTES", 180.0, "FREE_MINUTES"),
        warn_minutes=env_warns(),
    )
    watcher = ParkingWatcher(cfg)
    notifier = (
        BarkNotifier(
            server=os.getenv("BARK_SERVER", "https://api.day.app"),
            device_key=key,
            group=os.getenv("BARK_GROUP", "停车场"),
            sound=os.getenv("BARK_SOUND", "alarm"),
            level=os.getenv("BARK_LEVEL", "timeSensitive"),
        )
        if key
        else None
    )

    start_wall = datetime.now()
    end_sim = max(exit_after_min * 60.0 + 600.0, 600.0)
    t = 0.0
    printed_at = -1e9

    print(
        f"模拟 {cfg.place}：优惠 {cfg.discount_minutes:g} 分钟，提醒 {list(cfg.warn_minutes)} 分钟，"
        f"第 {exit_after_min:g} 分钟出场，加速 {speed:g}x"
        + ("，Bark 已启用" if notifier else "，未配置 BARK_KEY（只打印）")
    )

    while t <= end_sim:
        # 位置脚本：0~2 分钟驶入，2~3 分钟挪车位，之后停稳；到点直接开走
        if t < 120:
            metres = 400.0 * (1 - t / 120.0)
            shift = "D"
        elif t < 180:
            metres = 0.0
            shift = "D"
        elif t < exit_after_min * 60.0:
            metres = 0.0
            shift = "P"
        else:
            metres = min(400.0, (t - exit_after_min * 60.0) * 10.0)
            shift = "D"

        wall = start_wall + timedelta(seconds=t)
        sim_lat = lat + metres / 111_320.0

        events: list[Event] = watcher.observe(wall.timestamp(), sim_lat, lon, shift)
        events += watcher.tick(wall.timestamp())

        if t - printed_at >= 1800:
            remaining = watcher.remaining_seconds(wall.timestamp())
            state = "场内" if watcher.inside else ("候选" if watcher.state == "PENDING" else "场外")
            remain_txt = f"剩 {remaining / 60:.0f} 分钟" if remaining is not None else "未计时"
            print(f"  {wall:%H:%M} 距停车点 {metres:5.0f}m 挡位 {shift} {state} {remain_txt}")
            printed_at = t

        for event in events:
            print(f"  >>> [{event.kind}] {event.title} | {event.body}")
            if notifier is not None:
                notifier.send(event)

        t += STEP_SECONDS
        if speed > 0:
            time.sleep(STEP_SECONDS / speed)

    print("模拟结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
