"""Read xray configs and rewrite their inbounds for the selected mode.

Every file exported by export_configs.py is a complete xray config (dns,
routing, outbounds); only the inbound side differs between "run it as a local
SOCKS/HTTP proxy" and "run it as a system-wide TUN". Routing rules are left
untouched -- rules that do not name an inboundTag apply to TUN traffic just as
they do to SOCKS traffic.
"""

import copy
import json
from pathlib import Path

PROXY_PROTOCOLS = {"freedom", "blackhole", "dns", "loopback"}


def load(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    if not isinstance(config, dict):
        raise ValueError("not an xray config object (got a JSON array?)")
    return config


def summarize(config: dict) -> dict:
    """Short human description of what a config actually does."""
    outbounds = config.get("outbounds") or []
    routing = config.get("routing") or {}
    servers, networks, protocols = [], set(), set()

    for outbound in outbounds:
        protocol = outbound.get("protocol")
        if protocol in PROXY_PROTOCOLS:
            continue
        protocols.add(protocol)
        stream = outbound.get("streamSettings") or {}
        networks.add(stream.get("network") or "tcp")
        for address, port in _endpoints(outbound):
            servers.append(f"{address}:{port}")

    return {
        "servers": servers,
        "protocols": sorted(p for p in protocols if p),
        "networks": sorted(networks),
        "balancers": [b.get("tag") for b in (routing.get("balancers") or [])],
        "inbounds": [
            f"{i.get('protocol')}:{i.get('port')}" for i in (config.get("inbounds") or [])
        ],
    }


def _endpoints(outbound: dict):
    """(address, port) pairs of an outbound, across the protocol shapes we see."""
    settings = outbound.get("settings") or {}
    for entry in settings.get("vnext") or []:          # vless / vmess
        yield entry.get("address"), entry.get("port")
    for entry in settings.get("servers") or []:        # trojan / shadowsocks / socks
        yield entry.get("address"), entry.get("port")


def endpoints(config: dict):
    """All proxy endpoints of a config, in outbound order."""
    found = []
    for outbound in config.get("outbounds") or []:
        if outbound.get("protocol") in PROXY_PROTOCOLS:
            continue
        for address, port in _endpoints(outbound):
            if address and port:
                found.append((str(address), int(port)))
    return found


SNIFFING = {
    "enabled": True,
    "destOverride": ["http", "tls", "quic"],
    "routeOnly": False,
}


def _sniffing_of(config: dict) -> dict:
    """Reuse the config's own sniffing block so fakedns setups keep working."""
    for inbound in config.get("inbounds") or []:
        sniffing = inbound.get("sniffing")
        if sniffing:
            return copy.deepcopy(sniffing)
    return copy.deepcopy(SNIFFING)


def socks_inbound(listen: str, port: int, tag: str = "socks", sniffing: dict = None) -> dict:
    return {
        "tag": tag,
        "listen": listen,
        "port": int(port),
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": True},
        "sniffing": copy.deepcopy(sniffing or SNIFFING),
    }


def http_inbound(listen: str, port: int, tag: str = "http", sniffing: dict = None) -> dict:
    return {
        "tag": tag,
        "listen": listen,
        "port": int(port),
        "protocol": "http",
        "settings": {"allowTransparent": False},
        "sniffing": copy.deepcopy(sniffing or SNIFFING),
    }


def tun_inbound(tun: dict, sniffing: dict) -> dict:
    """The native xray TUN inbound (proxy/tun, xray-core >= 26.x).

    settings schema, as accepted by infra/conf.TunConfig:
        name                   string    device name
        mtu                    uint32
        gateway                []string  addresses assigned to the device
        dns                    []string  DNS servers announced on the device
        autoSystemRoutingTable []string  routes pointed at the device
        autoOutboundsInterface string    interface xray's own traffic leaves by
    """
    settings = {
        "name": tun.get("name") or "tun0",
        "mtu": int(tun.get("mtu") or 1500),
        "gateway": list(tun.get("gateway") or []),
        "dns": list(tun.get("dns") or []),
        "autoSystemRoutingTable": list(tun.get("routes") or []),
    }
    interface = (tun.get("outbound_interface") or "").strip()
    if interface:
        settings["autoOutboundsInterface"] = interface
    return {
        "tag": "tun-in",
        "protocol": "tun",
        "settings": settings,
        "sniffing": copy.deepcopy(sniffing),
    }


def build_run_config(config: dict, settings, tun_override: dict = None) -> dict:
    """The config actually handed to `xray run`, with inbounds for the mode.

    `tun_override` lets the runner substitute a resolved TUN block (e.g. with
    "auto" turned into the real default interface name).
    """
    run = copy.deepcopy(config)
    sniffing = _sniffing_of(config)

    listen = settings.get("proxy.listen", "127.0.0.1")
    socks_port = int(settings.get("proxy.socks_port", 10808))
    http_port = int(settings.get("proxy.http_port", 10809))

    if settings.mode == "tun":
        tun = tun_override if tun_override is not None else settings.get("tun", {})
        inbounds = [tun_inbound(tun, sniffing)]
        if tun.get("keep_socks", True):
            # Handy for testing while the TUN is up, and harmless otherwise.
            inbounds.append(socks_inbound(listen, socks_port, sniffing=sniffing))
    else:
        inbounds = [socks_inbound(listen, socks_port, sniffing=sniffing),
                    http_inbound(listen, http_port, sniffing=sniffing)]

    run["inbounds"] = inbounds

    log = dict(run.get("log") or {})
    log["loglevel"] = settings.get("loglevel", "warning")
    # Logs must reach our pipe, so drop any file/None redirection in the config.
    log.pop("access", None)
    log.pop("error", None)
    run["log"] = log
    return run


def build_ping_config(config: dict, port: int) -> dict:
    """A throwaway config exposing one SOCKS inbound, for latency probing."""
    run = copy.deepcopy(config)
    run["inbounds"] = [socks_inbound("127.0.0.1", port, sniffing=_sniffing_of(config))]
    run["log"] = {"loglevel": "error"}
    return run


def needs_assets(config: dict) -> set:
    """Which .dat files the routing rules of this config reference."""
    blob = json.dumps(config, ensure_ascii=False)
    needed = set()
    if '"geosite:' in blob or '"ext:geosite.dat' in blob:
        needed.add("geosite.dat")
    if '"geoip:' in blob or '"ext:geoip.dat' in blob:
        needed.add("geoip.dat")
    return needed


def write(config: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def list_configs(root) -> list:
    """*.json under root, recursively, sorted by (directory, name)."""
    root = Path(root)
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*.json") if p.is_file()]
    return sorted(files, key=lambda p: (str(p.parent), p.name))
