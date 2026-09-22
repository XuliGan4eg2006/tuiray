"""Full-screen views: the ping tool and the settings editor."""

import curses
import json
import os
from pathlib import Path

import pinger
import runner
import share
import ui
import xrayconf


# -- ping tool ------------------------------------------------------------

MODE_LABEL = {
    "proxy": "proxy latency (starts xray per config)",
    "tcp": "tcp handshake to endpoint (fast)",
}

DHCP_LABEL = {
    "dnsmasq": "dnsmasq (tuiray)",
    "nm": "NetworkManager shared",
    "off": "nobody",
}


def ping_screen(stdscr, settings) -> str:
    """Measure every config in the configs dir. Returns a chosen path or None."""
    paths = xrayconf.list_configs(settings.get("configs_dir"))
    if not paths:
        ui.popup_message(stdscr, "Ping", "No .json configs found in "
                         f"{settings.get('configs_dir')}.\n\n"
                         "Point 'Configs directory' at your exported configs in Settings.",
                         ui.WARN)
        return None

    session = pinger.PingSession(settings, paths)
    session.start()
    sort_by = "latency"
    index, top = 0, 0
    selected = session.results[0].path if session.results else None
    stdscr.timeout(200)
    chosen = None

    while True:
        rows = session.sorted_results(sort_by)
        height, width = stdscr.getmaxyx()
        list_height = max(1, height - 5)
        # Rows reorder as results arrive; follow the highlighted config, not the
        # row number, so the cursor does not jump under the user.
        if selected is not None:
            for position, row in enumerate(rows):
                if row.path == selected:
                    index = position
                    break
        index = max(0, min(index, len(rows) - 1))
        if index < top:
            top = index
        if index >= top + list_height:
            top = index - list_height + 1
        top = max(0, min(top, max(0, len(rows) - list_height)))

        stdscr.erase()
        done = sum(1 for r in rows if r.state in ("ok", "fail"))
        title = (f" Ping configs — {done}/{len(rows)} tested · mode: "
                 f"{MODE_LABEL.get(session.mode, session.mode)} ")
        ui.fill_line(stdscr, 0, 0, width, ui.attr(ui.HEADER, True))
        ui.addstr(stdscr, 0, 1, ui.clip(title, width - 2), ui.attr(ui.HEADER, True))

        header = f"{'#':>3}  {'group':<10} {'config':<38} {'result':>10}  detail"
        ui.addstr(stdscr, 1, 1, ui.clip(header, width - 2), ui.attr(ui.DIM, True))

        for offset in range(list_height):
            position = top + offset
            if position >= len(rows):
                break
            row = rows[position]
            selected = position == index
            if selected:
                style = ui.attr(ui.SELECT)
            elif row.state == "ok":
                style = ui.attr(ui.OK)
            elif row.state == "fail":
                style = ui.attr(ui.ERROR)
            elif row.state == "testing":
                style = ui.attr(ui.WARN)
            else:
                style = ui.attr(ui.NORMAL)
            ui.fill_line(stdscr, 2 + offset, 0, width, style)
            line = (f"{position + 1:>3}  {ui.clip(row.group, 10):<10} "
                    f"{ui.clip(row.name, 38):<38} {row.label:>10}  {row.detail}")
            ui.addstr(stdscr, 2 + offset, 1, ui.clip(line, width - 2), style)

        best = session.best()
        status = (f"fastest: {best.group}/{best.name} ({best.ms:.0f} ms)"
                  if best else "no successful probe yet")
        if session.busy:
            status = f"testing… {session.pending} left · " + status
        ui.addstr(stdscr, height - 3, 1, ui.clip(status, width - 2), ui.attr(ui.DIM))
        hints = ("Enter use · f use fastest · p ping all · r retest one · "
                 "m mode · s sort · c cancel · Esc back")
        ui.fill_line(stdscr, height - 1, 0, width, ui.attr(ui.HEADER))
        ui.addstr(stdscr, height - 1, 1, ui.clip(hints, width - 2), ui.attr(ui.HEADER))
        stdscr.refresh()

        key = ui.getkey(stdscr)
        if key is None:
            continue
        moved = True
        if key in (curses.KEY_UP, "k"):
            index -= 1
        elif key in (curses.KEY_DOWN, "j"):
            index += 1
        elif key == curses.KEY_NPAGE:
            index += list_height
        elif key == curses.KEY_PPAGE:
            index -= list_height
        elif key == curses.KEY_HOME:
            index = 0
        elif key == curses.KEY_END:
            index = len(rows) - 1
        elif key == curses.KEY_RESIZE:
            continue
        else:
            moved = False
        if moved:
            index = max(0, min(index, len(rows) - 1))
            selected = rows[index].path if rows else None
            continue
        elif key in ("p", "P"):
            session.start()
        elif key in ("r", "R"):
            if rows:
                session.start([rows[index]])
        elif key in ("c", "C"):
            session.cancel()
        elif key in ("s", "S"):
            sort_by = "name" if sort_by == "latency" else "latency"
        elif key in ("m", "M"):
            choice = ui.popup_list(stdscr, "Ping mode",
                                   [(MODE_LABEL["proxy"], "accurate, one xray per config"),
                                    (MODE_LABEL["tcp"], "connect() to the server, no xray")],
                                   0 if session.mode == "proxy" else 1)
            if choice is not None:
                mode = "proxy" if choice == 0 else "tcp"
                settings.set("ping.mode", mode)
                session.cancel()
                session = pinger.PingSession(settings, paths, mode=mode)
                session.start()
        elif key in ("f", "F"):
            if best:
                chosen = str(best.path)
                break
            ui.popup_message(stdscr, "Ping", "No config answered yet.", ui.WARN)
        elif ui.is_enter(key):
            if rows:
                chosen = str(rows[index].path)
                break
        elif ui.is_escape(key) or key in ("q", "Q"):
            break

    session.cancel()
    stdscr.timeout(-1)
    return chosen


# -- settings -------------------------------------------------------------

def _as_list(text: str) -> list:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def asset_value(settings, key: str) -> str:
    """A .dat path with its size -- or a loud complaint when it is not there."""
    path = settings.get(key)
    if not path:
        return "(not set)"
    if not Path(path).exists():
        return f"{path}  — MISSING"
    return f"{path}  ({ui.human_size(path)})"


class _Editors:
    """The popup editors a row list stores as its action.

    Lifted out of the settings screen so the share screen can offer the same
    handful of fields inline, instead of sending people round to Settings for
    an SSID. Each method returns a zero-argument callable.
    """

    def __init__(self, stdscr, settings):
        self.stdscr = stdscr
        self.settings = settings

    def text(self, key, prompt):
        def edit():
            value = ui.popup_input(self.stdscr, "Edit", prompt,
                                   str(self.settings.get(key, "")))
            if value is not None:
                self.settings.set(key, value.strip())
        return edit

    def number(self, key, prompt, cast=int):
        def edit():
            value = ui.popup_input(self.stdscr, "Edit", prompt,
                                   str(self.settings.get(key, "")))
            if value is None:
                return
            try:
                self.settings.set(key, cast(value.strip()))
            except ValueError:
                ui.popup_message(self.stdscr, "Invalid value",
                                 f"{value!r} is not a number.", ui.ERROR)
        return edit

    def list(self, key, prompt):
        def edit():
            current = ", ".join(self.settings.get(key, []) or [])
            value = ui.popup_input(self.stdscr, "Edit",
                                   prompt + " (comma separated)", current)
            if value is not None:
                self.settings.set(key, _as_list(value))
        return edit

    def toggle(self, key):
        def edit():
            self.settings.set(key, not bool(self.settings.get(key)))
        return edit

    def choice(self, key, title, options):
        def edit():
            values = [option[0] for option in options]
            current = self.settings.get(key)
            index = values.index(current) if current in values else 0
            picked = ui.popup_list(self.stdscr, title,
                                   [(o[1], o[2]) for o in options], index)
            if picked is not None:
                self.settings.set(key, values[picked])
        return edit

    def file(self, key, title, extensions):
        def edit():
            start = self.settings.get(key) or str(Path.home())
            path = ui.file_browser(self.stdscr, start, title, extensions=extensions)
            if path:
                self.settings.set(key, path)
        return edit

    def dir(self, key, title):
        def edit():
            path = ui.file_browser(self.stdscr,
                                   self.settings.get(key) or str(Path.home()),
                                   title, extensions=None, pick_dir=True)
            if path:
                self.settings.set(key, path)
        return edit

    def iface(self, key, kind):
        def edit():
            candidates = [(name, state) for name, found, state in share.interfaces()
                          if found == kind]
            if not candidates:
                ui.popup_message(self.stdscr, "No interface",
                                 f"No {'wireless' if kind == 'wifi' else 'wired'} "
                                 "interface found on this machine.", ui.WARN)
                return
            names = [name for name, _ in candidates]
            options = [(f"{name}  ({state})",
                        "wireless" if kind == "wifi" else "wired")
                       for name, state in candidates]
            options.append(("(auto)", "whichever is found first"))
            current = self.settings.get(key, "")
            picked = ui.popup_list(self.stdscr, "Interface", options,
                                   names.index(current) if current in names
                                   else len(names))
            if picked is not None:
                self.settings.set(key, "" if picked == len(names) else names[picked])
        return edit

    def range(self, key, prompt):
        def edit():
            current = ", ".join(self.settings.get(key, []) or [])
            value = ui.popup_input(self.stdscr, "Edit", prompt + " (first, last)",
                                   current)
            if value is None:
                return
            parts = _as_list(value)
            if len(parts) != 2:
                ui.popup_message(self.stdscr, "Invalid range",
                                 "Give exactly two addresses: first, last.", ui.ERROR)
                return
            self.settings.set(key, parts)
        return edit


def settings_screen(stdscr, settings, jump_to: str = None) -> None:
    """Editor for everything that is not on the main menu.

    `jump_to` places the cursor on the first row of that section.
    """

    edit = _Editors(stdscr, settings)

    def grant_caps():
        binary = settings.get("xray_bin")
        if not binary:
            return
        if not ui.popup_confirm(stdscr, "Grant capabilities",
                                f"Run  sudo setcap cap_net_admin,cap_net_raw+ep\n{binary}\n\n"
                                "TUN mode then works without sudo."):
            return
        password = None
        if os.geteuid() != 0 and not runner.sudo_ready():
            password = ui.popup_input(stdscr, "sudo", "Password for sudo:", secret=True)
            if password is None:
                return
        ok, message = runner.grant_net_caps(binary, password)
        if ok:
            ui.popup_message(stdscr, "Granted",
                             "xray can now create the TUN device without root.", ui.OK)
        else:
            ui.popup_message(stdscr, "setcap failed", message or "unknown error", ui.ERROR)

    def rows():
        tun = settings.get("tun", {})
        wifi = settings.get("share.wifi", {}) or {}
        lan = settings.get("share.lan", {}) or {}
        caps = "yes" if runner.binary_has_net_caps(settings.get("xray_bin", "")) else "no"
        return [
            ("section", "Paths", "", None),
            ("item", "xray binary", settings.get("xray_bin") or "(not set)",
             edit.file("xray_bin", "Select xray binary", None)),
            ("item", "Configs directory", settings.get("configs_dir") or "(not set)",
             edit.dir("configs_dir", "Select configs directory")),
            ("item", "GeoIP file", asset_value(settings, "geoip_path"),
             edit.file("geoip_path", "Select geoip.dat", (".dat",))),
            ("item", "GeoSite file", asset_value(settings, "geosite_path"),
             edit.file("geosite_path", "Select geosite.dat", (".dat",))),
            ("item", "Log level", settings.get("loglevel"),
             edit.choice("loglevel", "xray log level",
                           [(level, level, note) for level, note in [
                               ("debug", "everything"), ("info", "connections"),
                               ("warning", "default"), ("error", "problems only"),
                               ("none", "silent")]])),

            ("section", "Proxy mode", "", None),
            ("item", "Listen address", settings.get("proxy.listen"),
             edit.text("proxy.listen", "SOCKS/HTTP listen address:")),
            ("item", "SOCKS port", settings.get("proxy.socks_port"),
             edit.number("proxy.socks_port", "SOCKS5 port:")),
            ("item", "HTTP port", settings.get("proxy.http_port"),
             edit.number("proxy.http_port", "HTTP proxy port:")),
            ("item", "Set GNOME system proxy while running",
             "yes" if settings.get("proxy.set_system_proxy") else "no",
             edit.toggle("proxy.set_system_proxy")),

            ("section", "TUN mode", "", None),
            ("item", "Device name", tun.get("name"), edit.text("tun.name", "TUN device name:")),
            ("item", "MTU", tun.get("mtu"), edit.number("tun.mtu", "MTU:")),
            ("item", "Device addresses", ", ".join(tun.get("gateway") or []),
             edit.list("tun.gateway", "Addresses assigned to the device")),
            ("item", "DNS servers", ", ".join(tun.get("dns") or []),
             edit.list("tun.dns", "DNS servers announced on the device")),
            ("item", "Captured routes", ", ".join(tun.get("routes") or []),
             edit.list("tun.routes", "Routes pointed at the device")),
            ("item", "Bypass interface", tun.get("outbound_interface") or "(none)",
             edit.text("tun.outbound_interface",
                         "Interface xray's own traffic leaves by ('auto' to detect):")),
            ("item", "Keep SOCKS inbound in TUN mode",
             "yes" if tun.get("keep_socks") else "no", edit.toggle("tun.keep_socks")),
            ("item", f"xray has cap_net_admin: {caps}", "run setcap…", grant_caps),

            ("section", "Split routing", "", None),
            ("item", "Bypass LAN / private ranges",
             "yes" if settings.get("routing.bypass_private") else "no",
             edit.toggle("routing.bypass_private")),
            ("item", "Direct domains", ", ".join(settings.get("routing.direct_domains") or [])
             or "(none)",
             edit.list("routing.direct_domains",
                         "Domains that skip the proxy, e.g. regexp:.*\\.ru$, "
                         "geosite:category-ru, domain:example.com")),
            ("item", "Direct IPs", ", ".join(settings.get("routing.direct_ips") or [])
             or "(none)",
             edit.list("routing.direct_ips",
                         "IPs/CIDRs that skip the proxy, e.g. geoip:ru, 203.0.113.0/24")),
            ("item", "Force through proxy", ", ".join(settings.get("routing.proxy_domains") or [])
             or "(none)",
             edit.list("routing.proxy_domains",
                         "Domains always sent through the proxy (beats the direct rules)")),

            ("section", "Wi-Fi hotspot", "", None),
            ("item", "Share over Wi-Fi", "yes" if wifi.get("enabled") else "no",
             edit.toggle("share.wifi.enabled")),
            ("item", "Interface", wifi.get("iface") or "(auto)",
             edit.iface("share.wifi.iface", "wifi")),
            ("item", "Network name (SSID)", wifi.get("ssid") or "(not set)",
             edit.text("share.wifi.ssid", "Network name guests will see:")),
            ("item", "Password", wifi.get("password") or "(not set)",
             edit.text("share.wifi.password", "WPA2 password (8 to 63 characters):")),
            ("item", "Band", "2.4 GHz" if wifi.get("band") == "bg" else "5 GHz",
             edit.choice("share.wifi.band", "Band",
                           [("bg", "2.4 GHz", "works everywhere, slower"),
                            ("a", "5 GHz", "faster, often barred by the regulatory domain")])),
            ("item", "Channel", wifi.get("channel") or "(auto)",
             edit.number("share.wifi.channel", "Channel (0 lets NetworkManager pick):")),
            ("item", "Hidden network", "yes" if wifi.get("hidden") else "no",
             edit.toggle("share.wifi.hidden")),
            ("item", "Gateway address", wifi.get("address"),
             edit.text("share.wifi.address",
                         "Address and prefix of this machine on the hotspot:")),
            ("item", "DHCP", DHCP_LABEL.get(wifi.get("dhcp"), wifi.get("dhcp")),
             edit.choice("share.wifi.dhcp", "Who hands out addresses",
                           [(key, DHCP_LABEL[key], note) for key, note in [
                               ("dnsmasq", "full control, and a client list"),
                               ("nm", "no client list, no static leases"),
                               ("off", "guests configure themselves by hand")]])),
            ("item", "DHCP range", " – ".join(wifi.get("range") or []) or "(not set)",
             edit.range("share.wifi.range", "Addresses handed to guests")),
            ("item", "Lease time", wifi.get("lease"),
             edit.text("share.wifi.lease", "DHCP lease time, e.g. 12h:")),
            ("item", "Guest DNS", ", ".join(wifi.get("dns") or []) or "(none)",
             edit.list("share.wifi.dns",
                         "Upstream resolvers, reached through the tunnel")),
            ("item", "Static leases", ", ".join(wifi.get("static") or []) or "(none)",
             edit.list("share.wifi.static",
                         "Fixed addresses as mac,ip,name -- e.g. "
                         "aa:bb:cc:dd:ee:ff,10.20.0.10,printer")),

            ("section", "Wired LAN share", "", None),
            ("item", "Share over ethernet", "yes" if lan.get("enabled") else "no",
             edit.toggle("share.lan.enabled")),
            ("item", "Interface", lan.get("iface") or "(auto)",
             edit.iface("share.lan.iface", "lan")),
            ("item", "Gateway address", lan.get("address"),
             edit.text("share.lan.address",
                         "Address and prefix of this machine on that segment:")),
            ("item", "DHCP", DHCP_LABEL.get(lan.get("dhcp"), lan.get("dhcp")),
             edit.choice("share.lan.dhcp", "Who hands out addresses",
                           [(key, DHCP_LABEL[key], note) for key, note in [
                               ("dnsmasq", "full control, and a client list"),
                               ("nm", "no client list, no static leases"),
                               ("off", "guests configure themselves by hand")]])),
            ("item", "DHCP range", " – ".join(lan.get("range") or []) or "(not set)",
             edit.range("share.lan.range", "Addresses handed to guests")),
            ("item", "Lease time", lan.get("lease"),
             edit.text("share.lan.lease", "DHCP lease time, e.g. 12h:")),
            ("item", "Guest DNS", ", ".join(lan.get("dns") or []) or "(none)",
             edit.list("share.lan.dns",
                         "Upstream resolvers, reached through the tunnel")),
            ("item", "Static leases", ", ".join(lan.get("static") or []) or "(none)",
             edit.list("share.lan.static", "Fixed addresses as mac,ip,name")),

            ("section", "Guest network", "", None),
            ("item", "Isolate guests",
             "yes" if settings.get("share.isolate") else "no",
             edit.toggle("share.isolate")),
            ("item", "Kill-switch (no tunnel, no guest internet)",
             "yes" if settings.get("share.killswitch") else "no",
             edit.toggle("share.killswitch")),
            ("item", "Clamp guest MSS to the tunnel MTU",
             "yes" if settings.get("share.mss_clamp") else "no",
             edit.toggle("share.mss_clamp")),

            ("section", "Traffic stats", "", None),
            ("item", "Show traffic counters",
             "yes" if settings.get("stats.enabled") else "no",
             edit.toggle("stats.enabled")),
            ("item", "Stats API port", settings.get("stats.api_port"),
             edit.number("stats.api_port",
                           "Loopback port xray exposes its stats API on:")),

            ("section", "Connection watchdog", "", None),
            ("item", "Watch the connection",
             "yes" if settings.get("health.enabled") else "no",
             edit.toggle("health.enabled")),
            ("item", "Check interval (s)", settings.get("health.interval"),
             edit.number("health.interval", "Seconds between two checks:", float)),
            ("item", "Proxy check URL", settings.get("health.proxy_url"),
             edit.text("health.proxy_url", "URL fetched through the running proxy:")),
            ("item", "Check the direct connection too",
             "yes" if settings.get("health.check_internet") else "no",
             edit.toggle("health.check_internet")),
            ("item", "Direct check URL", settings.get("health.internet_url"),
             edit.text("health.internet_url",
                         "URL fetched without the proxy (in TUN mode the device "
                         "captures this too):")),
            ("item", "Check timeout (s)", settings.get("health.timeout"),
             edit.number("health.timeout", "Per-request timeout in seconds:", float)),
            ("item", "Failures before acting", settings.get("health.failures"),
             edit.number("health.failures",
                           "Consecutive failed checks before the config is declared dead:")),
            ("item", "Switch to a working config",
             "yes" if settings.get("health.autoswitch") else "no",
             edit.toggle("health.autoswitch")),
            ("item", "Only switch while the direct link works",
             "yes" if settings.get("health.require_internet") else "no",
             edit.toggle("health.require_internet")),
            ("item", "Cooldown between switches (s)", settings.get("health.cooldown"),
             edit.number("health.cooldown",
                           "Seconds to wait before switching again:", float)),
            ("item", "Configs tried per switch",
             settings.get("health.candidates") or "all",
             edit.number("health.candidates",
                           "How many configs to probe when switching (0 = all):")),

            ("section", "Ping tool", "", None),
            ("item", "Probe", MODE_LABEL.get(settings.get("ping.mode"), ""),
             edit.choice("ping.mode", "Ping mode",
                           [("proxy", MODE_LABEL["proxy"], "accurate"),
                            ("tcp", MODE_LABEL["tcp"], "fast")])),
            ("item", "Test URL", settings.get("ping.url"),
             edit.text("ping.url", "URL fetched through the proxy:")),
            ("item", "Timeout (s)", settings.get("ping.timeout"),
             edit.number("ping.timeout", "Per-probe timeout in seconds:", float)),
            ("item", "Parallel probes", settings.get("ping.concurrency"),
             edit.number("ping.concurrency", "How many configs to test at once:")),
            ("item", "Attempts per config", settings.get("ping.attempts"),
             edit.number("ping.attempts", "Requests per config (best is kept):")),
        ]

    index = 1
    top = 0
    stdscr.timeout(-1)

    if jump_to:
        for position, item in enumerate(rows()):
            if item[0] == "section" and item[1] == jump_to:
                index = position + 1
                break

    while True:
        items = rows()
        height, width = stdscr.getmaxyx()
        list_height = max(1, height - 3)
        index = max(0, min(index, len(items) - 1))
        while items[index][0] == "section" and index < len(items) - 1:
            index += 1
        if index < top:
            top = index
        if index >= top + list_height:
            top = index - list_height + 1
        top = max(0, min(top, max(0, len(items) - list_height)))

        stdscr.erase()
        ui.fill_line(stdscr, 0, 0, width, ui.attr(ui.HEADER, True))
        ui.addstr(stdscr, 0, 1, " Settings ", ui.attr(ui.HEADER, True))
        label_width = min(38, max(20, width // 3))

        for offset in range(list_height):
            position = top + offset
            if position >= len(items):
                break
            kind, label, value, _ = items[position]
            row = 1 + offset
            if kind == "section":
                heading = f"── {label} "
                heading += "─" * max(0, width - 2 - len(heading))
                ui.addstr(stdscr, row, 1, heading, ui.attr(ui.TITLE, True))
                continue
            selected = position == index
            style = ui.attr(ui.SELECT) if selected else ui.attr(ui.NORMAL)
            ui.fill_line(stdscr, row, 0, width, style)
            ui.addstr(stdscr, row, 3, ui.clip(label, label_width), style)
            ui.addstr(stdscr, row, 3 + label_width + 2,
                      ui.clip(str(value), width - label_width - 6), style)

        ui.fill_line(stdscr, height - 1, 0, width, ui.attr(ui.HEADER))
        ui.addstr(stdscr, height - 1, 1,
                  "Enter edit · ↑/↓ move · Esc back (settings save immediately)",
                  ui.attr(ui.HEADER))
        stdscr.refresh()

        key = ui.getkey(stdscr)
        if key in (curses.KEY_UP, "k"):
            index -= 1
            while index > 0 and items[index][0] == "section":
                index -= 1
        elif key in (curses.KEY_DOWN, "j"):
            index += 1
            while index < len(items) - 1 and items[index][0] == "section":
                index += 1
        elif key == curses.KEY_NPAGE:
            index += list_height
        elif key == curses.KEY_PPAGE:
            index -= list_height
        elif key == curses.KEY_HOME:
            index = 1
        elif key == curses.KEY_END:
            index = len(items) - 1
        elif ui.is_enter(key) or key == " ":
            editor = items[index][3]
            if editor:
                editor()
        elif ui.is_escape(key) or key in ("q", "Q"):
            break


# -- sharing --------------------------------------------------------------

def _human_lease(seconds: int) -> str:
    if seconds <= 0:
        return "expired"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m{rest % 60:02d}s"


def _share_start(stdscr, manager, xray, restart_xray) -> None:
    """Bring sharing up, asking for a password only if the machine needs one."""
    def attempt(password=None):
        manager.start(xray, password=password)

    try:
        attempt()
    except runner.NeedsPassword:
        password = ui.popup_input(stdscr, "sudo",
                                  "Sharing needs root to configure the interface, "
                                  "the firewall and DHCP. Password for sudo:",
                                  secret=True)
        if password is None:
            return
        try:
            attempt(password)
        except (share.ShareError, runner.NeedsPassword) as exc:
            ui.popup_message(stdscr, "Cannot share", str(exc), ui.ERROR)
            return
    except share.ShareError as exc:
        ui.popup_message(stdscr, "Cannot share", str(exc), ui.ERROR)
        return

    # The access point takes the card away from whatever it was doing, so an
    # xray bound to that interface has to be rebuilt against the uplink left.
    if manager.xray_restart_needed(xray) and restart_xray:
        ui.popup_message(stdscr, "Restarting xray",
                         "xray was reaching the internet through the interface "
                         "now serving guests, so the tunnel is being rebuilt "
                         "against the remaining uplink.", ui.WARN)
        restart_xray()


def share_stop(stdscr, manager) -> None:
    """Tear sharing down, asking for a password only if the machine needs one."""
    password = None
    if manager.needs_password():
        password = ui.popup_input(stdscr, "sudo",
                                  "Removing the access point, the firewall rules "
                                  "and DHCP needs root. Password for sudo:",
                                  secret=True)
        if password is None:
            return
    manager.stop(password=password)


DHCP_CHOICES = [(key, DHCP_LABEL[key], note) for key, note in [
    ("dnsmasq", "full control, and a client list"),
    ("nm", "no client list, no static leases"),
    ("off", "guests configure themselves by hand")]]


def _share_rows(edit, settings) -> list:
    """The fields worth having in front of you while starting a share.

    Deliberately a subset: the ones you change when a guest is standing there.
    Lease times, static leases and upstream DNS live in Settings, one row down.
    """
    wifi = settings.get("share.wifi", {}) or {}
    lan = settings.get("share.lan", {}) or {}
    return [
        ("section", "Wi-Fi hotspot", "", None),
        ("item", "Share over Wi-Fi", "yes" if wifi.get("enabled") else "no",
         edit.toggle("share.wifi.enabled")),
        ("item", "Network name", wifi.get("ssid") or "(not set)",
         edit.text("share.wifi.ssid", "Network name guests will see:")),
        ("item", "Password", wifi.get("password") or "(not set)",
         edit.text("share.wifi.password", "WPA2 password (8 to 63 characters):")),
        ("item", "Band", "2.4 GHz" if wifi.get("band") == "bg" else "5 GHz",
         edit.choice("share.wifi.band", "Band",
                     [("bg", "2.4 GHz", "works everywhere, slower"),
                      ("a", "5 GHz", "faster, often barred by the regulatory domain")])),
        ("item", "Interface", wifi.get("iface") or "(auto)",
         edit.iface("share.wifi.iface", "wifi")),
        ("item", "Gateway address", wifi.get("address"),
         edit.text("share.wifi.address",
                   "Address and prefix of this machine on the hotspot:")),
        ("item", "DHCP", DHCP_LABEL.get(wifi.get("dhcp"), "?"),
         edit.choice("share.wifi.dhcp", "Who hands out addresses", DHCP_CHOICES)),

        ("section", "Wired LAN", "", None),
        ("item", "Share over ethernet", "yes" if lan.get("enabled") else "no",
         edit.toggle("share.lan.enabled")),
        ("item", "Interface", lan.get("iface") or "(auto)",
         edit.iface("share.lan.iface", "lan")),
        ("item", "Gateway address", lan.get("address"),
         edit.text("share.lan.address",
                   "Address and prefix of this machine on that segment:")),
        ("item", "DHCP", DHCP_LABEL.get(lan.get("dhcp"), "?"),
         edit.choice("share.lan.dhcp", "Who hands out addresses", DHCP_CHOICES)),

        ("section", "Guests", "", None),
        ("item", "Isolate guests", "yes" if settings.get("share.isolate") else "no",
         edit.toggle("share.isolate")),
        ("item", "Kill-switch", "yes" if settings.get("share.killswitch") else "no",
         edit.toggle("share.killswitch")),
    ]


def _share_hint(settings, manager, xray, status) -> tuple:
    """The one line that says what is holding sharing back, and why.

    Only the cheap checks: this is redrawn once a second, so it must not shell
    out to nmcli. The full preflight runs when sharing actually starts.
    """
    if status["active"]:
        shared = {record["iface"] for record in status["segments"]}
        parts = [f"uplink {share.uplink_interface(shared) or '(none!)'}"]
        if any(record["dhcp"] == "dnsmasq" for record in status["segments"]):
            parts.append("dnsmasq " + ("running" if status["dnsmasq"] else "DEAD"))
        if not xray.is_running():
            return (" · ".join(parts + ["xray is down, the kill-switch is holding "
                                        "guests offline"]), ui.WARN)
        return " · ".join(parts), ui.DIM
    if not any((settings.get(f"share.{kind}.enabled") for kind in ("wifi", "lan"))):
        return "turn on Wi-Fi or ethernet below, then press s", ui.DIM
    if not xray.is_running():
        return "xray is not running — start it on the main screen first", ui.WARN
    if xray.running_mode() != "tun":
        return ("sharing needs TUN mode: proxy mode has no device to route "
                "guests into", ui.WARN)
    return "ready — press s to start sharing", ui.OK


def share_screen(stdscr, settings, manager, xray, restart_xray=None) -> None:
    """Status, the basic settings, start/stop, and the live client list."""
    edit = _Editors(stdscr, settings)
    index, top = 1, 0
    stdscr.timeout(1000)          # the client list refreshes on its own

    def all_settings():
        settings_screen(stdscr, settings, jump_to="Wi-Fi hotspot")

    while True:
        status = manager.status()
        clients = status["clients"]
        rows = _share_rows(edit, settings)
        rows.append(("item", "All settings…",
                     "lease time, static leases, guest DNS, channel", all_settings))
        height, width = stdscr.getmaxyx()

        client_height = max(4, min(3 + len(clients), 9))
        list_top = 3
        list_height = height - list_top - client_height - 1
        if list_height < 4:
            client_height = max(3, client_height - (4 - list_height))
            list_height = max(1, height - list_top - client_height - 1)

        index = max(0, min(index, len(rows) - 1))
        while rows[index][0] == "section" and index < len(rows) - 1:
            index += 1
        if index < top:
            top = index
        if index >= top + list_height:
            top = index - list_height + 1
        top = max(0, min(top, max(0, len(rows) - list_height)))

        stdscr.erase()
        ui.fill_line(stdscr, 0, 0, width, ui.attr(ui.HEADER, True))
        ui.addstr(stdscr, 0, 1, " Share the tunnel ", ui.attr(ui.HEADER, True))

        active = status["active"]
        marker, style = ("●", ui.OK) if active else ("○", ui.DIM)
        state = "sharing" if active else "not sharing"
        ui.addstr(stdscr, 1, 2, "State", ui.attr(ui.DIM))
        ui.addstr(stdscr, 1, 16, f"{marker} {state}", ui.attr(style, True))
        hint, hint_style = _share_hint(settings, manager, xray, status)
        ui.addstr(stdscr, 1, 16 + len(state) + 3,
                  ui.clip(hint, width - 19 - len(state)), ui.attr(hint_style))

        label_width = min(26, max(16, width // 4))
        for offset in range(list_height):
            position = top + offset
            if position >= len(rows):
                break
            kind, label, value, _ = rows[position]
            row = list_top + offset
            if kind == "section":
                heading = f"── {label} "
                heading += "─" * max(0, width - 2 - len(heading))
                ui.addstr(stdscr, row, 1, heading, ui.attr(ui.TITLE, True))
                continue
            selected = position == index
            style = ui.attr(ui.SELECT) if selected else ui.attr(ui.NORMAL)
            ui.fill_line(stdscr, row, 0, width, style)
            ui.addstr(stdscr, row, 3, ui.clip(label, label_width), style)
            ui.addstr(stdscr, row, 3 + label_width + 2,
                      ui.clip(str(value), width - label_width - 6), style)

        clients_top = height - 1 - client_height
        ui.draw_frame(stdscr, clients_top, 0, client_height, width,
                      f"Guests — {len(clients)}")
        header = f"{'address':<16} {'hardware':<19} {'name':<18} {'lease':>8}  seen"
        ui.addstr(stdscr, clients_top + 1, 2, ui.clip(header, width - 4),
                  ui.attr(ui.DIM, True))
        if not clients:
            ui.addstr(stdscr, clients_top + 2, 2,
                      "no leases yet" if active else
                      "start sharing to hand out addresses", ui.attr(ui.DIM))
        for offset, client in enumerate(clients[:client_height - 3]):
            line = (f"{client['ip']:<16} {client['mac']:<19} "
                    f"{ui.clip(client['name'] or '—', 18):<18} "
                    f"{_human_lease(client['seconds_left']):>8}  {client['state'] or '—'}")
            ui.addstr(stdscr, clients_top + 2 + offset, 2, ui.clip(line, width - 4),
                      ui.attr(ui.OK if client["state"] == "reachable" else ui.NORMAL))

        hints = ("Enter edit · " + ("s stop sharing" if active else "s start sharing")
                 + " · g all settings · Esc back (settings save immediately)")
        ui.fill_line(stdscr, height - 1, 0, width, ui.attr(ui.HEADER))
        ui.addstr(stdscr, height - 1, 1, ui.clip(hints, width - 2), ui.attr(ui.HEADER))
        stdscr.refresh()

        key = ui.getkey(stdscr)
        if key is None or key == curses.KEY_RESIZE:
            continue
        if key in (curses.KEY_UP, "k"):
            index -= 1
            while index > 0 and rows[index][0] == "section":
                index -= 1
        elif key in (curses.KEY_DOWN, "j"):
            index += 1
            while index < len(rows) - 1 and rows[index][0] == "section":
                index += 1
        elif key == curses.KEY_HOME:
            index = 1
        elif key == curses.KEY_END:
            index = len(rows) - 1
        elif ui.is_enter(key) or key == " ":
            action = rows[index][3]
            if action:
                before = json.dumps(settings.get("share", {}), sort_keys=True)
                action()
                stdscr.timeout(1000)
                changed = json.dumps(settings.get("share", {}), sort_keys=True) != before
                # A live share is running on the old settings until it is rebuilt.
                if changed and manager.is_active() and ui.popup_confirm(
                        stdscr, "Restart sharing?",
                        "Sharing is live and still using the old settings. "
                        "Restart it now to apply?"):
                    share_stop(stdscr, manager)
                    _share_start(stdscr, manager, xray, restart_xray)
        elif key in ("s", "S"):
            if active:
                ui.addstr(stdscr, height - 1, 1, "stopping…", ui.attr(ui.HEADER))
                stdscr.refresh()
                share_stop(stdscr, manager)
            else:
                ui.fill_line(stdscr, height - 1, 0, width, ui.attr(ui.HEADER))
                ui.addstr(stdscr, height - 1, 1,
                          "starting… the access point takes a few seconds",
                          ui.attr(ui.HEADER))
                stdscr.refresh()
                _share_start(stdscr, manager, xray, restart_xray)
        elif key in ("g", "G"):
            all_settings()
            stdscr.timeout(1000)
        elif ui.is_escape(key) or key in ("q", "Q"):
            break

    stdscr.timeout(-1)
