from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import queue
import re
import threading
import time
from typing import Any, Callable

from .utils import decode_bytes, format_hex, parse_hex_string, unescape_text


@dataclass
class PacketEvent:
    direction: str  # "RX" | "TX" | "INFO" | "ERROR"
    payload: bytes | str
    source: str = "SYS"  # "UART" | "BLE" | "NET" | "SYS" | "AUTO"
    extra: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now().strftime("%H:%M:%S.%f")[:-3]
    )


class EventBus:
    """线程安全的消息队列与收发流量统计中心."""

    def __init__(self) -> None:
        self.packet_queue: queue.Queue[PacketEvent] = queue.Queue()
        self.ui_callbacks: queue.Queue[Callable[[], None]] = queue.Queue()

        self._lock = threading.Lock()
        self.rx_bytes: int = 0
        self.tx_bytes: int = 0
        self.rx_packets: int = 0
        self.tx_packets: int = 0
        self._rx_window: deque[tuple[float, int]] = deque()
        self._tx_window: deque[tuple[float, int]] = deque()

    def emit(
        self,
        direction: str,
        payload: bytes | str,
        source: str = "SYS",
        extra: str = "",
    ) -> None:
        now = time.monotonic()
        byte_len = (
            len(payload)
            if isinstance(payload, bytes)
            else len(str(payload).encode("utf-8"))
        )
        with self._lock:
            if direction == "RX":
                self.rx_bytes += byte_len
                self.rx_packets += 1
                self._rx_window.append((now, byte_len))
            elif direction == "TX":
                self.tx_bytes += byte_len
                self.tx_packets += 1
                self._tx_window.append((now, byte_len))

        self.packet_queue.put(
            PacketEvent(
                direction=direction,
                payload=payload,
                source=source,
                extra=extra,
            )
        )

    def call_ui(self, callback: Callable[[], None]) -> None:
        """从工作线程安全调度 UI 更新回调到 Flet 主循环."""
        self.ui_callbacks.put(callback)

    def get_rates_bps(self) -> tuple[int, int]:
        """计算过去 1 秒内的 (RX 字节/秒, TX 字节/秒)."""
        now = time.monotonic()
        cutoff = now - 1.0
        with self._lock:
            while self._rx_window and self._rx_window[0][0] < cutoff:
                self._rx_window.popleft()
            while self._tx_window and self._tx_window[0][0] < cutoff:
                self._tx_window.popleft()
            rx_bps = sum(b for _, b in self._rx_window)
            tx_bps = sum(b for _, b in self._tx_window)
        return rx_bps, tx_bps

    def reset_stats(self) -> None:
        with self._lock:
            self.rx_bytes = 0
            self.tx_bytes = 0
            self.rx_packets = 0
            self.tx_packets = 0
            self._rx_window.clear()
            self._tx_window.clear()


def match_auto_replies(
    raw_data: bytes,
    encoding: str,
    rules: list[dict[str, Any]],
) -> list[tuple[str, bytes, int]]:
    """
    匹配自动应答规则，返回 [(规则名, 应答字节, 延迟ms), ...].
    """
    if not raw_data or not rules:
        return []

    decoded_text = decode_bytes(raw_data, encoding)
    hex_Compact = raw_data.hex().upper()
    matched: list[tuple[str, bytes, int]] = []

    for rule in rules:
        if not rule.get("enabled", False):
            continue
        trigger = str(rule.get("trigger", ""))
        if not trigger:
            continue

        mode = str(rule.get("match_mode", "CONTAINS")).upper()
        hit = False
        try:
            if mode == "CONTAINS":
                hit = unescape_text(trigger) in decoded_text
            elif mode == "EXACT":
                hit = decoded_text.strip("\r\n") == unescape_text(trigger).strip("\r\n")
            elif mode == "HEX":
                target_bytes = parse_hex_string(trigger)
                hit = bool(target_bytes) and (target_bytes.hex().upper() in hex_Compact)
            elif mode == "REGEX":
                hit = re.search(trigger, decoded_text) is not None
        except (ValueError, re.error):
            continue

        if hit:
            reply_raw = str(rule.get("reply", ""))
            reply_is_hex = bool(rule.get("reply_is_hex", False))
            delay_ms = max(0, int(rule.get("delay_ms", 50) or 0))
            try:
                if reply_is_hex:
                    reply_bytes = parse_hex_string(reply_raw)
                else:
                    reply_bytes = unescape_text(reply_raw).encode(
                        encoding, errors="replace"
                    )
                if reply_bytes:
                    matched.append((str(rule.get("name", "自动应答")), reply_bytes, delay_ms))
            except ValueError:
                continue

    return matched


__all__ = ["PacketEvent", "EventBus", "match_auto_replies", "format_hex"]
