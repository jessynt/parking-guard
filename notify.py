"""Bark HTTP 通知。"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from core import Event

log = logging.getLogger(__name__)


class BarkNotifier:
    def __init__(
        self,
        server: str,
        device_key: str,
        group: str = "停车场",
        sound: str = "alarm",
        level: str = "timeSensitive",
        extra_query: str = "",
        timeout: float = 10.0,
        retries: int = 3,
    ) -> None:
        self.server = (server or "https://api.day.app").rstrip("/")
        self.device_key = device_key.strip()
        self.group = group
        self.sound = sound
        self.level = level
        self.extra_query = extra_query.strip()
        self.timeout = timeout
        self.retries = max(1, retries)

    def build_url(self, title: str, body: str, level: Optional[str] = None, sound: Optional[str] = None) -> str:
        key = urllib.parse.quote(self.device_key, safe="")
        url = "{}/{}/{}/{}".format(
            self.server,
            key,
            urllib.parse.quote(title, safe=""),
            urllib.parse.quote(body, safe=""),
        )
        level = level or self.level
        pairs = [
            ("group", self.group),
            ("level", level),
            ("sound", sound or self.sound),
            ("isArchive", "1"),
        ]
        if level == "critical":
            pairs.append(("volume", "10"))
        query = urllib.parse.urlencode(pairs)
        if self.extra_query:  # 例如 "call=1&icon=..."，原样透传
            query = f"{query}&{self.extra_query}"
        return f"{url}?{query}"

    def send(self, event: Event) -> bool:
        if not self.device_key:
            log.error("BARK_KEY 未配置，跳过推送: %s", event)
            return False
        url = self.build_url(event.title, event.body, event.level, event.sound)
        safe_url = url.replace(urllib.parse.quote(self.device_key, safe=""), "***")
        log.debug("Bark 请求 [%s]：%s", event.kind, safe_url)
        for attempt in range(1, self.retries + 1):
            started = time.monotonic()
            try:
                request = urllib.request.Request(
                    url, method="GET", headers={"User-Agent": "parking-guard/1.0"}
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    status = resp.status
                    payload = resp.read(512).decode("utf-8", "replace")
                log.debug(
                    "Bark 响应 HTTP %s（%.2fs）：%s",
                    status, time.monotonic() - started, payload[:200],
                )
                if status == 200 and '"code":200' in payload.replace(" ", ""):
                    log.info("Bark 推送成功 %s", event.kind)
                    return True
                if status == 200:
                    # 部分自建服务只回 200，不保证 body 结构
                    log.info("Bark 已接受 %s（响应: %s）", event.kind, payload[:120])
                    return True
                log.warning("Bark 返回 %s: %s", status, safe_url)
            except urllib.error.HTTPError as exc:
                log.warning("Bark HTTP %s（第 %d/%d 次）", exc.code, attempt, self.retries)
                if exc.code in (400, 401, 403, 404):  # key/参数错误，重试无意义
                    log.debug("Bark HTTP %s 不重试（key/参数错误）", exc.code)
                    break
            except Exception as exc:  # noqa: BLE001 - 网络异常统一处理
                log.warning("Bark 发送失败（第 %d/%d 次）: %s", attempt, self.retries, exc)
            if attempt < self.retries:
                delay = min(2 ** attempt, 10)
                log.debug("Bark 第 %d 次失败，%.0f 秒后重试", attempt, delay)
                time.sleep(delay)
        log.error("Bark 推送最终失败 %s -> %s", event.kind, safe_url)
        return False
