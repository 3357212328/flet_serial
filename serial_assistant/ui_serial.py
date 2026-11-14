import threading
import time
from typing import Callable

import flet as ft
import serial
import serial.tools.list_ports

from .config import AppConfig
from .event_bus import EventBus
from .utils import safe_update

BAUDRATE_PRESETS = [
    "1200",
    "2400",
    "4800",
    "9600",
    "19200",
    "38400",
    "57600",
    "74880",
    "115200",
    "230400",
    "250000",
    "460800",
    "921600",
    "1000000",
    "1500000",
    "2000000",
    "CUSTOM",
]

BYTESIZE_MAP = {
    "8": serial.EIGHTBITS,
    "7": serial.SEVENBITS,
    "6": serial.SIXBITS,
    "5": serial.FIVEBITS,
}

PARITY_MAP = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
}

STOPBITS_MAP = {
    "1": serial.STOPBITS_ONE,
    "1.5": serial.STOPBITS_ONE_POINT_FIVE,
    "2": serial.STOPBITS_TWO,
}


class SerialChannel:
    """增强型串口 (UART) 控制模块：支持全参数配置、自定义波特率、DTR/RTS 控制与接收分包合并."""

    def __init__(
        self,
        page: ft.Page,
        config: AppConfig,
        bus: EventBus,
        on_status_change: Callable[[], None],
    ) -> None:
        self.page = page
        self.config = config
        self.bus = bus
        self.on_status_change = on_status_change

        self.ser: serial.Serial | None = None
        self.reading: bool = False
        self._port_details: dict[str, str] = {}

        cfg = self.config.get("serial", {})
        saved_baud = str(cfg.get("baud", "115200"))
        is_custom_baud = saved_baud not in BAUDRATE_PRESETS[:-1]

        # --- UI 组件 ---
        self.dd_ports = ft.Dropdown(
            label="串口设备",
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[],
            on_select=self._on_port_select,
        )
        self.btn_refresh = ft.IconButton(
            icon=ft.Icons.REFRESH,
            tooltip="重新扫描系统串口",
            on_click=self.refresh_click,
        )
        self.txt_port_info = ft.Text(
            "未检测到串口或请选择端口",
            size=11,
            color=ft.Colors.with_opacity(0.6, ft.Colors.ON_SURFACE),
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )

        self.dd_baud = ft.Dropdown(
            label="波特率",
            value="CUSTOM" if is_custom_baud else saved_baud,
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[
                ft.DropdownOption(
                    key=b,
                    text="自定义波特率..." if b == "CUSTOM" else b,
                )
                for b in BAUDRATE_PRESETS
            ],
            on_select=self._on_baud_change,
        )
        self.tf_custom_baud = ft.TextField(
            label="自定义波特率",
            value=saved_baud if is_custom_baud else "115200",
            text_size=13,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            visible=is_custom_baud,
            keyboard_type=ft.KeyboardType.NUMBER,
            on_change=lambda e: self._save_prefs(),
        )

        self.dd_bytesize = ft.Dropdown(
            label="数据位",
            value=str(cfg.get("bytesize", "8")),
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[ft.DropdownOption(key=k, text=k) for k in ("8", "7", "6", "5")],
            on_select=lambda e: self._save_prefs(),
        )
        self.dd_stopbits = ft.Dropdown(
            label="停止位",
            value=str(cfg.get("stopbits", "1")),
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[ft.DropdownOption(key=k, text=k) for k in ("1", "1.5", "2")],
            on_select=lambda e: self._save_prefs(),
        )
        self.dd_parity = ft.Dropdown(
            label="校验位",
            value=str(cfg.get("parity", "N")),
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[
                ft.DropdownOption(key="N", text="None (无)"),
                ft.DropdownOption(key="E", text="Even (偶)"),
                ft.DropdownOption(key="O", text="Odd (奇)"),
                ft.DropdownOption(key="M", text="Mark (1)"),
                ft.DropdownOption(key="S", text="Space (0)"),
            ],
            on_select=lambda e: self._save_prefs(),
        )
        self.dd_flow = ft.Dropdown(
            label="流控",
            value=str(cfg.get("flow_control", "None")),
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[
                ft.DropdownOption(key="None", text="None"),
                ft.DropdownOption(key="RTS/CTS", text="RTS/CTS"),
                ft.DropdownOption(key="XON/XOFF", text="XON/XOFF"),
            ],
            on_select=lambda e: self._save_prefs(),
        )

        self.tf_rx_timeout = ft.TextField(
            label="接收分包超时 (ms)",
            value=str(cfg.get("rx_timeout_ms", "20")),
            text_size=13,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            tooltip="在此时间内连续到达的字节合并为一帧显示（0 表示不合并）",
            on_change=lambda e: self._save_prefs(),
        )

        self.sw_dtr = ft.Checkbox(
            label="DTR",
            value=bool(cfg.get("dtr", False)),
            tooltip="Data Terminal Ready 电平控制（可用于复位 MCU）",
            on_change=self._on_dtr_change,
        )
        self.sw_rts = ft.Checkbox(
            label="RTS",
            value=bool(cfg.get("rts", False)),
            tooltip="Request To Send 电平控制",
            on_change=self._on_rts_change,
        )

        self.btn_connect = ft.FilledButton(
            content="打开串口",
            icon=ft.Icons.USB,
            expand=True,
            height=42,
            on_click=self.toggle_connect,
        )

        self.load_ports()

        self.adv_tile = ft.ExpansionTile(
            title=ft.Text("高级参数 (数据位/校验/流控/分包)", size=13, weight=ft.FontWeight.W_500),
            expanded=False,
            tile_padding=ft.Padding.symmetric(horizontal=4, vertical=0),
            controls_padding=ft.Padding.only(top=10, bottom=4),
            shape=ft.RoundedRectangleBorder(radius=8),
            collapsed_shape=ft.RoundedRectangleBorder(radius=8),
            controls=[
                ft.Container(
                    max_height=205,
                    content=ft.Column(
                        spacing=18,
                        tight=True,
                        scroll=ft.ScrollMode.AUTO,
                        controls=[
                            ft.Row(
                                spacing=10,
                                controls=[self.dd_bytesize, self.dd_stopbits],
                            ),
                            ft.Row(
                                spacing=10,
                                controls=[self.dd_parity, self.dd_flow],
                            ),
                            self.tf_rx_timeout,
                        ],
                    ),
                )
            ],
        )

        self.view = ft.Container(
            padding=ft.Padding.only(top=16, bottom=4),
            content=ft.Column(
                spacing=18,
                tight=True,
                controls=[
                    ft.Column(
                        spacing=6,
                        tight=True,
                        controls=[
                            ft.Row(
                                spacing=6,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[self.dd_ports, self.btn_refresh],
                            ),
                            self.txt_port_info,
                        ],
                    ),
                    ft.Row(controls=[self.dd_baud]),
                    self.tf_custom_baud,
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            self.btn_connect,
                            self.sw_dtr,
                            self.sw_rts,
                        ],
                    ),
                    self.adv_tile,
                ],
            ),
        )

    # --- 状态查询 ---

    @property
    def is_connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    @property
    def status_summary(self) -> str:
        if not self.is_connected or not self.ser:
            return "串口未连接"
        return (
            f"{self.ser.port} @ {self.ser.baudrate} "
            f"{self.dd_bytesize.value}{self.dd_parity.value}{self.dd_stopbits.value}"
        )

    def _get_effective_baud(self) -> int:
        raw = (
            self.tf_custom_baud.value
            if self.dd_baud.value == "CUSTOM"
            else self.dd_baud.value
        )
        baud = int((raw or "115200").strip())
        if baud <= 0:
            raise ValueError("波特率必须大于 0")
        return baud

    def _get_rx_timeout_sec(self) -> float:
        try:
            ms = float((self.tf_rx_timeout.value or "20").strip())
            return max(0.0, min(ms, 2000.0)) / 1000.0
        except ValueError:
            return 0.02

    def _save_prefs(self) -> None:
        try:
            baud_str = str(self._get_effective_baud())
        except ValueError:
            baud_str = "115200"
        self.config.update_section(
            "serial",
            baud=baud_str,
            bytesize=self.dd_bytesize.value or "8",
            parity=self.dd_parity.value or "N",
            stopbits=self.dd_stopbits.value or "1",
            flow_control=self.dd_flow.value or "None",
            rx_timeout_ms=self.tf_rx_timeout.value or "20",
            dtr=bool(self.sw_dtr.value),
            rts=bool(self.sw_rts.value),
        )

    # --- 端口管理 ---

    def load_ports(self) -> None:
        ports = list(serial.tools.list_ports.comports())
        self._port_details.clear()
        opts: list[ft.DropdownOption] = []
        for p in ports:
            clean_desc = (p.description or "").replace(f"({p.device})", "").strip()
            label_txt = f"{p.device} · {clean_desc}" if clean_desc and clean_desc != p.device else p.device
            opts.append(ft.DropdownOption(key=p.device, text=label_txt))
            vid_pid = (
                f"VID:PID={p.vid:04X}:{p.pid:04X}"
                if p.vid is not None and p.pid is not None
                else ""
            )
            detail = " | ".join(filter(None, [p.description, p.manufacturer, vid_pid]))
            self._port_details[p.device] = detail or p.device

        self.dd_ports.options = opts
        if ports:
            valid_keys = [p.device for p in ports]
            if self.dd_ports.value not in valid_keys:
                self.dd_ports.value = ports[0].device
            self.txt_port_info.value = self._port_details.get(self.dd_ports.value or "", "")
        else:
            self.dd_ports.value = None
            self.txt_port_info.value = "当前未发现可用串口设备"

    def _on_port_select(self, e: ft.Event[ft.Dropdown]) -> None:
        port = self.dd_ports.value or ""
        self.txt_port_info.value = self._port_details.get(port, port)
        self.view.update()

    def _on_baud_change(self, e: ft.Event[ft.Dropdown]) -> None:
        self.tf_custom_baud.visible = self.dd_baud.value == "CUSTOM"
        self._save_prefs()
        self.view.update()

    def refresh_click(self, e: ft.Event[ft.IconButton]) -> None:
        self.load_ports()
        self.bus.emit("INFO", f"已刷新串口列表，发现 {len(self.dd_ports.options)} 个端口", source="UART")
        self.view.update()

    def _on_dtr_change(self, e: ft.Event[ft.Checkbox]) -> None:
        self._save_prefs()
        if self.ser and self.ser.is_open:
            try:
                self.ser.dtr = bool(self.sw_dtr.value)
                self.bus.emit("INFO", f"DTR 已置为 {'HIGH (True)' if self.sw_dtr.value else 'LOW (False)'}", source="UART")
            except Exception as exc:
                self.bus.emit("ERROR", f"设置 DTR 失败: {exc}", source="UART")

    def _on_rts_change(self, e: ft.Event[ft.Checkbox]) -> None:
        self._save_prefs()
        if self.ser and self.ser.is_open:
            try:
                self.ser.rts = bool(self.sw_rts.value)
                self.bus.emit("INFO", f"RTS 已置为 {'HIGH (True)' if self.sw_rts.value else 'LOW (False)'}", source="UART")
            except Exception as exc:
                self.bus.emit("ERROR", f"设置 RTS 失败: {exc}", source="UART")

    # --- 打开/关闭与读写 ---

    def _set_controls_locked(self, locked: bool) -> None:
        self.dd_ports.disabled = locked
        self.btn_refresh.disabled = locked
        self.dd_baud.disabled = locked
        self.tf_custom_baud.disabled = locked
        self.dd_bytesize.disabled = locked
        self.dd_stopbits.disabled = locked
        self.dd_parity.disabled = locked
        self.dd_flow.disabled = locked

    def _sync_ui_disconnected(self) -> None:
        self.btn_connect.content = "打开串口"
        self.btn_connect.icon = ft.Icons.USB
        self.btn_connect.style = None
        self._set_controls_locked(False)
        safe_update(self.view)
        self.on_status_change()

    def toggle_connect(self, e: ft.Event[ft.FilledButton] | None = None) -> None:
        if self.is_connected:
            self.close()
        else:
            port = self.dd_ports.value
            if not port:
                self.bus.emit("ERROR", "请先选择有效的串口端口", source="UART")
                return
            try:
                baud = self._get_effective_baud()
                flow = self.dd_flow.value or "None"
                ser = serial.Serial()
                ser.port = port
                ser.baudrate = baud
                ser.bytesize = BYTESIZE_MAP.get(self.dd_bytesize.value or "8", serial.EIGHTBITS)
                ser.parity = PARITY_MAP.get(self.dd_parity.value or "N", serial.PARITY_NONE)
                ser.stopbits = STOPBITS_MAP.get(self.dd_stopbits.value or "1", serial.STOPBITS_ONE)
                ser.rtscts = flow == "RTS/CTS"
                ser.xonxoff = flow == "XON/XOFF"
                ser.timeout = 0.02
                ser.write_timeout = 1.0
                if flow != "RTS/CTS":
                    ser.dtr = bool(self.sw_dtr.value)
                    ser.rts = bool(self.sw_rts.value)
                ser.open()

                self.ser = ser
                self.reading = True
                self._save_prefs()

                self.btn_connect.content = "关闭串口"
                self.btn_connect.icon = ft.Icons.USB_OFF
                self.btn_connect.style = ft.ButtonStyle(
                    bgcolor=ft.Colors.RED_600,
                    color=ft.Colors.WHITE,
                )
                self._set_controls_locked(True)
                self.bus.emit("INFO", f"已打开串口 {self.status_summary}", source="UART")
                threading.Thread(target=self._read_loop, daemon=True).start()
                self.view.update()
                self.on_status_change()
            except Exception as exc:
                self.bus.emit("ERROR", f"打开串口失败: {exc}", source="UART")

    def close(self) -> None:
        self.reading = False
        port_name = self.ser.port if self.ser else ""
        if self.ser:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None
        if port_name:
            self.bus.emit("INFO", f"串口 {port_name} 已关闭", source="UART")
        self._sync_ui_disconnected()

    def _read_loop(self) -> None:
        buf = bytearray()
        last_rx_time = 0.0

        while self.reading and self.ser and self.ser.is_open:
            try:
                waiting = self.ser.in_waiting
                if waiting > 0:
                    chunk = self.ser.read(waiting)
                    if chunk:
                        timeout_sec = self._get_rx_timeout_sec()
                        if timeout_sec <= 0:
                            self.bus.emit("RX", bytes(chunk), source="UART")
                        else:
                            buf.extend(chunk)
                            last_rx_time = time.monotonic()
                            if len(buf) >= 4096:
                                self.bus.emit("RX", bytes(buf), source="UART")
                                buf.clear()
                else:
                    if buf:
                        timeout_sec = self._get_rx_timeout_sec()
                        if (time.monotonic() - last_rx_time) >= timeout_sec:
                            self.bus.emit("RX", bytes(buf), source="UART")
                            buf.clear()
                    time.sleep(0.005)
            except Exception as exc:
                if buf:
                    self.bus.emit("RX", bytes(buf), source="UART")
                    buf.clear()
                if self.reading:
                    self.bus.emit("ERROR", f"串口读取异常断开: {exc}", source="UART")
                    self.reading = False
                    try:
                        if self.ser:
                            self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
                    self.bus.call_ui(self._sync_ui_disconnected)
                break

        if buf:
            self.bus.emit("RX", bytes(buf), source="UART")
            buf.clear()

    def send(self, data: bytes) -> bool:
        if not self.ser or not self.ser.is_open:
            self.bus.emit("ERROR", "发送失败：串口未打开", source="UART")
            return False
        try:
            self.ser.write(data)
            self.bus.emit("TX", data, source="UART")
            return True
        except Exception as exc:
            self.bus.emit("ERROR", f"串口写入失败: {exc}", source="UART")
            return False
