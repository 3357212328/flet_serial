import re
import socket
import struct
from typing import Any
import zlib


def safe_update(control: Any) -> None:
    """仅在控件已挂载到 Page 时安全刷新（适配 Flet 1.x 未挂载控件调用 update 抛出 RuntimeError）."""
    if control is None:
        return
    try:
        control.update()
    except RuntimeError:
        pass


def parse_hex_string(hex_str: str) -> bytes:
    """解析支持空格、逗号、换行、0x前缀的十六进制字符串."""
    cleaned = re.sub(r"0[xX]", " ", hex_str)
    cleaned = re.sub(r"[\s,;:\-]+", "", cleaned)
    if not cleaned:
        return b""
    if len(cleaned) % 2 != 0:
        raise ValueError(f"HEX 字符长度必须为偶数（当前有效字符数: {len(cleaned)}）")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError("包含非法十六进制字符（仅支持 0-9, A-F）") from exc


def format_hex(data: bytes) -> str:
    """将字节格式化为大写空格分隔 HEX 字符串."""
    return data.hex(" ").upper() if data else ""


def decode_bytes(data: bytes, encoding: str = "utf-8") -> str:
    """按指定编码安全解码字节流."""
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def format_dual_hex_ascii(data: bytes, encoding: str = "utf-8") -> str:
    """同时展示 HEX 与可读文本对照."""
    hex_part = format_hex(data)
    txt_part = decode_bytes(data, encoding)
    txt_printable = (
        txt_part.replace("\r\n", "⏎")
        .replace("\n", "⏎")
        .replace("\r", "⏎")
        .replace("\t", "⇥")
    )
    return f"{hex_part}   │   {txt_printable}"


def unescape_text(raw: str) -> str:
    """支持常见转义字符如 \\r \\n \\t \\0."""
    return (
        raw.replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\0", "\0")
    )


def apply_eol(text: str, eol_mode: str) -> str:
    """为文本追加指定行尾结束符."""
    if eol_mode == "CRLF":
        return text + "\r\n"
    if eol_mode == "LF":
        return text + "\n"
    if eol_mode == "CR":
        return text + "\r"
    return text


# --- 校验算法 ---

def calc_crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def calc_crc16_ccitt(data: bytes, poly: int = 0x1021, init: int = 0xFFFF) -> int:
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ poly) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def calc_crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def calc_xor(data: bytes) -> int:
    res = 0
    for b in data:
        res ^= b
    return res & 0xFF


def calc_sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def append_checksum(data: bytes, mode: str) -> bytes:
    """按所选模式在数据包末尾追加校验字节."""
    if not data or mode == "NONE":
        return data
    if mode == "MODBUS_LE":
        crc = calc_crc16_modbus(data)
        return data + struct.pack("<H", crc)
    if mode == "MODBUS_BE":
        crc = calc_crc16_modbus(data)
        return data + struct.pack(">H", crc)
    if mode == "CRC16_CCITT":
        crc = calc_crc16_ccitt(data)
        return data + struct.pack(">H", crc)
    if mode == "XOR":
        return data + bytes([calc_xor(data)])
    if mode == "SUM8":
        return data + bytes([calc_sum8(data)])
    return data


# --- IEEE 754 浮点数互转 ---

def float_to_hex(value: float) -> tuple[str, str]:
    """返回单精度浮点数 (大端HEX, 小端HEX)."""
    be = struct.pack(">f", value).hex(" ").upper()
    le = struct.pack("<f", value).hex(" ").upper()
    return be, le


def hex_to_float(hex_str: str) -> tuple[float, float]:
    """从4字节HEX解析单精度浮点数，返回 (大端值, 小端值)."""
    raw = parse_hex_string(hex_str)
    if len(raw) != 4:
        raise ValueError(f"IEEE-754 单精度浮点数需要恰好 4 字节（当前 {len(raw)} 字节）")
    be_val = struct.unpack(">f", raw)[0]
    le_val = struct.unpack("<f", raw)[0]
    return be_val, le_val


# --- 网络与格式化工具 ---

def get_local_ips() -> list[str]:
    """获取本机局域网 IPv4 地址列表."""
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    if not ips:
        ips.append("127.0.0.1")
    return ips


def format_byte_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


# --- 波形数据数值解析 ---

_KV_PATTERN = re.compile(
    r"([A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]{0,15})\s*[=:]\s*([-+]?\d+(?:\.\d+)?)"
)
_NUM_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")


def extract_waveform_samples(line: str) -> dict[str, float]:
    """
    从单行串口文本提取多通道浮点数据。
    支持：
      1) 键值对格式: temp=25.4, hum=61.2 或 T:25.4 H:61.2
      2) VOFA+ FireWater / CSV / 空格分隔: data: 12.3, 45.6, -7.8 或 12.3, 45.6
    """
    stripped = line.strip()
    if not stripped:
        return {}

    # 优先匹配全行键值对（排除类似 data: 1.0, 2.0 这种单前缀多数值情况）
    kv_matches = _KV_PATTERN.findall(stripped)
    if len(kv_matches) >= 2:
        result: dict[str, float] = {}
        for k, v in kv_matches[:6]:
            try:
                result[k] = float(v)
            except ValueError:
                pass
        if result:
            return result

    if len(kv_matches) == 1:
        # 检查是否只有一个数值（如 temp=25.4），而非 prefix: 1.0, 2.0, 3.0
        all_nums = _NUM_PATTERN.findall(stripped)
        if len(all_nums) == 1:
            k, v = kv_matches[0]
            try:
                return {k: float(v)}
            except ValueError:
                return {}

    # 若包含 prefix: 1.2, 3.4 则截取冒号后部分
    if ":" in stripped:
        after_colon = stripped.split(":", 1)[1]
        nums = _NUM_PATTERN.findall(after_colon)
    else:
        nums = _NUM_PATTERN.findall(stripped)

    if not nums:
        return {}

    samples: dict[str, float] = {}
    for idx, token in enumerate(nums[:6], start=1):
        try:
            samples[f"CH{idx}"] = float(token)
        except ValueError:
            continue
    return samples
