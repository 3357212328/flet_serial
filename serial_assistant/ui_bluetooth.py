import asyncio
from typing import Any, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
import flet as ft

from .config import AppConfig
from .event_bus import EventBus
from .utils import safe_update

PREFERRED_WRITE_UUID_KEYWORDS = (
    "6e400002",  # Nordic NUS RX
    "0000ffe1",  # HM-10 / JDY BLE UART
    "0000fff2",  # Common BLE transparent UART
    "0000ffe9",
)

PREFERRED_NOTIFY_UUID_KEYWORDS = (
    "6e400003",  # Nordic NUS TX
    "0000ffe1",  # HM-10 / JDY BLE UART
    "0000fff1",
    "0000ffe4",
)

GENERIC_SKIP_UUID_KEYWORDS = (
    "00002a00",  # Device Name
    "00002a01",  # Appearance
    "00002a05",  # Service Changed
)


class BluetoothChannel:
    """增强型低功耗蓝牙 (BLE GATT) 模块：支持信号强度排序、过滤无名设备、特征值智能选择与 MTU 分包."""

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

        self.client: BleakClient | None = None
        self.scanned_devices: dict[str, Any] = {}
        self.device_names: dict[str, str] = {}
        self.write_chars: dict[str, BleakGATTCharacteristic] = {}
        self.notify_chars: dict[str, BleakGATTCharacteristic] = {}
        self._active_notify_key: str | None = None

        cfg = self.config.get("ble", {})

        # --- UI 组件 ---
        self.dd_devices = ft.Dropdown(
            label="BLE 蓝牙设备 (按信号强度排序)",
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[],
        )

        self.tf_scan_sec = ft.TextField(
            label="扫描时长 (秒)",
            value=str(cfg.get("scan_timeout", "5")),
            width=148,
            text_size=13,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            on_change=lambda e: self._save_prefs(),
        )

        self.sw_filter_unnamed = ft.Checkbox(
            label="过滤无名设备",
            value=bool(cfg.get("filter_unnamed", True)),
            on_change=lambda e: self._save_prefs(),
        )

        self.btn_scan = ft.FilledTonalButton(
            content="扫描蓝牙",
            icon=ft.Icons.BLUETOOTH_SEARCHING,
            expand=True,
            height=42,
            on_click=self._on_scan_click,
        )

        self.btn_connect = ft.FilledButton(
            content="连接设备",
            icon=ft.Icons.BLUETOOTH_CONNECTED,
            expand=True,
            height=42,
            on_click=self._on_connect_click,
        )

        self.scan_progress = ft.ProgressBar(visible=False, bar_height=3)

        self.dd_write_char = ft.Dropdown(
            label="写入特征值 (Write)",
            expand=True,
            expanded_insets=0,
            text_size=12.5,
            disabled=True,
            options=[],
        )

        self.dd_notify_char = ft.Dropdown(
            label="监听特征值 (Notify)",
            expand=True,
            expanded_insets=0,
            text_size=12.5,
            disabled=True,
            options=[],
            on_select=self._on_notify_char_change,
        )

        self.tf_chunk_size = ft.TextField(
            label="BLE 分包长度 (Bytes)",
            value=str(cfg.get("chunk_size", "240")),
            text_size=13,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            tooltip="单次 GATT 写入最大字节数，超长报文自动切片发送（经典低版本 BLE 可设为 20）",
            on_change=lambda e: self._save_prefs(),
        )

        self.gatt_tile = ft.ExpansionTile(
            title=ft.Text("GATT 特征值与分包设置", size=13, weight=ft.FontWeight.W_500),
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
                            ft.Row(controls=[self.dd_write_char]),
                            ft.Row(controls=[self.dd_notify_char]),
                            self.tf_chunk_size,
                        ],
                    ),
                )
            ],
        )

        self.view = ft.Container(
            padding=ft.Padding.only(top=16, bottom=2),
            content=ft.Column(
                spacing=22,
                tight=True,
                controls=[
                    ft.Row(controls=[self.dd_devices]),
                    ft.Row(
                        spacing=14,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[self.tf_scan_sec, self.sw_filter_unnamed],
                    ),
                    ft.Column(
                        spacing=6,
                        tight=True,
                        controls=[
                            self.scan_progress,
                            ft.Row(
                                spacing=10,
                                controls=[self.btn_scan, self.btn_connect],
                            ),
                        ],
                    ),
                    self.gatt_tile,
                ],
            ),
        )

    # --- 状态属性 ---

    @property
    def is_connected(self) -> bool:
        return bool(self.client and getattr(self.client, "is_connected", False))

    @property
    def status_summary(self) -> str:
        if not self.is_connected:
            return "蓝牙未连接"
        addr = self.dd_devices.value or ""
        name = self.device_names.get(addr, addr)
        return f"BLE: {name}"

    def _save_prefs(self) -> None:
        self.config.update_section(
            "ble",
            scan_timeout=self.tf_scan_sec.value or "5",
            filter_unnamed=bool(self.sw_filter_unnamed.value),
            chunk_size=self.tf_chunk_size.value or "240",
        )

    # --- 扫描逻辑 ---

    def _on_scan_click(self, e: ft.Event[ft.FilledTonalButton]) -> None:
        self.page.run_task(self.start_scan)

    async def start_scan(self) -> None:
        try:
            timeout = max(1.0, min(float(self.tf_scan_sec.value or "5"), 30.0))
        except ValueError:
            timeout = 5.0

        self.btn_scan.disabled = True
        self.scan_progress.visible = True
        self.view.update()
        self.bus.emit("INFO", f"正在扫描周边 BLE 设备 ({timeout:.0f}s)...", source="BLE")

        try:
            discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
            items: list[tuple[int, str, str, Any]] = []
            hide_unnamed = bool(self.sw_filter_unnamed.value)

            if isinstance(discovered, dict):
                for addr, pair in discovered.items():
                    dev, adv = pair
                    name = (getattr(adv, "local_name", None) or dev.name or "").strip()
                    if hide_unnamed and not name:
                        continue
                    rssi = int(getattr(adv, "rssi", -100) or -100)
                    disp_name = name or "Unknown"
                    items.append((rssi, addr, disp_name, dev))
            else:
                for dev in discovered:
                    name = (dev.name or "").strip()
                    if hide_unnamed and not name:
                        continue
                    items.append((-99, dev.address, name or "Unknown", dev))

            # 按信号强度从高到低排序
            items.sort(key=lambda x: x[0], reverse=True)

            self.scanned_devices = {addr: dev for _, addr, _, dev in items}
            self.device_names = {addr: name for _, addr, name, _ in items}
            self.dd_devices.options = [
                ft.DropdownOption(
                    key=addr,
                    text=f"[{rssi} dBm] {name} ({addr})",
                )
                for rssi, addr, name, _ in items
            ]
            if items:
                self.dd_devices.value = items[0][1]
            else:
                self.dd_devices.value = None

            self.bus.emit("INFO", f"BLE 扫描完成，共筛选出 {len(items)} 个设备", source="BLE")
        except Exception as exc:
            self.bus.emit("ERROR", f"BLE 扫描失败: {exc}", source="BLE")
        finally:
            self.btn_scan.disabled = False
            self.scan_progress.visible = False
            safe_update(self.view)

    # --- 连接与特征值发现 ---

    def _on_connect_click(self, e: ft.Event[ft.FilledButton]) -> None:
        self.page.run_task(self.toggle_connect)

    async def toggle_connect(self) -> None:
        if self.is_connected:
            await self.disconnect_async()
            return

        addr = self.dd_devices.value
        if not addr:
            self.bus.emit("ERROR", "请先扫描并选择要连接的 BLE 设备", source="BLE")
            return

        self.btn_connect.content = "连接中..."
        self.btn_connect.disabled = True
        self.view.update()

        try:
            target = self.scanned_devices.get(addr, addr)
            client = BleakClient(target, disconnected_callback=self._on_ble_disconnected)
            await client.connect()
            self.client = client

            self._discover_gatt_characteristics(client)

            if self.dd_notify_char.value:
                await self._subscribe_notify(self.dd_notify_char.value)

            self.btn_connect.content = "断开蓝牙"
            self.btn_connect.icon = ft.Icons.BLUETOOTH_DISABLED
            self.btn_connect.style = ft.ButtonStyle(
                bgcolor=ft.Colors.RED_600,
                color=ft.Colors.WHITE,
            )
            self.btn_connect.disabled = False
            self.dd_devices.disabled = True
            self.btn_scan.disabled = True

            self.bus.emit(
                "INFO",
                f"已连接 {self.device_names.get(addr, addr)} "
                f"(可写特征: {len(self.write_chars)}, 通知特征: {len(self.notify_chars)})",
                source="BLE",
            )
            self.view.update()
            self.on_status_change()
        except Exception as exc:
            self.client = None
            self.bus.emit("ERROR", f"BLE 连接失败: {exc}", source="BLE")
            self._sync_ui_disconnected()

    def _discover_gatt_characteristics(self, client: BleakClient) -> None:
        self.write_chars.clear()
        self.notify_chars.clear()
        w_opts: list[ft.DropdownOption] = []
        n_opts: list[ft.DropdownOption] = []

        preferred_w_key: str | None = None
        fallback_w_key: str | None = None
        preferred_n_key: str | None = None
        fallback_n_key: str | None = None

        for service in client.services:
            for char in service.characteristics:
                key = f"{char.handle}:{char.uuid}"
                short_uuid = char.uuid.split("-")[0] if "-" in char.uuid else char.uuid
                props_str = ",".join(char.properties)
                label = f"[0x{char.handle:02X}] {short_uuid} ({props_str})"
                uuid_lower = char.uuid.lower()
                is_generic = any(g in uuid_lower for g in GENERIC_SKIP_UUID_KEYWORDS)

                if "write" in char.properties or "write-without-response" in char.properties:
                    self.write_chars[key] = char
                    w_opts.append(ft.DropdownOption(key=key, text=label))
                    if any(k in uuid_lower for k in PREFERRED_WRITE_UUID_KEYWORDS):
                        preferred_w_key = key
                    elif not is_generic and fallback_w_key is None:
                        fallback_w_key = key

                if "notify" in char.properties or "indicate" in char.properties:
                    self.notify_chars[key] = char
                    n_opts.append(ft.DropdownOption(key=key, text=label))
                    if any(k in uuid_lower for k in PREFERRED_NOTIFY_UUID_KEYWORDS):
                        preferred_n_key = key
                    elif not is_generic and fallback_n_key is None:
                        fallback_n_key = key

        self.dd_write_char.options = w_opts
        self.dd_write_char.value = (
            preferred_w_key
            or fallback_w_key
            or (w_opts[0].key if w_opts else None)
        )
        self.dd_write_char.disabled = len(w_opts) == 0

        self.dd_notify_char.options = n_opts
        self.dd_notify_char.value = (
            preferred_n_key
            or fallback_n_key
            or (n_opts[0].key if n_opts else None)
        )
        self.dd_notify_char.disabled = len(n_opts) == 0

    async def _subscribe_notify(self, char_key: str) -> None:
        if not self.client or not self.is_connected:
            return
        if self._active_notify_key and self._active_notify_key in self.notify_chars:
            old_char = self.notify_chars[self._active_notify_key]
            try:
                await self.client.stop_notify(old_char)
            except Exception:
                pass
            self._active_notify_key = None

        char = self.notify_chars.get(char_key)
        if not char:
            return
        try:
            await self.client.start_notify(char, self._notification_handler)
            self._active_notify_key = char_key
            self.bus.emit("INFO", f"已订阅通知特征值: {char.uuid}", source="BLE")
        except Exception as exc:
            self.bus.emit("ERROR", f"订阅通知特征失败: {exc}", source="BLE")

    def _on_notify_char_change(self, e: ft.Event[ft.Dropdown]) -> None:
        if self.dd_notify_char.value:
            self.page.run_task(self._subscribe_notify, self.dd_notify_char.value)

    def _notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self.bus.emit("RX", bytes(data), source="BLE")

    def _on_ble_disconnected(self, client: BleakClient) -> None:
        self.client = None
        self._active_notify_key = None
        self.bus.emit("INFO", "蓝牙设备连接已断开", source="BLE")
        self.bus.call_ui(self._sync_ui_disconnected)

    def _sync_ui_disconnected(self) -> None:
        self.btn_connect.content = "连接设备"
        self.btn_connect.icon = ft.Icons.BLUETOOTH_CONNECTED
        self.btn_connect.style = None
        self.btn_connect.disabled = False
        self.dd_devices.disabled = False
        self.btn_scan.disabled = False
        self.dd_write_char.disabled = True
        self.dd_notify_char.disabled = True
        safe_update(self.view)
        self.on_status_change()

    async def disconnect_async(self) -> None:
        client = self.client
        self.client = None
        self._active_notify_key = None
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        self._sync_ui_disconnected()

    def close(self) -> None:
        if self.is_connected:
            self.page.run_task(self.disconnect_async)

    # --- 数据发送（自动按 MTU 切片） ---

    async def _send_async(self, data: bytes) -> None:
        if not self.client or not self.is_connected:
            self.bus.emit("ERROR", "发送失败：蓝牙未连接", source="BLE")
            return
        w_key = self.dd_write_char.value
        char = self.write_chars.get(w_key or "")
        if not char:
            self.bus.emit("ERROR", "未选择可用的 BLE 写入特征值", source="BLE")
            return

        try:
            chunk_limit = max(1, min(int(self.tf_chunk_size.value or "240"), 512))
        except ValueError:
            chunk_limit = 240

        # 优先使用无响应快速写入，否则使用带响应写入
        use_response = "write-without-response" not in char.properties
        try:
            for offset in range(0, len(data), chunk_limit):
                part = data[offset : offset + chunk_limit]
                await self.client.write_gatt_char(char, part, response=use_response)
                if offset + chunk_limit < len(data):
                    await asyncio.sleep(0.01)
            self.bus.emit("TX", data, source="BLE")
        except Exception as exc:
            self.bus.emit("ERROR", f"BLE 发送失败: {exc}", source="BLE")

    def send(self, data: bytes) -> bool:
        if not self.is_connected:
            self.bus.emit("ERROR", "发送失败：蓝牙未连接", source="BLE")
            return False
        self.page.run_task(self._send_async, data)
        return True
