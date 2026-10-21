"""停车场围栏、优惠计时和提醒状态机。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any, Dict, List, Optional, Set, Tuple

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

log = logging.getLogger("parking-guard.core")

EARTH_RADIUS_M = 6_371_008.8

STATE_OUTSIDE = "OUTSIDE"
STATE_PENDING = "PENDING"
STATE_INSIDE = "INSIDE"


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """球面距离（米），haversine。"""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def resolve_tz(name: str) -> tzinfo:
    """尽量用 IANA 时区；Windows 上没有 tzdata 时退化为本机时区。"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}小时{minutes:02d}分"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分"


@dataclass
class Event:
    kind: str
    title: str
    body: str
    level: str = "active"  # active / timeSensitive / critical（Bark 语义）
    sound: str = "default"

    def __str__(self) -> str:  # 日志友好
        return f"[{self.kind}] {self.title} | {self.body}"


@dataclass
class Config:
    lat: float
    lon: float
    place: str = "停车场"
    enter_radius_m: float = 80.0
    exit_radius_m: float = 150.0
    confirm_seconds: float = 90.0
    require_park: bool = True
    accept_unknown_shift: bool = True
    discount_minutes: Optional[float] = None
    free_minutes: Optional[float] = None
    warn_minutes: Tuple[float, ...] = (30.0, 10.0, 5.0)
    exit_buffer_minutes: float = 10.0
    max_session_hours: float = 14.0
    tz: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if self.discount_minutes is None:
            self.discount_minutes = self.free_minutes if self.free_minutes is not None else 180.0
        self.free_minutes = self.discount_minutes
        if not math.isfinite(self.lat) or not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"纬度不合法: {self.lat}")
        if not math.isfinite(self.lon) or not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"经度不合法: {self.lon}")
        if not math.isfinite(self.discount_minutes) or self.discount_minutes <= 0:
            raise ValueError("DISCOUNT_MINUTES 必须大于 0")
        if not math.isfinite(self.enter_radius_m) or not math.isfinite(self.exit_radius_m):
            raise ValueError("围栏半径必须是有限数字")
        if self.enter_radius_m <= 0 or self.exit_radius_m <= 0:
            raise ValueError("围栏半径必须大于 0")
        if not math.isfinite(self.confirm_seconds) or not math.isfinite(self.exit_buffer_minutes):
            raise ValueError("确认时间和出场提前量必须是有限数字")
        if self.confirm_seconds < 0 or self.exit_buffer_minutes < 0:
            raise ValueError("确认时间和出场提前量不能小于 0")
        if not math.isfinite(self.max_session_hours):
            raise ValueError("会话上限必须是有限数字")
        if self.max_session_hours < 0:
            raise ValueError("会话上限不能小于 0")
        if self.exit_radius_m < self.enter_radius_m:
            raise ValueError("PARK_RADIUS_EXIT 必须 >= PARK_RADIUS_ENTER")
        try:
            warn_minutes = tuple(float(m) for m in self.warn_minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError("WARN_MINUTES 必须是数字列表") from exc
        if any(not math.isfinite(m) or m <= 0 for m in warn_minutes):
            raise ValueError("WARN_MINUTES 必须大于 0")
        self.warn_minutes = tuple(sorted(set(warn_minutes), reverse=True))
        if self.warn_minutes and max(self.warn_minutes) >= self.discount_minutes:
            raise ValueError("WARN_MINUTES 的最大值必须小于 DISCOUNT_MINUTES")


class ParkingWatcher:
    """一次停车会话的状态机 + 计时器。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._tz = resolve_tz(cfg.tz)
        self.state = STATE_OUTSIDE
        self.entry_ts: Optional[float] = None
        self.pending_since: Optional[float] = None
        self.fired: Set[float] = set()
        self.last_fix: Optional[Tuple[float, float]] = None
        self.last_fix_ts: Optional[float] = None

    @property
    def inside(self) -> bool:
        return self.state == STATE_INSIDE

    def deadline_ts(self) -> Optional[float]:
        if self.entry_ts is None:
            return None
        return self.entry_ts + self.cfg.discount_minutes * 60.0

    def remaining_seconds(self, now: float) -> Optional[float]:
        deadline = self.deadline_ts()
        return None if deadline is None else deadline - now

    def elapsed_seconds(self, now: float) -> Optional[float]:
        return None if self.entry_ts is None else now - self.entry_ts

    def observe(
        self,
        now: float,
        lat: Optional[float],
        lon: Optional[float],
        shift_state: Optional[str] = None,
    ) -> List[Event]:
        """喂一条位置上报，返回由此产生的事件（通常为空）。"""
        if lat is None or lon is None:
            log.debug("忽略位置：经纬度不完整 lat=%r lon=%r", lat, lon)
            return []
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            log.debug("忽略位置：无法解析 lat=%r lon=%r", lat, lon)
            return []
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
            log.debug("忽略位置：坐标越界 lat=%r lon=%r", lat_f, lon_f)
            return []
        if lat_f == 0.0 and lon_f == 0.0:
            log.debug("忽略位置：坐标为 (0, 0)，通常是占位值")
            return []

        dist = distance_m(lat_f, lon_f, self.cfg.lat, self.cfg.lon)
        park_ok = self._park_ok(shift_state)
        self.last_fix = (lat_f, lon_f)
        self.last_fix_ts = now
        log.debug(
            "位置判定：lat=%.6f lon=%.6f 距停车点 %.1fm（进入 %.0fm / 离开 %.0fm），"
            "挡位 %r，park_ok=%s，状态=%s",
            lat_f, lon_f, dist, self.cfg.enter_radius_m, self.cfg.exit_radius_m,
            shift_state, park_ok, self.state,
        )
        events: List[Event] = []

        if self.state == STATE_OUTSIDE:
            if dist <= self.cfg.enter_radius_m and park_ok:
                self.state = STATE_PENDING
                self.pending_since = now
                log.debug(
                    "进入候选：距停车点 %.1fm，需连续 %.0f 秒确认", dist, self.cfg.confirm_seconds
                )
            else:
                log.debug("仍在场外：距离 %.1fm，park_ok=%s", dist, park_ok)

        elif self.state == STATE_PENDING:
            if dist >= self.cfg.exit_radius_m or not park_ok:
                log.debug(
                    "候选取消：距停车点 %.1fm（离开半径 %.0fm），挡位 %r park_ok=%s",
                    dist, self.cfg.exit_radius_m, shift_state, park_ok,
                )
                self.state = STATE_OUTSIDE
                self.pending_since = None
            elif self.pending_since is not None and now - self.pending_since >= self.cfg.confirm_seconds:
                self._enter(events)

        elif self.state == STATE_INSIDE:
            if dist >= self.cfg.exit_radius_m:
                log.debug(
                    "离开围栏：距停车点 %.1fm >= 离开半径 %.0fm", dist, self.cfg.exit_radius_m
                )
                events.extend(self.close(now))

        return events

    def _enter(self, events: List[Event]) -> None:
        """确认入场：入场时间取首次进入围栏的时刻。"""
        if self.pending_since is None:
            return
        log.debug(
            "确认入场：entry_ts=%.3f，状态 %s -> %s",
            self.pending_since, self.state, STATE_INSIDE,
        )
        self.state = STATE_INSIDE
        self.entry_ts = self.pending_since
        self.pending_since = None
        self.fired.clear()
        events.append(self._entered_event(self.entry_ts))

    def close(self, now: float, reason: str = "left") -> List[Event]:
        """结束会话（离开或异常过期）。"""
        if self.entry_ts is None:
            log.debug("结束会话：没有进行中的计时（reason=%s）", reason)
            self.reset()
            return []
        elapsed = now - self.entry_ts
        over = elapsed - self.cfg.discount_minutes * 60.0
        log.debug(
            "结束会话：reason=%s 停留 %.0f 秒，优惠 %.0f 秒，超出 %.0f 秒",
            reason, elapsed, self.cfg.discount_minutes * 60.0, over,
        )
        if over > 0:
            title = f"已离开{self.cfg.place}（超出优惠 {format_duration(over)}）"
            body = (
                f"本次停留 {format_duration(elapsed)}，优惠时长 {self.cfg.discount_minutes:g} 分钟，"
                f"超出优惠 {format_duration(over)}，按停车场规则可能已产生费用。"
            )
            level = "timeSensitive"
        else:
            title = f"已离开{self.cfg.place}，计时结束"
            body = (
                f"本次停留 {format_duration(elapsed)}，"
                f"还剩 {format_duration(-over)} 优惠时长未用完。"
            )
            level = "active"
        event = Event("exited", title, body, level=level)
        self.reset()
        return [event]

    def tick(self, now: float) -> List[Event]:
        """周期性调用：负责确认候选入场和到点提醒。可不依赖任何新位置上报。

        TeslaMate 在车辆停稳/休眠后可能很久才发下一条位置。若只在 observe()
        里确认入场，车在围栏内只上报一次时会永远停在 PENDING，既不计时也不提醒。
        因此 PENDING 的确认必须由 tick 定时推进。
        """
        events: List[Event] = []
        if self.state == STATE_PENDING:
            self._confirm_pending(now, events)
        if self.state != STATE_INSIDE or self.entry_ts is None:
            return events

        elapsed = now - self.entry_ts
        if self.cfg.max_session_hours > 0 and elapsed >= self.cfg.max_session_hours * 3600:
            log.debug(
                "会话超上限：已停 %.0f 秒 >= %.0f 小时，自动结束",
                elapsed, self.cfg.max_session_hours,
            )
            events.append(
                Event(
                    "expired",
                    f"{self.cfg.place}计时已自动结束",
                    f"连续停留 {format_duration(elapsed)} 未检测到离开，"
                    f"超过 {self.cfg.max_session_hours:g} 小时上限，先停止计时以免误报。",
                )
            )
            self.reset()
            return events

        remaining = self.deadline_ts() - now  # type: ignore[operator]

        if remaining <= 0:
            # 到点后再提醒已经来不及，最后提醒是倒数最小档位。
            log.debug("优惠已结束，不再发送到点提醒")
            self.fired.update(self.cfg.warn_minutes)
            return events

        for minutes in sorted(self.cfg.warn_minutes, reverse=True):
            if minutes in self.fired:
                continue
            if remaining <= minutes * 60.0:
                self.fired.add(minutes)
                log.debug("触发 %g 分钟提醒：剩余 %.0f 秒", minutes, remaining)
                events.append(self._warn_event(now, minutes))
        return events

    def _confirm_pending(self, now: float, events: List[Event]) -> None:
        """候选满确认时间后转为场内；以最后一次已知位置为准。"""
        if self.pending_since is None:
            return
        waited = now - self.pending_since
        if waited < self.cfg.confirm_seconds:
            log.debug("候选等待：%.1f/%.0f 秒", waited, self.cfg.confirm_seconds)
            return
        if self.last_fix is None:
            log.debug("候选已满 %.0f 秒，但没有最后位置，暂不确认", waited)
            return
        last_dist = distance_m(self.last_fix[0], self.last_fix[1], self.cfg.lat, self.cfg.lon)
        if last_dist >= self.cfg.exit_radius_m:
            log.debug(
                "候选已满 %.0f 秒，但最后位置 %.1fm 已在离开半径 %.0fm 外，暂不确认",
                waited, last_dist, self.cfg.exit_radius_m,
            )
            return
        log.debug(
            "候选确认：等待 %.1f 秒，最后位置 %.1fm（离开半径 %.0fm）",
            waited, last_dist, self.cfg.exit_radius_m,
        )
        self._enter(events)

    def status_text(self, now: float) -> str:
        """一行状态摘要，供日志/排查使用。"""
        if self.state == STATE_INSIDE and self.entry_ts is not None:
            elapsed = now - self.entry_ts
            remaining = self.deadline_ts() - now  # type: ignore[operator]
            if remaining >= 0:
                return f"场内，已停 {format_duration(elapsed)}，优惠还剩 {format_duration(remaining)}"
            return f"场内，已停 {format_duration(elapsed)}，已超出优惠 {format_duration(-remaining)}"
        if self.state == STATE_PENDING and self.pending_since is not None:
            waited = max(0.0, now - self.pending_since)
            need = self.cfg.confirm_seconds
            if need <= 0:
                return "候选确认中"
            return f"候选确认中 {min(waited, need):.0f}/{need:.0f} 秒"
        return "场外"

    def _park_ok(self, shift_state: Optional[str]) -> bool:
        if not self.cfg.require_park:
            return True
        value = (shift_state or "").strip().upper()
        if value in ("P", "N"):
            return True
        if value == "":
            return self.cfg.accept_unknown_shift
        return False

    def _fmt(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self._tz).strftime("%H:%M")

    def _entered_event(self, now: float) -> Event:
        deadline = self.deadline_ts() or now
        leave_hint = deadline - self.cfg.exit_buffer_minutes * 60.0
        return Event(
            "entered",
            f"已进入{self.cfg.place}",
            f"开始计时：优惠 {self.cfg.discount_minutes:g} 分钟，"
            f"{self._fmt(deadline)} 结束"
            + (
                f"，建议 {self._fmt(leave_hint)} 前开始出场。"
                if self.cfg.exit_buffer_minutes > 0
                else "。"
            ),
            level="timeSensitive",
            sound="default",
        )

    def _warn_event(self, now: float, minutes: float) -> Event:
        deadline = self.deadline_ts() or now
        elapsed = now - (self.entry_ts or now)
        is_last = bool(self.cfg.warn_minutes) and minutes == min(self.cfg.warn_minutes)
        if is_last:
            title = f"最后提醒：{self.cfg.place}优惠还剩 {minutes:g} 分钟"
            body = (
                f"已停 {format_duration(elapsed)}，优惠截至 {self._fmt(deadline)}，"
                "现在必须出场，否则会产生额外费用。"
            )
        else:
            title = f"{self.cfg.place}优惠还剩 {minutes:g} 分钟"
            body = f"已停 {format_duration(elapsed)}，优惠截至 {self._fmt(deadline)}，现在动身更稳妥。"
        return Event(
            f"warn{minutes:g}",
            title,
            body,
            level="critical" if is_last else "timeSensitive",
            sound="alarm",
        )

    def reset(self) -> None:
        if self.state != STATE_OUTSIDE or self.entry_ts is not None:
            log.debug(
                "重置状态机：state=%s entry_ts=%s pending_since=%s fired=%s",
                self.state, self.entry_ts, self.pending_since, sorted(self.fired),
            )
        self.state = STATE_OUTSIDE
        self.entry_ts = None
        self.pending_since = None
        self.fired.clear()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "state": self.state,
            "entry_ts": self.entry_ts,
            "pending_since": self.pending_since,
            "fired": sorted(self.fired),
            "last_fix": list(self.last_fix) if self.last_fix else None,
            "last_fix_ts": self.last_fix_ts,
            "place": self.cfg.place,
        }

    def restore(self, data: Dict[str, Any]) -> bool:
        """从磁盘状态恢复计时。返回是否恢复了进行中的会话。"""
        if not isinstance(data, dict) or data.get("version") != 1:
            return False
        state = data.get("state")
        entry_ts = data.get("entry_ts")
        if state not in (STATE_OUTSIDE, STATE_PENDING, STATE_INSIDE):
            return False
        if state == STATE_INSIDE:
            if not isinstance(entry_ts, (int, float)) or not math.isfinite(entry_ts):
                return False
            try:
                fired = {float(m) for m in data.get("fired") or []}
            except (TypeError, ValueError):
                return False
            if not all(math.isfinite(m) for m in fired):
                return False
            self.state = STATE_INSIDE
            self.entry_ts = float(entry_ts)
            self.pending_since = None
            self.fired = fired
            fix = data.get("last_fix")
            if isinstance(fix, list) and len(fix) == 2:
                try:
                    self.last_fix = (float(fix[0]), float(fix[1]))
                except (TypeError, ValueError):
                    self.last_fix = None
            last_fix_ts = data.get("last_fix_ts")
            self.last_fix_ts = (
                float(last_fix_ts)
                if isinstance(last_fix_ts, (int, float)) and math.isfinite(last_fix_ts)
                else None
            )
            log.debug(
                "恢复会话：entry_ts=%.3f fired=%s last_fix=%s",
                self.entry_ts, sorted(self.fired), self.last_fix,
            )
            return True
        if state == STATE_PENDING and isinstance(data.get("pending_since"), (int, float)):
            if not math.isfinite(data["pending_since"]):
                return False
            self.state = STATE_PENDING
            self.pending_since = float(data["pending_since"])
            log.debug("恢复候选：pending_since=%.3f（等待定时器确认）", self.pending_since)
            return False
        log.debug("状态文件未恢复：state=%r", state)
        return False
