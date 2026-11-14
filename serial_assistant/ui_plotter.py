import asyncio
from collections import deque
import math

import flet as ft
import flet.canvas as cv

from .utils import decode_bytes, extract_waveform_samples, safe_update

CHANNEL_PALETTE = [
    "#38BDF8",  # Sky Blue
    "#34D399",  # Emerald
    "#FBBF24",  # Amber
    "#F472B6",  # Pink
    "#A78BFA",  # Violet
    "#FB7185",  # Rose
]


class WaveformPlotterPanel:
    """实时多通道串口数字示波器（基于 flet.canvas 绘制），支持自动解析数值与内置模拟信号演示."""

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.window_size: int = 200
        self.paused: bool = False
        self.demo_running: bool = False
        self._demo_phase: float = 0.0

        self.channels: dict[str, deque[float]] = {}
        self.channel_visible: dict[str, bool] = {}
        self.channel_colors: dict[str, str] = {}
        self._rx_line_buf: str = ""

        self._canvas_width: float = 720.0
        self._canvas_height: float = 320.0
        self._dirty: bool = True

        # --- 控件 ---
        self.dd_points = ft.Dropdown(
            value="200",
            width=168,
            expanded_insets=0,
            dense=True,
            text_size=13,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            options=[
                ft.DropdownOption(key="100", text="100 采样点"),
                ft.DropdownOption(key="200", text="200 采样点"),
                ft.DropdownOption(key="400", text="400 采样点"),
            ],
            tooltip="波形横轴窗口点数",
            on_select=self._on_points_change,
        )

        self.btn_demo = ft.FilledTonalButton(
            content="模拟信号演示",
            icon=ft.Icons.AUTO_GRAPH,
            on_click=self._toggle_demo,
            tooltip="生成多通道正弦/余弦测试波形，无需硬件即可体验示波器",
        )

        self.btn_pause = ft.IconButton(
            icon=ft.Icons.PAUSE_CIRCLE_OUTLINE,
            tooltip="暂停/继续刷新波形",
            on_click=self._toggle_pause,
        )

        self.btn_clear = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_color=ft.Colors.ERROR,
            tooltip="清空波形缓存与通道",
            on_click=lambda e: self.clear(),
        )

        self.legend_row = ft.Row(
            wrap=True,
            spacing=8,
            run_spacing=6,
            controls=[
                ft.Text(
                    "支持格式示例: 12.5, 36.8, -4.2  或  temp=25.4, hum=61.2 （以换行符结尾）",
                    size=12,
                    color=ft.Colors.with_opacity(0.6, ft.Colors.ON_SURFACE),
                )
            ],
        )

        self.canvas = cv.Canvas(
            expand=True,
            resize_interval=20,
            on_resize=self._on_canvas_resize,
            shapes=[],
        )

        toolbar = ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
            controls=[
                ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        self.dd_points,
                        self.btn_demo,
                    ],
                ),
                ft.Row(
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        self.btn_pause,
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
                    self.legend_row,
                    ft.Container(
                        expand=True,
                        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                        border_radius=10,
                        bgcolor="#0F172A",  # 专业深色示波器背景
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        padding=ft.Padding.all(4),
                        content=self.canvas,
                    ),
                ],
            ),
        )
        self._redraw_canvas()

    # --- 数据馈入 ---

    def feed_rx_bytes(self, data: bytes, encoding: str = "utf-8") -> None:
        if self.paused or not data:
            return
        chunk = decode_bytes(data, encoding)
        self._rx_line_buf += chunk
        if len(self._rx_line_buf) > 8192:
            self._rx_line_buf = self._rx_line_buf[-4096:]

        while "\n" in self._rx_line_buf:
            line, self._rx_line_buf = self._rx_line_buf.split("\n", 1)
            samples = extract_waveform_samples(line)
            if samples:
                self.push_samples(samples)

    def push_samples(self, samples: dict[str, float]) -> None:
        if self.paused or not samples:
            return
        new_channel_added = False
        for name, val in samples.items():
            if name not in self.channels:
                if len(self.channels) >= 6:
                    continue
                color = CHANNEL_PALETTE[len(self.channels) % len(CHANNEL_PALETTE)]
                self.channels[name] = deque(maxlen=self.window_size)
                self.channel_visible[name] = True
                self.channel_colors[name] = color
                new_channel_added = True
            self.channels[name].append(val)
        self._dirty = True
        if new_channel_added:
            self._rebuild_legend()

    def refresh_if_dirty(self) -> None:
        if not self._dirty or self.paused:
            return
        self._dirty = False
        self._rebuild_legend()
        self._redraw_canvas()
        safe_update(self.view)

    # --- 绘制逻辑 ---

    def _on_canvas_resize(self, e: cv.CanvasResizeEvent) -> None:
        if e.width > 20 and e.height > 20:
            self._canvas_width = float(e.width)
            self._canvas_height = float(e.height)
            self._redraw_canvas()
            safe_update(self.canvas)

    def _rebuild_legend(self) -> None:
        if not self.channels:
            self.legend_row.controls = [
                ft.Text(
                    "支持格式示例: 12.5, 36.8, -4.2  或  temp=25.4, hum=61.2 （也可点击上方「模拟信号演示」预览）",
                    size=12,
                    color=ft.Colors.with_opacity(0.6, ft.Colors.ON_SURFACE),
                )
            ]
            return

        chips: list[ft.Control] = []
        for name, buf in self.channels.items():
            color = self.channel_colors[name]
            vis = self.channel_visible.get(name, True)
            cur = buf[-1] if buf else 0.0
            mn = min(buf) if buf else 0.0
            mx = max(buf) if buf else 0.0
            chips.append(
                ft.Chip(
                    selected=vis,
                    show_checkmark=False,
                    leading=ft.Icon(ft.Icons.CIRCLE, size=10, color=color),
                    label=ft.Text(
                        f"{name}: {cur:.2f}  [{mn:.1f} ~ {mx:.1f}]",
                        size=11.5,
                        font_family="Consolas",
                    ),
                    on_select=lambda e, ch=name: self._toggle_channel(ch),
                )
            )
        self.legend_row.controls = chips

    def _toggle_channel(self, name: str) -> None:
        self.channel_visible[name] = not self.channel_visible.get(name, True)
        self._rebuild_legend()
        self._redraw_canvas()
        self.view.update()

    def _redraw_canvas(self) -> None:
        w = max(self._canvas_width, 200.0)
        h = max(self._canvas_height, 140.0)
        pad_left = 56.0
        pad_right = 16.0
        pad_top = 16.0
        pad_bottom = 22.0
        plot_w = max(w - pad_left - pad_right, 50.0)
        plot_h = max(h - pad_top - pad_bottom, 50.0)

        shapes: list[cv.Shape] = []
        grid_paint = ft.Paint(
            color="#1E293B",
            stroke_width=1,
            style=ft.PaintingStyle.STROKE,
        )
        label_style = ft.TextStyle(
            size=10,
            color="#94A3B8",
            font_family="Consolas",
        )

        # 收集可见通道极值
        all_vals: list[float] = []
        for name, buf in self.channels.items():
            if self.channel_visible.get(name, True) and buf:
                all_vals.extend(buf)

        if all_vals:
            v_min = min(all_vals)
            v_max = max(all_vals)
            span = v_max - v_min
            if span < 1e-6:
                v_min -= 1.0
                v_max += 1.0
            else:
                v_min -= span * 0.08
                v_max += span * 0.08
        else:
            v_min, v_max = -10.0, 10.0

        # 绘制水平与垂直网格线 + Y轴刻度
        h_steps = 5
        for i in range(h_steps + 1):
            frac = i / h_steps
            y = pad_top + frac * plot_h
            val = v_max - frac * (v_max - v_min)
            shapes.append(cv.Line(pad_left, y, pad_left + plot_w, y, paint=grid_paint))
            shapes.append(cv.Text(6, y - 6, value=f"{val:6.1f}", style=label_style))

        v_steps = 8
        for j in range(v_steps + 1):
            x = pad_left + (j / v_steps) * plot_w
            shapes.append(cv.Line(x, pad_top, x, pad_top + plot_h, paint=grid_paint))

        # 0 基准虚线
        if v_min < 0 < v_max:
            zero_y = pad_top + ((v_max - 0.0) / (v_max - v_min)) * plot_h
            shapes.append(
                cv.Line(
                    pad_left,
                    zero_y,
                    pad_left + plot_w,
                    zero_y,
                    paint=ft.Paint(
                        color="#475569",
                        stroke_width=1.2,
                        stroke_dash_pattern=[4, 4],
                        style=ft.PaintingStyle.STROKE,
                    ),
                )
            )

        # 绘制各通道折线
        if not all_vals:
            shapes.append(
                cv.Text(
                    pad_left + plot_w * 0.28,
                    pad_top + plot_h * 0.45,
                    value="暂无波形数据 · 连接串口发送数值或开启「模拟信号演示」",
                    style=ft.TextStyle(size=12, color="#64748B"),
                )
            )
        else:
            denom = max(self.window_size - 1, 1)
            for name, buf in self.channels.items():
                if not self.channel_visible.get(name, True) or len(buf) < 2:
                    continue
                color = self.channel_colors[name]
                elems: list[cv.Path.PathElement] = []
                offset = self.window_size - len(buf)
                for idx, val in enumerate(buf):
                    px = pad_left + ((offset + idx) / denom) * plot_w
                    py = pad_top + ((v_max - val) / (v_max - v_min)) * plot_h
                    if idx == 0:
                        elems.append(cv.Path.MoveTo(px, py))
                    else:
                        elems.append(cv.Path.LineTo(px, py))
                shapes.append(
                    cv.Path(
                        elements=elems,
                        paint=ft.Paint(
                            color=color,
                            stroke_width=2.0,
                            anti_alias=True,
                            stroke_join=ft.StrokeJoin.ROUND,
                            stroke_cap=ft.StrokeCap.ROUND,
                            style=ft.PaintingStyle.STROKE,
                        ),
                    )
                )

        self.canvas.shapes = shapes

    # --- 控制栏动作 ---

    def _on_points_change(self, e: ft.Event[ft.Dropdown]) -> None:
        try:
            self.window_size = int(self.dd_points.value or "200")
        except ValueError:
            self.window_size = 200
        for name, old_buf in list(self.channels.items()):
            self.channels[name] = deque(old_buf, maxlen=self.window_size)
        self._dirty = True
        self.refresh_if_dirty()

    def _toggle_pause(self, e: ft.Event[ft.IconButton]) -> None:
        self.paused = not self.paused
        self.btn_pause.icon = (
            ft.Icons.PLAY_CIRCLE_FILLED if self.paused else ft.Icons.PAUSE_CIRCLE_OUTLINE
        )
        self.btn_pause.icon_color = ft.Colors.AMBER_500 if self.paused else None
        self.view.update()

    def clear(self) -> None:
        self.channels.clear()
        self.channel_visible.clear()
        self.channel_colors.clear()
        self._rx_line_buf = ""
        self._rebuild_legend()
        self._redraw_canvas()
        self.view.update()

    def _toggle_demo(self, e: ft.Event[ft.FilledTonalButton]) -> None:
        self.demo_running = not self.demo_running
        if self.demo_running:
            self.btn_demo.content = "停止模拟演示"
            self.btn_demo.icon = ft.Icons.STOP_CIRCLE_OUTLINED
            self.page.run_task(self._demo_loop)
        else:
            self.btn_demo.content = "模拟信号演示"
            self.btn_demo.icon = ft.Icons.AUTO_GRAPH
        self.view.update()

    async def _demo_loop(self) -> None:
        while self.demo_running:
            if not self.paused:
                t = self._demo_phase
                self._demo_phase += 0.14
                self.push_samples(
                    {
                        "Roll": 45.0 * math.sin(t) + 5.0 * math.sin(3.1 * t),
                        "Pitch": 30.0 * math.cos(0.8 * t),
                        "Temp": 26.5 + 3.5 * math.sin(0.25 * t),
                    }
                )
                self.refresh_if_dirty()
            await asyncio.sleep(0.05)
