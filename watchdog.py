"""Connection watchdog: keep an eye on the tunnel, swap a dead config out.

Two independent probes run on a timer:

  * proxy   -- a request through the running xray. In proxy mode (and in TUN
    mode while the SOCKS inbound is kept) it goes through the local SOCKS
    inbound; in a TUN-only setup it is a plain request, which the tunnel
    captures anyway. This is the one that decides whether the config is alive.
  * internet -- the same request made directly, so a dead probe can be blamed
    on the right thing: no uplink at all is not a bad proxy, and switching
    configs on a train that went into a tunnel only burns through the list.
    Both URLs are settings (health.proxy_url / health.internet_url).

    In TUN mode "directly" is a white lie: the device captures everything, so
    unless the URL's host is in the direct-routing rules this probe travels
    through the tunnel too and says the same thing as the proxy probe.

After `health.failures` consecutive failures the watchdog probes the other
configs in the configs directory -- each in its own throwaway xray, exactly as
the ping tool does -- and restarts on the first one that answers.
"""

import socket
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pinger
import runner
import xrayconf

# state values, in the order the UI colours them
OFF = "off"
IDLE = "idle"
OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
NO_INTERNET = "no internet"
SWITCHING = "switching"
EXHAUSTED = "no working config"


def direct_request(url: str, timeout: float) -> float:
    """Time one request to `url` without a proxy, in ms. Raises on failure."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise OSError(f"bad URL: {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    started = time.perf_counter()
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        stream = sock
        if parts.scheme == "https":
            stream = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        stream.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                        "User-Agent: tuiray/1.0\r\nConnection: close\r\n\r\n").encode())
        if not stream.recv(1):
            raise OSError("no response")
        return (time.perf_counter() - started) * 1000.0
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _loopback(listen: str) -> str:
    """Address to dial an inbound that listens on `listen`."""
    if listen in ("0.0.0.0", "::", "*", ""):
        return "127.0.0.1"
    return listen


class Watchdog:
    """Background health checks, and the config switch they may trigger.

    One thread for the whole TUI; it idles on an event, so toggling the mode or
    asking for a check now takes effect immediately instead of at the next tick.
    """

    SETTLE = 6.0         # seconds to wait for a freshly started xray to answer

    def __init__(self, settings, xray, log):
        self.settings = settings
        self.xray = xray
        self.log = log
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._paused = 0
        self._live = set()           # probe processes, killed when we shut down
        self._good = {}              # config path -> when it last answered
        self._bad = {}               # config path -> when it last failed
        self._state = self._empty()

    @staticmethod
    def _empty() -> dict:
        return {
            "state": OFF,
            "proxy_ok": None, "proxy_ms": 0.0, "proxy_error": "",
            "internet_ok": None, "internet_ms": 0.0, "internet_error": "",
            "failures": 0, "checks": 0, "switches": 0,
            "last_check": 0.0, "last_switch": 0.0, "message": "",
        }

    # -- state ------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("health.enabled"))

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _update(self, **fields) -> None:
        with self._lock:
            self._state.update(fields)

    def summary(self) -> str:
        """One line for the main menu."""
        if not self.enabled:
            return "off"
        state = self.snapshot()
        parts = [{OK: "watching", IDLE: "idle (xray stopped)"}.get(
            state["state"], state["state"])]
        if state["proxy_ok"]:
            parts.append(f"proxy {state['proxy_ms']:.0f} ms")
        elif state["proxy_ok"] is False:
            parts.append("proxy down")
        if state["internet_ok"] is False:
            parts.append("no uplink")
        elif state["internet_ok"]:
            parts.append("uplink ok")
        if state["switches"]:
            parts.append(f"{state['switches']} switch"
                         f"{'es' if state['switches'] > 1 else ''}")
        return " · ".join(parts)

    # -- control ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._kill_probes()
        self._thread = None

    def set_enabled(self, value: bool) -> None:
        self.settings.set("health.enabled", bool(value))
        if not value:
            self._update(**self._empty())
        else:
            self._update(state=IDLE, message="")
        self.log.note(f"watchdog {'enabled' if value else 'disabled'}")
        self.check_now()

    def toggle(self) -> bool:
        self.set_enabled(not self.enabled)
        return self.enabled

    def check_now(self) -> None:
        self._wake.set()

    def pause(self) -> None:
        """Hold the checks off -- the ping tool wants the CPU and the ports."""
        with self._lock:
            self._paused += 1

    def resume(self) -> None:
        with self._lock:
            self._paused = max(0, self._paused - 1)
        self._wake.set()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused > 0

    # -- probes -----------------------------------------------------------
    def _socks_endpoint(self):
        """(host, port) of the live SOCKS inbound, or None when there is none."""
        mode = self.xray.running_mode() or self.settings.mode
        if mode == "tun" and not self.settings.get("tun.keep_socks", True):
            return None
        port = int(self.settings.get("proxy.socks_port", 10808) or 0)
        if not port:
            return None
        return _loopback(self.settings.get("proxy.listen", "127.0.0.1")), port

    def check_proxy(self) -> tuple:
        """(ok, ms, error) for a request through the running instance."""
        timeout = float(self.settings.get("health.timeout", 6.0))
        url = self.settings.get("health.proxy_url") or self.settings.get("ping.url")
        endpoint = self._socks_endpoint()
        try:
            if endpoint:
                ms = pinger.socks5_request(endpoint[1], url, timeout, proxy_host=endpoint[0])
            else:
                # TUN mode without a SOCKS inbound: the device captures this.
                ms = direct_request(url, timeout)
        except Exception as exc:
            return False, 0.0, str(exc).strip() or exc.__class__.__name__
        return True, ms, ""

    def check_internet(self) -> tuple:
        """(ok, ms, error) for a request that does not go through the proxy."""
        timeout = float(self.settings.get("health.timeout", 6.0))
        url = self.settings.get("health.internet_url") or self.settings.get("ping.url")
        try:
            ms = direct_request(url, timeout)
        except Exception as exc:
            return False, 0.0, str(exc).strip() or exc.__class__.__name__
        return True, ms, ""

    # -- loop -------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = max(5.0, float(self.settings.get("health.interval", 30.0)))
            if not self.enabled or self.paused:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                self._cycle()
            except Exception as exc:                # a watchdog must never die
                self.log.note(f"watchdog error: {exc}")
                self._update(message=str(exc)[:120])
            self._wake.wait(interval)
            self._wake.clear()

    def _cycle(self) -> None:
        if not self.xray.is_running():
            self._update(state=IDLE, proxy_ok=None, internet_ok=None,
                         failures=0, message="xray is not running")
            return

        proxy_ok, proxy_ms, proxy_error = self.check_proxy()
        internet_ok, internet_ms, internet_error = (None, 0.0, "")
        if self.settings.get("health.check_internet", True):
            internet_ok, internet_ms, internet_error = self.check_internet()

        state = self.snapshot()
        failures = 0 if proxy_ok else state["failures"] + 1
        self._update(proxy_ok=proxy_ok, proxy_ms=proxy_ms, proxy_error=proxy_error,
                     internet_ok=internet_ok, internet_ms=internet_ms,
                     internet_error=internet_error, failures=failures,
                     checks=state["checks"] + 1, last_check=time.time())

        if proxy_ok:
            current = self.settings.config_path
            if current:
                self._good[current] = time.time()
                self._bad.pop(current, None)
            self._update(state=OK, message="")
            return

        threshold = max(1, int(self.settings.get("health.failures", 2)))
        self.log.note(f"watchdog: proxy check failed ({proxy_error}) "
                      f"[{failures}/{threshold}]")
        if failures < threshold:
            self._update(state=DEGRADED, message=proxy_error[:120])
            return

        if (self.settings.get("health.require_internet", True)
                and self.settings.get("health.check_internet", True)
                and internet_ok is False):
            self._update(state=NO_INTERNET,
                         message="uplink is down, leaving the config alone")
            self.log.note("watchdog: no internet without the proxy either — "
                          "not switching")
            return

        if not self.settings.get("health.autoswitch", True):
            self._update(state=DOWN, message=proxy_error[:120])
            return

        cooldown = float(self.settings.get("health.cooldown", 60.0))
        since = time.time() - (state["last_switch"] or 0.0)
        if state["last_switch"] and since < cooldown:
            self._update(state=DOWN,
                         message=f"switched {since:.0f}s ago, waiting out the cooldown")
            return

        self._switch()

    # -- switching --------------------------------------------------------
    def _track(self, process) -> None:
        if self._stop.is_set():
            pinger._terminate(process)
            return
        with self._lock:
            self._live.add(process)

    def _kill_probes(self) -> None:
        with self._lock:
            processes, self._live = list(self._live), set()
        for process in processes:
            pinger._terminate(process)

    def candidates(self) -> list:
        """Configs to try, best bet first: proven ones, then untried, then duds."""
        paths = xrayconf.list_configs(self.settings.get("configs_dir"))
        current = self.settings.config_path

        def rank(path):
            key = str(path)
            if key in self._good:
                return (0, -self._good[key])          # answered most recently
            if key in self._bad:
                return (2, self._bad[key])            # failed longest ago
            return (1, 0)                             # never tried

        return sorted((p for p in paths if str(p) != current), key=rank)

    def _can_restart(self) -> bool:
        """Will a start succeed without asking the user for a password?"""
        if not self.xray.needs_root():
            return True
        return bool(runner.cached_password()) or runner.sudo_ready()

    def _switch(self) -> None:
        original = self.settings.config_path
        candidates = self.candidates()
        if not candidates:
            self._update(state=EXHAUSTED, message="no other config in the configs directory")
            self.log.note("watchdog: nothing to switch to")
            return

        limit = int(self.settings.get("health.candidates", 0) or 0)
        if limit > 0:
            candidates = candidates[:limit]

        if not self._can_restart():
            self._update(state=DOWN,
                         message="TUN needs sudo; start xray once from the TUI first")
            self.log.note("watchdog: cannot restart xray unattended (sudo password "
                          "not cached) — not switching")
            return

        self._update(state=SWITCHING, message=f"trying {len(candidates)} config(s)")
        self.log.note(f"watchdog: proxy is dead, trying {len(candidates)} other config(s)")

        # In TUN mode the device swallows the probe's own traffic, so a
        # candidate would be measured through the tunnel that just died. Take
        # the tunnel down first; it goes back up with the winner (or the
        # original config, if none of them answers).
        tun_mode = (self.xray.running_mode() or self.settings.mode) == "tun"
        lowered = False
        if tun_mode and self.xray.is_running():
            self.log.note("watchdog: stopping xray so candidates are probed off the tunnel")
            if not self.xray.stop():
                self._update(state=DOWN, message="could not stop xray to probe candidates")
                return
            lowered = True

        timeout = float(self.settings.get("health.timeout", 6.0))
        url = self.settings.get("health.proxy_url") or self.settings.get("ping.url")
        try:
            for path in candidates:
                if self._stop.is_set() or not self.enabled:
                    break
                try:
                    ms, _ = pinger.probe_proxy(path, self.settings, timeout, 1,
                                               on_spawn=self._track, url=url)
                except Exception as exc:
                    self._bad[str(path)] = time.time()
                    self.log.note(f"watchdog: {path.name} failed "
                                  f"({str(exc).strip()[:80]})")
                    continue
                self.log.note(f"watchdog: {path.name} answered in {ms:.0f} ms — switching")
                if self._activate(path):
                    state = self.snapshot()
                    self._update(state=OK, failures=0, last_switch=time.time(),
                                 switches=state["switches"] + 1,
                                 message=f"switched to {path.name}")
                    return
                self._bad[str(path)] = time.time()
        finally:
            self._kill_probes()
            if self.settings.config_path != original:
                if self.snapshot()["state"] != OK:
                    self.settings.set("config_path", original)
            if lowered and not self.xray.is_running():
                self.log.note("watchdog: bringing the original config back up")
                self._start_xray()

        self._update(state=EXHAUSTED, failures=0,
                     message="no config in the directory answered")
        self.log.note("watchdog: none of the configs answered")

    def _start_xray(self) -> bool:
        try:
            self.xray.start()
        except runner.NeedsPassword:
            self.log.note("watchdog: xray needs a sudo password — start it from the TUI")
            return False
        except RuntimeError as exc:
            self.log.note(f"watchdog: cannot start xray ({exc})")
            return False
        return True

    def _activate(self, path: Path) -> bool:
        """Point xray at `path`, restart it and confirm the tunnel answers."""
        if self.xray.is_running() and not self.xray.stop():
            self.log.note("watchdog: could not stop the running xray")
            return False
        self.settings.set("config_path", str(path))
        if not self._start_xray():
            return False

        deadline = time.time() + self.SETTLE
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            if self.xray.is_running() and self.check_proxy()[0]:
                self._good[str(path)] = time.time()
                self._update(proxy_ok=True, proxy_error="")
                return True
            time.sleep(0.5)

        self.log.note(f"watchdog: {path.name} started but did not answer, moving on")
        return False
