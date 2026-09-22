"""Start/stop the xray process and follow its log.

Two design points worth knowing:

  * xray's output goes to a file (~/.cache/tuiray/xray.log) which the UI tails,
    not to a pipe. The process therefore survives quitting the TUI, and a later
    run re-attaches to it through the pid file.
  * TUN mode needs CAP_NET_ADMIN to create the device and touch the routing
    table, so the binary is run directly when it carries file capabilities and
    through sudo otherwise.
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import xrayconf
from settings import CACHE_DIR, CONFIG_HOME, chown_back, mkdir_owned

RUN_CONFIG = CACHE_DIR / "run.json"
LOG_FILE = CACHE_DIR / "xray.log"
PID_FILE = CACHE_DIR / "xray.pid"


class NeedsPassword(Exception):
    """TUN mode requires root and sudo has no cached credentials."""


class LogBuffer:
    """Bounded, thread-safe line buffer shared between the tail thread and UI."""

    def __init__(self, maxlen: int = 3000):
        self._lines = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.revision = 0

    def add(self, line: str) -> None:
        with self._lock:
            self._lines.append(line.rstrip("\n"))
            self.revision += 1

    def note(self, line: str) -> None:
        self.add(f"[tuiray] {line}")

    def snapshot(self) -> list:
        with self._lock:
            return list(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self.revision += 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)


class TrafficStats:
    """Traffic counters read from xray's StatsService.

    `xray api statsquery` is spawned once a second rather than a gRPC client
    being written here: the binary we already depend on speaks the protocol, and
    the TUI has no third-party dependencies on purpose. Counters are cumulative
    since xray started, so the rates are deltas between two polls.
    """

    INTERVAL = 1.0
    SMOOTHING = 0.6      # weight of the newest sample in the displayed rate

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._thread = None
        self._stop = None
        self._port = 0
        self._previous = None        # (monotonic, uplink, downlink)
        self._state = self._empty()

    @staticmethod
    def _empty() -> dict:
        return {"ok": False, "uplink": 0, "downlink": 0,
                "up_rate": 0.0, "down_rate": 0.0}

    # -- lifecycle --------------------------------------------------------
    def start(self, port: int) -> None:
        self.stop()
        if not port:
            return
        self._port = int(port)
        self._previous = None
        with self._lock:
            self._state = self._empty()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(self._stop,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop:
            self._stop.set()
        self._stop = None
        self._thread = None
        self._previous = None
        with self._lock:
            self._state = self._empty()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    # -- polling ----------------------------------------------------------
    def _loop(self, stop_event) -> None:
        while not stop_event.is_set():
            counters = self._query()
            if counters is None:
                with self._lock:
                    self._state["ok"] = False
                    self._state["up_rate"] = 0.0
                    self._state["down_rate"] = 0.0
            else:
                self._absorb(*counters)
            stop_event.wait(self.INTERVAL)

    def _query(self):
        """(uplink, downlink) totals across all inbounds, or None when the API is down."""
        binary = self.settings.get("xray_bin", "")
        if not binary:
            return None
        try:
            done = subprocess.run(
                [binary, "api", "statsquery", f"--server=127.0.0.1:{self._port}",
                 "-t", "2", "-pattern", "inbound>>>"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        try:
            stats = json.loads(done.stdout).get("stat") or []
        except (json.JSONDecodeError, AttributeError):
            return None

        uplink = downlink = 0
        for entry in stats:
            name = entry.get("name") or ""
            # A zero counter comes back without its "value" field at all.
            value = int(entry.get("value") or 0)
            if not name.startswith("inbound>>>"):
                continue
            if name.endswith(">>>uplink"):
                uplink += value
            elif name.endswith(">>>downlink"):
                downlink += value
        return uplink, downlink

    def _absorb(self, uplink: int, downlink: int) -> None:
        now = time.monotonic()
        up_rate = down_rate = 0.0
        if self._previous:
            before, last_up, last_down = self._previous
            elapsed = now - before
            # A restarted xray zeroes its counters; treat the drop as no traffic
            # rather than as a negative rate.
            if elapsed > 0 and uplink >= last_up and downlink >= last_down:
                up_rate = (uplink - last_up) / elapsed
                down_rate = (downlink - last_down) / elapsed
        self._previous = (now, uplink, downlink)

        with self._lock:
            previous_ok = self._state["ok"]
            weight = self.SMOOTHING if previous_ok else 1.0
            self._state = {
                "ok": True,
                "uplink": uplink,
                "downlink": downlink,
                "up_rate": weight * up_rate + (1 - weight) * self._state["up_rate"],
                "down_rate": weight * down_rate + (1 - weight) * self._state["down_rate"],
            }


# -- system helpers -------------------------------------------------------

_IFACE_CACHE = {}          # exclusion key -> (interface, read at)


def detect_default_interface(exclude=(), max_age: float = 10.0) -> str:
    """Physical interface xray's own traffic should leave by.

    Deliberately not `ip route get`: once the tunnel is up that answers "tun0",
    and handing xray its own device as the bypass interface makes it route
    itself in a loop. The default routes are read instead -- lowest metric
    first -- skipping the tunnel device and any interface handed to guests,
    which has no way out once it is an access point.

    Cached per exclusion set: the main screen asks on every repaint.
    """
    key = tuple(sorted(exclude))
    cached = _IFACE_CACHE.get(key)
    if cached and time.time() - cached[1] < max_age:
        return cached[0]
    routes = []
    try:
        out = subprocess.run(["ip", "-4", "route", "show", "default"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            device = re.search(r"\bdev\s+(\S+)", line)
            if not device or device.group(1) in exclude:
                continue
            metric = re.search(r"\bmetric\s+(\d+)", line)
            routes.append((int(metric.group(1)) if metric else 0, device.group(1)))
    except (OSError, subprocess.SubprocessError):
        pass
    if not routes:
        return ""
    value = min(routes)[1]
    _IFACE_CACHE[key] = (value, time.time())
    return value


def shared_interfaces(settings) -> set:
    """Interfaces tuiray hands to guests, named in the settings.

    Read from settings rather than from share.py, which imports this module.
    ShareManager writes the interface it actually picked back into settings, so
    an "auto" choice is visible here too.
    """
    found = set()
    for kind in ("wifi", "lan"):
        config = settings.get(f"share.{kind}", {}) or {}
        name = (config.get("iface") or "").strip()
        if config.get("enabled") and name:
            found.add(name)
    return found


def binary_has_net_caps(path: str) -> bool:
    """True when the xray binary can create a TUN device without root."""
    if not path:
        return False
    try:
        out = subprocess.run(["getcap", path], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "cap_net_admin" in out.lower()


# One credential for the whole TUI: xray in TUN mode and the share manager both
# need root, and asking twice in one session for the same password is just rude.
# In memory only -- it is never written anywhere.
_CREDENTIAL = {"password": None}


def remember_password(password: str) -> None:
    if password:
        _CREDENTIAL["password"] = password


def cached_password():
    return _CREDENTIAL["password"]


def sudo_ready() -> bool:
    """Does sudo have a cached (or passwordless) credential right now?"""
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def sudo_run(command: list, password: str = None, timeout: int = 30) -> tuple:
    """Run `command` as root; (ok, message). Tries the cached credential first."""
    if os.geteuid() == 0:
        attempts = [(command, None)]
    else:
        attempts = [(["sudo", "-n"] + command, None)]
        if password:
            attempts.append((["sudo", "-S", "-p", ""] + command, password + "\n"))
    last = "no sudo credentials"
    for argv, stdin in attempts:
        try:
            done = subprocess.run(argv, input=stdin, capture_output=True,
                                  text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            last = str(exc)
            continue
        if done.returncode == 0:
            return True, done.stdout.strip()
        last = (done.stderr or done.stdout or f"exit {done.returncode}").strip()
    return False, last


def grant_net_caps(path: str, password: str = None) -> tuple:
    """setcap cap_net_admin,cap_net_raw+ep <xray> -- makes TUN work rootless."""
    return sudo_run(["setcap", "cap_net_admin,cap_net_raw+ep", path], password)


def pid_alive(pid: int) -> bool:
    return bool(pid) and Path(f"/proc/{pid}").exists()


def _descendants(pid: int) -> list:
    """PIDs below `pid` (Linux /proc), so a sudo child can be signalled."""
    found, stack = [], [pid]
    while stack:
        current = stack.pop()
        for task in Path(f"/proc/{current}/task").glob("*/children"):
            try:
                kids = [int(x) for x in task.read_text().split()]
            except (OSError, ValueError):
                continue
            found.extend(kids)
            stack.extend(kids)
    return found


class XrayRunner:
    def __init__(self, settings, log: LogBuffer):
        self.settings = settings
        self.log = log
        self.process = None          # None when the instance was adopted
        self.meta = {}               # pid / mode / config / sudo / started_at
        self._password = None        # in memory only, to signal a sudo'd child
        self._tail_stop = None
        self._tail_thread = None
        self.stats = TrafficStats(settings)
        # The watchdog restarts xray from its own thread while the UI may be
        # doing the same; one lifecycle at a time.
        self.lifecycle = threading.RLock()

    # -- state ------------------------------------------------------------
    @property
    def pid(self):
        return self.meta.get("pid")

    def is_running(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        return pid_alive(self.pid) and self._looks_like_xray(self.pid)

    def _looks_like_xray(self, pid: int) -> bool:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            return False
        return "xray" in cmdline or "sudo" in cmdline

    def uptime(self) -> str:
        started = self.meta.get("started_at")
        if not self.is_running() or not started:
            return "--:--:--"
        seconds = int(time.time() - started)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    def running_mode(self) -> str:
        return self.meta.get("mode", "") if self.is_running() else ""

    def running_config(self) -> str:
        return self.meta.get("config", "") if self.is_running() else ""

    def needs_root(self) -> bool:
        return (self.settings.mode == "tun"
                and os.geteuid() != 0
                and not binary_has_net_caps(self.settings.get("xray_bin", "")))

    # -- adoption ---------------------------------------------------------
    def adopt(self) -> bool:
        """Re-attach to an instance left running by an earlier TUI session."""
        try:
            meta = json.loads(PID_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        pid = meta.get("pid")
        if not pid_alive(pid) or not self._looks_like_xray(pid):
            PID_FILE.unlink(missing_ok=True)
            return False
        self.meta = meta
        self.log.note(f"attached to running xray (pid {pid}, mode {meta.get('mode')})")
        self._start_tail(from_start=True)
        self.stats.start(meta.get("api_port") or 0)
        return True

    # -- lifecycle --------------------------------------------------------
    def preflight(self) -> list:
        """Problems that would stop a start, as human-readable strings."""
        problems = []
        binary = self.settings.get("xray_bin", "")
        if not binary or not Path(binary).is_file():
            problems.append("xray binary not found - set it in Settings")
        elif not os.access(binary, os.X_OK):
            problems.append(f"{binary} is not executable")

        config = self.settings.config_path
        if not config:
            problems.append("no config selected")
        elif not Path(config).is_file():
            problems.append(f"config not found: {config}")
        else:
            try:
                parsed = xrayconf.load(config)
            except (OSError, ValueError) as exc:
                problems.append(f"cannot read config: {exc}")
                return problems
            missing = []
            for asset in xrayconf.needs_assets(parsed):
                key = "geoip_path" if asset == "geoip.dat" else "geosite_path"
                path = self.settings.get(key)
                if not path or not Path(path).exists():
                    missing.append(asset)
            if missing:
                problems.append("config needs " + ", ".join(sorted(missing))
                                + " - set it in the main menu")
        return problems

    def effective_tun(self) -> dict:
        """TUN block with "auto" resolved to the real default interface."""
        tun = dict(self.settings.get("tun", {}))
        if (tun.get("outbound_interface") or "auto").strip() == "auto":
            exclude = {tun.get("name") or "tun0"} | shared_interfaces(self.settings)
            tun["outbound_interface"] = detect_default_interface(exclude)
        return tun

    def build(self) -> Path:
        """Materialise the config xray will actually run."""
        config = xrayconf.load(self.settings.config_path)
        tun = self.effective_tun() if self.settings.mode == "tun" else None
        run = xrayconf.build_run_config(config, self.settings, tun_override=tun)
        path = xrayconf.write(run, RUN_CONFIG)
        chown_back(path)
        return path

    def start(self, password: str = None) -> None:
        with self.lifecycle:
            self._start(password)

    def _start(self, password: str = None) -> None:
        if self.is_running():
            return
        password = password or cached_password()
        problems = self.preflight()
        if problems:
            raise RuntimeError("; ".join(problems))

        run_config = self.build()
        stats_port = (int(self.settings.get("stats.api_port", 10815))
                      if self.settings.get("stats.enabled", True) else 0)
        assets = self.settings.asset_dir()
        binary = self.settings.get("xray_bin")
        mode = self.settings.mode

        base = [binary, "run", "-c", str(run_config)]
        stdin_data = None
        used_sudo = False

        if self.needs_root():
            if password:
                cmd = ["sudo", "-S", "-p", "", "env", f"XRAY_LOCATION_ASSET={assets}"] + base
                stdin_data = password + "\n"
                self._password = password
                remember_password(password)
            elif sudo_ready():
                cmd = ["sudo", "-n", "env", f"XRAY_LOCATION_ASSET={assets}"] + base
            else:
                raise NeedsPassword("TUN mode needs root privileges")
            used_sudo = True
        else:
            cmd = base

        mkdir_owned(CACHE_DIR)
        LOG_FILE.write_text("", encoding="utf-8")       # each run starts clean
        chown_back(LOG_FILE)
        self.log.clear()
        self.log.note(f"$ {' '.join(shlex.quote(part) for part in cmd)}")
        self.log.note(f"mode={mode}  config={self.settings.config_path}")
        if mode == "tun":
            tun = self.effective_tun()
            self.log.note(f"tun dev={tun.get('name')} mtu={tun.get('mtu')} "
                          f"routes={','.join(tun.get('routes') or [])} "
                          f"bypass-iface={tun.get('outbound_interface') or '(none)'}")

        env = dict(os.environ, XRAY_LOCATION_ASSET=str(assets))
        try:
            sink = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
            self.process = subprocess.Popen(
                cmd, env=env, stdin=subprocess.PIPE, stdout=sink,
                stderr=subprocess.STDOUT, text=True, start_new_session=True)
            sink.close()
        except OSError as exc:
            self.process = None
            raise RuntimeError(f"failed to launch xray: {exc}") from exc

        if stdin_data:
            try:
                self.process.stdin.write(stdin_data)
                self.process.stdin.flush()
            except OSError:
                pass
        try:
            self.process.stdin.close()
        except OSError:
            pass

        self.meta = {
            "pid": self.process.pid,
            "mode": mode,
            "config": self.settings.config_path,
            "sudo": used_sudo,
            "started_at": time.time(),
            "run_config": str(run_config),
            "api_port": stats_port,
            # Remembered so sharing can tell when the interface xray leaves by
            # is one it is about to hand to guests.
            "tun_interface": (self.effective_tun().get("outbound_interface") or ""
                              if mode == "tun" else ""),
        }
        self._write_pidfile()
        self._start_tail(from_start=True)
        self.stats.start(stats_port)

        if mode == "proxy" and self.settings.get("proxy.set_system_proxy"):
            ok, message = set_system_proxy(
                self.settings.get("proxy.listen", "127.0.0.1"),
                int(self.settings.get("proxy.socks_port", 10808)),
                int(self.settings.get("proxy.http_port", 10809)))
            self.log.note("system proxy: " + ("set" if ok else f"failed ({message})"))

    def stop(self, timeout: float = 6.0, password: str = None) -> bool:
        """Stop the instance. False means it is still running (sudo refused)."""
        with self.lifecycle:
            return self._stop(timeout, password)

    def _stop(self, timeout: float = 6.0, password: str = None) -> bool:
        if not self.is_running():
            self._cleanup_after_stop()
            return True

        if password:
            self._password = password
            remember_password(password)
        pid = self.pid
        if self.meta.get("sudo"):
            self._sudo_stop(pid, "TERM")
        else:
            self._signal_group(pid, signal.SIGTERM)

        deadline = time.time() + timeout
        while self.is_running() and time.time() < deadline:
            time.sleep(0.15)

        if self.is_running():
            self.log.note("xray did not stop, sending SIGKILL")
            if self.meta.get("sudo"):
                self._sudo_stop(pid, "KILL")
            else:
                self._signal_group(pid, signal.SIGKILL)
            deadline = time.time() + 3
            while self.is_running() and time.time() < deadline:
                time.sleep(0.1)

        if self.is_running():
            # Almost always a root-owned instance whose sudo credential expired.
            self.log.note("could not stop xray (need root privileges?)")
            return False

        if self.process is not None:
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

        was_proxy = self.meta.get("mode") == "proxy"
        self._cleanup_after_stop()
        self.log.note("stopped")

        if was_proxy and self.settings.get("proxy.set_system_proxy"):
            ok, message = clear_system_proxy()
            self.log.note("system proxy: " + ("restored" if ok else f"restore failed ({message})"))
        return True

    def _cleanup_after_stop(self) -> None:
        self.stats.stop()
        self.process = None
        self.meta = {}
        PID_FILE.unlink(missing_ok=True)

    def _signal_group(self, pid: int, sig) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    def _sudo_stop(self, pid: int, signal_name: str) -> None:
        """Signal the root-owned xray running under our sudo child."""
        password = self._password or cached_password()
        targets = [str(child) for child in _descendants(pid)] + [str(pid)]
        if sudo_run(["kill", f"-{signal_name}"] + targets, password, timeout=15)[0]:
            return
        run_config = self.meta.get("run_config", str(RUN_CONFIG))
        sudo_run(["pkill", f"-{signal_name}", "-f", f"run -c {run_config}"],
                 password, timeout=15)

    def _write_pidfile(self) -> None:
        try:
            PID_FILE.write_text(json.dumps(self.meta), encoding="utf-8")
            chown_back(PID_FILE)
        except OSError:
            pass

    # -- log tail ---------------------------------------------------------
    def _start_tail(self, from_start: bool = True) -> None:
        self._stop_tail()
        self._tail_stop = threading.Event()
        self._tail_thread = threading.Thread(
            target=self._tail, args=(self._tail_stop, from_start), daemon=True)
        self._tail_thread.start()

    def _stop_tail(self) -> None:
        if self._tail_stop:
            self._tail_stop.set()
        self._tail_thread = None

    def _tail(self, stop_event, from_start: bool) -> None:
        position = 0
        pending = ""
        if not from_start:
            try:
                position = LOG_FILE.stat().st_size
            except OSError:
                position = 0
        while not stop_event.is_set():
            try:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    if size < position:          # truncated by a fresh start
                        position, pending = 0, ""
                    handle.seek(position)
                    chunk = handle.read()
                    position = handle.tell()
            except OSError:
                chunk = ""
            if chunk:
                pending += chunk
                *lines, pending = pending.split("\n")
                for line in lines:
                    self.log.add(line)
            stop_event.wait(0.25)


# -- system proxy ---------------------------------------------------------
#
# The tactic is the one Throne (and Qv2ray before it) uses: tell every desktop
# proxy store on the machine at once, because no single one of them is "the"
# system proxy on Linux.
#
#   * GNOME, Cinnamon, MATE, Budgie, XFCE-with-gsettings and every GTK app that
#     reads GProxyResolver -> the org.gnome.system.proxy gsettings tree.
#   * KDE/Plasma (and Trinity) -> [Proxy Settings] in ~/.config/kioslaverc,
#     written with kwriteconfig, followed by a D-Bus signal so the running
#     KIO workers re-read it instead of waiting for a logout.
#
# What tuiray adds on top is symmetry: the previous state of both stores is
# remembered, so stopping puts the machine back where it was instead of
# blanket-setting "no proxy" the way Throne does.

PREV_PROXY_STATE = CACHE_DIR / "sysproxy.prev"
KIOSLAVERC = CONFIG_HOME / "kioslaverc"
KDE_GROUP = "Proxy Settings"


def _desktop_prefix() -> list:
    """Argv prefix that runs a desktop command as the user, not as root.

    Started through sudo there is no session bus for root, so gsettings would
    fail outright or write into a throwaway dconf profile that no running
    desktop ever reads. Hand it back to the invoking user with their own bus.
    """
    if os.geteuid() != 0:
        return []
    user, uid = os.environ.get("SUDO_USER"), os.environ.get("SUDO_UID")
    if not user or not uid:
        return []
    return ["sudo", "-u", user, "env",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
            f"XDG_RUNTIME_DIR=/run/user/{uid}"]


def _desktop_run(program: str, args: list, timeout: float = 5.0) -> tuple:
    """Run a desktop helper as the invoking user; (ok, output-or-error)."""
    if not shutil.which(program):
        return False, f"{program} not found"
    try:
        done = subprocess.run(_desktop_prefix() + [program] + args,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout or f"{program} failed").strip()
    return True, done.stdout.strip()


# -- GNOME (gsettings) ----------------------------------------------------

def _gsettings(args: list) -> tuple:
    return _desktop_run("gsettings", args)


def _gnome_apply(host: str, socks_port: int, http_port: int) -> tuple:
    steps = [["set", "org.gnome.system.proxy", "mode", "manual"],
             ["set", "org.gnome.system.proxy.socks", "host", host],
             ["set", "org.gnome.system.proxy.socks", "port", str(socks_port)]]
    # http, https and ftp all go to the HTTP inbound, as Throne does.
    for protocol in ("http", "https", "ftp"):
        steps.append(["set", f"org.gnome.system.proxy.{protocol}", "host", host])
        steps.append(["set", f"org.gnome.system.proxy.{protocol}", "port", str(http_port)])
    for step in steps:
        ok, message = _gsettings(step)
        if not ok:
            return False, message
    return True, ""


def _gnome_mode() -> str:
    ok, value = _gsettings(["get", "org.gnome.system.proxy", "mode"])
    return value.strip().strip("'") if ok else ""


# -- KDE (kioslaverc) -----------------------------------------------------

def _kde_tool(kind: str) -> str:
    """kwriteconfig/kreadconfig binary for this Plasma generation, or ""."""
    version = os.environ.get("KDE_SESSION_VERSION", "")
    order = [f"{kind}{version}"] if version in ("5", "6") else []
    order += [f"{kind}6", f"{kind}5", kind]
    for name in order:
        if shutil.which(name):
            return name
    return ""


def is_kde() -> bool:
    """Is there a KDE proxy store worth writing to?

    The desktop name decides it when we have one. Under sudo the variable is
    usually stripped, so fall back to "are the KDE tools even installed" --
    which is false on a machine that has no KDE to configure.
    """
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if desktops:
        return any(name in desktops for name in ("kde", "plasma", "trinity"))
    return bool(_kde_tool("kwriteconfig"))


def _kde_write(key: str, value: str) -> tuple:
    tool = _kde_tool("kwriteconfig")
    if not tool:
        return False, "kwriteconfig not found"
    ok, message = _desktop_run(tool, ["--file", str(KIOSLAVERC), "--group", KDE_GROUP,
                                      "--key", key, value])
    if ok:
        chown_back(KIOSLAVERC)
    return ok, message


def _kde_read(key: str) -> str:
    tool = _kde_tool("kreadconfig")
    if not tool:
        return ""
    ok, value = _desktop_run(tool, ["--file", str(KIOSLAVERC), "--group", KDE_GROUP,
                                    "--key", key])
    return value.strip() if ok else ""


def _kde_reload() -> None:
    """Tell the running KIO workers to re-read kioslaverc."""
    _desktop_run("dbus-send", ["--type=signal", "/KIO/Scheduler",
                               "org.kde.KIO.Scheduler.reparseSlaveConfiguration",
                               "string:''"])


def _kde_apply(host: str, socks_port: int, http_port: int) -> tuple:
    entries = [("socksProxy", f"socks://{host} {socks_port}")]
    for protocol in ("http", "https", "ftp"):
        entries.append((f"{protocol}Proxy", f"http://{host} {http_port}"))
    entries.append(("ProxyType", "1"))          # 1 = manual
    for key, value in entries:
        ok, message = _kde_write(key, value)
        if not ok:
            return False, message
    _kde_reload()
    return True, ""


# -- both -----------------------------------------------------------------

def _remember(state: dict) -> None:
    try:
        mkdir_owned(CACHE_DIR)
        PREV_PROXY_STATE.write_text(json.dumps(state), encoding="utf-8")
        chown_back(PREV_PROXY_STATE)
    except OSError:
        pass


def _remembered() -> dict:
    try:
        text = PREV_PROXY_STATE.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    try:
        state = json.loads(text)
    except json.JSONDecodeError:
        # Pre-KDE tuiray wrote the bare gsettings mode into this file.
        return {"gnome": text} if text in ("none", "auto", "manual") else {}
    return state if isinstance(state, dict) else {}


def set_system_proxy(host: str, socks_port: int, http_port: int) -> tuple:
    """Point every desktop proxy store at our inbounds; (ok, what was set).

    Failing on one desktop is not a failure: a GNOME box has no kwriteconfig
    and a Plasma box may have no gsettings schema, and neither is a reason to
    leave the other one unset.
    """
    # A proxy listening on every address is still dialled by loopback.
    if host in ("0.0.0.0", "::", "*", ""):
        host = "127.0.0.1"
    # Remember what the machine had, so stopping restores it instead of
    # blindly switching everything to "no proxy".
    kde = is_kde()
    _remember({"gnome": _gnome_mode(),
               "kde": _kde_read("ProxyType") if kde else ""})

    applied, problems = [], []
    ok, message = _gnome_apply(host, socks_port, http_port)
    if ok:
        applied.append("GNOME")
    else:
        problems.append(f"GNOME: {message}")
    if kde:
        ok, message = _kde_apply(host, socks_port, http_port)
        if ok:
            applied.append("KDE")
        else:
            problems.append(f"KDE: {message}")
    if applied:
        return True, ", ".join(applied)
    return False, "; ".join(problems)


def clear_system_proxy() -> tuple:
    """Put the desktop proxy stores back where they were; (ok, what was reset)."""
    previous = _remembered()

    # Restoring "manual" would leave our dead proxy configured; only the modes
    # that route around it are safe to hand back.
    gnome = previous.get("gnome") or "none"
    if gnome not in ("none", "auto"):
        gnome = "none"
    kde = previous.get("kde") or "0"
    if kde not in ("0", "2", "3"):              # 0 none, 2 PAC, 3 WPAD; 1 is ours
        kde = "0"

    applied, problems = [], []
    ok, message = _gsettings(["set", "org.gnome.system.proxy", "mode", gnome])
    if ok:
        applied.append("GNOME")
    else:
        problems.append(f"GNOME: {message}")
    if is_kde():
        ok, message = _kde_write("ProxyType", kde)
        if ok:
            _kde_reload()
            applied.append("KDE")
        else:
            problems.append(f"KDE: {message}")
    if applied:
        return True, ", ".join(applied)
    return False, "; ".join(problems)


_PROXY_MODE_CACHE = {"value": "?", "at": 0.0}

KDE_PROXY_TYPE = {"0": "none", "1": "manual", "2": "auto", "3": "auto", "4": "system"}


def system_proxy_mode(max_age: float = 3.0) -> str:
    """What the desktop says right now; cached, the main screen polls it."""
    if time.time() - _PROXY_MODE_CACHE["at"] < max_age:
        return _PROXY_MODE_CACHE["value"]
    value = _gnome_mode()
    if not value and is_kde():
        value = KDE_PROXY_TYPE.get(_kde_read("ProxyType"), "")
    _PROXY_MODE_CACHE.update(value=value or "?", at=time.time())
    return _PROXY_MODE_CACHE["value"]
