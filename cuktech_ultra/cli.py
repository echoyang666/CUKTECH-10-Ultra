"""Rich CLI for CUKTECH 10 GaN Charger Ultra standalone offline BLE client."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .ble_client import BleakTransport, CuktechUltraClient, scan_chargers
from .config import DeviceConfig, load_config, save_config
from .device import (
    PORT_NAMES,
    ChargerState,
    ExtendedProtocolSwitches,
    SceneMode,
    ScreenLanguage,
    ScreenTimeout,
)
from .mock_device import MOCK_DEFAULT_TOKEN, MockChargerTransport

app = typer.Typer(
    name="cuktech-ultra",
    help="酷态科10号超级电能充Ultra 独立离线 BLE 客户端（无需米家 App）",
    no_args_is_help=True,
)
console = Console(emoji=False)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if verbose:
        logging.getLogger("cuktech_ultra").setLevel(logging.DEBUG)
        logging.getLogger("bleak").setLevel(logging.WARNING)


def _resolve_client(
    address: str | None,
    mac: str | None,
    token: str | None,
    mock: bool,
) -> CuktechUltraClient:
    if mock:
        tok = token or MOCK_DEFAULT_TOKEN
        client = CuktechUltraClient(MockChargerTransport(token_hex=tok), tok)
        client.state.mock_mode = True
        return client

    cfg = load_config()
    final_token = token or cfg.token
    final_addr = address or cfg.ble_address
    final_mac = mac or cfg.mac

    if not final_token:
        console.print(
            "[red]缺少设备 BLE Token！[/red]\n"
            "请先运行 [cyan]cuktech-ultra configure --mac <MAC> --token <TOKEN>[/cyan] "
            "或 [cyan]cuktech-ultra auth-qr[/cyan] 一次性获取密钥，或使用 [cyan]--mock[/cyan] 体验模拟模式。"
        )
        raise typer.Exit(code=2)
    if not (final_addr or final_mac):
        console.print(
            "[red]缺少设备蓝牙地址或 MAC！[/red]\n"
            "请先运行 [cyan]cuktech-ultra scan[/cyan] 查找设备，再配置 --address 或 --mac。"
        )
        raise typer.Exit(code=2)

    transport = BleakTransport(address=final_addr or None, mac=final_mac or None)
    return CuktechUltraClient(transport, final_token)


def _render_state_panel(state: ChargerState) -> Panel:
    mode_label = SceneMode(state.scene_mode).label if state.scene_mode in SceneMode._value2member_map_ else str(state.scene_mode)
    timeout_label = (
        ScreenTimeout(state.screen_save_time).label
        if state.screen_save_time in ScreenTimeout._value2member_map_
        else str(state.screen_save_time)
    )
    lang_label = ScreenLanguage(state.screen_language).label if state.screen_language in (0, 1) else str(state.screen_language)

    table = Table(expand=True, show_header=True, header_style="bold cyan")
    table.add_column("端口", style="bold")
    table.add_column("开关")
    table.add_column("电压 (V)", justify="right")
    table.add_column("电流 (A)", justify="right")
    table.add_column("功率 (W)", justify="right", style="bold green")
    table.add_column("快充协议")
    table.add_column("定时关闭")

    for name in PORT_NAMES:
        p = state.ports[name]
        sw = "[green]开启[/green]" if p.enabled else "[red]关闭[/red]"
        proto = f"{p.protocol_name} ({p.protocol_power_w}W)" if p.protocol_name != "NONE" else "-"
        if p.shared_c3_a:
            proto += " [yellow](C3+A 共享5V)[/yellow]"
        timer_str = f"{p.delay_minutes} 分钟" if p.delay_enabled and p.delay_minutes > 0 else "-"
        table.add_row(
            name.upper(),
            sw,
            f"{p.voltage_v:.1f}",
            f"{p.current_a:.1f}",
            f"{p.power_w:.1f}",
            proto,
            timer_str,
        )

    subtitle = (
        f"总功率: [bold green]{state.total_power_w:.1f} W[/bold green]  |  "
        f"模式: [cyan]{mode_label}[/cyan]  |  "
        f"熄屏: {timeout_label}  |  语言: {lang_label}  |  "
        f"USB-A小电流: {'开' if state.usb_a_always_on else '关'}  |  "
        f"空闲熄屏: {'开' if state.screenoff_while_idle else '关'}  |  "
        f"方向锁定: {'开' if state.screen_direction_lock else '关'}"
    )
    tag = " [yellow](MOCK)[/yellow]" if state.mock_mode else ""
    title = f"[bold]CUKTECH 10 Ultra[/bold] ({state.model} / FW {state.firmware_revision or 'N/A'}){tag}"
    return Panel(table, title=title, subtitle=subtitle, border_style="bright_blue")


@app.command()
def scan(
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="扫描时长（秒）")] = 8.0,
) -> None:
    """扫描附近广播 0xFE95 的小米/酷态科 BLE 设备。"""
    console.print(f"[cyan]正在扫描蓝牙设备 ({timeout:.0f}s)...[/cyan]")
    devices = asyncio.run(scan_chargers(timeout=timeout))
    if not devices:
        console.print("[yellow]未发现 0xFE95 或酷态科蓝牙设备。请确认充电器已通电且未被米家 App 占用连接。[/yellow]")
        return

    table = Table(title="发现的 BLE 设备")
    table.add_column("匹配", justify="center")
    table.add_column("名称")
    table.add_column("BLE 地址 / UUID")
    table.add_column("MiBeacon MAC")
    table.add_column("PDID", justify="right")
    table.add_column("RSSI", justify="right")

    for d in devices:
        mark = "[bold green]★[/bold green]" if d.likely_cuktech_10_ultra else ""
        table.add_row(
            mark,
            d.name,
            d.address,
            d.mac or "-",
            str(d.pdid) if d.pdid is not None else "-",
            f"{d.rssi} dBm",
        )
    console.print(table)


@app.command()
def configure(
    token: Annotated[str, typer.Option("--token", help="12/16 字节十六进制 BLE Token/Key")] = "",
    mac: Annotated[str, typer.Option("--mac", help="设备蓝牙 MAC（如 AA:BB:CC:DD:EE:FF）")] = "",
    address: Annotated[str, typer.Option("--address", help="macOS CoreBluetooth UUID 或蓝牙地址")] = "",
    name: Annotated[str, typer.Option("--name", help="设备显示名称")] = "",
) -> None:
    """保存或更新设备 BLE Token / MAC / 地址到本地配置（~/.cuktech-ultra/config.json）。"""
    cfg = load_config()
    if token:
        cfg.token = token.strip()
    if mac:
        cfg.mac = mac.strip().upper()
    if address:
        cfg.ble_address = address.strip()
    if name:
        cfg.name = name
    saved = save_config(cfg)
    console.print(f"[green]配置已保存至 {saved}[/green]")


@app.command("auth-qr")
def auth_qr(
    server: Annotated[str, typer.Option("--server", help="小米云区域: cn/de/us/ru/sg/i2")] = "cn",
    all_devices: Annotated[bool, typer.Option("--all", help="显示账号下全部设备")] = False,
) -> None:
    """一次性小米账号扫码登录，拉取充电器 MAC 与 BLE Token 并保存到本地（后续完全离线）。"""
    from .xiaomi_cloud import XiaomiCloudQRClient

    cloud = XiaomiCloudQRClient(server=server)
    try:
        qr_info = cloud.request_qr_session()
        login_url = qr_info.get("loginUrl", "")
        qr_img = qr_info.get("qr", "")
        lp_url = qr_info.get("lp", "")
        console.print(
            Panel(
                f"请用手机打开浏览器或米家扫码以下登录链接：\n\n[bold cyan]{login_url}[/bold cyan]\n\n"
                f"二维码图片地址：[dim]{qr_img}[/dim]",
                title="小米账号一次性扫码取 Token",
            )
        )
        console.print("[yellow]等待扫码确认...[/yellow]")
        cloud.wait_for_qr_confirmation(lp_url)
        console.print("[green]登录成功，正在查询酷态科设备...[/green]")
        devices = cloud.list_devices(only_cuktech=not all_devices)
    finally:
        cloud.close()

    if not devices:
        console.print("[yellow]未在该账号下找到 njcuk.fitting.ad1204 设备（可加 --all 查看全部设备）。[/yellow]")
        return

    table = Table(title="云端设备列表")
    table.add_column("#")
    table.add_column("名称")
    table.add_column("Model")
    table.add_column("MAC")
    table.add_column("StandardAuth Token")
    table.add_column("BeaconKey (参考)")
    for idx, d in enumerate(devices, start=1):
        table.add_row(str(idx), d.name, d.model, d.mac, d.token or "-", d.beaconkey or "-")
    console.print(table)

    chosen = devices[0]
    if len(devices) > 1:
        sel = typer.prompt("请输入要保存的设备编号", default="1")
        chosen = devices[max(0, min(len(devices) - 1, int(sel) - 1))]

    existing = load_config()
    ble_addr = existing.ble_address if existing.mac.upper() == chosen.mac.upper() else ""
    if not ble_addr:
        try:
            console.print("[cyan]正在快速扫描蓝牙以关联本机 BLE 地址 (5s)...[/cyan]")
            for item in asyncio.run(scan_chargers(timeout=5.0)):
                if item.mac and item.mac.upper() == chosen.mac.upper():
                    ble_addr = item.address
                    break
        except Exception:
            pass

    cfg = DeviceConfig(
        token=chosen.best_ble_token,
        mac=chosen.mac,
        ble_address=ble_addr,
        name=chosen.name,
        did=chosen.did,
        model=chosen.model,
    )
    path = save_config(cfg)
    console.print(f"[green]已保存 {chosen.name} ({chosen.mac}) 至 {path}，后续可完全离线直连蓝牙！[/green]")


@app.command()
def status(
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock", help="使用本地模拟充电器")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """读取充电器当前四口电压/电流/功率、快充协议与全部设置。"""
    _setup_logging(verbose)
    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            console.print(_render_state_panel(client.state))
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command()
def watch(
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    poll: Annotated[float, typer.Option("--poll", help="主动兜底轮询周期（秒，0 表示仅靠 001B 推送）")] = 3.0,
    mock: Annotated[bool, typer.Option("--mock")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """实时监控充电器四口功率、电压、电流和协议（按 Ctrl+C 退出）。"""
    _setup_logging(verbose)
    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            with Live(_render_state_panel(client.state), console=console, refresh_per_second=4) as live:
                client.add_listener(lambda st: live.update(_render_state_panel(st)))
                while True:
                    await asyncio.sleep(poll if poll > 0 else 5.0)
                    if poll > 0:
                        await client.refresh_telemetry()
                        live.update(_render_state_panel(client.state))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await client.disconnect()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


@app.command()
def mode(
    scene: Annotated[str, typer.Argument(help="模式: ai(1) / eco(2) / turbo(3) / balanced(4)")],
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock")] = False,
) -> None:
    """切换功率分配场景模式（2.5 scene_mode）。"""
    mapping = {
        "1": SceneMode.AI_SMART,
        "ai": SceneMode.AI_SMART,
        "2": SceneMode.DIGITAL_ECO,
        "eco": SceneMode.DIGITAL_ECO,
        "3": SceneMode.TURBO_SINGLE,
        "turbo": SceneMode.TURBO_SINGLE,
        "4": SceneMode.BALANCED,
        "balanced": SceneMode.BALANCED,
    }
    target = mapping.get(scene.strip().lower())
    if target is None:
        raise typer.BadParameter("可选模式: ai / eco / turbo / balanced (或 1..4)")

    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            await client.set_scene_mode(target)
            console.print(f"[green]已切换至模式: {target.label} ({int(target)})[/green]")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command()
def port(
    name: Annotated[str, typer.Argument(help="端口名: c1 / c2 / c3 / a")],
    action: Annotated[str, typer.Argument(help="动作: on / off")],
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock")] = False,
) -> None:
    """开启或关闭指定端口输出（2.16 port_ctrl）。"""
    enabled = action.strip().lower() in ("on", "1", "true", "enable")
    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            await client.set_port_enabled(name, enabled)
            console.print(f"[green]端口 {name.upper()} 已{'开启' if enabled else '关闭'}[/green]")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command()
def timer(
    name: Annotated[str, typer.Argument(help="端口名: c1 / c2 / c3 / a")],
    minutes: Annotated[int, typer.Argument(help="定时关闭分钟数（0 表示取消定时）")],
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock")] = False,
) -> None:
    """设置或取消端口定时关闭（2.8 + 2.9..2.12）。"""
    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            await client.set_port_timer(name, minutes)
            if minutes == 0:
                console.print(f"[green]已取消端口 {name.upper()} 的定时关闭[/green]")
            else:
                console.print(f"[green]已设置端口 {name.upper()} {minutes} 分钟后关闭[/green]")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command()
def screen(
    timeout: Annotated[Optional[str], typer.Option("--timeout", help="熄屏时间: 1m/5m/10m/30m/always")] = None,
    lang: Annotated[Optional[str], typer.Option("--lang", help="屏幕语言: zh / en")] = None,
    idle_off: Annotated[Optional[bool], typer.Option("--idle-off/--no-idle-off", help="空闲5分钟自动熄屏")] = None,
    dir_lock: Annotated[Optional[bool], typer.Option("--dir-lock/--no-dir-lock", help="屏幕方向锁定")] = None,
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock")] = False,
) -> None:
    """调整充电器屏幕熄屏时间、语言、空闲熄屏与方向锁定。"""
    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            if timeout is not None:
                st = ScreenTimeout.from_name(timeout)
                await client.set_screen_timeout(st)
                console.print(f"[green]熄屏时间已设为: {st.label}[/green]")
            if lang is not None:
                sl = ScreenLanguage.CHINESE if lang.lower() in ("zh", "cn", "1") else ScreenLanguage.ENGLISH
                await client.set_screen_language(sl)
                console.print(f"[green]屏幕语言已设为: {sl.label}[/green]")
            if idle_off is not None:
                await client.set_screenoff_while_idle(idle_off)
                console.print(f"[green]空闲自动熄屏: {'开启' if idle_off else '关闭'}[/green]")
            if dir_lock is not None:
                await client.set_screen_direction_lock(dir_lock)
                console.print(f"[green]屏幕方向锁定: {'开启' if dir_lock else '关闭'}[/green]")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command("usb-a-trickle")
def usb_a_trickle(
    action: Annotated[str, typer.Argument(help="on / off")],
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock")] = False,
) -> None:
    """开关 USB-A 小电流模式（2.15 usb_a_always_on，开启后 4 小时自动关闭）。"""
    enabled = action.strip().lower() in ("on", "1", "true")
    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            await client.set_usb_a_always_on(enabled)
            console.print(f"[green]USB-A 小电流模式已{'开启' if enabled else '关闭'}[/green]")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command()
def protocol(
    port_name: Annotated[str, typer.Argument(help="端口名: c1 / c2 / c3 / a")],
    proto: Annotated[str, typer.Argument(help="协议名: c1/c2 支持 pd/pps/ufcs；c3/a 支持 scp/ufcs")],
    action: Annotated[str, typer.Argument(help="on / off")],
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock")] = False,
) -> None:
    """开关指定端口的快充协议（2.21 protocol_ctl_extend，需固件 >= 2.1.2_0040）。"""
    p_key = port_name.strip().lower()
    proto_key = proto.strip().lower()
    field_name = f"{p_key}_{proto_key}"
    valid_fields = {
        "c1_pd", "c1_pps", "c1_ufcs",
        "c2_pd", "c2_pps", "c2_ufcs",
        "c3_scp", "c3_ufcs",
        "a_scp", "a_ufcs",
    }
    if field_name not in valid_fields:
        raise typer.BadParameter(
            f"不支持的端口/协议组合: {port_name}/{proto}。"
            "c1/c2 支持 pd/pps/ufcs；c3/a 支持 scp/ufcs。"
        )
    enabled = action.strip().lower() in ("on", "1", "true")
    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            cur = client.state.protocol_v2
            next_sw = ExtendedProtocolSwitches(
                c1_pd=enabled if field_name == "c1_pd" else cur.c1_pd,
                c1_pps=enabled if field_name == "c1_pps" else cur.c1_pps,
                c1_ufcs=enabled if field_name == "c1_ufcs" else cur.c1_ufcs,
                c2_pd=enabled if field_name == "c2_pd" else cur.c2_pd,
                c2_pps=enabled if field_name == "c2_pps" else cur.c2_pps,
                c2_ufcs=enabled if field_name == "c2_ufcs" else cur.c2_ufcs,
                c3_scp=enabled if field_name == "c3_scp" else cur.c3_scp,
                c3_ufcs=enabled if field_name == "c3_ufcs" else cur.c3_ufcs,
                a_scp=enabled if field_name == "a_scp" else cur.a_scp,
                a_ufcs=enabled if field_name == "a_ufcs" else cur.a_ufcs,
            )
            await client.set_protocol_switches_v2(next_sw)
            console.print(f"[green]端口 {p_key.upper()} 的 {proto_key.upper()} 协议已{'开启' if enabled else '关闭'}[/green]")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command()
def reset(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock")] = False,
) -> None:
    """恢复充电器默认设置（Action 2.1 reset_setting）。"""
    if not yes and not typer.confirm("确定要恢复充电器所有默认设置吗？"):
        raise typer.Abort()
    client = _resolve_client(address, mac, token, mock)

    async def _run() -> None:
        await client.connect()
        try:
            await client.reset_settings()
            console.print("[green]充电器已恢复默认设置[/green]")
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command()
def web(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p")] = 8765,
    address: Annotated[Optional[str], typer.Option("--address")] = None,
    mac: Annotated[Optional[str], typer.Option("--mac")] = None,
    token: Annotated[Optional[str], typer.Option("--token")] = None,
    mock: Annotated[bool, typer.Option("--mock", help="以模拟充电器模式启动 Web 控制台")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """启动本地 Web 实时仪表盘与控制面板。"""
    import uvicorn

    from .web import create_app

    _setup_logging(verbose)
    client = _resolve_client(address, mac, token, mock)
    fastapi_app = create_app(client)
    console.print(
        f"[bold green]CUKTECH 10 Ultra 本地控制台已启动:[/bold green] "
        f"[link=http://{host}:{port}]http://{host}:{port}[/link]"
        + (" [yellow](MOCK 模式)[/yellow]" if mock else "")
    )
    uvicorn.run(
        fastapi_app,
        host=host,
        port=port,
        loop="asyncio",
        log_level="info" if verbose else "warning",
    )
