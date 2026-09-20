"""Persistent settings for tuiray.

Stored as JSON under $XDG_CONFIG_HOME/tuiray/settings.json so the repo stays
clean (the exported configs there are secrets and already gitignored).
Keys are addressed with dots: settings.get("tun.mtu").
"""

import json
import os
import shutil
from pathlib import Path

APP = "tuiray"
HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def _xdg(var: str, fallback: str) -> Path:
    base = os.environ.get(var)
    return Path(base) if base else Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / APP
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / APP
SETTINGS_FILE = CONFIG_DIR / "settings.json"


def _default_xray() -> str:
    """The xray shipped next to this repo wins over one on $PATH."""
    local = REPO / "xray"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("xray") or ""


def _default_path(name: str) -> str:
    candidate = REPO / name
    return str(candidate) if candidate.exists() else ""


DEFAULTS = {
    "xray_bin": "",
    "config_path": "",          # the xray config currently selected
    "configs_dir": "",          # where the config picker / ping tool look
    "geoip_path": "",
    "geosite_path": "",
    "mode": "proxy",            # "proxy" | "tun"
    "loglevel": "warning",      # debug|info|warning|error|none
    "log_lines": 3000,
    "proxy": {
        "listen": "127.0.0.1",
        "socks_port": 10808,
        "http_port": 10809,
        "set_system_proxy": False,   # GNOME gsettings while running
    },
    "tun": {
        "name": "tun0",
        "mtu": 1500,
        "gateway": ["10.10.0.1/24", "fd00:10:10::1/64"],
        "dns": ["1.1.1.1", "8.8.8.8"],
        # -> settings.autoSystemRoutingTable. Split defaults rather than
        # 0.0.0.0/0: a /1 is more specific than any existing default route, so
        # capture works whatever metric NetworkManager gave the real link.
        "routes": ["0.0.0.0/1", "128.0.0.0/1", "::/1", "8000::/1"],
        "outbound_interface": "auto",      # physical iface for xray's own traffic
        "keep_socks": True,                # also expose the SOCKS inbound in TUN mode
    },
    "stats": {
        # xray's StatsService, polled for the traffic counters in the corner.
        "enabled": True,
        "api_port": 10815,      # loopback-only gRPC API the TUI queries
    },
    "routing": {
        # Split routing applied on top of whatever the subscription config does.
        "bypass_private": True,      # LAN / private ranges never enter the tunnel
        "direct_domains": [],        # e.g. "regexp:.*\\.ru$", "geosite:category-ru"
        "direct_ips": [],            # e.g. "geoip:ru", "203.0.113.0/24"
        "proxy_domains": [],         # forced through the proxy, beats the direct rules
    },
    "ping": {
        "mode": "proxy",        # "proxy" (real latency through xray) | "tcp" (endpoint handshake)
        "url": "http://cp.cloudflare.com/generate_204",
        "timeout": 8.0,
        "concurrency": 4,
        "attempts": 2,
    },
}


def _merge(base, override):
    """Recursive dict merge -- keeps new default keys when the file is older."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Settings:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls) -> "Settings":
        data = dict(DEFAULTS)
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            data = _merge(data, stored)
        except (OSError, json.JSONDecodeError):
            pass

        # First run (or a moved repo): fill in whatever we can find locally.
        if not data["xray_bin"] or not Path(data["xray_bin"]).exists():
            data["xray_bin"] = _default_xray()
        if not data["configs_dir"] or not Path(data["configs_dir"]).is_dir():
            data["configs_dir"] = _default_path("configs") or str(REPO)
        if not data["geoip_path"] or not Path(data["geoip_path"]).exists():
            data["geoip_path"] = _default_path("geoip.dat")
        if not data["geosite_path"] or not Path(data["geosite_path"]).exists():
            data["geosite_path"] = _default_path("geosite.dat")

        # Paths may arrive relative (CLI flags, a hand-edited file); store them
        # absolute so the TUI works when started from any directory. A relative
        # path is resolved against the working directory first, then the repo.
        for key in ("xray_bin", "config_path", "configs_dir",
                    "geoip_path", "geosite_path"):
            value = data.get(key)
            if not value or Path(value).is_absolute():
                continue
            for base in (Path.cwd(), REPO):
                candidate = (base / value).expanduser()
                if candidate.exists():
                    data[key] = str(candidate.resolve())
                    break
            else:
                data[key] = str((REPO / value).expanduser())
        return cls(data)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)

    def get(self, key: str, default=None):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value) -> None:
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self.save()

    # -- convenience ------------------------------------------------------
    @property
    def mode(self) -> str:
        return self.get("mode", "proxy")

    @property
    def config_path(self) -> str:
        return self.get("config_path", "")

    def asset_dir(self) -> Path:
        """Directory handed to xray as XRAY_LOCATION_ASSET.

        The two .dat files can be picked independently and may live in
        different directories under different names, so a cache dir of symlinks
        is materialised for xray, which insists on `<dir>/geoip.dat`.
        """
        dest = CACHE_DIR / "assets"
        dest.mkdir(parents=True, exist_ok=True)
        for name, key in (("geoip.dat", "geoip_path"), ("geosite.dat", "geosite_path")):
            src = self.get(key) or ""
            link = dest / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                if src and Path(src).exists():
                    link.symlink_to(Path(src).resolve())
            except OSError:
                pass
        return dest
