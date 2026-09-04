"""Full-screen views: the ping tool and the settings editor."""

import curses
import os
from pathlib import Path

import pinger
import runner
import ui
import xrayconf


# -- ping tool ------------------------------------------------------------

MODE_LABEL = {
    "proxy": "proxy latency (starts xray per config)",
    "tcp": "tcp handshake to endpoint (fast)",
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


def settings_screen(stdscr, settings) -> None:
    """Editor for everything that is not on the main menu."""

    def text_editor(key, prompt):
        def edit():
            value = ui.popup_input(stdscr, "Edit", prompt, str(settings.get(key, "")))
            if value is not None:
                settings.set(key, value.strip())
        return edit

    def number_editor(key, prompt, cast=int):
        def edit():
            value = ui.popup_input(stdscr, "Edit", prompt, str(settings.get(key, "")))
            if value is None:
                return
            try:
                settings.set(key, cast(value.strip()))
            except ValueError:
                ui.popup_message(stdscr, "Invalid value", f"{value!r} is not a number.", ui.ERROR)
        return edit

    def list_editor(key, prompt):
        def edit():
            current = ", ".join(settings.get(key, []) or [])
            value = ui.popup_input(stdscr, "Edit", prompt + " (comma separated)", current)
            if value is not None:
                settings.set(key, _as_list(value))
        return edit

    def toggle(key):
        def edit():
            settings.set(key, not bool(settings.get(key)))
        return edit

    def choice_editor(key, title, options):
        def edit():
            values = [option[0] for option in options]
            current = settings.get(key)
            index = values.index(current) if current in values else 0
            picked = ui.popup_list(stdscr, title, [(o[1], o[2]) for o in options], index)
            if picked is not None:
                settings.set(key, values[picked])
        return edit

    def file_editor(key, title, extensions):
        def edit():
            start = settings.get(key) or str(Path.home())
            path = ui.file_browser(stdscr, start, title, extensions=extensions)
            if path:
                settings.set(key, path)
        return edit

    def dir_editor(key, title):
        def edit():
            path = ui.file_browser(stdscr, settings.get(key) or str(Path.home()),
                                   title, extensions=None, pick_dir=True)
            if path:
                settings.set(key, path)
        return edit

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
        caps = "yes" if runner.binary_has_net_caps(settings.get("xray_bin", "")) else "no"
        return [
            ("section", "Paths", "", None),
            ("item", "xray binary", settings.get("xray_bin") or "(not set)",
             file_editor("xray_bin", "Select xray binary", None)),
            ("item", "Configs directory", settings.get("configs_dir") or "(not set)",
             dir_editor("configs_dir", "Select configs directory")),
            ("item", "GeoIP file", settings.get("geoip_path") or "(not set)",
             file_editor("geoip_path", "Select geoip.dat", (".dat",))),
            ("item", "GeoSite file", settings.get("geosite_path") or "(not set)",
             file_editor("geosite_path", "Select geosite.dat", (".dat",))),
            ("item", "Log level", settings.get("loglevel"),
             choice_editor("loglevel", "xray log level",
                           [(level, level, note) for level, note in [
                               ("debug", "everything"), ("info", "connections"),
                               ("warning", "default"), ("error", "problems only"),
                               ("none", "silent")]])),

            ("section", "Proxy mode", "", None),
            ("item", "Listen address", settings.get("proxy.listen"),
             text_editor("proxy.listen", "SOCKS/HTTP listen address:")),
            ("item", "SOCKS port", settings.get("proxy.socks_port"),
             number_editor("proxy.socks_port", "SOCKS5 port:")),
            ("item", "HTTP port", settings.get("proxy.http_port"),
             number_editor("proxy.http_port", "HTTP proxy port:")),
            ("item", "Set GNOME system proxy while running",
             "yes" if settings.get("proxy.set_system_proxy") else "no",
             toggle("proxy.set_system_proxy")),

            ("section", "TUN mode", "", None),
            ("item", "Device name", tun.get("name"), text_editor("tun.name", "TUN device name:")),
            ("item", "MTU", tun.get("mtu"), number_editor("tun.mtu", "MTU:")),
            ("item", "Device addresses", ", ".join(tun.get("gateway") or []),
             list_editor("tun.gateway", "Addresses assigned to the device")),
            ("item", "DNS servers", ", ".join(tun.get("dns") or []),
             list_editor("tun.dns", "DNS servers announced on the device")),
            ("item", "Captured routes", ", ".join(tun.get("routes") or []),
             list_editor("tun.routes", "Routes pointed at the device")),
            ("item", "Bypass interface", tun.get("outbound_interface") or "(none)",
             text_editor("tun.outbound_interface",
                         "Interface xray's own traffic leaves by ('auto' to detect):")),
            ("item", "Keep SOCKS inbound in TUN mode",
             "yes" if tun.get("keep_socks") else "no", toggle("tun.keep_socks")),
            ("item", f"xray has cap_net_admin: {caps}", "run setcap…", grant_caps),

            ("section", "Ping tool", "", None),
            ("item", "Probe", MODE_LABEL.get(settings.get("ping.mode"), ""),
             choice_editor("ping.mode", "Ping mode",
                           [("proxy", MODE_LABEL["proxy"], "accurate"),
                            ("tcp", MODE_LABEL["tcp"], "fast")])),
            ("item", "Test URL", settings.get("ping.url"),
             text_editor("ping.url", "URL fetched through the proxy:")),
            ("item", "Timeout (s)", settings.get("ping.timeout"),
             number_editor("ping.timeout", "Per-probe timeout in seconds:", float)),
            ("item", "Parallel probes", settings.get("ping.concurrency"),
             number_editor("ping.concurrency", "How many configs to test at once:")),
            ("item", "Attempts per config", settings.get("ping.attempts"),
             number_editor("ping.attempts", "Requests per config (best is kept):")),
        ]

    index = 1
    top = 0
    stdscr.timeout(-1)

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
