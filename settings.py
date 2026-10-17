"""Persistent settings for tuiray.

Stored as JSON under $XDG_CONFIG_HOME/tuiray/settings.json so the repo stays
clean (the exported configs there are secrets and already gitignored).
Keys are addressed with dots: settings.get("tun.mtu").
"""

import json
import os
import pwd
import shutil
from pathlib import Path

APP = "tuiray"
HERE = Path(__file__).resolve().parent
# The tuiray folder itself: where the xray binary and the .dat files are looked
# for (and downloaded to). It used to be the parent, which put them in $HOME.
REPO = HERE


def _invoking_user() -> tuple:
    """(home, uid, gid) of the human running tuiray, even under sudo.

    Sharing a connection needs root, so the TUI is often started with sudo --
    and then $HOME is /root. Settings and the cache have to stay with the real
    user: otherwise they silently reset on every privileged run, and every file
    written lands root-owned in the user's directories, breaking the next
    unprivileged one.
    """
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        try:
            entry = pwd.getpwuid(int(os.environ["SUDO_UID"]))
            return Path(entry.pw_dir), entry.pw_uid, entry.pw_gid
        except (KeyError, ValueError):
            pass
    return Path.home(), os.getuid(), os.getgid()


HOME, USER_UID, USER_GID = _invoking_user()


def _xdg(var: str, fallback: str) -> Path:
    # sudo may pass XDG_* through unchanged (or point them at root's dirs);
    # only honour one that actually lives in the invoking user's home.
    base = os.environ.get(var)
    if base:
        path = Path(base)
        if USER_UID == os.getuid() or path == HOME or HOME in path.parents:
            return path
    return HOME / fallback


def chown_back(path) -> None:
    """Hand a file written as root back to the user who started tuiray."""
    if os.geteuid() != 0 or USER_UID == 0:
        return
    try:
        # A symlink must not be followed: the asset links point at the .dat
        # files, which are none of our business.
        if os.path.islink(path):
            os.lchown(path, USER_UID, USER_GID)
        else:
            os.chown(path, USER_UID, USER_GID)
    except OSError:
        pass


def mkdir_owned(path: Path) -> Path:
    """mkdir -p, with each level we create handed back to the invoking user."""
    created = []
    probe = path
    while not probe.exists() and probe != probe.parent:
        created.append(probe)
        probe = probe.parent
    path.mkdir(parents=True, exist_ok=True)
    for level in created:
        chown_back(level)
    return path


CONFIG_HOME = _xdg("XDG_CONFIG_HOME", ".config")    # KDE's kioslaverc lives here
CONFIG_DIR = CONFIG_HOME / APP
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / APP
# Where subscriptions and pasted links are written. Only ever written into,
# never scanned: the configs page reads just the sources the user added.
EXPORT_DIR = _xdg("XDG_DATA_HOME", ".local/share") / APP / "configs"
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
    "configs_dir": "",          # where subscriptions / links are exported to
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
    "share": {
        # Hand the tunnel to other machines: an access point on the Wi-Fi card
        # and/or a wired segment. Guests are masqueraded into the TUN device,
        # which is why sharing needs TUN mode.
        "isolate": True,       # guests reach the internet and nothing local
        "killswitch": True,    # tunnel down -> guests get nothing, never a leak
        "mss_clamp": True,     # the tunnel's MTU is not the guest's MTU
        "wifi": {
            "enabled": False,
            "iface": "",                # "" -> the first wireless device
            "ssid": "TUIray",
            "password": "",             # WPA2-PSK, 8 to 63 characters
            "band": "bg",               # "bg" 2.4 GHz | "a" 5 GHz
            "channel": 0,               # 0 -> NetworkManager decides
            "hidden": False,
            "address": "10.20.0.1/24",
            "dhcp": "dnsmasq",          # "dnsmasq" (ours) | "nm" | "off"
            "range": ["10.20.0.50", "10.20.0.200"],
            "lease": "12h",
            "dns": ["1.1.1.1", "8.8.8.8"],   # upstream, resolved through the tunnel
            "static": [],               # "aa:bb:cc:dd:ee:ff,10.20.0.10,name"
        },
        "lan": {
            "enabled": False,
            "iface": "",                # "" -> the first wired device
            "address": "10.21.0.1/24",
            "dhcp": "dnsmasq",
            "range": ["10.21.0.50", "10.21.0.200"],
            "lease": "12h",
            "dns": ["1.1.1.1", "8.8.8.8"],
            "static": [],
        },
    },
    # Config sources the user added: {"type": "file"|"links", "name": ..., and
    # "path" or "links"}. Subscriptions are not here -- they live in the
    # submodule's profiles.json, which the command-line converter shares.
    "sources": [],
    "subs": {
        # Subscription profiles, handled by the Happ-converter submodule.
        "profiles_file": "",    # "" -> tuiray/Happ-converter/profiles.json
        "timeout": 30.0,
        "hwid": "",             # X-Hwid for new profiles; made on first use
    },
    "health": {
        # Watchdog: poll the tunnel, and switch to another config when it dies.
        "enabled": False,
        "interval": 30.0,       # seconds between two checks
        "timeout": 6.0,         # per-request timeout
        "proxy_url": "http://cp.cloudflare.com/generate_204",     # fetched through xray
        "internet_url": "http://cp.cloudflare.com/generate_204",  # fetched directly
        "check_internet": True,   # also probe the bare uplink
        "failures": 2,          # consecutive failed checks before acting
        "autoswitch": True,     # dead proxy -> find a working config and restart
        "require_internet": True,   # never switch while the uplink itself is down
        "cooldown": 60.0,       # seconds to wait before switching again
        "candidates": 0,        # configs tried per switch; 0 -> all of them
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

        # Files that older versions put next to the tuiray folder now live in
        # it; follow them once they are there.
        for key, name in (("xray_bin", "xray"), ("geoip_path", "geoip.dat"),
                          ("geosite_path", "geosite.dat")):
            value = data.get(key)
            if value and Path(value).parent == REPO.parent and (REPO / name).is_file():
                data[key] = str(REPO / name)

        # First run (or a moved repo): fill in whatever we can find locally.
        if not data["xray_bin"] or not Path(data["xray_bin"]).exists():
            data["xray_bin"] = _default_xray()
        # Older versions fell back to the repo's parent -- usually $HOME -- and
        # then crawled it for *.json on every repaint. Never export there.
        stored_dir = data["configs_dir"]
        if (not stored_dir or Path(stored_dir).expanduser() in (HOME, REPO, REPO.parent, Path("/"))
                or not Path(stored_dir).expanduser().is_dir()):
            data["configs_dir"] = str(EXPORT_DIR)
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
        mkdir_owned(CONFIG_DIR)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
        chown_back(SETTINGS_FILE)

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
        dest = mkdir_owned(CACHE_DIR / "assets")
        for name, key in (("geoip.dat", "geoip_path"), ("geosite.dat", "geosite_path")):
            src = self.get(key) or ""
            link = dest / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                if src and Path(src).exists():
                    link.symlink_to(Path(src).resolve())
                    chown_back(link)
            except OSError:
                pass
        return dest
