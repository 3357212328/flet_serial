"""Automated verification suite for OmniPort Studio source and packaged binaries."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def test_utils_and_protocols() -> None:
    """Verify checksums, float-HEX conversion, encoding, EOL, and waveform parsing."""
    from serial_assistant.event_bus import EventBus, PacketEvent, match_auto_replies
    from serial_assistant.utils import (
        append_checksum,
        apply_eol,
        calc_crc16_ccitt,
        calc_crc16_modbus,
        calc_crc32,
        calc_sum8,
        calc_xor,
        decode_bytes,
        extract_waveform_samples,
        float_to_hex,
        format_byte_size,
        format_dual_hex_ascii,
        format_hex,
        get_local_ips,
        hex_to_float,
        parse_hex_string,
        unescape_text,
    )

    frame = b"\x01\x03\x00\x00\x00\x02"
    assert calc_crc16_modbus(frame) == 0x0BC4
    assert append_checksum(frame, "MODBUS_LE") == frame + b"\xC4\x0B"
    assert append_checksum(frame, "MODBUS_BE") == frame + b"\x0B\xC4"
    assert calc_crc16_ccitt(b"123456789") == 0x29B1
    assert append_checksum(b"123456789", "CRC16_CCITT")[-2:] == b"\x29\xB1"
    assert calc_crc32(b"123456789") == 0xCBF43926
    assert calc_xor(b"\x12\x34\x56") == (0x12 ^ 0x34 ^ 0x56)
    assert append_checksum(b"\x12\x34", "XOR")[-1] == (0x12 ^ 0x34)
    assert calc_sum8(b"\xFF\x02") == 0x01
    assert append_checksum(b"\xFF\x02", "SUM8")[-1] == 0x01

    be_hex, le_hex = float_to_hex(12.5)
    assert be_hex == "41 48 00 00"
    assert le_hex == "00 00 48 41"
    be_val, _ = hex_to_float(be_hex)
    _, le_val = hex_to_float(le_hex)
    assert abs(be_val - 12.5) < 1e-6 and abs(le_val - 12.5) < 1e-6

    assert parse_hex_string("aa 55 01") == b"\xAA\x55\x01"
    assert format_hex(b"\xAA\x05") == "AA 05"
    assert "AA 05" in format_dual_hex_ascii(b"\xAA\x05", "utf-8")
    assert decode_bytes("串口测试".encode("gbk"), "gbk") == "串口测试"
    assert apply_eol(unescape_text("AT\\t1"), "CRLF") == "AT\t1\r\n"
    assert format_byte_size(2048) == "2.0 KB"
    assert isinstance(get_local_ips(), list)

    assert extract_waveform_samples("12.5, -3.5, 42") == {
        "CH1": 12.5,
        "CH2": -3.5,
        "CH3": 42.0,
    }
    assert extract_waveform_samples("Roll=12.5, Pitch=-3.4, Temp=25.0") == {
        "Roll": 12.5,
        "Pitch": -3.4,
        "Temp": 25.0,
    }

    bus = EventBus()
    bus.emit("TX", b"1234", source="NET")
    bus.emit("RX", b"123456", source="NET")
    assert bus.tx_bytes == 4 and bus.tx_packets == 1
    assert bus.rx_bytes == 6 and bus.rx_packets == 1
    assert bus.packet_queue.qsize() == 2
    ev0 = bus.packet_queue.get_nowait()
    ev1 = bus.packet_queue.get_nowait()
    assert isinstance(ev0, PacketEvent) and ev0.payload == b"1234"
    assert isinstance(ev1, PacketEvent) and ev1.payload == b"123456"
    rx_bps, tx_bps = bus.get_rates_bps()
    assert rx_bps == 6 and tx_bps == 4
    bus.reset_stats()
    assert bus.tx_bytes == 0 and bus.rx_bytes == 0

    rules = [
        {
            "enabled": True,
            "name": "txt_contains",
            "match_mode": "CONTAINS",
            "trigger": "PING",
            "reply": "PONG\\r\\n",
            "reply_is_hex": False,
            "delay_ms": 5,
        },
        {
            "enabled": True,
            "name": "hex_rule",
            "match_mode": "HEX",
            "trigger": "AA 55 01",
            "reply": "55 AA 00",
            "reply_is_hex": True,
            "delay_ms": 10,
        },
        {
            "enabled": True,
            "name": "exact_rule",
            "match_mode": "EXACT",
            "trigger": "ID?",
            "reply": "OMNIPORT",
            "reply_is_hex": False,
            "delay_ms": 0,
        },
        {
            "enabled": True,
            "name": "regex_rule",
            "match_mode": "REGEX",
            "trigger": r"CMD:(\d+)",
            "reply": "ACK",
            "reply_is_hex": False,
            "delay_ms": 0,
        },
    ]
    assert match_auto_replies(b"hello PING!", "utf-8", rules) == [("txt_contains", b"PONG\r\n", 5)]
    assert match_auto_replies(b"\xAA\x55\x01", "utf-8", rules) == [("hex_rule", b"\x55\xAA\x00", 10)]
    assert match_auto_replies(b"ID?\r\n", "utf-8", rules) == [("exact_rule", b"OMNIPORT", 0)]
    assert match_auto_replies(b"CMD:42", "utf-8", rules) == [("regex_rule", b"ACK", 0)]


def test_config_persistence() -> None:
    """Verify AppConfig initialization, mutation, and JSON persistence roundtrip."""
    from serial_assistant.config import AppConfig

    with tempfile.TemporaryDirectory(prefix="omniport_cfg_") as tmp:
        cfg_file = Path(tmp) / "test_config.json"
        cfg = AppConfig(str(cfg_file))
        assert cfg_file.is_file()
        cfg.update_section("serial", baudrate="921600", dtr=True)
        macros = list(cfg.get("macros", []))
        macros.append({"name": "TEST", "content": "AT+VER\\r\\n", "is_hex": False, "color": "cyan"})
        cfg.set("macros", macros)

        reloaded = AppConfig(str(cfg_file))
        assert reloaded.get("serial", {})["baudrate"] == "921600"
        assert reloaded.get("serial", {})["dtr"] is True
        assert any(m["name"] == "TEST" for m in reloaded.get("macros", []))


def test_flet_pruned_runtime_and_hashlib() -> None:
    """Verify Flet icons JSON, canvas primitives, core controls, and hashlib fallback work."""
    import flet as ft
    import flet.canvas as cv
    import flet_desktop  # noqa: F401

    assert hashlib.sha256(b"omniport").hexdigest() == (
        "5a718b70648f37b4166d8cbde2bd728c7738100539b2f21216b2684f5262b4fa"
    )
    assert str(ft.Icons.USB)
    assert str(ft.Icons.BLUETOOTH)
    assert str(ft.Icons.LAN)
    assert str(ft.Icons.AUTO_GRAPH)
    assert str(ft.Icons.CALCULATE_OUTLINED)
    assert str(ft.Colors.CYAN_500)

    canvas = cv.Canvas(
        shapes=[
            cv.Line(0, 0, 10, 10, paint=ft.Paint(color="#38BDF8", stroke_width=2)),
            cv.Path(
                elements=[cv.Path.MoveTo(0, 0), cv.Path.LineTo(20, 20)],
                paint=ft.Paint(color="#22C55E", stroke_width=2),
            ),
            cv.Text(5, 5, value="0.0"),
        ]
    )
    assert len(canvas.shapes) == 3


def test_hardware_and_network_backends() -> None:
    """Verify pyserial, WinRT Bleak backend, and TCP/UDP loopback sockets."""
    import serial
    import serial.tools.list_ports

    ports = list(serial.tools.list_ports.comports())
    assert isinstance(ports, list)
    assert callable(serial.Serial)

    from bleak import BleakClient, BleakScanner

    assert callable(BleakClient) and callable(BleakScanner)

    async def _ble_check() -> None:
        try:
            devices = await BleakScanner.discover(timeout=1.0, return_adv=True)
            assert isinstance(devices, (dict, list))
        except Exception as exc:
            # Physical Bluetooth radio may be powered off or absent on CI runners;
            # missing Python/WinRT bindings would raise ImportError/ModuleNotFoundError.
            if isinstance(exc, (ImportError, ModuleNotFoundError)):
                raise

    asyncio.run(_ble_check())

    # Raw TCP loopback smoke
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as cli:
            conn, _ = srv.accept()
            with conn:
                cli.sendall(b"TCP_PING")
                assert conn.recv(64) == b"TCP_PING"
                conn.sendall(b"TCP_PONG")
                assert cli.recv(64) == b"TCP_PONG"

    # Raw UDP loopback smoke
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b,
    ):
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        b.settimeout(2.0)
        a.sendto(b"UDP_OK", b.getsockname())
        payload, _ = b.recvfrom(64)
        assert payload == b"UDP_OK"


def run_unit_suite() -> None:
    """Run all functional unit/integration checks."""
    checks = [
        ("utils_and_protocols", test_utils_and_protocols),
        ("config_persistence", test_config_persistence),
        ("flet_pruned_runtime_and_hashlib", test_flet_pruned_runtime_and_hashlib),
        ("hardware_and_network_backends", test_hardware_and_network_backends),
    ]
    for name, fn in checks:
        fn()
        print(f"[PASS] {name}")


def _find_visible_window(title_substring: str) -> int | None:
    user32 = ctypes.windll.user32
    found: int | None = None

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(hwnd: int, _: int) -> bool:
        nonlocal found
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if title_substring in buf.value:
                    found = hwnd
                    return False
        return True

    user32.EnumWindows(enum_cb, 0)
    return found


def verify_packaged_exe(exe_path: Path, timeout_sec: float = 25.0) -> None:
    """Launch packaged onefile EXE in isolated USERPROFILE and verify runtime state."""
    if not exe_path.is_file():
        raise FileNotFoundError(f"Executable not found: {exe_path}")

    with tempfile.TemporaryDirectory(prefix="omniport_exe_verify_") as tmp:
        tmp_dir = Path(tmp)
        work_exe = tmp_dir / exe_path.name
        shutil.copy2(exe_path, work_exe)
        fake_home = tmp_dir / "home"
        fake_home.mkdir()

        env = os.environ.copy()
        env["USERPROFILE"] = str(fake_home)
        env["HOME"] = str(fake_home)

        proc = subprocess.Popen(
            [str(work_exe)],
            cwd=str(tmp_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            hwnd: int | None = None
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    out, err = proc.communicate(timeout=2)
                    raise RuntimeError(
                        f"Packaged EXE exited prematurely ({proc.returncode}): "
                        f"stdout={out!r} stderr={err!r}"
                    )
                hwnd = _find_visible_window("OmniPort Studio")
                flet_exes = list((fake_home / ".flet" / "client").rglob("flet.exe"))
                if hwnd is not None and (tmp_dir / "flet_config.json").is_file() and flet_exes:
                    break
                time.sleep(0.25)

            if hwnd is None:
                raise TimeoutError("Timed out waiting for OmniPort Studio window")

            cfg_path = tmp_dir / "flet_config.json"
            assert cfg_path.is_file(), "flet_config.json was not created on startup"
            cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            for section in ("ui", "serial", "ble", "network", "tx", "macros", "auto_replies"):
                assert section in cfg_data, f"Missing config section: {section}"

            # Give client cache extraction a moment and verify minimal client payload
            client_root = fake_home / ".flet" / "client"
            flet_exes = list(client_root.rglob("flet.exe"))
            assert len(flet_exes) == 1, f"Expected 1 extracted flet.exe, got {flet_exes}"
            client_dir = flet_exes[0].parent
            assert (client_dir / "data" / "app.so").is_file()
            for bloated_dll in (
                "libmpv-2.dll",
                "rive_native.dll",
                "vk_swiftshader.dll",
                "libEGL.dll",
                "libGLESv2.dll",
            ):
                assert not (client_dir / bloated_dll).exists(), f"Unexpected fat DLL: {bloated_dll}"

            # Graceful close via WM_CLOSE
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
            proc.wait(timeout=8)
            print(
                f"[PASS] packaged_exe_launch: exe={exe_path.name}, "
                f"size={exe_path.stat().st_size:,} bytes, hwnd={hwnd}, client={client_dir.parent.name}"
            )
        finally:
            if proc.poll() is None:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify OmniPort Studio functionality.")
    parser.add_argument("--unit", action="store_true", help="Run functional unit & backend suite.")
    parser.add_argument("--launch-exe", type=Path, default=None, help="Verify built onefile EXE launch.")
    args = parser.parse_args()

    if not args.unit and args.launch_exe is None:
        args.unit = True

    if args.unit:
        run_unit_suite()
    if args.launch_exe is not None:
        verify_packaged_exe(args.launch_exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
