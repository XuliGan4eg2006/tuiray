"""Fetch the xray binary from the XTLS/Xray-core releases.

Every Xray-core release is published as one zip per platform, next to a `.dgst`
file holding its checksums, and the zip carries `xray`, `geoip.dat` and
`geosite.dat` -- everything tuiray needs. So: ask the API for the newest
release, pick the asset this machine can actually run, check the SHA-256 the
release itself published, and unpack it.

Xray tags every build a prerelease, so "the latest stable" would find nothing;
the newest entry of any kind is what gets installed.

Stdlib only, like the rest of the tool -- urllib honours `https_proxy` from the
environment, which is the one way a box with no xray yet can still reach GitHub
through someone else's tunnel.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from settings import CACHE_DIR, REPO, chown_back, mkdir_owned

RELEASES_API = "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=10"
USER_AGENT = "tuiray (+https://github.com/XTLS/Xray-core)"

# uname machine -> the architecture part of the asset name
ARCH = {
    "x86_64": "64", "amd64": "64", "x64": "64",
    "i386": "32", "i486": "32", "i586": "32", "i686": "32", "x86": "32",
    "aarch64": "arm64-v8a", "arm64": "arm64-v8a", "armv8l": "arm64-v8a",
    "armv7l": "arm32-v7a", "armv7": "arm32-v7a", "armhf": "arm32-v7a", "arm": "arm32-v7a",
    "armv6l": "arm32-v6",
    "armv5tel": "arm32-v5", "armv5l": "arm32-v5",
    "riscv64": "riscv64",
    "loongarch64": "loong64",
    "ppc64": "ppc64", "ppc64le": "ppc64le",
    "s390x": "s390x",
}

SYSTEMS = {"linux": "linux", "darwin": "macos", "freebsd": "freebsd",
           "openbsd": "openbsd", "windows": "windows"}


class Cancelled(Exception):
    """The user pressed Esc while something was in flight."""


def machine_arch(machine: str = None) -> str:
    """Asset architecture for a uname machine string; "" when unknown."""
    machine = (machine or platform.machine() or "").lower()
    if machine.startswith("mips"):
        # The name alone does not say which way round the bytes go: a
        # little-endian build on a big-endian box is a segfault, not a bug
        # report, so ask the interpreter instead of trusting "mipsel".
        bits = "64" if "64" in machine else "32"
        return f"mips{bits}" + ("le" if sys.byteorder == "little" else "")
    return ARCH.get(machine, "")


def system_name() -> str:
    """Asset OS name for this machine; "" when unknown."""
    if hasattr(sys, "getandroidapilevel") or os.environ.get("ANDROID_ROOT"):
        return "android"
    return SYSTEMS.get(platform.system().lower(), "")


def asset_name() -> str:
    """The release asset this machine runs, e.g. "Xray-linux-arm64-v8a.zip"."""
    system, arch = system_name(), machine_arch()
    if not system or not arch:
        raise RuntimeError(f"unsupported platform: {platform.system()} "
                           f"{platform.machine()}")
    if system == "macos" and arch not in ("64", "arm64-v8a"):
        raise RuntimeError(f"no macOS build for {platform.machine()}")
    if system == "android" and arch not in ("64", "arm64-v8a"):
        raise RuntimeError(f"no Android build for {platform.machine()}")
    if system == "android" and arch == "64":
        arch = "amd64"                      # the one asset that breaks the pattern
    return f"Xray-{system}-{arch}.zip"


def platform_label() -> str:
    """Human summary of what was detected, for the download screen."""
    try:
        return f"{platform.system()} {platform.machine()} → {asset_name()}"
    except RuntimeError as exc:
        return str(exc)


def binary_name() -> str:
    return "xray.exe" if system_name() == "windows" else "xray"


# -- network --------------------------------------------------------------

def _open(url: str, timeout: float = 30.0):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and "rate limit" in (exc.reason or "").lower():
            raise RuntimeError("GitHub is rate-limiting this address; "
                               "try again in a few minutes") from exc
        raise RuntimeError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"cannot reach GitHub: {reason}") from exc


def latest_release(timeout: float = 30.0) -> dict:
    """The newest release or prerelease: {tag, published, prerelease, assets}."""
    with _open(RELEASES_API, timeout) as response:
        try:
            releases = json.loads(response.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"unreadable answer from GitHub: {exc}") from exc
    published = [r for r in releases if not r.get("draft")]
    if not published:
        raise RuntimeError("no published release found")
    newest = max(published, key=lambda r: r.get("published_at") or "")
    return {
        "tag": newest.get("tag_name", "?"),
        "published": (newest.get("published_at") or "")[:10],
        "prerelease": bool(newest.get("prerelease")),
        "assets": {a["name"]: (a["browser_download_url"], a.get("size", 0))
                   for a in newest.get("assets", [])},
    }


def download(url: str, dest: Path, expected_size: int = 0,
             progress=None, cancel=None, timeout: float = 30.0) -> Path:
    """Stream `url` to `dest`, reporting (done, total) as it goes."""
    with _open(url, timeout) as response:
        total = int(response.headers.get("Content-Length") or expected_size or 0)
        done = 0
        with open(dest, "wb") as sink:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                sink.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    return dest


def expected_sha256(dgst_url: str, timeout: float = 30.0) -> str:
    """The SHA2-256 line of a release's .dgst file, or "" when absent."""
    try:
        with _open(dgst_url, timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except RuntimeError:
        return ""
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name.strip().upper() in ("SHA2-256", "SHA256"):
            return value.strip().lower()
    return ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# -- installing -----------------------------------------------------------

def install_dir() -> Path:
    """Where the binary goes: next to the repo if we may write there.

    The repo is where settings.py already looks for xray and the .dat files on
    a first run, so a download that lands there needs no settings at all if the
    config is ever thrown away.
    """
    if os.access(REPO, os.W_OK):
        return REPO
    return mkdir_owned(CACHE_DIR / "bin")


def unpack(archive: Path, target: Path, with_assets: bool = True) -> dict:
    """Extract the binary (and optionally the .dat files); {name: path}."""
    wanted = {binary_name()}
    if with_assets:
        wanted |= {"geoip.dat", "geosite.dat"}
    installed = {}
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = Path(member.filename).name
            if name not in wanted or member.is_dir():
                continue
            destination = target / name
            # Write beside the target and rename: replacing a running binary
            # in place is what gives "Text file busy".
            staged = destination.with_suffix(destination.suffix + ".new")
            with bundle.open(member) as source, open(staged, "wb") as sink:
                shutil.copyfileobj(source, sink)
            if name == binary_name():
                staged.chmod(0o755)
            os.replace(staged, destination)
            chown_back(destination)
            installed[name] = destination
    if binary_name() not in installed:
        raise RuntimeError(f"{archive.name} contains no {binary_name()}")
    return installed


_VERSION_CACHE = {}          # (path, mtime, size) -> version string


def installed_version(binary: str) -> str:
    """"Xray 26.9.9" as the binary reports it, or "" when it will not run.

    Cached on the file's identity: the settings screen asks on every repaint,
    and running the binary for that would be a subprocess per keystroke.
    """
    if not binary or not Path(binary).is_file():
        return ""
    try:
        stat = Path(binary).stat()
    except OSError:
        return ""
    key = (binary, stat.st_mtime, stat.st_size)
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    _VERSION_CACHE[key] = version = _run_version(binary)
    return version


def _run_version(binary: str) -> str:
    try:
        done = subprocess.run([binary, "version"], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (done.stdout or "").strip().splitlines()
    return first[0].split("(")[0].strip() if first else ""


def fetch(settings, with_assets: bool = True, progress=None, cancel=None) -> dict:
    """Whole job: newest release -> verified zip -> installed files.

    `progress(phase, done, total)` is called as it runs; `cancel` is checked
    between chunks. Returns {tag, target, installed, verified}.
    """
    def step(phase, done=0, total=0):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress:
            progress(phase, done, total)

    step("asking GitHub for the newest release")
    release = latest_release()
    name = asset_name()
    if name not in release["assets"]:
        raise RuntimeError(f"{release['tag']} has no {name}")
    url, size = release["assets"][name]

    target = install_dir()
    staging = mkdir_owned(CACHE_DIR / "download")
    archive = staging / name
    step(f"downloading {name}", 0, size)
    try:
        download(url, archive, size,
                 progress=lambda done, total: step(f"downloading {name}", done, total),
                 cancel=cancel)

        step("checking the published SHA-256")
        wanted = expected_sha256(url + ".dgst")
        verified = bool(wanted) and sha256(archive) == wanted
        if wanted and not verified:
            raise RuntimeError("checksum mismatch — the download was corrupted "
                               "or tampered with; nothing was installed")

        step(f"unpacking into {target}")
        installed = unpack(archive, target, with_assets)
    finally:
        try:
            archive.unlink()
        except OSError:
            pass

    return {"tag": release["tag"], "published": release["published"],
            "target": target, "installed": installed, "verified": verified}
