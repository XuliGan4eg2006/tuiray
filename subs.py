"""Subscription profiles, through the Happ-converter submodule.

The heavy lifting is not reimplemented here: `export_configs.export_for_profile`
writes the per-entry Xray configs, `parser.parse_response` turns them back into
share links, `profiles.load_profiles` / `save_profiles` own profiles.json, and
`happ_decrypt.decrypt` opens `happ://crypt` links. This module is the adapter --
it puts the submodule on the path, keeps the file paths pointing where tuiray
wants them, and does the HTTP fetch with urllib so the TUI itself stays free of
third-party packages.

The submodule's own extras (requests, cryptography) are only needed for the
parts that use them; everything else keeps working when they are missing.
"""

import base64
import gzip
import json
import sys
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path

import links
from settings import chown_back, mkdir_owned

HERE = Path(__file__).resolve().parent
SUBMODULE = HERE / "Happ-converter"

DEFAULT_HEADERS = {
    "User-Agent": "Happ/3.13.0",
    "X-Device-Os": "Android",
    "X-Device-Locale": "ru",
    "X-Device-Model": "ELP-NX1",
    "X-Ver-Os": "15",
    "Accept-Encoding": "gzip",
    "Connection": "close",
}

MISSING = ("the Happ-converter submodule is not checked out — run\n"
           "    git submodule update --init\n"
           "in the tuiray directory")


class Unavailable(RuntimeError):
    """The submodule, or something it needs, is not there."""


def installed() -> bool:
    return (SUBMODULE / "export_configs.py").is_file()


def module(name: str):
    """Import `name` from the submodule; Unavailable when that cannot work."""
    if not installed():
        raise Unavailable(MISSING)
    # Appended, not prepended: the submodule has modules called parser.py and
    # server.py, and nothing of ours should ever lose a name to them.
    if str(SUBMODULE) not in sys.path:
        sys.path.append(str(SUBMODULE))
    try:
        loaded = __import__(name)
    except ImportError as exc:
        raise Unavailable(f"{name}.py needs {exc.name}, which is not installed "
                          f"(pip install -r {SUBMODULE / 'requirements.txt'})") from exc
    except Exception as exc:
        # The submodule annotates with `dict | None`, which older interpreters
        # evaluate at import time and reject. Nothing here is worth a crash.
        raise Unavailable(f"{name}.py will not load on this Python: {exc}") from exc
    if not str(Path(getattr(loaded, "__file__", "")).resolve()).startswith(str(SUBMODULE)):
        raise Unavailable(f"{name} resolved to {loaded.__file__}, not the submodule")
    return loaded


def status() -> str:
    """One line for the UI: what is available right now."""
    if not installed():
        return "submodule not checked out"
    try:
        module("export_configs")
    except Unavailable as exc:
        return str(exc).splitlines()[0]
    return "ready"


# -- profiles.json --------------------------------------------------------

def profiles_file(settings) -> Path:
    """Where profiles live: the setting, else the submodule's own file."""
    configured = (settings.get("subs.profiles_file") or "").strip()
    return Path(configured).expanduser() if configured else SUBMODULE / "profiles.json"


def _profiles_module(settings):
    """The submodule's profiles.py, pointed at our file -- or None.

    It reaches for `requests` through ipdetect at import time; when that is not
    installed the file is still plain JSON, so we read it ourselves rather than
    refuse to show anything.
    """
    try:
        loaded = module("profiles")
    except Unavailable:
        return None
    loaded.PROFILES_FILE = str(profiles_file(settings))
    return loaded


def load_profiles(settings) -> list:
    helper = _profiles_module(settings)
    if helper is not None:
        return helper.load_profiles()
    try:
        data = json.loads(profiles_file(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("sub_url")] \
        if isinstance(data, list) else []


def save_profiles(settings, profiles: list) -> None:
    helper = _profiles_module(settings)
    if helper is not None:
        helper.save_profiles(profiles)
        return
    path = profiles_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")


_COUNT_CACHE = {}          # (path, mtime) -> how many profiles it holds


def profile_count(settings) -> int:
    """How many profiles are saved; cached, the main menu asks every repaint."""
    path = profiles_file(settings)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return 0
    if key not in _COUNT_CACHE:
        _COUNT_CACHE.clear()
        _COUNT_CACHE[key] = len(load_profiles(settings))
    return _COUNT_CACHE[key]


def hwid(settings) -> str:
    """This installation's device id, sent as `X-Hwid`.

    Happ ties a subscription to the device that asked for it and generates 16
    hex characters for the purpose. One is kept per installation rather than
    per profile: it is one machine, and a fresh id on every fetch would look
    like a new device each time.
    """
    value = (settings.get("subs.hwid") or "").strip()
    if not value:
        value = uuid.uuid4().hex[:16]
        settings.set("subs.hwid", value)
    return value


def default_headers(settings) -> dict:
    """The headers a new profile starts with, X-Hwid included."""
    helper = _profiles_module(settings)
    headers = dict(getattr(helper, "DEFAULT_HEADERS", None) or DEFAULT_HEADERS)
    headers.setdefault("X-Hwid", hwid(settings))
    return headers


# -- fetching -------------------------------------------------------------

def decrypt(url: str) -> str:
    """Resolve a happ://crypt link to the real subscription URL."""
    return module("happ_decrypt").decrypt(url)


def _decompress(raw: bytes, encoding: str = None) -> bytes:
    """Undo Content-Encoding, which urllib -- unlike requests -- leaves on.

    The Happ profiles ask for gzip in their headers, so this is not an edge
    case: without it the body is binary and nothing downstream recognises it.
    The magic bytes are checked too, for servers that compress without saying.
    """
    encoding = (encoding or "").lower()
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except (OSError, zlib.error):
            return raw
    if encoding == "deflate":
        for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS):
            try:
                return zlib.decompress(raw, wbits)
            except zlib.error:
                continue
    return raw


def fetch(url: str, headers: dict = None, timeout: float = 30.0) -> str:
    """GET the subscription and return its body, base64-decoded when it is.

    urllib rather than the submodule's requests: this is the one piece that has
    to run on a machine where nothing was installed yet.
    """
    if url.startswith("happ://"):
        url = decrypt(url)
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = _decompress(response.read(), response.headers.get("Content-Encoding"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason} — check the URL, the "
                           f"headers, or the HWID/IP they are tied to") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"cannot fetch: {getattr(exc, 'reason', exc)}") from exc
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except Exception:
        return raw.decode("utf-8", "replace")


# -- writing configs ------------------------------------------------------

def safe_name(name: str) -> str:
    try:
        return module("export_configs").safe_name(name)
    except Unavailable:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_") or "profile"


def profile_dir(settings, name: str) -> Path:
    return Path(settings.get("configs_dir")) / safe_name(name)


# -- the sources list -----------------------------------------------------

def load_sources(settings) -> list:
    """File and link sources, in the order they were added."""
    stored = settings.get("sources") or []
    return [dict(item) for item in stored if isinstance(item, dict) and item.get("name")]


def save_sources(settings, sources: list) -> None:
    settings.set("sources", [dict(item) for item in sources])


def source_dir(settings, source: dict) -> Path:
    """Where this source's configs are: its own path, or the exported folder."""
    if source.get("type") in ("file", "folder"):
        return Path(source.get("path") or "")
    return profile_dir(settings, source.get("name") or "")


def configs_in(directory) -> list:
    """The configs of one source: the file itself, or the *.json directly in
    the folder. Deliberately not recursive -- a folder added by mistake (say,
    $HOME) must cost one directory listing, not a crawl of everything below.
    """
    if not directory:
        return []
    directory = Path(directory)
    if directory.is_file():
        return [directory]
    if not directory.is_dir():
        return []
    try:
        return sorted(p for p in directory.glob("*.json") if p.is_file())
    except OSError:
        return []


def count_configs(directory: Path) -> int:
    return len(configs_in(directory))


def source_groups(settings) -> dict:
    """{source name: [config paths]} for every subscription and added source.

    This is the whole universe of configs the ping tool and the watchdog look
    at; nothing outside what the user added is ever read.
    """
    groups = {}
    for profile in load_profiles(settings):
        paths = configs_in(profile_dir(settings, profile.get("name") or ""))
        if paths:
            groups.setdefault(profile.get("name") or "unnamed", []).extend(paths)
    for source in load_sources(settings):
        paths = configs_in(source_dir(settings, source))
        if paths:
            groups.setdefault(source["name"], []).extend(paths)
    return groups


def all_configs(settings) -> list:
    """Every config of every source, each path once."""
    seen, out = set(), []
    for paths in source_groups(settings).values():
        for path in paths:
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def refresh_links(settings, source: dict) -> dict:
    """Rewrite a link source's configs from the links it stores."""
    return write_links(settings, source.get("name") or "links", source.get("links") or "")


def _clear(directory: Path) -> None:
    """Drop the previous run's configs so removed servers do not linger."""
    if directory.is_dir():
        for stale in directory.glob("*.json"):
            stale.unlink()


def _hand_back(directory: Path) -> None:
    """Give exported configs to the user who started tuiray.

    TUN mode and sharing run the whole TUI under sudo, and configs written as
    root cannot be replaced by the next unprivileged update -- the import would
    fail on a directory the user owns everywhere else.
    """
    if not directory.is_dir():
        return
    chown_back(directory)
    for written in directory.glob("*.json"):
        chown_back(written)


def write_links(settings, name: str, text: str) -> dict:
    """Write one config per share link found in `text`."""
    parsed, failures = links.parse_all(text)
    if not parsed:
        raise RuntimeError("no usable links found"
                           + (f" ({failures[0][1]})" if failures else ""))
    export = None
    try:
        export = module("export_configs")
    except Unavailable:
        pass

    directory = mkdir_owned(profile_dir(settings, name))
    _clear(directory)
    needs_happ = 0
    for index, (title, config, scheme) in enumerate(parsed):
        slug = (export.slugify(title, index) if export
                else f"{index:02d}_{safe_name(title)}")
        (directory / f"{slug}.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        if scheme in links.NEEDS_HAPP_CORE:
            needs_happ += 1
    _hand_back(directory)
    return {"dir": directory, "count": len(parsed), "failed": failures,
            "hysteria": needs_happ, "kind": "links"}


def write_subscription(settings, name: str, text: str) -> dict:
    """Write the configs of a Happ subscription body (a JSON array of configs)."""
    export = module("export_configs")
    mkdir_owned(Path(settings.get("configs_dir")))
    directory, count = export.export_for_profile(
        text, name, base_dir=str(settings.get("configs_dir")))
    _hand_back(Path(directory))
    return {"dir": Path(directory), "count": count, "failed": [],
            "hysteria": 0, "kind": "subscription"}


def import_text(settings, name: str, text: str) -> dict:
    """Write configs from whatever the subscription turned out to be.

    Happ hands out a JSON array of whole Xray configs; everyone else hands out
    share links. Both are supported, and which one arrived is only visible
    after it has been fetched.
    """
    stripped = (text or "").strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        return write_subscription(settings, name, stripped)
    return write_links(settings, name, stripped)


def update_profile(settings, profile: dict) -> dict:
    """Fetch one saved profile and write its configs."""
    name = profile.get("name") or "profile"
    text = fetch(profile["sub_url"],
                 profile.get("headers") or default_headers(settings),
                 float(settings.get("subs.timeout", 30.0)))
    result = import_text(settings, name, text)
    result["name"] = name
    return result


def import_url(settings, name: str, url: str) -> dict:
    """Fetch a plain subscription URL -- no profile, no headers."""
    text = fetch(url, {"User-Agent": "tuiray/1.0"},
                 float(settings.get("subs.timeout", 30.0)))
    result = import_text(settings, name, text)
    result["name"] = name
    return result


def share_links(settings, profile_name: str) -> list:
    """The share links of an already-exported subscription, via parser.py."""
    return share_links_from(settings, profile_dir(settings, profile_name))


def share_links_from(settings, directory) -> list:
    """The share links of every config under `directory` (or of one file)."""
    parse_response = module("parser").parse_response
    found = []
    for path in configs_in(directory):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entry["remarks"] = path.stem
        try:
            found.extend(parse_response(json.dumps([entry])))
        except Exception:
            continue
    return found
