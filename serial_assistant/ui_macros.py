import json
from typing import Any, Callable

import flet as ft

from .config import AppConfig
from .utils import parse_hex_string, safe_update, unescape_text

MACRO_COLORS: dict[str, str] = {
    "blue": "#3B82F6",
    "teal": "#14B8A6",
    "green": "#22C55E",
    "amber": "#F59E0B",
    "purple": "#A855F7",
    "red": "#EF4444",
}


class MacrosDock:
    """常驻快捷指令面板：一键发送、标签着色、编辑删除与 JSON 导入导出."""

    def __init__(
        self,
        page: ft.Page,
        config: AppConfig,
        file_picker: ft.FilePicker,
        send_bytes_callback: Callable[[bytes], None],
    ) -> None:
        self.page = page
        self.config = config
        self.file_picker = file_picker
        self.send_cb = send_bytes_callback

        self.list_view = ft.ListView(
            expand=True,
            spacing=6,
            padding=ft.Padding.only(right=4),
        )

        self.empty_box = ft.Container(
            padding=ft.Padding.symmetric(vertical=24, horizontal=12),
            alignment=ft.Alignment.CENTER,
            content=ft.Text(
                "暂无快捷指令，点击右上角「+」添加常用 AT / Modbus 指令",
                size=12,
                text_align=ft.TextAlign.CENTER,
                color=ft.Colors.with_opacity(0.55, ft.Colors.ON_SURFACE),
            ),
        )

        header = ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(ft.Icons.BOLT, size=18, color=ft.Colors.AMBER_500),
                        ft.Text("快捷指令库", size=13.5, weight=ft.FontWeight.W_600),
                    ],
                ),
                ft.Row(
                    spacing=0,
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                            icon_size=19,
                            tooltip="新建快捷指令",
                            on_click=lambda e: self.open_editor(-1),
                        ),
                        ft.PopupMenuButton(
                            icon=ft.Icons.MORE_HORIZ,
                            icon_size=19,
                            tooltip="导入/导出指令集",
                            items=[
                                ft.PopupMenuItem(
                                    content="导入指令集 (.json)",
                                    icon=ft.Icons.FOLDER_OPEN,
                                    on_click=lambda e: self.page.run_task(self._import_macros),
                                ),
                                ft.PopupMenuItem(
                                    content="导出指令集 (.json)",
                                    icon=ft.Icons.SAVE_ALT,
                                    on_click=lambda e: self.page.run_task(self._export_macros),
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )

        self.body_col = ft.Column(
            expand=True,
            spacing=6,
            controls=[header, self.empty_box, self.list_view],
        )

        self.view = ft.Container(
            expand=True,
            content=self.body_col,
        )
        self.refresh_list(update_ui=False)

    def refresh_list(self, update_ui: bool = True) -> None:
        macros: list[dict[str, Any]] = self.config.get("macros", [])
        self.list_view.controls.clear()
        self.empty_box.visible = len(macros) == 0
        self.list_view.visible = len(macros) > 0

        for idx, macro in enumerate(macros):
            accent = MACRO_COLORS.get(str(macro.get("color", "blue")), "#3B82F6")
            is_hex = bool(macro.get("is_hex", False))
            preview = str(macro.get("content", "")).replace("\r", "\\r").replace("\n", "\\n")

            card = ft.Container(
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=8,
                bgcolor=ft.Colors.with_opacity(0.35, ft.Colors.SURFACE_CONTAINER_HIGHEST),
                padding=ft.Padding.symmetric(horizontal=10, vertical=7),
                ink=True,
                tooltip=f"点击立即发送: {preview}",
                on_click=lambda e, m=macro: self._execute_macro(m),
                content=ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            width=4,
                            height=28,
                            border_radius=2,
                            bgcolor=accent,
                        ),
                        ft.Column(
                            expand=True,
                            spacing=1,
                            controls=[
                                ft.Row(
                                    spacing=6,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    controls=[
                                        ft.Text(
                                            str(macro.get("name", "未命名")),
                                            size=12.5,
                                            weight=ft.FontWeight.W_600,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                            expand=True,
                                        ),
                                        ft.Container(
                                            bgcolor=ft.Colors.with_opacity(0.15, accent),
                                            padding=ft.Padding.symmetric(horizontal=5, vertical=1),
                                            border_radius=4,
                                            content=ft.Text(
                                                "HEX" if is_hex else "TXT",
                                                size=9.5,
                                                weight=ft.FontWeight.BOLD,
                                                color=accent,
                                                font_family="Consolas",
                                            ),
                                        ),
                                    ],
                                ),
                                ft.Text(
                                    preview,
                                    size=11,
                                    font_family="Consolas",
                                    color=ft.Colors.with_opacity(0.6, ft.Colors.ON_SURFACE),
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                        ),
                        ft.IconButton(
                            icon=ft.Icons.EDIT_OUTLINED,
                            icon_size=16,
                            tooltip="编辑或删除此指令",
                            on_click=lambda e, i=idx: self.open_editor(i),
                        ),
                    ],
                ),
            )
            self.list_view.controls.append(card)

        if update_ui:
            safe_update(self.view)

    def _execute_macro(self, macro: dict[str, Any]) -> None:
        raw_str = str(macro.get("content", ""))
        is_hex = bool(macro.get("is_hex", False))
        encoding = str(self.config.get("ui", {}).get("encoding", "utf-8"))
        try:
            if is_hex:
                payload = parse_hex_string(raw_str)
            else:
                payload = unescape_text(raw_str).encode(encoding, errors="replace")
            if not payload:
                return
            self.send_cb(payload)
        except Exception as exc:
            self.page.show_dialog(
                ft.SnackBar(
                    ft.Text(f"快捷指令解析失败: {exc}"),
                    bgcolor=ft.Colors.ERROR,
                )
            )

    def open_editor(self, index: int) -> None:
        macros: list[dict[str, Any]] = list(self.config.get("macros", []))
        is_new = index < 0 or index >= len(macros)
        current = (
            {"name": "新指令", "content": "AT\\r\\n", "is_hex": False, "color": "blue"}
            if is_new
            else macros[index]
        )

        tf_name = ft.TextField(
            label="指令名称",
            value=str(current.get("name", "")),
            dense=True,
            autofocus=True,
        )
        tf_content = ft.TextField(
            label="指令内容（文本模式支持 \\r \\n \\t 转义）",
            value=str(current.get("content", "")),
            multiline=True,
            min_lines=3,
            max_lines=5,
            text_style=ft.TextStyle(font_family="Consolas", size=13),
        )
        sw_hex = ft.Switch(
            label="按十六进制 (HEX) 解析发送",
            value=bool(current.get("is_hex", False)),
        )
        dd_color = ft.Dropdown(
            label="标签颜色",
            value=str(current.get("color", "blue")),
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[
                ft.DropdownOption(key="blue", text="科技蓝 (Blue)"),
                ft.DropdownOption(key="teal", text="青翠绿 (Teal)"),
                ft.DropdownOption(key="green", text="翠绿色 (Green)"),
                ft.DropdownOption(key="amber", text="琥珀黄 (Amber)"),
                ft.DropdownOption(key="purple", text="极光紫 (Purple)"),
                ft.DropdownOption(key="red", text="警示红 (Red)"),
            ],
        )

        def on_save(ev: ft.Event[ft.Control]) -> None:
            name_val = (tf_name.value or "").strip() or "未命名指令"
            content_val = tf_content.value or ""
            if sw_hex.value:
                try:
                    parse_hex_string(content_val)
                except ValueError as err:
                    self.page.show_dialog(
                        ft.SnackBar(ft.Text(str(err)), bgcolor=ft.Colors.ERROR)
                    )
                    return

            item = {
                "name": name_val,
                "content": content_val,
                "is_hex": bool(sw_hex.value),
                "color": dd_color.value or "blue",
            }
            if is_new:
                macros.append(item)
            else:
                macros[index] = item
            self.config.set("macros", macros)
            self.page.pop_dialog()
            self.refresh_list()

        def on_delete(ev: ft.Event[ft.Control]) -> None:
            if not is_new:
                del macros[index]
                self.config.set("macros", macros)
            self.page.pop_dialog()
            self.refresh_list()

        actions: list[ft.Control] = [
            ft.TextButton(content="取消", on_click=lambda e: self.page.pop_dialog()),
            ft.FilledButton(content="保存", icon=ft.Icons.CHECK, on_click=on_save),
        ]
        if not is_new:
            actions.insert(
                0,
                ft.TextButton(
                    content="删除",
                    icon=ft.Icons.DELETE_OUTLINE,
                    style=ft.ButtonStyle(color=ft.Colors.ERROR),
                    on_click=on_delete,
                ),
            )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("新建快捷指令" if is_new else "编辑快捷指令", size=17, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                width=420,
                padding=ft.Padding.only(top=8),
                content=ft.Column(
                    tight=True,
                    spacing=16,
                    controls=[tf_name, tf_content, dd_color, sw_hex],
                ),
            ),
            actions=actions,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    async def _export_macros(self) -> None:
        macros = self.config.get("macros", [])
        raw = json.dumps(macros, indent=2, ensure_ascii=False).encode("utf-8")
        path = await self.file_picker.save_file(
            dialog_title="导出快捷指令集",
            file_name="serial_macros.json",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["json"],
            src_bytes=raw,
        )
        if path:
            try:
                with open(path, "wb") as fp:
                    fp.write(raw)
            except OSError:
                pass
            self.page.show_dialog(ft.SnackBar(ft.Text(f"已导出 {len(macros)} 条快捷指令")))

    async def _import_macros(self) -> None:
        files = await self.file_picker.pick_files(
            dialog_title="选择快捷指令 JSON 文件",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["json"],
            with_data=True,
        )
        if not files:
            return
        picked = files[0]
        try:
            if picked.bytes:
                raw_text = picked.bytes.decode("utf-8")
            elif picked.path:
                with open(picked.path, "r", encoding="utf-8") as fp:
                    raw_text = fp.read()
            else:
                return
            loaded = json.loads(raw_text)
            if not isinstance(loaded, list):
                raise ValueError("JSON 根节点必须是指令数组")
            macros = list(self.config.get("macros", []))
            added = 0
            for item in loaded:
                if isinstance(item, dict) and "name" in item and "content" in item:
                    macros.append(
                        {
                            "name": str(item["name"]),
                            "content": str(item["content"]),
                            "is_hex": bool(item.get("is_hex", False)),
                            "color": str(item.get("color", "blue")),
                        }
                    )
                    added += 1
            self.config.set("macros", macros)
            self.refresh_list()
            self.page.show_dialog(ft.SnackBar(ft.Text(f"成功导入 {added} 条快捷指令")))
        except Exception as exc:
            self.page.show_dialog(
                ft.SnackBar(ft.Text(f"导入指令失败: {exc}"), bgcolor=ft.Colors.ERROR)
            )
