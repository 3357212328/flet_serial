import socket
import threading
from typing import Callable

import flet as ft

from .config import AppConfig
from .event_bus import EventBus
from .utils import get_local_ips, safe_update


class NetworkChannel:
    """增强型网络调试模块：支持 TCP Client、多客户端 TCP Server（广播/单发）与双向独立端口 UDP."""

    def __init__(
        self,
        page: ft.Page,
        config: AppConfig,
        bus: EventBus,
        clipboard: ft.Clipboard,
        on_status_change: Callable[[], None],
    ) -> None:
        self.page = page
        self.config = config
        self.bus = bus
        self.clipboard = clipboard
        self.on_status_change = on_status_change

        self.running: bool = False
        self.sock: socket.socket | None = None
        self._clients_lock = threading.Lock()
        self.tcp_clients: dict[str, socket.socket] = {}
        self.last_udp_peer: tuple[str, int] | None = None

        cfg = self.config.get("network", {})
        proto = str(cfg.get("protocol", "TCP Client"))

        # --- 本机局域网 IP 提示条 ---
        local_ips = get_local_ips()
        ip_chips = [
            ft.Container(
                content=ft.Text(
                    f"本机: {ip}",
                    size=11,
                    font_family="Consolas",
                    color=ft.Colors.PRIMARY,
                ),
                bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.PRIMARY),
                padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                border_radius=6,
                ink=True,
                tooltip="点击复制此本机 IP 地址",
                on_click=lambda e, addr=ip: self.page.run_task(self._copy_ip, addr),
            )
            for ip in local_ips[:2]
        ]

        # --- UI 控件 ---
        self.dd_proto = ft.Dropdown(
            label="网络协议模式",
            value=proto,
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[
                ft.DropdownOption(key="TCP Client", text="TCP Client (客户端)"),
                ft.DropdownOption(key="TCP Server", text="TCP Server (多连接服务端)"),
                ft.DropdownOption(key="UDP", text="UDP (数据报)"),
            ],
            on_select=self._on_proto_change,
        )

        self.tf_ip = ft.TextField(
            label="目标主机 IP",
            value=str(cfg.get("ip", "127.0.0.1")),
            expand=True,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            on_change=lambda e: self._save_prefs(),
        )
        self.tf_port = ft.TextField(
            label="目标端口",
            value=str(cfg.get("port", "8080")),
            width=118,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            on_change=lambda e: self._save_prefs(),
        )

        self.tf_local_port = ft.TextField(
            label="本地绑定监听端口",
            value=str(cfg.get("local_port", "8081")),
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            on_change=lambda e: self._save_prefs(),
        )

        self.sw_udp_reply_peer = ft.Checkbox(
            label="自动回复至最新UDP来源地址",
            value=False,
        )

        self.dd_clients = ft.Dropdown(
            label="已连接客户端（发送目标）",
            value="ALL",
            expand=True,
            expanded_insets=0,
            text_size=13,
            options=[ft.DropdownOption(key="ALL", text="所有客户端 (广播)")],
        )
        self.btn_kick = ft.IconButton(
            icon=ft.Icons.PERSON_REMOVE_OUTLINED,
            icon_color=ft.Colors.ERROR,
            tooltip="断开所选客户端连接",
            on_click=self._kick_selected_client,
        )
        self.row_server_clients = ft.Row(
            spacing=6,
            controls=[self.dd_clients, self.btn_kick],
        )

        self.btn_connect = ft.FilledButton(
            content="启动连接",
            icon=ft.Icons.LAN,
            expand=True,
            height=42,
            on_click=self.toggle_connect,
        )

        self._apply_proto_visibility(proto)

        self.view = ft.Container(
            padding=ft.Padding.only(top=16, bottom=4),
            content=ft.Column(
                spacing=18,
                tight=True,
                controls=[
                    ft.Row(spacing=6, wrap=True, controls=ip_chips),
                    ft.Row(controls=[self.dd_proto]),
                    ft.Row(spacing=10, controls=[self.tf_ip, self.tf_port]),
                    self.tf_local_port,
                    self.sw_udp_reply_peer,
                    self.row_server_clients,
                    ft.Row(controls=[self.btn_connect]),
                ],
            ),
        )

    # --- 状态查询 ---

    @property
    def is_connected(self) -> bool:
        return self.running

    @property
    def status_summary(self) -> str:
        if not self.running:
            return "网络未连接"
        proto = self.dd_proto.value or "TCP Client"
        if proto == "TCP Server":
            with self._clients_lock:
                cnt = len(self.tcp_clients)
            return f"TCP Server :{self.tf_port.value} ({cnt} 客户端)"
        if proto == "UDP":
            return f"UDP :{self.tf_local_port.value} -> {self.tf_ip.value}:{self.tf_port.value}"
        return f"TCP -> {self.tf_ip.value}:{self.tf_port.value}"

    async def _copy_ip(self, addr: str) -> None:
        await self.clipboard.set(addr)
        self.page.show_dialog(ft.SnackBar(ft.Text(f"已复制本机 IP: {addr}")))

    def _save_prefs(self) -> None:
        self.config.update_section(
            "network",
            protocol=self.dd_proto.value or "TCP Client",
            ip=self.tf_ip.value or "127.0.0.1",
            port=self.tf_port.value or "8080",
            local_port=self.tf_local_port.value or "8081",
        )

    def _apply_proto_visibility(self, proto: str) -> None:
        if proto == "TCP Server":
            self.tf_ip.label = "监听网卡地址"
            if self.tf_ip.value == "127.0.0.1":
                self.tf_ip.value = "0.0.0.0"
            self.tf_port.label = "监听端口"
            self.tf_local_port.visible = False
            self.sw_udp_reply_peer.visible = False
            self.row_server_clients.visible = True
            if not self.running:
                self.btn_connect.content = "开始监听"
        elif proto == "UDP":
            self.tf_ip.label = "目标远程 IP"
            if self.tf_ip.value == "0.0.0.0":
                self.tf_ip.value = "127.0.0.1"
            self.tf_port.label = "目标端口"
            self.tf_local_port.visible = True
            self.sw_udp_reply_peer.visible = True
            self.row_server_clients.visible = False
            if not self.running:
                self.btn_connect.content = "打开 UDP"
        else:
            self.tf_ip.label = "远程服务器 IP"
            if self.tf_ip.value == "0.0.0.0":
                self.tf_ip.value = "127.0.0.1"
            self.tf_port.label = "远程端口"
            self.tf_local_port.visible = False
            self.sw_udp_reply_peer.visible = False
            self.row_server_clients.visible = False
            if not self.running:
                self.btn_connect.content = "连接服务器"

    def _on_proto_change(self, e: ft.Event[ft.Dropdown]) -> None:
        proto = self.dd_proto.value or "TCP Client"
        self._apply_proto_visibility(proto)
        self._save_prefs()
        self.view.update()

    # --- 启动/停止逻辑 ---

    def toggle_connect(self, e: ft.Event[ft.FilledButton] | None = None) -> None:
        if self.running:
            self.close()
        else:
            self._start_network()

    def _start_network(self) -> None:
        proto = self.dd_proto.value or "TCP Client"
        ip = (self.tf_ip.value or "127.0.0.1").strip()
        try:
            port = int((self.tf_port.value or "8080").strip())
            local_port = int((self.tf_local_port.value or "8081").strip())
            if not (1 <= port <= 65535) or not (1 <= local_port <= 65535):
                raise ValueError
        except ValueError:
            self.bus.emit("ERROR", "端口号必须为 1 ~ 65535 之间的整数", source="NET")
            return

        self._save_prefs()
        self.running = True
        self.dd_proto.disabled = True
        if proto != "UDP":
            self.tf_ip.disabled = True
            self.tf_port.disabled = True
        self.tf_local_port.disabled = True

        self.btn_connect.content = "停止网络"
        self.btn_connect.icon = ft.Icons.WIFI_OFF
        self.btn_connect.style = ft.ButtonStyle(
            bgcolor=ft.Colors.RED_600,
            color=ft.Colors.WHITE,
        )
        self.view.update()
        self.on_status_change()

        threading.Thread(
            target=self._network_worker,
            args=(proto, ip, port, local_port),
            daemon=True,
        ).start()

    def close(self) -> None:
        was_running = self.running
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

        with self._clients_lock:
            for c_sock in self.tcp_clients.values():
                try:
                    c_sock.close()
                except OSError:
                    pass
            self.tcp_clients.clear()

        if was_running:
            self.bus.emit("INFO", "网络通道已停止", source="NET")
        self._sync_ui_stopped()

    def _sync_ui_stopped(self) -> None:
        proto = self.dd_proto.value or "TCP Client"
        self.dd_proto.disabled = False
        self.tf_ip.disabled = False
        self.tf_port.disabled = False
        self.tf_local_port.disabled = False
        self.btn_connect.icon = ft.Icons.LAN
        self.btn_connect.style = None
        self._apply_proto_visibility(proto)
        self._refresh_clients_dropdown()
        safe_update(self.view)
        self.on_status_change()

    def _refresh_clients_dropdown(self) -> None:
        with self._clients_lock:
            keys = list(self.tcp_clients.keys())
        opts = [ft.DropdownOption(key="ALL", text=f"所有客户端 (广播 · {len(keys)})")]
        for k in keys:
            opts.append(ft.DropdownOption(key=k, text=k))
        self.dd_clients.options = opts
        if self.dd_clients.value != "ALL" and self.dd_clients.value not in keys:
            self.dd_clients.value = "ALL"
        safe_update(self.view)
        self.on_status_change()

    def _kick_selected_client(self, e: ft.Event[ft.IconButton]) -> None:
        target = self.dd_clients.value
        with self._clients_lock:
            if target == "ALL":
                items = list(self.tcp_clients.items())
                self.tcp_clients.clear()
            elif target and target in self.tcp_clients:
                items = [(target, self.tcp_clients.pop(target))]
            else:
                items = []

        for key, c_sock in items:
            try:
                c_sock.close()
            except OSError:
                pass
            self.bus.emit("INFO", f"已主动断开客户端 {key}", source="NET")
        self._refresh_clients_dropdown()

    # --- 后台线程 ---

    def _network_worker(self, proto: str, ip: str, port: int, local_port: int) -> None:
        try:
            if proto == "TCP Client":
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3.0)
                s.connect((ip, port))
                s.settimeout(0.5)
                self.sock = s
                self.bus.emit("INFO", f"已连接 TCP 服务器 {ip}:{port}", source="NET")
                while self.running:
                    try:
                        data = s.recv(4096)
                        if not data:
                            self.bus.emit("INFO", "远程服务器已关闭连接", source="NET")
                            break
                        self.bus.emit("RX", data, source="NET")
                    except socket.timeout:
                        continue
                    except OSError:
                        break

            elif proto == "TCP Server":
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                bind_addr = ip if ip else "0.0.0.0"
                s.bind((bind_addr, port))
                s.listen(8)
                s.settimeout(0.5)
                self.sock = s
                self.bus.emit("INFO", f"TCP Server 正在监听 {bind_addr}:{port}", source="NET")
                while self.running:
                    try:
                        client_sock, addr = s.accept()
                        client_key = f"{addr[0]}:{addr[1]}"
                        client_sock.settimeout(0.5)
                        with self._clients_lock:
                            self.tcp_clients[client_key] = client_sock
                        self.bus.emit("INFO", f"客户端上线: {client_key}", source="NET")
                        self.bus.call_ui(self._refresh_clients_dropdown)
                        threading.Thread(
                            target=self._tcp_client_reader,
                            args=(client_key, client_sock),
                            daemon=True,
                        ).start()
                    except socket.timeout:
                        continue
                    except OSError:
                        break

            elif proto == "UDP":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", local_port))
                s.settimeout(0.5)
                self.sock = s
                self.bus.emit("INFO", f"UDP 已绑定本地端口 :{local_port}", source="NET")
                while self.running:
                    try:
                        data, addr = s.recvfrom(8192)
                        self.last_udp_peer = (addr[0], addr[1])
                        self.bus.emit(
                            "RX",
                            data,
                            source="NET",
                            extra=f"{addr[0]}:{addr[1]}",
                        )
                    except socket.timeout:
                        continue
                    except OSError:
                        break

        except Exception as exc:
            if self.running:
                self.bus.emit("ERROR", f"网络通道异常: {exc}", source="NET")
        finally:
            if self.running:
                self.running = False
                if self.sock:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
                self.bus.call_ui(self._sync_ui_stopped)

    def _tcp_client_reader(self, key: str, client_sock: socket.socket) -> None:
        while self.running:
            try:
                data = client_sock.recv(4096)
                if not data:
                    break
                self.bus.emit("RX", data, source="NET", extra=key)
            except socket.timeout:
                continue
            except OSError:
                break

        with self._clients_lock:
            self.tcp_clients.pop(key, None)
        try:
            client_sock.close()
        except OSError:
            pass
        if self.running:
            self.bus.emit("INFO", f"客户端离线: {key}", source="NET")
            self.bus.call_ui(self._refresh_clients_dropdown)

    # --- 发送数据 ---

    def send(self, data: bytes) -> bool:
        if not self.running:
            self.bus.emit("ERROR", "发送失败：网络未连接", source="NET")
            return False

        proto = self.dd_proto.value or "TCP Client"
        try:
            if proto == "TCP Client":
                if not self.sock:
                    return False
                self.sock.sendall(data)
                self.bus.emit("TX", data, source="NET")
                return True

            if proto == "TCP Server":
                target = self.dd_clients.value or "ALL"
                with self._clients_lock:
                    if not self.tcp_clients:
                        self.bus.emit("ERROR", "发送失败：当前没有已连接的 TCP 客户端", source="NET")
                        return False
                    targets = (
                        list(self.tcp_clients.items())
                        if target == "ALL"
                        else [(target, self.tcp_clients[target])]
                        if target in self.tcp_clients
                        else []
                    )
                if not targets:
                    self.bus.emit("ERROR", "所选客户端已离线", source="NET")
                    return False
                for key, c_sock in targets:
                    c_sock.sendall(data)
                extra_tag = "广播" if target == "ALL" else target
                self.bus.emit("TX", data, source="NET", extra=extra_tag)
                return True

            if proto == "UDP":
                if not self.sock:
                    return False
                if self.sw_udp_reply_peer.value and self.last_udp_peer:
                    dst = self.last_udp_peer
                else:
                    dst_ip = (self.tf_ip.value or "127.0.0.1").strip()
                    dst_port = int((self.tf_port.value or "8080").strip())
                    dst = (dst_ip, dst_port)
                self.sock.sendto(data, dst)
                self.bus.emit("TX", data, source="NET", extra=f"->{dst[0]}:{dst[1]}")
                return True

        except Exception as exc:
            self.bus.emit("ERROR", f"网络发送失败: {exc}", source="NET")
        return False
