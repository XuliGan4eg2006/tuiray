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
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import xrayconf
from settings import CACHE_DIR

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


# -- system helpers -------------------------------------------------------

_IFACE_CACHE = {"value": "", "at": 0.0}


def detect_default_interface(max_age: float = 10.0) -> str:
    """Physical interface of the default route -- what TUN mode must bypass.

    Cached: the main screen asks for this on every repaint.
    """
    if _IFACE_CACHE["value"] and time.time() - _IFACE_CACHE["at"] < max_age:
        return _IFACE_CACHE["value"]
    try:
        out = subprocess.run(["ip", "route", "get", "1.1.1.1"],
                             capture_output=True, text=True, timeout=3).stdout
        match = re.search(r"\bdev\s+(\S+)", out)
        if match:
            _IFACE_CACHE.update(value=match.group(1), at=time.time())
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def binary_has_net_caps(path: str) -> bool:
    """True when the xray binary can create a TUN device without root."""
    if not path:
        return False
    try:
        out = subprocess.run(["getcap", path], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "cap_net_admin" in out.lower()


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
            tun["outbound_interface"] = detect_default_interface()
        return tun

    def build(self) -> Path:
        """Materialise the config xray will actually run."""
        config = xrayconf.load(self.settings.config_path)
        tun = self.effective_tun() if self.settings.mode == "tun" else None
        run = xrayconf.build_run_config(config, self.settings, tun_override=tun)
        return xrayconf.write(run, RUN_CONFIG)

    def start(self, password: str = None) -> None:
        if self.is_running():
            return
        problems = self.preflight()
        if problems:
            raise RuntimeError("; ".join(problems))

        run_config = self.build()
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
            elif sudo_ready():
                cmd = ["sudo", "-n", "env", f"XRAY_LOCATION_ASSET={assets}"] + base
            else:
                raise NeedsPassword("TUN mode needs root privileges")
            used_sudo = True
        else:
            cmd = base

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text("", encoding="utf-8")       # each run starts clean
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
        }
        self._write_pidfile()
        self._start_tail(from_start=True)

        if mode == "proxy" and self.settings.get("proxy.set_system_proxy"):
            ok, message = set_system_proxy(
                self.settings.get("proxy.listen", "127.0.0.1"),
                int(self.settings.get("proxy.socks_port", 10808)),
                int(self.settings.get("proxy.http_port", 10809)))
            self.log.note("system proxy: " + ("set" if ok else f"failed ({message})"))

    def stop(self, timeout: float = 6.0, password: str = None) -> bool:
        """Stop the instance. False means it is still running (sudo refused)."""
        if not self.is_running():
            self._cleanup_after_stop()
            return True

        if password:
            self._password = password
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
        targets = [str(child) for child in _descendants(pid)] + [str(pid)]
        if sudo_run(["kill", f"-{signal_name}"] + targets, self._password, timeout=15)[0]:
            return
        run_config = self.meta.get("run_config", str(RUN_CONFIG))
        sudo_run(["pkill", f"-{signal_name}", "-f", f"run -c {run_config}"],
                 self._password, timeout=15)

    def _write_pidfile(self) -> None:
        try:
            PID_FILE.write_text(json.dumps(self.meta), encoding="utf-8")
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


# -- GNOME system proxy ---------------------------------------------------

PREV_PROXY_MODE = CACHE_DIR / "sysproxy.prev"


def _gsettings(args: list) -> tuple:
    try:
        done = subprocess.run(["gsettings"] + args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stderr or "gsettings failed").strip()
    return True, done.stdout.strip()


def set_system_proxy(host: str, socks_port: int, http_port: int) -> tuple:
    # Remember what the desktop had, so stopping restores it instead of
    # blindly switching the machine to "no proxy".
    ok, previous = _gsettings(["get", "org.gnome.system.proxy", "mode"])
    if ok:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            PREV_PROXY_MODE.write_text(previous.strip().strip("'"), encoding="utf-8")
        except OSError:
            pass

    steps = [
        ["set", "org.gnome.system.proxy", "mode", "manual"],
        ["set", "org.gnome.system.proxy.socks", "host", host],
        ["set", "org.gnome.system.proxy.socks", "port", str(socks_port)],
        ["set", "org.gnome.system.proxy.http", "host", host],
        ["set", "org.gnome.system.proxy.http", "port", str(http_port)],
        ["set", "org.gnome.system.proxy.https", "host", host],
        ["set", "org.gnome.system.proxy.https", "port", str(http_port)],
    ]
    for step in steps:
        ok, message = _gsettings(step)
        if not ok:
            return False, message
    return True, ""


def clear_system_proxy() -> tuple:
    previous = "none"
    try:
        stored = PREV_PROXY_MODE.read_text(encoding="utf-8").strip()
        if stored in ("none", "auto", "manual"):
            previous = stored
    except OSError:
        pass
    # Restoring "manual" would leave our dead proxy configured; only auto/none
    # are safe to hand back.
    if previous == "manual":
        previous = "none"
    return _gsettings(["set", "org.gnome.system.proxy", "mode", previous])


_PROXY_MODE_CACHE = {"value": "?", "at": 0.0}


def system_proxy_mode(max_age: float = 3.0) -> str:
    """Current desktop proxy mode; cached, it is polled by the main screen."""
    if time.time() - _PROXY_MODE_CACHE["at"] < max_age:
        return _PROXY_MODE_CACHE["value"]
    ok, value = _gsettings(["get", "org.gnome.system.proxy", "mode"])
    _PROXY_MODE_CACHE.update(value=value.strip("'") if ok else "?", at=time.time())
    return _PROXY_MODE_CACHE["value"]
