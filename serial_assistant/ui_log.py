from collections import deque
import csv
from datetime import datetime
import io

import flet as ft

from .config import AppConfig
from .event_bus import PacketEvent
from .utils import decode_bytes, format_dual_hex_ascii, format_hex, safe_update


class TerminalLogPanel:
    """现代化终端日志面板：支持 ASCII/HEX/双显、多编码、方向与关键字过滤、暂停滚动、复制与导出."""

    def __init__(
        self,
        page: ft.Page,
        config: AppConfig,
        clipboard: ft.Clipboard,
        file_picker: ft.FilePicker,
    ) -> None:
        self.page = page
        self.config = config
        self.clipboard = clipboard
        self.file_picker = file_picker

        ui_cfg = self.config.get("ui", {})
        self.view_mode: str = str(ui_cfg.get("view_mode", "ASCII"))
        self.encoding: str = str(ui_cfg.get("encoding", "utf-8"))
        self.show_timestamp: bool = bool(ui_cfg.get("show_timestamp", True))
        self.max_entries: int = int(ui_cfg.get("max_log_entries", 2000))
        self.dir_filter: str = "ALL"
        self.search_keyword: str = ""
        self.paused: bool = False

        self.entries: deque[PacketEvent] = deque(maxlen=self.max_entries)

        # --- 工具栏控件 ---
        self.seg_view = ft.SegmentedButton(
            selected=[self.view_mode],
            allow_empty_selection=False,
            allow_multiple_selection=False,
            show_selected_icon=False,
            segments=[
                ft.Segment(value="ASCII", label="文本"),
                ft.Segment(value="HEX", label="HEX"),
                ft.Segment(value="DUAL", label="双显"),
            ],
            on_change=self._on_view_mode_change,
        )

        self.dd_encoding = ft.Dropdown(
            value=self.encoding,
            width=142,
            expanded_insets=0,
            dense=True,
            text_size=13,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            options=[
                ft.DropdownOption(key="utf-8", text="UTF-8"),
                ft.DropdownOption(key="gbk", text="GBK"),
                ft.DropdownOption(key="ascii", text="ASCII"),
                ft.DropdownOption(key="latin-1", text="Latin-1"),
            ],
            tooltip="字符解码格式（中文单片机常用 GBK / UTF-8）",
            on_select=self._on_encoding_change,
        )

        self.dd_dir = ft.Dropdown(
            value="ALL",
            width=128,
            expanded_insets=0,
            dense=True,
            text_size=13,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            options=[
                ft.DropdownOption(key="ALL", text="全部"),
                ft.DropdownOption(key="RX", text="仅 RX"),
                ft.DropdownOption(key="TX", text="仅 TX"),
                ft.DropdownOption(key="SYS", text="系统"),
            ],
            tooltip="按收发方向筛选日志",
            on_select=self._on_dir_filter_change,
        )

        self.tf_search = ft.TextField(
            hint_text="搜索日志内容...",
            prefix_icon=ft.Icons.SEARCH,
            width=180,
            dense=True,
            text_size=12,
            content_padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            on_change=self._on_search_change,
        )

        self.btn_timestamp = ft.IconButton(
            icon=ft.Icons.ACCESS_TIME,
            selected=self.show_timestamp,
            icon_size=18,
            tooltip="显示/隐藏时间戳与字节数",
            on_click=self._toggle_timestamp,
        )

        self.btn_pause = ft.IconButton(
            icon=ft.Icons.PAUSE_CIRCLE_OUTLINE,
            icon_size=18,
            tooltip="暂停自动滚动（方便查看历史帧）",
            on_click=self._toggle_pause,
        )

        self.btn_copy = ft.IconButton(
            icon=ft.Icons.CONTENT_COPY,
            icon_size=18,
            tooltip="复制当前日志到剪贴板",
            on_click=self._copy_logs,
        )

        self.btn_export = ft.PopupMenuButton(
            icon=ft.Icons.SAVE_ALT,
            icon_size=18,
            tooltip="导出日志到文件",
            items=[
                ft.PopupMenuItem(
                    content="导出为 TXT 文本 (.txt)",
                    icon=ft.Icons.DESCRIPTION_OUTLINED,
                    on_click=lambda e: self.page.run_task(self._export_logs, "txt"),
                ),
                ft.PopupMenuItem(
                    content="导出为 CSV 表格 (.csv)",
                    icon=ft.Icons.TABLE_CHART_OUTLINED,
                    on_click=lambda e: self.page.run_task(self._export_logs, "csv"),
                ),
            ],
        )

        self.btn_clear = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_color=ft.Colors.ERROR,
            icon_size=18,
            tooltip="清空全部日志",
            on_click=lambda e: self.clear(),
        )

        self.list_view = ft.ListView(
            expand=True,
            spacing=3,
            auto_scroll=True,
            padding=ft.Padding.all(10),
        )

        self.empty_hint = ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
                controls=[
                    ft.Icon(
                        ft.Icons.TERMINAL,
                        size=42,
                        color=ft.Colors.with_opacity(0.3, ft.Colors.ON_SURFACE),
                    ),
                    ft.Text(
                        "等待设备连接或数据收发...",
                        size=13,
                        color=ft.Colors.with_opacity(0.5, ft.Colors.ON_SURFACE),
                    ),
                ],
            ),
        )

        self.terminal_stack = ft.Stack(
            expand=True,
            controls=[self.list_view, self.empty_hint],
        )

        toolbar = ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
            run_spacing=6,
            controls=[
                ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                    controls=[
                        self.seg_view,
                        self.dd_encoding,
                        self.dd_dir,
                        self.tf_search,
                    ],
                ),
                ft.Row(
                    spacing=2,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        self.btn_timestamp,
                        self.btn_pause,
                        self.btn_copy,
                        self.btn_export,
                        self.btn_clear,
                    ],
                ),
            ],
        )

        self.view = ft.Container(
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=8,
                controls=[
                    toolbar,
                    ft.Container(
                        expand=True,
                        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                        border_radius=10,
                        bgcolor=ft.Colors.with_opacity(0.45, ft.Colors.SURFACE_CONTAINER_HIGHEST),
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        content=self.terminal_stack,
                    ),
                ],
            ),
        )

    # --- 格式化与筛选逻辑 ---

    def _format_payload(self, ev: PacketEvent) -> str:
        if isinstance(ev.payload, bytes):
            if self.view_mode == "HEX":
                text = format_hex(ev.payload)
            elif self.view_mode == "DUAL":
                text = format_dual_hex_ascii(ev.payload, self.encoding)
            else:
                text = decode_bytes(ev.payload, self.encoding).rstrip("\r\n")
        else:
            text = str(ev.payload)

        if ev.extra:
            return f"[{ev.extra}] {text}"
        return text

    def _matches_filter(self, ev: PacketEvent, formatted_text: str) -> bool:
        if self.dir_filter == "RX" and ev.direction != "RX":
            return False
        if self.dir_filter == "TX" and ev.direction != "TX":
            return False
        if self.dir_filter == "SYS" and ev.direction not in ("INFO", "ERROR"):
            return False
        if self.search_keyword:
            kw = self.search_keyword.lower()
            if kw not in formatted_text.lower() and kw not in ev.source.lower():
                return False
        return True

    def _build_row(self, ev: PacketEvent, formatted_text: str) -> ft.Control:
        if ev.direction == "RX":
            tag = "RX"
            badge_bg = ft.Colors.with_opacity(0.16, ft.Colors.CYAN_500)
            badge_fg = ft.Colors.CYAN_600 if self.page.theme_mode == ft.ThemeMode.LIGHT else ft.Colors.CYAN_300
            text_color = ft.Colors.ON_SURFACE
        elif ev.direction == "TX":
            tag = "TX"
            badge_bg = ft.Colors.with_opacity(0.16, ft.Colors.GREEN_500)
            badge_fg = ft.Colors.GREEN_700 if self.page.theme_mode == ft.ThemeMode.LIGHT else ft.Colors.GREEN_300
            text_color = ft.Colors.ON_SURFACE
        elif ev.direction == "ERROR":
            tag = "ERR"
            badge_bg = ft.Colors.with_opacity(0.18, ft.Colors.RED_500)
            badge_fg = ft.Colors.RED_600 if self.page.theme_mode == ft.ThemeMode.LIGHT else ft.Colors.RED_300
            text_color = badge_fg
        else:
            tag = "SYS"
            badge_bg = ft.Colors.with_opacity(0.16, ft.Colors.AMBER_600)
            badge_fg = ft.Colors.AMBER_800 if self.page.theme_mode == ft.ThemeMode.LIGHT else ft.Colors.AMBER_300
            text_color = ft.Colors.ON_SURFACE_VARIANT

        meta_parts: list[str] = []
        if self.show_timestamp:
            meta_parts.append(ev.timestamp)
            if ev.source and ev.source != "SYS":
                meta_parts.append(ev.source)
            if isinstance(ev.payload, bytes):
                meta_parts.append(f"{len(ev.payload)}B")

        controls: list[ft.Control] = [
            ft.Container(
                content=ft.Text(
                    tag,
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=badge_fg,
                    font_family="Consolas",
                ),
                bgcolor=badge_bg,
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
                width=38,
                alignment=ft.Alignment.CENTER,
            )
        ]

        if meta_parts:
            controls.append(
                ft.Text(
                    " · ".join(meta_parts),
                    size=11,
                    font_family="Consolas",
                    color=ft.Colors.with_opacity(0.55, ft.Colors.ON_SURFACE),
                    no_wrap=True,
                )
            )

        controls.append(
            ft.Text(
                formatted_text,
                size=12.5,
                font_family="Consolas",
                color=text_color,
                selectable=True,
                expand=True,
            )
        )

        return ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=controls,
        )

    def append_batch(self, events: list[PacketEvent]) -> None:
        """批量追加日志项并刷新视图."""
        if not events:
            return
        added_ui = False
        for ev in events:
            self.entries.append(ev)
            formatted = self._format_payload(ev)
            if self._matches_filter(ev, formatted):
                self.list_view.controls.append(self._build_row(ev, formatted))
                added_ui = True

        # 限制 UI 节点上限，避免超长时间运行内存膨胀
        overflow = len(self.list_view.controls) - self.max_entries
        if overflow > 0:
            del self.list_view.controls[:overflow]

        self.empty_hint.visible = len(self.list_view.controls) == 0
        if added_ui or self.empty_hint.visible:
            safe_update(self.view)

    def rebuild_view(self) -> None:
        """当切换编码、HEX/文本模式或搜索筛选时重绘当前缓存日志."""
        rows: list[ft.Control] = []
        for ev in self.entries:
            formatted = self._format_payload(ev)
            if self._matches_filter(ev, formatted):
                rows.append(self._build_row(ev, formatted))
        self.list_view.controls = rows
        self.empty_hint.visible = len(rows) == 0
        safe_update(self.view)

    def clear(self) -> None:
        self.entries.clear()
        self.list_view.controls.clear()
        self.empty_hint.visible = True
        safe_update(self.view)

    # --- 事件响应 ---

    def _on_view_mode_change(self, e: ft.Event[ft.SegmentedButton]) -> None:
        sel = list(e.control.selected)
        if sel:
            self.view_mode = sel[0]
            self.config.update_section("ui", view_mode=self.view_mode)
            self.rebuild_view()

    def _on_encoding_change(self, e: ft.Event[ft.Dropdown]) -> None:
        if self.dd_encoding.value:
            self.encoding = self.dd_encoding.value
            self.config.update_section("ui", encoding=self.encoding)
            self.rebuild_view()

    def _on_dir_filter_change(self, e: ft.Event[ft.Dropdown]) -> None:
        self.dir_filter = self.dd_dir.value or "ALL"
        self.rebuild_view()

    def _on_search_change(self, e: ft.Event[ft.TextField]) -> None:
        self.search_keyword = (self.tf_search.value or "").strip()
        self.rebuild_view()

    def _toggle_timestamp(self, e: ft.Event[ft.IconButton]) -> None:
        self.show_timestamp = not self.show_timestamp
        self.btn_timestamp.selected = self.show_timestamp
        self.config.update_section("ui", show_timestamp=self.show_timestamp)
        self.rebuild_view()

    def _toggle_pause(self, e: ft.Event[ft.IconButton]) -> None:
        self.paused = not self.paused
        self.list_view.auto_scroll = not self.paused
        self.btn_pause.icon = (
            ft.Icons.PLAY_CIRCLE_FILLED if self.paused else ft.Icons.PAUSE_CIRCLE_OUTLINE
        )
        self.btn_pause.icon_color = ft.Colors.AMBER_600 if self.paused else None
        self.btn_pause.tooltip = (
            "恢复自动滚动到底部" if self.paused else "暂停自动滚动（方便查看历史帧）"
        )
        self.view.update()

    async def _copy_logs(self, e: ft.Event[ft.IconButton]) -> None:
        lines = self._export_plain_lines()
        if not lines:
            self.page.show_dialog(ft.SnackBar(ft.Text("当前没有可复制的日志内容")))
            return
        await self.clipboard.set("\n".join(lines))
        self.page.show_dialog(
            ft.SnackBar(ft.Text(f"已复制 {len(lines)} 条日志到剪贴板"))
        )

    def _export_plain_lines(self) -> list[str]:
        out: list[str] = []
        for ev in self.entries:
            formatted = self._format_payload(ev)
            if self._matches_filter(ev, formatted):
                size_tag = f" ({len(ev.payload)}B)" if isinstance(ev.payload, bytes) else ""
                out.append(f"[{ev.timestamp}] [{ev.direction}] [{ev.source}{size_tag}] {formatted}")
        return out

    async def _export_logs(self, fmt: str) -> None:
        if not self.entries:
            self.page.show_dialog(ft.SnackBar(ft.Text("当前日志为空，无需导出")))
            return

        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Timestamp", "Direction", "Source", "Bytes", "HEX", "Decoded"])
            for ev in self.entries:
                if isinstance(ev.payload, bytes):
                    b_len = len(ev.payload)
                    hex_s = format_hex(ev.payload)
                    txt_s = decode_bytes(ev.payload, self.encoding).rstrip("\r\n")
                else:
                    b_len = len(str(ev.payload))
                    hex_s = ""
                    txt_s = str(ev.payload)
                writer.writerow([ev.timestamp, ev.direction, ev.source, b_len, hex_s, txt_s])
            raw_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
            default_name = f"serial_log_{now_str}.csv"
            exts = ["csv"]
        else:
            raw_bytes = "\r\n".join(self._export_plain_lines()).encode("utf-8")
            default_name = f"serial_log_{now_str}.txt"
            exts = ["txt", "log"]

        saved_path = await self.file_picker.save_file(
            dialog_title="导出串口调试日志",
            file_name=default_name,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=exts,
            src_bytes=raw_bytes,
        )
        if saved_path:
            # 在桌面端确保写入内容（若平台仅创建了空文件）
            try:
                with open(saved_path, "wb") as fp:
                    fp.write(raw_bytes)
            except OSError:
                pass
            self.page.show_dialog(ft.SnackBar(ft.Text(f"日志已成功导出至: {saved_path}")))
