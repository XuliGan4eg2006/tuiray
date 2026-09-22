"""Turn `protocol://` share links into standalone xray configs.

The Happ-converter submodule goes the other way -- subscription JSON to share
links -- so this is its mirror image, and deliberately speaks the same query
vocabulary (`type`, `security`, `sni`, `fp`, `pbk`, `sid`, `spx`, `flow`,
`path`, `host`, `mode`, `serviceName`, `extra`, ...) that `parser.py` emits.

What comes out is an outbounds-only config, exactly like the ones exported from
a subscription: tuiray's own inbounds and routing rules are layered on at start
time by xrayconf, so a link config and a subscription config run the same way.
"""

import base64
import binascii
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

# Protocols Xray-core can actually dial as an outbound, plus hysteria2, which
# only the Happ fork of the core understands -- it is parsed so a mixed
# subscription does not lose entries, and flagged so nobody wonders why the
# official binary rejects it.
SCHEMES = ("vless", "vmess", "trojan", "ss", "socks", "socks5", "hysteria2", "hy2")
NEEDS_HAPP_CORE = ("hysteria2", "hy2")

LINK_RE = re.compile(r"\b(" + "|".join(SCHEMES) + r")://\S+", re.IGNORECASE)
# Anything else that looks like a share link is kept too, so an unsupported
# protocol is reported as one rather than silently vanishing. Plain web URLs
# are not share links and are left out of that.
ANY_LINK_RE = re.compile(r"\b(?!https?://|ftp://)[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)


class LinkError(ValueError):
    """A link that cannot be turned into an outbound."""


def _b64(text: str) -> str:
    """Decode base64 that may be urlsafe and is usually missing its padding."""
    cleaned = re.sub(r"\s+", "", text)
    cleaned = cleaned.replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned).decode("utf-8", "replace")
    except (binascii.Error, ValueError) as exc:
        raise LinkError(f"bad base64: {exc}") from exc


def split(text: str) -> list:
    """Every link in `text`, whether it arrives as a list or a base64 blob.

    Subscription bodies come base64-encoded as a whole; pasted text arrives as
    lines, and sometimes as one line with the links run together.
    """
    text = (text or "").strip()
    if not text:
        return []
    if not LINK_RE.search(text):
        try:
            text = _b64(text)
        except LinkError:
            return []
    return [match.group(0).strip().rstrip(",;") for match in ANY_LINK_RE.finditer(text)]


def _name(parts, fallback: str) -> str:
    return unquote(parts.fragment).strip() or fallback


def _params(query: str) -> dict:
    """Query string as a flat dict; empty values are dropped, as Happ does."""
    return {key: values[0] for key, values in parse_qs(query, keep_blank_values=False).items()
            if values and values[0] != ""}


def _stream(params: dict, host_fallback: str = "") -> dict:
    """streamSettings for the transport and security named in the query."""
    network = params.get("type") or "tcp"
    if network == "raw":                       # xray 25+ name for plain tcp
        network = "tcp"
    stream = {"network": network}

    security = params.get("security") or "none"
    if security in ("tls", "xtls", "reality"):
        stream["security"] = security
        settings = {}
        server_name = params.get("sni") or params.get("host") or host_fallback
        if server_name:
            settings["serverName"] = server_name
        if params.get("fp"):
            settings["fingerprint"] = params["fp"]
        if params.get("alpn"):
            settings["alpn"] = [part for part in params["alpn"].split(",") if part]
        if params.get("allowInsecure") in ("1", "true") or params.get("insecure") in ("1", "true"):
            settings["allowInsecure"] = True
        if security == "reality":
            for key, field in (("pbk", "publicKey"), ("sid", "shortId"),
                               ("spx", "spiderX"), ("pqv", "mldsa65Verify")):
                if params.get(key):
                    settings[field] = params[key]
            stream["realitySettings"] = settings
        else:
            stream["tlsSettings"] = settings

    host = params.get("host") or ""
    path = params.get("path") or "/"

    if network == "ws":
        ws = {"path": path}
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": path, "host": host}
    elif network in ("xhttp", "splithttp"):
        xhttp = {"path": path}
        if params.get("mode"):
            xhttp["mode"] = params["mode"]
        if host:
            xhttp["host"] = host
        extra = params.get("extra")
        if extra:
            try:
                xhttp["extra"] = json.loads(extra)
            except json.JSONDecodeError:
                pass                            # a broken extra is better dropped
        stream["xhttpSettings"] = xhttp
    elif network == "grpc":
        grpc = {}
        if params.get("serviceName"):
            grpc["serviceName"] = params["serviceName"]
        if params.get("authority"):
            grpc["authority"] = params["authority"]
        if params.get("mode") == "multi":
            grpc["multiMode"] = True
        stream["grpcSettings"] = grpc
    elif network == "kcp":
        kcp = {"header": {"type": params.get("headerType") or "none"}}
        if params.get("seed"):
            kcp["seed"] = params["seed"]
        stream["kcpSettings"] = kcp
    elif network == "tcp" and params.get("headerType") == "http":
        request = {"headers": {"Host": [part for part in host.split(",") if part]}}
        if path:
            request["path"] = [path]
        stream["tcpSettings"] = {"header": {"type": "http", "request": request}}

    return stream


def _endpoint(parts) -> tuple:
    if not parts.hostname:
        raise LinkError("no server address")
    port = parts.port
    if not port:
        raise LinkError("no port")
    return parts.hostname, int(port)


# -- per protocol ---------------------------------------------------------

def _vless(link: str) -> tuple:
    parts = urlsplit(link)
    params = _params(parts.query)
    address, port = _endpoint(parts)
    if not parts.username:
        raise LinkError("no uuid")
    user = {"id": unquote(parts.username), "encryption": params.get("encryption") or "none"}
    if params.get("flow"):
        user["flow"] = params["flow"]
    outbound = {
        "tag": "proxy", "protocol": "vless",
        "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]},
        "streamSettings": _stream(params, address),
    }
    return outbound, _name(parts, f"{address}:{port}")


def _trojan(link: str) -> tuple:
    parts = urlsplit(link)
    params = _params(parts.query)
    address, port = _endpoint(parts)
    if not parts.username:
        raise LinkError("no password")
    server = {"address": address, "port": port, "password": unquote(parts.username)}
    if params.get("flow"):
        server["flow"] = params["flow"]
    outbound = {
        "tag": "proxy", "protocol": "trojan",
        "settings": {"servers": [server]},
        "streamSettings": _stream(params, address),
    }
    return outbound, _name(parts, f"{address}:{port}")


def _vmess(link: str) -> tuple:
    """The v2rayN format: vmess:// followed by base64 of a flat JSON object."""
    body = link.split("://", 1)[1]
    try:
        config = json.loads(_b64(body))
    except (json.JSONDecodeError, LinkError) as exc:
        raise LinkError(f"not a v2rayN vmess link ({exc})") from exc
    address = str(config.get("add") or "")
    if not address:
        raise LinkError("no server address")
    try:
        port = int(config.get("port") or 0)
    except (TypeError, ValueError):
        raise LinkError("bad port") from None
    if not port:
        raise LinkError("no port")

    # The JSON carries the same information as a query string, under shorter
    # names; translate it and reuse the one transport builder.
    params = {"type": config.get("net") or "tcp",
              "security": config.get("tls") or "none",
              "host": config.get("host") or "",
              "path": config.get("path") or "/",
              "sni": config.get("sni") or "",
              "alpn": config.get("alpn") or "",
              "fp": config.get("fp") or "",
              "headerType": config.get("type") or "none",
              "serviceName": config.get("path") if config.get("net") == "grpc" else "",
              "mode": config.get("mode") or ""}
    params = {key: value for key, value in params.items() if value}

    user = {"id": str(config.get("id") or ""), "security": config.get("scy") or "auto"}
    if not user["id"]:
        raise LinkError("no uuid")
    try:
        user["alterId"] = int(config.get("aid") or 0)
    except (TypeError, ValueError):
        user["alterId"] = 0
    outbound = {
        "tag": "proxy", "protocol": "vmess",
        "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]},
        "streamSettings": _stream(params, address),
    }
    return outbound, str(config.get("ps") or f"{address}:{port}")


def _shadowsocks(link: str) -> tuple:
    """Both the old all-base64 form and SIP002."""
    body = link.split("://", 1)[1]
    fragment = ""
    if "#" in body:
        body, fragment = body.split("#", 1)
    body = body.split("?", 1)[0]

    if "@" in body:                              # SIP002: base64(method:pass)@host:port
        userinfo, _, endpoint = body.rpartition("@")
        try:
            method, _, password = _b64(userinfo).partition(":")
        except LinkError:
            method, _, password = unquote(userinfo).partition(":")
    else:                                        # legacy: base64(method:pass@host:port)
        decoded = _b64(body)
        userinfo, _, endpoint = decoded.rpartition("@")
        method, _, password = userinfo.partition(":")

    host, _, port = endpoint.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit():
        raise LinkError("no server address")
    if not method or not password:
        raise LinkError("no method or password")

    outbound = {
        "tag": "proxy", "protocol": "shadowsocks",
        "settings": {"servers": [{"address": host, "port": int(port),
                                  "method": method, "password": password}]},
        "streamSettings": {"network": "tcp"},
    }
    return outbound, unquote(fragment).strip() or f"{host}:{port}"


def _socks(link: str) -> tuple:
    parts = urlsplit(link)
    address, port = _endpoint(parts)
    server = {"address": address, "port": port}
    if parts.username:
        server["users"] = [{"user": unquote(parts.username),
                            "pass": unquote(parts.password or "")}]
    outbound = {"tag": "proxy", "protocol": "socks",
                "settings": {"servers": [server]},
                "streamSettings": {"network": "tcp"}}
    return outbound, _name(parts, f"{address}:{port}")


def _hysteria2(link: str) -> tuple:
    """The shape Happ's subscriptions use -- upstream Xray-core has no hysteria."""
    parts = urlsplit(link)
    params = _params(parts.query)
    address, port = _endpoint(parts)
    hysteria = {"version": 2, "auth": unquote(parts.username or "")}
    if params.get("obfs"):
        hysteria["obfs"] = params["obfs"]
        if params.get("obfs-password"):
            hysteria["obfsParam"] = params["obfs-password"]
    tls = {"serverName": params.get("sni") or address, "alpn": ["h3"]}
    if params.get("insecure") in ("1", "true"):
        tls["allowInsecure"] = True
    outbound = {
        "tag": "proxy", "protocol": "hysteria",
        "settings": {"address": address, "port": port, "version": 2},
        "streamSettings": {"network": "hysteria", "hysteriaSettings": hysteria,
                           "security": "tls", "tlsSettings": tls},
    }
    return outbound, _name(parts, f"{address}:{port}")


PARSERS = {
    "vless": _vless, "trojan": _trojan, "vmess": _vmess,
    "ss": _shadowsocks, "socks": _socks, "socks5": _socks,
    "hysteria2": _hysteria2, "hy2": _hysteria2,
}


# -- public ---------------------------------------------------------------

def parse(link: str) -> tuple:
    """(config, name, scheme) for one link; raises LinkError."""
    link = link.strip()
    scheme = link.split("://", 1)[0].lower()
    if scheme not in PARSERS:
        raise LinkError(f"unsupported protocol: {scheme or link[:20]}")
    outbound, name = PARSERS[scheme](link)
    config = {"outbounds": [outbound,
                            {"tag": "direct", "protocol": "freedom"},
                            {"tag": "block", "protocol": "blackhole"}]}
    return config, name, scheme


def parse_all(text: str) -> tuple:
    """(configs, failures) for every link in `text`.

    configs is [(name, config, scheme)] in the order they appeared; failures is
    [(link, reason)] -- one bad link in a subscription is not a reason to
    refuse the other forty.
    """
    configs, failures = [], []
    for link in split(text):
        try:
            config, name, scheme = parse(link)
        except LinkError as exc:
            failures.append((link, str(exc)))
            continue
        except Exception as exc:                 # a malformed link is data, not a crash
            failures.append((link, f"{exc.__class__.__name__}: {exc}"))
            continue
        configs.append((name, config, scheme))
    return configs, failures
