import asyncio
import ctypes
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import flet as ft


def _setup_compiled_runtime_env() -> None:
    """配置 Nuitka/打包环境下的 Flet 运行时与 Windows 任务栏标识。"""
    compiled = globals().get("__compiled__")
    if compiled is None and not getattr(sys, "frozen", False):
        return
    os.environ.setdefault("FLET_SERVER_IP", "127.0.0.1")
    if sys.platform == "win32":
        os.environ.setdefault("FLET_DESKTOP_FLAVOR", "full")
        exe_path = (
            getattr(compiled, "process_exe", None)
            if compiled is not None
            else None
        ) or os.path.abspath(sys.argv[0] or sys.executable)
        if "FLET_APP_USER_MODEL_ID" not in os.environ:
            os.environ["FLET_APP_USER_MODEL_ID"] = exe_path
            os.environ.setdefault("FLET_APP_RELAUNCH_COMMAND", f'"{exe_path}"')
            os.environ.setdefault("FLET_APP_RELAUNCH_DISPLAY_NAME", "OmniPort Studio")
            os.environ.setdefault("FLET_APP_RELAUNCH_ICON", f"{exe_path},0")


_setup_compiled_runtime_env()

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON_ICO = ASSETS_DIR / "icon.ico"


def apply_win32_window_icon(window_title: str, ico_path: Path) -> None:
    """在 Windows 下强制刷新窗口标题栏与任务栏图标（兼容 flet.exe 宿主进程）。"""
    if sys.platform != "win32" or not ico_path.exists():
        return

    def _worker():
        try:
            user32 = ctypes.windll.user32
            try:
                aumid = os.environ.get("FLET_APP_USER_MODEL_ID") or "flet.omniport.studio.v2"
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(aumid)
            except Exception:
                pass
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x00000010
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            GCLP_HICON = -14
            GCLP_HICONSM = -34

            hicon_small = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            hicon_big = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
            if not hicon_big:
                hicon_big = user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 64, 64, LR_LOADFROMFILE)
            if not hicon_small and not hicon_big:
                return

            set_class_long = getattr(user32, "SetClassLongPtrW", getattr(user32, "SetClassLongW", None))

            for _ in range(30):
                hwnd = user32.FindWindowW("FLUTTER_RUNNER_WIN32_WINDOW", window_title)
                if not hwnd:
                    hwnd = user32.FindWindowW(None, window_title)
                if hwnd:
                    if hicon_small:
                        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
                    if hicon_big:
                        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                    if set_class_long:
                        if hicon_big:
                            set_class_long(hwnd, GCLP_HICON, hicon_big)
                        if hicon_small:
                            set_class_long(hwnd, GCLP_HICONSM, hicon_small)
                time.sleep(0.15)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()

from serial_assistant.config import AppConfig
from serial_assistant.event_bus import EventBus, PacketEvent, match_auto_replies
from serial_assistant.ui_bluetooth import BluetoothChannel
from serial_assistant.ui_log import TerminalLogPanel
from serial_assistant.ui_macros import MacrosDock
from serial_assistant.ui_network import NetworkChannel
from serial_assistant.ui_plotter import WaveformPlotterPanel
from serial_assistant.ui_serial import SerialChannel
from serial_assistant.ui_tools_dialogs import open_auto_reply_dialog, open_toolbox_dialog
from serial_assistant.utils import (
    append_checksum,
    apply_eol,
    format_byte_size,
    parse_hex_string,
    safe_update,
    unescape_text,
)


async def main(page: ft.Page) -> None:
    config = AppConfig()
    ui_cfg = config.get("ui", {})
    tx_cfg = config.get("tx", {})

    # --- 页面主题与窗口初始化 ---
    page.title = "OmniPort Studio · 多功能串口/蓝牙/网络调试助手"
    saved_theme = str(ui_cfg.get("theme_mode", "dark")).lower()
    page.theme_mode = ft.ThemeMode.LIGHT if saved_theme == "light" else ft.ThemeMode.DARK
    page.theme = ft.Theme(
        color_scheme_seed=ft.Colors.INDIGO,
        font_family="Microsoft YaHei",
    )
    page.dark_theme = ft.Theme(
        color_scheme_seed=ft.Colors.CYAN,
        font_family="Microsoft YaHei",
    )
    page.padding = 12
    page.spacing = 10

    try:
        if APP_ICON_ICO.exists():
            page.window.icon = str(APP_ICON_ICO)
            apply_win32_window_icon(page.title, APP_ICON_ICO)
        page.window.width = 1320
        page.window.height = 880
        page.window.min_width = 1000
        page.window.min_height = 700
        await page.window.center()
    except Exception:
        pass

    # --- 注册 Flet 1.x 原生服务 ---
    clipboard = ft.Clipboard()
    file_picker = ft.FilePicker()

    bus = EventBus()
    app_running = True

    # --- 顶部状态指示与流量统计组件 ---
    status_dot = ft.Icon(ft.Icons.CIRCLE, size=10, color=ft.Colors.OUTLINE)
    status_text = ft.Text(
        "未连接设备",
        size=12,
        weight=ft.FontWeight.W_500,
    )
    status_pill = ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
        border_radius=16,
        bgcolor=ft.Colors.with_opacity(0.45, ft.Colors.SURFACE_CONTAINER_HIGHEST),
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        content=ft.Row(
            spacing=6,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[status_dot, status_text],
        ),
    )

    txt_rx_stat = ft.Text(
        "RX: 0 B (0)",
        size=11.5,
        font_family="Consolas",
        color=(
            ft.Colors.CYAN_700
            if page.theme_mode == ft.ThemeMode.LIGHT
            else ft.Colors.CYAN_300
        ),
    )
    txt_tx_stat = ft.Text(
        "TX: 0 B (0)",
        size=11.5,
        font_family="Consolas",
        color=(
            ft.Colors.GREEN_700
            if page.theme_mode == ft.ThemeMode.LIGHT
            else ft.Colors.GREEN_300
        ),
    )
    txt_rate_stat = ft.Text(
        "↓0 B/s  ↑0 B/s",
        size=11,
        font_family="Consolas",
        color=ft.Colors.with_opacity(0.65, ft.Colors.ON_SURFACE),
    )

    # --- 核心视图与通道实例 ---
    log_panel = TerminalLogPanel(page, config, clipboard, file_picker)
    plotter_panel = WaveformPlotterPanel(page)

    def refresh_connection_pill() -> None:
        active_labels: list[str] = []
        if serial_ch.is_connected:
            active_labels.append(serial_ch.status_summary)
        if ble_ch.is_connected:
            active_labels.append(ble_ch.status_summary)
        if net_ch.is_connected:
            active_labels.append(net_ch.status_summary)

        if active_labels:
            status_dot.color = ft.Colors.GREEN_500
            status_text.value = "  |  ".join(active_labels)
        else:
            status_dot.color = ft.Colors.OUTLINE
            status_text.value = "未连接设备"
        safe_update(status_pill)

    serial_ch = SerialChannel(page, config, bus, refresh_connection_pill)
    ble_ch = BluetoothChannel(page, config, bus, refresh_connection_pill)
    net_ch = NetworkChannel(page, config, bus, clipboard, refresh_connection_pill)

    # --- 侧边栏：连接通道切换 + 常驻快捷指令 ---
    active_conn_tab = "UART"
    conn_body = ft.Container(content=serial_ch.view)

    def _on_conn_tab_change(e: ft.Event[ft.SegmentedButton]) -> None:
        nonlocal active_conn_tab
        sel = list(e.control.selected)
        if not sel:
            return
        active_conn_tab = sel[0]
        if active_conn_tab == "UART":
            conn_body.content = serial_ch.view
        elif active_conn_tab == "BLE":
            conn_body.content = ble_ch.view
        else:
            conn_body.content = net_ch.view
        conn_body.update()

    seg_channel = ft.SegmentedButton(
        selected=["UART"],
        allow_empty_selection=False,
        allow_multiple_selection=False,
        show_selected_icon=False,
        expand=True,
        segments=[
            ft.Segment(value="UART", label="串口", icon=ft.Icons.USB),
            ft.Segment(value="BLE", label="蓝牙", icon=ft.Icons.BLUETOOTH),
            ft.Segment(value="NET", label="网络", icon=ft.Icons.LAN),
        ],
        on_change=_on_conn_tab_change,
    )

    # --- 发送路由逻辑 ---
    dd_target = ft.Dropdown(
        value="AUTO",
        width=192,
        expanded_insets=0,
        dense=True,
        text_size=13,
        content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        tooltip="选择数据发送目标通道",
        options=[
            ft.DropdownOption(key="AUTO", text="自动活动通道"),
            ft.DropdownOption(key="UART", text="仅串口 (UART)"),
            ft.DropdownOption(key="BLE", text="仅蓝牙 (BLE)"),
            ft.DropdownOption(key="NET", text="仅网络 (TCP/UDP)"),
            ft.DropdownOption(key="ALL", text="全部已连接通道"),
        ],
    )

    def dispatch_bytes(payload: bytes) -> bool:
        if not payload:
            return False
        mode = dd_target.value or "AUTO"

        if mode == "UART":
            return serial_ch.send(payload)
        if mode == "BLE":
            return ble_ch.send(payload)
        if mode == "NET":
            return net_ch.send(payload)
        if mode == "ALL":
            sent_any = False
            if serial_ch.is_connected:
                sent_any = serial_ch.send(payload) or sent_any
            if ble_ch.is_connected:
                sent_any = ble_ch.send(payload) or sent_any
            if net_ch.is_connected:
                sent_any = net_ch.send(payload) or sent_any
            if not sent_any:
                page.show_dialog(
                    ft.SnackBar(
                        ft.Text("发送失败：当前没有任何已连接的通信通道"),
                        bgcolor=ft.Colors.ERROR,
                    )
                )
            return sent_any

        # AUTO 模式：优先使用当前正在查看且已连接的通道，否则按已连接顺序匹配
        channel_map = {
            "UART": serial_ch,
            "BLE": ble_ch,
            "NET": net_ch,
        }
        preferred = channel_map.get(active_conn_tab)
        if preferred and preferred.is_connected:
            return preferred.send(payload)
        for ch in (serial_ch, ble_ch, net_ch):
            if ch.is_connected:
                return ch.send(payload)

        page.show_dialog(
            ft.SnackBar(
                ft.Text("未连接任何设备，请先在左侧打开串口、蓝牙或网络通道"),
                bgcolor=ft.Colors.ERROR,
            )
        )
        return False

    macros_dock = MacrosDock(
        page=page,
        config=config,
        file_picker=file_picker,
        send_bytes_callback=lambda b: dispatch_bytes(b),
    )

    sidebar_visible = bool(ui_cfg.get("sidebar_visible", True))
    sidebar_container = ft.Container(
        width=360,
        visible=sidebar_visible,
        content=ft.Column(
            expand=True,
            spacing=12,
            controls=[
                ft.Container(
                    padding=14,
                    border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                    border_radius=12,
                    bgcolor=ft.Colors.with_opacity(0.28, ft.Colors.SURFACE_CONTAINER_HIGH),
                    content=ft.Column(
                        spacing=10,
                        tight=True,
                        controls=[
                            ft.Row(controls=[seg_channel]),
                            conn_body,
                        ],
                    ),
                ),
                ft.Container(
                    expand=True,
                    padding=14,
                    border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                    border_radius=12,
                    bgcolor=ft.Colors.with_opacity(0.28, ft.Colors.SURFACE_CONTAINER_HIGH),
                    content=macros_dock.view,
                ),
            ],
        ),
    )

    # --- 右侧主工作区（终端日志 / 实时波形 切换） ---
    active_work_view = "LOG"
    work_body = ft.Container(expand=True, content=log_panel.view)

    def _on_work_view_change(e: ft.Event[ft.SegmentedButton]) -> None:
        nonlocal active_work_view
        sel = list(e.control.selected)
        if not sel:
            return
        active_work_view = sel[0]
        if active_work_view == "LOG":
            work_body.content = log_panel.view
            log_panel.rebuild_view()
        else:
            work_body.content = plotter_panel.view
            plotter_panel._dirty = True
            plotter_panel.refresh_if_dirty()
        work_body.update()

    seg_workspace = ft.SegmentedButton(
        selected=["LOG"],
        allow_empty_selection=False,
        allow_multiple_selection=False,
        show_selected_icon=False,
        segments=[
            ft.Segment(value="LOG", label="终端收发日志", icon=ft.Icons.TERMINAL),
            ft.Segment(value="PLOT", label="实时数字示波器", icon=ft.Icons.SHOW_CHART),
        ],
        on_change=_on_work_view_change,
    )

    # --- 底部发送编辑器区 ---
    sw_hex_send = ft.Switch(
        label="HEX 发送",
        value=bool(tx_cfg.get("hex_send", False)),
    )

    dd_eol = ft.Dropdown(
        value=str(tx_cfg.get("eol", "CRLF")),
        width=168,
        expanded_insets=0,
        dense=True,
        text_size=13,
        disabled=bool(tx_cfg.get("hex_send", False)),
        content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        tooltip="文本模式下自动追加行尾符",
        options=[
            ft.DropdownOption(key="CRLF", text="追加 \\r\\n"),
            ft.DropdownOption(key="LF", text="追加 \\n"),
            ft.DropdownOption(key="CR", text="追加 \\r"),
            ft.DropdownOption(key="NONE", text="不追加换行"),
        ],
    )

    dd_checksum = ft.Dropdown(
        value=str(tx_cfg.get("checksum", "NONE")),
        width=220,
        expanded_insets=0,
        dense=True,
        text_size=13,
        content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        tooltip="在发送字节末尾自动计算并附加校验码",
        options=[
            ft.DropdownOption(key="NONE", text="无附加校验"),
            ft.DropdownOption(key="MODBUS_LE", text="Modbus CRC16 (LE)"),
            ft.DropdownOption(key="MODBUS_BE", text="Modbus CRC16 (BE)"),
            ft.DropdownOption(key="CRC16_CCITT", text="CRC16-CCITT"),
            ft.DropdownOption(key="XOR", text="XOR 异或校验"),
            ft.DropdownOption(key="SUM8", text="SUM8 累加和"),
        ],
    )

    tf_tx_input = ft.TextField(
        hint_text=(
            "输入十六进制字节，例如: 01 03 00 00 00 02"
            if sw_hex_send.value
            else "输入要发送的指令或文本（支持 \\r \\n \\t 转义）..."
        ),
        multiline=True,
        min_lines=2,
        max_lines=4,
        expand=True,
        text_style=ft.TextStyle(font_family="Consolas", size=13),
    )

    def _save_tx_prefs(_: Any = None) -> None:
        dd_eol.disabled = bool(sw_hex_send.value)
        tf_tx_input.hint_text = (
            "输入十六进制字节，例如: 01 03 00 00 00 02"
            if sw_hex_send.value
            else "输入要发送的指令或文本（支持 \\r \\n \\t 转义）..."
        )
        config.update_section(
            "tx",
            hex_send=bool(sw_hex_send.value),
            eol=dd_eol.value or "CRLF",
            checksum=dd_checksum.value or "NONE",
            auto_interval_ms=tf_auto_ms.value or "1000",
        )
        page.update()

    sw_hex_send.on_change = _save_tx_prefs
    dd_eol.on_select = _save_tx_prefs
    dd_checksum.on_select = _save_tx_prefs

    # 发送历史菜单
    history_menu = ft.PopupMenuButton(
        icon=ft.Icons.HISTORY,
        icon_size=20,
        tooltip="最近发送历史记录",
        items=[],
    )

    def _rebuild_history_menu() -> None:
        hist: list[str] = list(config.get("tx", {}).get("history", []))
        if not hist:
            history_menu.items = [ft.PopupMenuItem(content="暂无发送历史")]
            return
        items: list[ft.PopupMenuItem] = []
        for entry in hist[:15]:
            disp = entry.replace("\r", "\\r").replace("\n", "\\n")
            if len(disp) > 45:
                disp = disp[:42] + "..."
            items.append(
                ft.PopupMenuItem(
                    content=disp,
                    on_click=lambda e, raw=entry: _fill_tx_box(raw, None),
                )
            )
        history_menu.items = items

    def _fill_tx_box(content: str, force_hex: bool | None) -> None:
        tf_tx_input.value = content
        if force_hex is not None:
            sw_hex_send.value = force_hex
            _save_tx_prefs()
        else:
            tf_tx_input.update()

    def _record_history(raw_text: str) -> None:
        cleaned = raw_text.strip()
        if not cleaned:
            return
        hist: list[str] = list(config.get("tx", {}).get("history", []))
        if cleaned in hist:
            hist.remove(cleaned)
        hist.insert(0, cleaned)
        hist = hist[:15]
        config.update_section("tx", history=hist)
        _rebuild_history_menu()
        safe_update(history_menu)

    _rebuild_history_menu()

    def send_composer_data() -> bool:
        raw_input = tf_tx_input.value or ""
        if not raw_input:
            return False
        encoding = log_panel.encoding
        try:
            if sw_hex_send.value:
                payload = parse_hex_string(raw_input)
            else:
                text_val = unescape_text(raw_input)
                text_val = apply_eol(text_val, dd_eol.value or "CRLF")
                payload = text_val.encode(encoding, errors="replace")

            payload = append_checksum(payload, dd_checksum.value or "NONE")
        except ValueError as err:
            bus.emit("ERROR", f"发送格式错误: {err}", source="SYS")
            return False

        ok = dispatch_bytes(payload)
        if ok:
            _record_history(raw_input)
        return ok

    # --- 定时循环发送（一体化胶囊组件） ---
    auto_send_active = False
    tf_auto_ms = ft.TextField(
        value=str(tx_cfg.get("auto_interval_ms", "1000")),
        width=62,
        dense=True,
        border=ft.NoInputBorder(),
        text_align=ft.TextAlign.CENTER,
        text_style=ft.TextStyle(
            font_family="Consolas",
            size=13,
            weight=ft.FontWeight.W_600,
        ),
        content_padding=ft.Padding.symmetric(horizontal=4, vertical=4),
        on_change=_save_tx_prefs,
    )
    sw_auto_send = ft.Checkbox(label="定时发送", value=False)

    auto_send_capsule = ft.Container(
        height=38,
        padding=ft.Padding.only(left=6, right=10),
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=8,
        bgcolor=ft.Colors.with_opacity(0.32, ft.Colors.SURFACE_CONTAINER_HIGHEST),
        content=ft.Row(
            spacing=6,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                sw_auto_send,
                ft.Container(
                    width=1,
                    height=18,
                    bgcolor=ft.Colors.OUTLINE_VARIANT,
                ),
                ft.Container(
                    height=26,
                    border_radius=5,
                    bgcolor=ft.Colors.SURFACE,
                    border=ft.Border.all(
                        1, ft.Colors.with_opacity(0.6, ft.Colors.OUTLINE_VARIANT)
                    ),
                    alignment=ft.Alignment.CENTER,
                    content=tf_auto_ms,
                ),
                ft.Text(
                    "ms",
                    size=12,
                    font_family="Consolas",
                    weight=ft.FontWeight.W_500,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
        ),
    )

    async def _auto_send_loop() -> None:
        nonlocal auto_send_active
        while auto_send_active and app_running:
            ok = send_composer_data()
            if not ok:
                auto_send_active = False
                sw_auto_send.value = False
                safe_update(sw_auto_send)
                break
            try:
                ms = max(10, int(tf_auto_ms.value or "1000"))
            except ValueError:
                ms = 1000
            await asyncio.sleep(ms / 1000.0)

    def _on_toggle_auto_send(e: ft.Event[ft.Checkbox]) -> None:
        nonlocal auto_send_active
        if sw_auto_send.value:
            if not (tf_tx_input.value or "").strip():
                sw_auto_send.value = False
                sw_auto_send.update()
                page.show_dialog(ft.SnackBar(ft.Text("请先在发送框输入要定时发送的内容")))
                return
            auto_send_active = True
            page.run_task(_auto_send_loop)
        else:
            auto_send_active = False

    sw_auto_send.on_change = _on_toggle_auto_send

    # --- 分块发送文件功能 ---
    file_sending = False
    file_progress = ft.ProgressBar(value=0, visible=False, bar_height=4)

    async def _send_file_flow() -> None:
        nonlocal file_sending
        if file_sending:
            file_sending = False
            return

        files = await file_picker.pick_files(
            dialog_title="选择要通过当前通道发送的文件",
            allow_multiple=False,
            with_data=True,
        )
        if not files:
            return
        picked = files[0]
        try:
            if picked.bytes is not None:
                raw_bytes = picked.bytes
            elif picked.path:
                with open(picked.path, "rb") as fp:
                    raw_bytes = fp.read()
            else:
                return
        except OSError as err:
            bus.emit("ERROR", f"读取文件失败: {err}", source="SYS")
            return

        if not raw_bytes:
            page.show_dialog(ft.SnackBar(ft.Text("所选文件为空")))
            return

        chunk_size = max(16, int(tx_cfg.get("file_chunk_size", 256)))
        delay_sec = max(0.002, int(tx_cfg.get("file_chunk_delay_ms", 20)) / 1000.0)
        total = len(raw_bytes)

        file_sending = True
        btn_send_file.content = "取消发送"
        file_progress.value = 0.0
        file_progress.visible = True
        page.update()

        bus.emit("INFO", f"开始发送文件 '{picked.name}' ({format_byte_size(total)})...", source="SYS")
        sent = 0
        for offset in range(0, total, chunk_size):
            if not file_sending or not app_running:
                bus.emit("INFO", "文件发送已手动中止", source="SYS")
                break
            chunk = raw_bytes[offset : offset + chunk_size]
            if not dispatch_bytes(chunk):
                file_sending = False
                break
            sent += len(chunk)
            file_progress.value = sent / total
            file_progress.update()
            await asyncio.sleep(delay_sec)
        else:
            bus.emit("INFO", f"文件 '{picked.name}' 发送完成 (共 {sent} 字节)", source="SYS")

        file_sending = False
        btn_send_file.content = "发送文件"
        file_progress.visible = False
        page.update()

    btn_send_file = ft.OutlinedButton(
        content="发送文件",
        icon=ft.Icons.UPLOAD_FILE,
        height=38,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        on_click=lambda e: page.run_task(_send_file_flow),
    )

    btn_send = ft.FilledButton(
        content="发送",
        icon=ft.Icons.SEND_ROUNDED,
        width=108,
        height=58,
        on_click=lambda e: send_composer_data(),
    )

    tx_card = ft.Container(
        padding=12,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=12,
        bgcolor=ft.Colors.with_opacity(0.28, ft.Colors.SURFACE_CONTAINER_HIGH),
        content=ft.Column(
            spacing=8,
            tight=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                    run_spacing=6,
                    controls=[
                        ft.Row(
                            spacing=8,
                            wrap=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                dd_target,
                                sw_hex_send,
                                dd_eol,
                                dd_checksum,
                            ],
                        ),
                        ft.Row(
                            spacing=8,
                            wrap=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                auto_send_capsule,
                                btn_send_file,
                                history_menu,
                                ft.IconButton(
                                    icon=ft.Icons.CLEAR_ALL,
                                    icon_size=20,
                                    tooltip="清空发送输入框",
                                    on_click=lambda e: _fill_tx_box("", None),
                                ),
                            ],
                        ),
                    ],
                ),
                file_progress,
                ft.Row(
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[tf_tx_input, btn_send],
                ),
            ],
        ),
    )

    right_workspace = ft.Column(
        expand=True,
        spacing=10,
        controls=[
            ft.Container(
                expand=True,
                padding=12,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=12,
                bgcolor=ft.Colors.with_opacity(0.28, ft.Colors.SURFACE_CONTAINER_HIGH),
                content=ft.Column(
                    expand=True,
                    spacing=8,
                    controls=[
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                seg_workspace,
                                ft.Text(
                                    "提示: 示波器自动提取 RX 行数值，日志支持实时编码切换",
                                    size=11.5,
                                    color=ft.Colors.with_opacity(0.5, ft.Colors.ON_SURFACE),
                                ),
                            ],
                        ),
                        work_body,
                    ],
                ),
            ),
            tx_card,
        ],
    )

    # --- 顶部全局导航工具栏 ---
    def _count_enabled_rules() -> int:
        rules = config.get("auto_replies", [])
        return sum(1 for r in rules if isinstance(r, dict) and r.get("enabled"))

    auto_reply_badge = ft.Badge(
        label=str(_count_enabled_rules()),
        label_visible=_count_enabled_rules() > 0,
    )

    def _refresh_auto_reply_badge() -> None:
        cnt = _count_enabled_rules()
        auto_reply_badge.label = str(cnt)
        auto_reply_badge.label_visible = cnt > 0
        page.update()

    btn_sidebar_toggle = ft.IconButton(
        icon=ft.Icons.MENU_OPEN if sidebar_visible else ft.Icons.MENU,
        tooltip="展开/收起左侧配置栏",
    )

    def _toggle_sidebar(e: ft.Event[ft.IconButton]) -> None:
        sidebar_container.visible = not sidebar_container.visible
        btn_sidebar_toggle.icon = (
            ft.Icons.MENU_OPEN if sidebar_container.visible else ft.Icons.MENU
        )
        config.update_section("ui", sidebar_visible=sidebar_container.visible)
        page.update()

    btn_sidebar_toggle.on_click = _toggle_sidebar

    btn_theme = ft.IconButton(
        icon=(
            ft.Icons.LIGHT_MODE
            if page.theme_mode == ft.ThemeMode.DARK
            else ft.Icons.DARK_MODE
        ),
        tooltip="切换深色 / 浅色界面主题",
    )

    def _toggle_theme(e: ft.Event[ft.IconButton]) -> None:
        is_dark = page.theme_mode == ft.ThemeMode.DARK
        page.theme_mode = ft.ThemeMode.LIGHT if is_dark else ft.ThemeMode.DARK
        btn_theme.icon = (
            ft.Icons.LIGHT_MODE
            if page.theme_mode == ft.ThemeMode.DARK
            else ft.Icons.DARK_MODE
        )
        txt_rx_stat.color = (
            ft.Colors.CYAN_700
            if page.theme_mode == ft.ThemeMode.LIGHT
            else ft.Colors.CYAN_300
        )
        txt_tx_stat.color = (
            ft.Colors.GREEN_700
            if page.theme_mode == ft.ThemeMode.LIGHT
            else ft.Colors.GREEN_300
        )
        config.update_section(
            "ui",
            theme_mode="light" if page.theme_mode == ft.ThemeMode.LIGHT else "dark",
        )
        log_panel.rebuild_view()
        page.update()

    btn_theme.on_click = _toggle_theme

    def _reset_counters(e: ft.Event[ft.IconButton]) -> None:
        bus.reset_stats()
        _refresh_traffic_bar()

    header_bar = ft.Container(
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=12,
        bgcolor=ft.Colors.with_opacity(0.35, ft.Colors.SURFACE_CONTAINER_HIGH),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
            controls=[
                ft.Row(
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        btn_sidebar_toggle,
                        ft.Image(
                            src=str(ASSETS_DIR / "icon.png"),
                            width=36,
                            height=36,
                            border_radius=8,
                        ),
                        ft.Column(
                            spacing=0,
                            tight=True,
                            controls=[
                                ft.Text(
                                    "OmniPort Studio",
                                    size=15,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    "UART · BLE · TCP/UDP 综合调试工作台",
                                    size=10.5,
                                    color=ft.Colors.with_opacity(0.6, ft.Colors.ON_SURFACE),
                                ),
                            ],
                        ),
                        status_pill,
                    ],
                ),
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                            border_radius=8,
                            bgcolor=ft.Colors.with_opacity(0.4, ft.Colors.SURFACE_CONTAINER_HIGHEST),
                            content=ft.Row(
                                spacing=12,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    txt_rx_stat,
                                    txt_tx_stat,
                                    txt_rate_stat,
                                ],
                            ),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.RESTART_ALT,
                            icon_size=18,
                            tooltip="清零收发字节与包计数",
                            on_click=_reset_counters,
                        ),
                        ft.VerticalDivider(width=6),
                        ft.IconButton(
                            icon=ft.Icons.AUTO_MODE,
                            badge=auto_reply_badge,
                            tooltip="自动应答规则引擎（模拟从机回复）",
                            on_click=lambda e: open_auto_reply_dialog(
                                page, config, _refresh_auto_reply_badge
                            ),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.CALCULATE_OUTLINED,
                            tooltip="协议校验与编码转换工具箱 (CRC / IEEE754)",
                            on_click=lambda e: open_toolbox_dialog(
                                page, clipboard, _fill_tx_box
                            ),
                        ),
                        btn_theme,
                    ],
                ),
            ],
        ),
    )

    page.add(
        header_bar,
        ft.Row(
            expand=True,
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[sidebar_container, right_workspace],
        ),
    )

    # --- 自动应答异步执行器 ---
    async def _send_delayed_reply(rule_name: str, reply_bytes: bytes, delay_ms: int) -> None:
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        if app_running:
            bus.emit("INFO", f"触发自动应答 [{rule_name}]", source="AUTO")
            dispatch_bytes(reply_bytes)

    def _refresh_traffic_bar() -> None:
        rx_bps, tx_bps = bus.get_rates_bps()
        new_rx = f"RX: {format_byte_size(bus.rx_bytes)} ({bus.rx_packets})"
        new_tx = f"TX: {format_byte_size(bus.tx_bytes)} ({bus.tx_packets})"
        new_rate = f"↓{format_byte_size(rx_bps)}/s  ↑{format_byte_size(tx_bps)}/s"
        if (
            txt_rx_stat.value != new_rx
            or txt_tx_stat.value != new_tx
            or txt_rate_stat.value != new_rate
        ):
            txt_rx_stat.value = new_rx
            txt_tx_stat.value = new_tx
            txt_rate_stat.value = new_rate
            safe_update(header_bar)

    # --- 核心异步事件泵（30Hz 批量刷新 UI，保证高波特率绝不卡死） ---
    async def _ui_pump_loop() -> None:
        tick = 0
        while app_running:
            # 1. 执行跨线程 UI 状态回调
            while not bus.ui_callbacks.empty():
                try:
                    cb = bus.ui_callbacks.get_nowait()
                    cb()
                except Exception:
                    pass

            # 2. 批量消费收发数据包
            batch: list[PacketEvent] = []
            while not bus.packet_queue.empty() and len(batch) < 200:
                try:
                    batch.append(bus.packet_queue.get_nowait())
                except Exception:
                    break

            if batch:
                log_panel.append_batch(batch)
                rules = config.get("auto_replies", [])
                for ev in batch:
                    if ev.direction == "RX" and isinstance(ev.payload, bytes):
                        plotter_panel.feed_rx_bytes(ev.payload, log_panel.encoding)
                        if rules:
                            hits = match_auto_replies(ev.payload, log_panel.encoding, rules)
                            for r_name, r_bytes, d_ms in hits:
                                page.run_task(_send_delayed_reply, r_name, r_bytes, d_ms)

            if active_work_view == "PLOT":
                plotter_panel.refresh_if_dirty()

            tick += 1
            if batch or tick % 8 == 0:
                _refresh_traffic_bar()

            await asyncio.sleep(0.033)

    page.run_task(_ui_pump_loop)
    bus.emit("INFO", "OmniPort Studio 已就绪 (Flet 1.x 引擎)", source="SYS")

    # --- 窗口关闭资源清理 ---
    def _cleanup_all(*_: Any) -> None:
        nonlocal app_running, auto_send_active, file_sending
        app_running = False
        auto_send_active = False
        file_sending = False
        serial_ch.close()
        net_ch.close()
        ble_ch.close()

    def _on_window_event(e: ft.WindowEvent) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            _cleanup_all()

    page.window.on_event = _on_window_event
    page.on_disconnect = _cleanup_all


if __name__ == "__main__":
    ft.run(main, assets_dir=str(ASSETS_DIR))
