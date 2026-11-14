import copy
import json
import sys
from pathlib import Path
from typing import Any


def _is_compiled_runtime() -> bool:
    return "__compiled__" in globals() or bool(getattr(sys, "frozen", False))


def _get_compiled_exe_dir() -> Path:
    compiled = globals().get("__compiled__")
    if compiled is not None:
        if getattr(compiled, "onefile", False):
            containing_dir = getattr(compiled, "containing_dir", None)
            if containing_dir:
                return Path(containing_dir)
            process_exe = getattr(compiled, "process_exe", None)
            if process_exe:
                return Path(process_exe).resolve().parent
            return Path(sys.argv[0]).resolve().parent
        return Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def _resolve_config_paths(filename: str) -> tuple[Path, Path | None]:
    p = Path(filename)
    if p.is_absolute():
        return p, None
    if _is_compiled_runtime():
        primary = _get_compiled_exe_dir() / p
        fallback = Path.home() / ".omniport_studio" / p
        return primary, fallback
    return Path(__file__).resolve().parent.parent / p, None


DEFAULT_SETTINGS: dict[str, Any] = {
    "ui": {
        "theme_mode": "dark",
        "sidebar_visible": True,
        "encoding": "utf-8",
        "view_mode": "ASCII",  # ASCII | HEX | DUAL
        "show_timestamp": True,
        "max_log_entries": 2000,
    },
    "serial": {
        "baud": "115200",
        "bytesize": "8",
        "parity": "N",
        "stopbits": "1",
        "flow_control": "None",
        "rx_timeout_ms": "20",
        "dtr": False,
        "rts": False,
        "eol": "CRLF",
        "hex_view": False,
    },
    "ble": {
        "scan_timeout": "5",
        "filter_unnamed": True,
        "chunk_size": "240",
    },
    "network": {
        "protocol": "TCP Client",
        "ip": "127.0.0.1",
        "port": "8080",
        "local_port": "8081",
    },
    "tx": {
        "hex_send": False,
        "eol": "CRLF",  # CRLF | LF | CR | NONE
        "checksum": "NONE",  # NONE | MODBUS_LE | MODBUS_BE | XOR | SUM8
        "auto_interval_ms": "1000",
        "file_chunk_size": "256",
        "file_chunk_delay_ms": "20",
        "history": [
            "AT",
            "AT+GMR",
            "01 03 00 00 00 02",
        ],
    },
    "macros": [
        {
            "name": "AT 测试",
            "content": "AT\\r\\n",
            "is_hex": False,
            "color": "blue",
        },
        {
            "name": "查询固件版本",
            "content": "AT+GMR\\r\\n",
            "is_hex": False,
            "color": "teal",
        },
        {
            "name": "Modbus 读寄存器",
            "content": "01 03 00 00 00 02 C4 0B",
            "is_hex": True,
            "color": "amber",
        },
        {
            "name": "复位设备",
            "content": "AT+RST\\r\\n",
            "is_hex": False,
            "color": "red",
        },
    ],
    "auto_replies": [
        {
            "enabled": False,
            "name": "AT 心跳应答",
            "match_mode": "CONTAINS",  # CONTAINS | EXACT | HEX | REGEX
            "trigger": "PING",
            "reply": "PONG\\r\\n",
            "reply_is_hex": False,
            "delay_ms": 50,
        }
    ],
}


def _deep_merge(defaults: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class AppConfig:
    """持久化配置管理器，兼容旧版 flet_config.json 并自动补全默认字段."""

    def __init__(self, filename: str = "flet_config.json"):
        primary, fallback = _resolve_config_paths(filename)
        self._fallback_path = fallback
        if fallback is not None and not primary.exists() and fallback.exists():
            self.filepath = fallback
        else:
            self.filepath = primary
        self.settings: dict[str, Any] = self.load()

    def load(self) -> dict[str, Any]:
        if not self.filepath.exists():
            data = copy.deepcopy(DEFAULT_SETTINGS)
            self._write_safe(data)
            return data

        raw_data = None
        for enc in ("utf-8", "gbk"):
            try:
                raw_text = self.filepath.read_text(encoding=enc)
                raw_data = json.loads(raw_text)
                break
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                continue

        if not isinstance(raw_data, dict):
            return copy.deepcopy(DEFAULT_SETTINGS)

        merged = _deep_merge(DEFAULT_SETTINGS, raw_data)
        # 规范化旧版宏数据结构
        normalized_macros = []
        for m in merged.get("macros", []):
            if isinstance(m, dict) and "name" in m and "content" in m:
                normalized_macros.append(
                    {
                        "name": str(m.get("name", "指令")),
                        "content": str(m.get("content", "")),
                        "is_hex": bool(m.get("is_hex", False)),
                        "color": str(m.get("color", "blue")),
                    }
                )
        merged["macros"] = normalized_macros
        return merged

    def _write_safe(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=4, ensure_ascii=False)
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            self.filepath.write_text(payload, encoding="utf-8")
            return
        except OSError:
            pass
        if self._fallback_path is not None and self.filepath != self._fallback_path:
            try:
                self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
                self._fallback_path.write_text(payload, encoding="utf-8")
                self.filepath = self._fallback_path
            except OSError:
                pass

    def save(self) -> None:
        self._write_safe(self.settings)

    def get(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.save()

    def update_section(self, section: str, **kwargs: Any) -> None:
        sec = self.settings.setdefault(section, {})
        if isinstance(sec, dict):
            sec.update(kwargs)
            self.save()
