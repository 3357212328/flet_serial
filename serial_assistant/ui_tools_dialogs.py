import struct
from typing import Any, Callable

import flet as ft

from .config import AppConfig
from .utils import (
    calc_crc16_ccitt,
    calc_crc16_modbus,
    calc_crc32,
    calc_sum8,
    calc_xor,
    float_to_hex,
    format_hex,
    hex_to_float,
    parse_hex_string,
    safe_update,
)


def open_toolbox_dialog(
    page: ft.Page,
    clipboard: ft.Clipboard,
    fill_tx_callback: Callable[[str, bool], None],
) -> None:
    """打开协议与编码工具箱弹窗（CRC计算、HEX↔字符、IEEE754浮点数转换）."""

    # --- 1. CRC / 校验计算页 ---
    tf_crc_input = ft.TextField(
        label="待计算数据（默认 HEX，如 01 03 00 00 00 02）",
        value="01 03 00 00 00 02",
        multiline=True,
        min_lines=2,
        max_lines=3,
        text_style=ft.TextStyle(font_family="Consolas", size=13),
    )
    sw_crc_hex = ft.Switch(label="按 HEX 解析输入", value=True)
    res_col = ft.Column(spacing=6)

    def _copy_text(val: str) -> None:
        async def _do() -> None:
            await clipboard.set(val)
            page.show_dialog(ft.SnackBar(ft.Text(f"已复制: {val}")))

        page.run_task(_do)

    def _compute_crc(_: Any = None) -> None:
        raw = tf_crc_input.value or ""
        try:
            data = (
                parse_hex_string(raw)
                if sw_crc_hex.value
                else raw.encode("utf-8")
            )
        except ValueError as err:
            res_col.controls = [ft.Text(str(err), color=ft.Colors.ERROR, size=12)]
            safe_update(res_col)
            return

        if not data:
            res_col.controls = [ft.Text("请输入数据以计算校验值", size=12)]
            safe_update(res_col)
            return

        mb = calc_crc16_modbus(data)
        mb_le = struct.pack("<H", mb).hex(" ").upper()
        mb_be = struct.pack(">H", mb).hex(" ").upper()
        ccitt = struct.pack(">H", calc_crc16_ccitt(data)).hex(" ").upper()
        crc32_s = struct.pack(">I", calc_crc32(data)).hex(" ").upper()
        xor_s = f"{calc_xor(data):02X}"
        sum8_s = f"{calc_sum8(data):02X}"
        full_modbus_frame = f"{format_hex(data)} {mb_le}"

        items = [
            ("Modbus CRC16 (低字节在前 LE)", mb_le),
            ("Modbus CRC16 (高字节在前 BE)", mb_be),
            ("CRC16-CCITT (0x1021)", ccitt),
            ("CRC32 (IEEE 802.3)", crc32_s),
            ("XOR 异或校验 (1 Byte)", xor_s),
            ("SUM8 累加和低八位 (1 Byte)", sum8_s),
        ]

        rows: list[ft.Control] = []
        for title, hex_val in items:
            rows.append(
                ft.Container(
                    bgcolor=ft.Colors.with_opacity(0.35, ft.Colors.SURFACE_CONTAINER_HIGHEST),
                    border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Text(title, size=12),
                            ft.Row(
                                spacing=6,
                                controls=[
                                    ft.Text(
                                        hex_val,
                                        size=12.5,
                                        weight=ft.FontWeight.BOLD,
                                        font_family="Consolas",
                                        color=ft.Colors.PRIMARY,
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.CONTENT_COPY,
                                        icon_size=15,
                                        tooltip="复制结果",
                                        on_click=lambda e, v=hex_val: _copy_text(v),
                                    ),
                                ],
                            ),
                        ],
                    ),
                )
            )

        rows.append(
            ft.Row(
                alignment=ft.MainAxisAlignment.END,
                controls=[
                    ft.FilledTonalButton(
                        content="将完整 Modbus 帧填入发送框",
                        icon=ft.Icons.SEND_ROUNDED,
                        on_click=lambda e: (
                            fill_tx_callback(full_modbus_frame, True),
                            page.pop_dialog(),
                        ),
                    )
                ],
            )
        )
        res_col.controls = rows
        safe_update(res_col)

    tf_crc_input.on_change = _compute_crc
    sw_crc_hex.on_change = _compute_crc
    _compute_crc()

    crc_tab_view = ft.Container(
        padding=ft.Padding.only(top=10),
        content=ft.Column(
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            controls=[tf_crc_input, sw_crc_hex, res_col],
        ),
    )

    # --- 2. IEEE-754 浮点数互转页 ---
    tf_float = ft.TextField(
        label="十进制单精度浮点数 (Float32)",
        value="25.4",
        dense=True,
        text_style=ft.TextStyle(font_family="Consolas"),
    )
    tf_fhex = ft.TextField(
        label="4 字节十六进制 (HEX)",
        value="41 CB 33 33",
        dense=True,
        text_style=ft.TextStyle(font_family="Consolas"),
    )
    txt_ieee_result = ft.Text(
        "大端 (BE): 41 CB 33 33   |   小端 (LE): 33 33 CB 41",
        font_family="Consolas",
        size=12.5,
        selectable=True,
    )

    def _on_float_to_hex(_: Any) -> None:
        try:
            v = float((tf_float.value or "0").strip())
            be, le = float_to_hex(v)
            tf_fhex.value = be
            txt_ieee_result.value = f"大端 (ABCD): {be}   |   小端 (DCBA): {le}"
            txt_ieee_result.color = None
        except ValueError as err:
            txt_ieee_result.value = f"浮点数格式无效: {err}"
            txt_ieee_result.color = ft.Colors.ERROR
        page.update()

    def _on_hex_to_float(_: Any) -> None:
        try:
            be_v, le_v = hex_to_float(tf_fhex.value or "")
            tf_float.value = f"{be_v:.6g}"
            txt_ieee_result.value = f"按大端解析: {be_v:.6g}   |   按小端解析: {le_v:.6g}"
            txt_ieee_result.color = None
        except ValueError as err:
            txt_ieee_result.value = str(err)
            txt_ieee_result.color = ft.Colors.ERROR
        page.update()

    # --- 3. 文本 ↔ HEX 编码互转 ---
    tf_plain = ft.TextField(
        label="文本字符串",
        value="Hello 串口助手",
        multiline=True,
        min_lines=2,
        max_lines=3,
    )
    dd_conv_enc = ft.Dropdown(
        label="字符编码",
        value="utf-8",
        width=155,
        expanded_insets=0,
        text_size=13,
        dense=True,
        options=[
            ft.DropdownOption(key="utf-8", text="UTF-8"),
            ft.DropdownOption(key="gbk", text="GBK"),
        ],
    )
    tf_encoded_hex = ft.TextField(
        label="对应十六进制字节",
        value=format_hex("Hello 串口助手".encode("utf-8")),
        multiline=True,
        min_lines=2,
        max_lines=3,
        text_style=ft.TextStyle(font_family="Consolas", size=12.5),
    )

    def _text_to_hex(_: Any) -> None:
        enc = dd_conv_enc.value or "utf-8"
        raw_b = (tf_plain.value or "").encode(enc, errors="replace")
        tf_encoded_hex.value = format_hex(raw_b)
        page.update()

    def _hex_to_text(_: Any) -> None:
        enc = dd_conv_enc.value or "utf-8"
        try:
            raw_b = parse_hex_string(tf_encoded_hex.value or "")
            tf_plain.value = raw_b.decode(enc, errors="replace")
        except ValueError as err:
            page.show_dialog(ft.SnackBar(ft.Text(str(err)), bgcolor=ft.Colors.ERROR))
        page.update()

    conv_tab_view = ft.Container(
        padding=ft.Padding.only(top=10),
        content=ft.Column(
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                ft.Text("IEEE-754 单精度浮点数 (Float32) 转换", weight=ft.FontWeight.W_600, size=13),
                ft.Row(
                    spacing=8,
                    controls=[
                        ft.Container(expand=True, content=tf_float),
                        ft.FilledTonalButton(content="Float → HEX", on_click=_on_float_to_hex),
                    ],
                ),
                ft.Row(
                    spacing=8,
                    controls=[
                        ft.Container(expand=True, content=tf_fhex),
                        ft.FilledTonalButton(content="HEX → Float", on_click=_on_hex_to_float),
                    ],
                ),
                txt_ieee_result,
                ft.Divider(),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("文本 ↔ HEX 编码互转", weight=ft.FontWeight.W_600, size=13),
                        dd_conv_enc,
                    ],
                ),
                tf_plain,
                ft.Row(
                    spacing=8,
                    controls=[
                        ft.FilledTonalButton(
                            content="文本 → HEX ↓",
                            on_click=_text_to_hex,
                        ),
                        ft.FilledTonalButton(
                            content="HEX → 文本 ↑",
                            on_click=_hex_to_text,
                        ),
                    ],
                ),
                tf_encoded_hex,
            ],
        ),
    )

    tabs = ft.Tabs(
        length=2,
        selected_index=0,
        expand=True,
        content=ft.Column(
            expand=True,
            controls=[
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="CRC / 校验计算", icon=ft.Icons.VERIFIED_OUTLINED),
                        ft.Tab(label="浮点数 & 编码转换", icon=ft.Icons.SWAP_HORIZ),
                    ]
                ),
                ft.TabBarView(
                    expand=True,
                    controls=[crc_tab_view, conv_tab_view],
                ),
            ],
        ),
    )

    dlg = ft.AlertDialog(
        modal=False,
        title=ft.Row(
            spacing=8,
            controls=[
                ft.Icon(ft.Icons.CALCULATE_OUTLINED, color=ft.Colors.PRIMARY),
                ft.Text("协议校验与编码工具箱", size=17, weight=ft.FontWeight.BOLD),
            ],
        ),
        content=ft.Container(width=540, height=440, content=tabs),
        actions=[
            ft.TextButton(content="关闭", on_click=lambda e: page.pop_dialog()),
        ],
    )
    page.show_dialog(dlg)


def open_auto_reply_dialog(
    page: ft.Page,
    config: AppConfig,
    on_rules_updated: Callable[[], None],
) -> None:
    """打开自动应答规则管理弹窗."""
    rules: list[dict[str, Any]] = list(config.get("auto_replies", []))
    rules_list = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, height=260)

    tf_name = ft.TextField(label="规则名称", value="心跳应答", expand=True)
    dd_mode = ft.Dropdown(
        label="匹配方式",
        value="CONTAINS",
        width=175,
        expanded_insets=0,
        text_size=13,
        options=[
            ft.DropdownOption(key="CONTAINS", text="包含文本"),
            ft.DropdownOption(key="EXACT", text="完全相等"),
            ft.DropdownOption(key="HEX", text="包含 HEX"),
            ft.DropdownOption(key="REGEX", text="正则表达式"),
        ],
    )
    tf_delay = ft.TextField(
        label="延迟 (ms)",
        value="50",
        width=130,
        text_size=13,
    )
    tf_trigger = ft.TextField(
        label="触发内容 (如 PING 或 AA 55)",
        expand=True,
        text_style=ft.TextStyle(font_family="Consolas"),
    )
    tf_reply = ft.TextField(
        label="自动回复内容 (支持 \\r\\n)",
        expand=True,
        text_style=ft.TextStyle(font_family="Consolas"),
    )
    sw_reply_hex = ft.Checkbox(label="HEX回复", value=False)

    def _save_and_render() -> None:
        config.set("auto_replies", rules)
        on_rules_updated()
        _render_rules()
        safe_update(rules_list)

    def _toggle_rule(idx: int, val: bool) -> None:
        if 0 <= idx < len(rules):
            rules[idx]["enabled"] = val
            _save_and_render()

    def _delete_rule(idx: int) -> None:
        if 0 <= idx < len(rules):
            del rules[idx]
            _save_and_render()

    def _add_rule(_: Any) -> None:
        trig = (tf_trigger.value or "").strip()
        rep = tf_reply.value or ""
        if not trig or not rep:
            page.show_dialog(
                ft.SnackBar(ft.Text("请填写触发条件与自动回复内容"), bgcolor=ft.Colors.ERROR)
            )
            return
        try:
            delay_val = max(0, int(tf_delay.value or "50"))
        except ValueError:
            delay_val = 50
        rules.append(
            {
                "enabled": True,
                "name": (tf_name.value or "应答规则").strip(),
                "match_mode": dd_mode.value or "CONTAINS",
                "trigger": trig,
                "reply": rep,
                "reply_is_hex": bool(sw_reply_hex.value),
                "delay_ms": delay_val,
            }
        )
        tf_trigger.value = ""
        tf_reply.value = ""
        _save_and_render()
        page.update()

    def _render_rules() -> None:
        rules_list.controls.clear()
        if not rules:
            rules_list.controls.append(
                ft.Container(
                    padding=20,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Text("暂无自动应答规则，可在下方添加模拟从机应答", size=12),
                )
            )
            return

        for idx, r in enumerate(rules):
            rules_list.controls.append(
                ft.Container(
                    border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                    border_radius=8,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                    content=ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Switch(
                                value=bool(r.get("enabled", False)),
                                on_change=lambda e, i=idx: _toggle_rule(i, bool(e.control.value)),
                            ),
                            ft.Column(
                                expand=True,
                                spacing=2,
                                controls=[
                                    ft.Text(
                                        f"{r.get('name', '规则')}  [{r.get('match_mode', 'CONTAINS')} · 延迟 {r.get('delay_ms', 0)}ms]",
                                        size=12.5,
                                        weight=ft.FontWeight.W_600,
                                    ),
                                    ft.Text(
                                        f"收到: {r.get('trigger')}  ➜  回复({'HEX' if r.get('reply_is_hex') else 'TXT'}): {r.get('reply')}",
                                        size=11.5,
                                        font_family="Consolas",
                                        color=ft.Colors.with_opacity(0.7, ft.Colors.ON_SURFACE),
                                    ),
                                ],
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_color=ft.Colors.ERROR,
                                icon_size=18,
                                on_click=lambda e, i=idx: _delete_rule(i),
                            ),
                        ],
                    ),
                )
            )

    _render_rules()

    dlg = ft.AlertDialog(
        modal=True,
        title=ft.Row(
            spacing=8,
            controls=[
                ft.Icon(ft.Icons.AUTO_MODE, color=ft.Colors.PRIMARY),
                ft.Text("自动应答规则引擎（从机模拟）", size=17, weight=ft.FontWeight.BOLD),
            ],
        ),
        content=ft.Container(
            width=580,
            content=ft.Column(
                tight=True,
                spacing=14,
                controls=[
                    rules_list,
                    ft.Divider(),
                    ft.Text("添加新规则", size=13, weight=ft.FontWeight.W_600),
                    ft.Row(spacing=10, controls=[tf_name, dd_mode, tf_delay]),
                    ft.Row(spacing=10, controls=[tf_trigger]),
                    ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            tf_reply,
                            sw_reply_hex,
                            ft.FilledButton(content="添加", icon=ft.Icons.ADD, height=40, on_click=_add_rule),
                        ],
                    ),
                ],
            ),
        ),
        actions=[
            ft.TextButton(content="完成", on_click=lambda e: page.pop_dialog()),
        ],
    )
    page.show_dialog(dlg)
