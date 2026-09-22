"""Latency testing for a directory of xray configs.

Two probes:

  * "proxy" -- start xray on a throwaway SOCKS port with the config under test
    and time a real request through it. Slow but honest: it exercises the whole
    chain, including balancer configs where the server is picked at runtime.
  * "tcp"   -- plain TCP handshake against the config's own endpoints. Fast,
    needs no xray, but says nothing about whether the proxy itself works.
"""

import atexit
import os
import queue
import signal
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import xrayconf
from settings import CACHE_DIR, mkdir_owned

PING_DIR = CACHE_DIR / "ping"

# Every xray started for a probe is registered here, so a cancelled pass -- or
# an interpreter shutdown -- never leaves an orphan holding a port.
_LIVE_PROBES = set()
_LIVE_LOCK = threading.Lock()


def _register(process) -> None:
    with _LIVE_LOCK:
        _LIVE_PROBES.add(process)


def _unregister(process) -> None:
    with _LIVE_LOCK:
        _LIVE_PROBES.discard(process)


def kill_all_probes() -> None:
    with _LIVE_LOCK:
        processes = list(_LIVE_PROBES)
    for process in processes:
        _terminate(process)
        _unregister(process)


atexit.register(kill_all_probes)


@dataclass
class Result:
    path: Path
    name: str
    group: str
    ms: float = None
    error: str = ""
    state: str = "idle"          # idle | testing | ok | fail
    detail: str = ""

    @property
    def label(self) -> str:
        if self.state == "testing":
            return "testing…"
        if self.state == "ok":
            return f"{self.ms:6.0f} ms"
        if self.state == "fail":
            return "failed"
        return "-"

    @property
    def sort_key(self):
        return (0, self.ms) if self.state == "ok" else (1, float("inf"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _recv_exact(sock, count: int) -> bytes:
    buffer = b""
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise OSError("connection closed by proxy")
        buffer += chunk
    return buffer


def socks5_request(port: int, url: str, timeout: float, proxy_host: str = "127.0.0.1") -> float:
    """Time one request to `url` through a SOCKS5 proxy, in ms."""
    parts = urlsplit(url)
    host = parts.hostname or "cp.cloudflare.com"
    target_port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"

    started = time.perf_counter()
    sock = socket.create_connection((proxy_host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        greeting = _recv_exact(sock, 2)
        if greeting[0:1] != b"\x05" or greeting[1:2] != b"\x00":
            raise OSError("SOCKS5 handshake rejected")

        encoded = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded
                     + target_port.to_bytes(2, "big"))
        reply = _recv_exact(sock, 4)
        if reply[1] != 0:
            raise OSError(f"SOCKS5 CONNECT failed (code {reply[1]})")
        atyp = reply[3]
        if atyp == 1:
            _recv_exact(sock, 4 + 2)
        elif atyp == 3:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length + 2)
        elif atyp == 4:
            _recv_exact(sock, 16 + 2)

        stream = sock
        if parts.scheme == "https":
            context = ssl.create_default_context()
            stream = context.wrap_socket(sock, server_hostname=host)

        request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                   "User-Agent: tuiray/1.0\r\nConnection: close\r\n\r\n")
        stream.sendall(request.encode())
        if not stream.recv(1):
            raise OSError("no response through proxy")
        return (time.perf_counter() - started) * 1000.0
    finally:
        try:
            sock.close()
        except OSError:
            pass


def tcp_latency(host: str, port: int, timeout: float) -> float:
    started = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        return (time.perf_counter() - started) * 1000.0


def _wait_for_port(port: int, process, deadline: float) -> bool:
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def probe_proxy(path: Path, settings, timeout: float, attempts: int, on_spawn=None,
                url: str = None) -> tuple:
    """(ms, detail) through a private xray instance; raises on failure.

    `url` overrides the ping tool's test URL -- the watchdog probes candidate
    configs with its own.
    """
    config = xrayconf.load(path)
    port = free_port()
    mkdir_owned(PING_DIR)
    run_config = PING_DIR / f"probe-{port}.json"
    xrayconf.write(xrayconf.build_ping_config(config, port), run_config)

    env = dict(os.environ, XRAY_LOCATION_ASSET=str(settings.asset_dir()))
    log_file = run_config.with_suffix(".log")

    with open(log_file, "w", encoding="utf-8") as sink:
        process = subprocess.Popen(
            [settings.get("xray_bin"), "run", "-c", str(run_config)],
            env=env, stdout=sink, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    _register(process)
    if on_spawn:
        on_spawn(process)
    try:
        if not _wait_for_port(port, process, time.time() + min(timeout, 10.0)):
            tail = _tail(log_file)
            raise OSError(tail or "xray did not start")

        target = url or settings.get("ping.url")
        best, last_error = None, None
        for _ in range(max(1, attempts)):
            try:
                value = socks5_request(port, target, timeout)
                best = value if best is None else min(best, value)
            except (OSError, ssl.SSLError) as exc:
                last_error = exc
        if best is None:
            raise OSError(str(last_error) if last_error else "no reply")
        return best, ""
    finally:
        _terminate(process)
        _unregister(process)
        for leftover in (run_config, log_file):
            try:
                leftover.unlink()
            except OSError:
                pass


def _terminate(process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            process.kill()


def _tail(path: Path, lines: int = 2) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    interesting = [ln for ln in content if "Failed" in ln or "failed" in ln] or content
    return " | ".join(interesting[-lines:])[:160]


def probe_tcp(path: Path, timeout: float, attempts: int) -> tuple:
    config = xrayconf.load(path)
    endpoints = xrayconf.endpoints(config)
    if not endpoints:
        raise OSError("no proxy endpoint in config")

    best, best_endpoint, last_error = None, None, None
    for host, port in endpoints[:8]:
        for _ in range(max(1, attempts)):
            try:
                value = tcp_latency(host, port, timeout)
            except OSError as exc:
                last_error = exc
                continue
            if best is None or value < best:
                best, best_endpoint = value, f"{host}:{port}"
    if best is None:
        raise OSError(str(last_error) if last_error else "unreachable")
    return best, best_endpoint


class PingSession:
    """Runs probes over a list of configs with a bounded worker pool."""

    def __init__(self, settings, paths, mode: str = None):
        self.settings = settings
        self.mode = mode or settings.get("ping.mode", "proxy")
        self.results = [Result(path=Path(p), name=Path(p).stem, group=Path(p).parent.name)
                        for p in paths]
        self.revision = 0
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._queue = None
        self._threads = []
        self._live = set()

    # -- control ----------------------------------------------------------
    def start(self, targets=None) -> None:
        """Probe `targets` (default: everything), replacing any running pass."""
        self.cancel()
        self._cancel = threading.Event()
        self._queue = queue.Queue()
        for result in (targets if targets is not None else self.results):
            result.state = "idle"
            result.ms, result.error, result.detail = None, "", ""
            self._queue.put(result)
        self._bump()

        workers = max(1, int(self.settings.get("ping.concurrency", 4)))
        if self.mode == "tcp":
            workers = max(workers, 8)      # no xray process per probe, so go wider
        workers = min(workers, max(1, self._queue.qsize()))
        self._threads = [threading.Thread(target=self._work, daemon=True) for _ in range(workers)]
        for thread in self._threads:
            thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        # Kill the xray instances of probes still in flight; their worker
        # threads then unwind on the next socket error. Sweep repeatedly: a
        # worker can spawn one more probe between the flag and its next check.
        for _ in range(4):
            with self._lock:
                processes, self._live = list(self._live), set()
            for process in processes:
                _terminate(process)
                _unregister(process)
            alive = [thread for thread in self._threads if thread.is_alive()]
            if not alive:
                break
            for thread in alive:
                thread.join(timeout=0.4)
        self._threads = []
        with self._lock:
            for result in self.results:
                if result.state == "testing":
                    result.state, result.error = "fail", "cancelled"
        self._bump()

    @property
    def busy(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    @property
    def pending(self) -> int:
        with self._lock:
            return sum(1 for r in self.results if r.state in ("idle", "testing"))

    def sorted_results(self, by: str = "latency") -> list:
        with self._lock:
            items = list(self.results)
        if by == "latency":
            return sorted(items, key=lambda r: (r.sort_key, r.group, r.name))
        return sorted(items, key=lambda r: (r.group, r.name))

    def best(self):
        ok = [r for r in self.results if r.state == "ok"]
        return min(ok, key=lambda r: r.ms) if ok else None

    # -- worker -----------------------------------------------------------
    def _track(self, process) -> None:
        if self._cancel.is_set():          # spawned just after cancel -- undo it
            _terminate(process)
            _unregister(process)
            return
        with self._lock:
            self._live.add(process)

    def _bump(self) -> None:
        self.revision += 1

    def _work(self) -> None:
        timeout = float(self.settings.get("ping.timeout", 8.0))
        attempts = int(self.settings.get("ping.attempts", 2))
        while not self._cancel.is_set():
            try:
                result = self._queue.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                result.state = "testing"
            self._bump()
            try:
                if self.mode == "tcp":
                    value, detail = probe_tcp(result.path, timeout, attempts)
                else:
                    value, detail = probe_proxy(result.path, self.settings, timeout,
                                                attempts, on_spawn=self._track)
                with self._lock:
                    result.ms, result.detail, result.state, result.error = value, detail, "ok", ""
            except Exception as exc:            # a bad config must not kill the pass
                if self._cancel.is_set():
                    return
                message = str(exc).strip() or exc.__class__.__name__
                with self._lock:
                    result.state = "fail"
                    result.error = message[:120]
                    result.detail = result.error
            self._bump()
