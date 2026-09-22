"""Main screen: status, menu, and the xray log pane at the bottom."""

import curses
import json
import os
import time
from pathlib import Path

import runner
import screens
import share
import ui
import watchdog
import xrayconf

SEPARATOR = {"id": "sep"}


class App:
    def __init__(self, stdscr, settings):
        self.stdscr = stdscr
        self.settings = settings
        self.log = runner.LogBuffer(int(settings.get("log_lines", 3000)))
        self.xray = runner.XrayRunner(settings, self.log)
        self.share = share.ShareManager(settings, self.log)
        self.watchdog = watchdog.Watchdog(settings, self.xray, self.log)
        self.focus = "menu"          # menu | log
        self.index = 0
        self.menu_top = 0            # first menu row drawn, for short terminals
        self.log_offset = 0          # lines scrolled up from the tail
        self.summary = None
        self.summary_for = None
        self.message = ""
        self.message_until = 0

    # -- helpers ----------------------------------------------------------
    def notify(self, text: str, seconds: float = 4.0) -> None:
        self.message = text
        self.message_until = time.time() + seconds

    def config_summary(self) -> str:
        path = self.settings.config_path
        if path != self.summary_for:
            self.summary_for = path
            self.summary = None
            try:
                info = xrayconf.summarize(xrayconf.load(path))
            except (OSError, ValueError):
                self.summary = "unreadable config"
            else:
                parts = []
                if info["protocols"]:
                    parts.append("/".join(info["protocols"]))
                if info["networks"]:
                    parts.append("+".join(info["networks"]))
                if info["servers"]:
                    parts.append(f"{len(info['servers'])} server"
                                 f"{'s' if len(info['servers']) > 1 else ''}")
                if info["balancers"]:
                    parts.append("balancer")
                self.summary = " · ".join(parts)
        return self.summary or ""

    def mode_label(self) -> str:
        if self.settings.mode == "tun":
            tun = self.settings.get("tun", {})
            return f"TUN — system-wide via {tun.get('name', 'tun0')}"
        return "Proxy — local SOCKS + HTTP"

    def access_line(self) -> str:
        listen = self.settings.get("proxy.listen", "127.0.0.1")
        socks = self.settings.get("proxy.socks_port")
        http = self.settings.get("proxy.http_port")
        if self.settings.mode == "tun":
            tun = self.xray.effective_tun()
            routes = ", ".join(tun.get("routes") or []) or "(none)"
            via = tun.get("outbound_interface") or "auto"
            extra = f" · socks5 {listen}:{socks}" if tun.get("keep_socks") else ""
            return f"{tun.get('name')} · routes {routes} · bypass {via}{extra}"
        proxy_state = ""
        if self.settings.get("proxy.set_system_proxy"):
            proxy_state = f" · GNOME proxy: {runner.system_proxy_mode()}"
        return f"socks5 {listen}:{socks} · http {listen}:{http}{proxy_state}"

    def routing_label(self) -> str:
        """One-line summary of the split-routing rules on top of the config."""
        parts = []
        if self.settings.get("routing.bypass_private", True):
            parts.append("LAN/private direct")
        for key, noun in (("routing.direct_domains", "direct domain"),
                          ("routing.direct_ips", "direct IP rule"),
                          ("routing.proxy_domains", "forced-proxy domain")):
            count = len(self.settings.get(key) or [])
            if count:
                parts.append(f"{count} {noun}{'s' if count > 1 else ''}")
        return " · ".join(parts) if parts else "config rules only"

    def short_config(self) -> str:
        path = self.settings.config_path
        if not path:
            return "(none selected)"
        parent = Path(path).parent.name
        return f"{parent}/{Path(path).name}" if parent else Path(path).name

    def menu(self) -> list:
        running = self.xray.is_running()
        return [
            {"id": "config", "label": "Config", "value": self.short_config(),
             "note": self.config_summary()},
            {"id": "mode", "label": "Mode", "value": self.mode_label()},
            {"id": "routing", "label": "Routing", "value": self.routing_label()},
            {"id": "share", "label": "Share", "value": self.share.summary()},
            {"id": "watchdog", "label": "Watchdog", "value": self.watchdog.summary()},
            {"id": "geoip", "label": "GeoIP data",
             "value": self._asset_value("geoip_path")},
            {"id": "geosite", "label": "GeoSite data",
             "value": self._asset_value("geosite_path")},
            SEPARATOR,
            {"id": "start", "label": "Stop xray" if running else "Start xray",
             "value": "", "accent": True},
            {"id": "restart", "label": "Restart", "value": ""},
            SEPARATOR,
            {"id": "ping", "label": "Ping configs…", "value": ""},
            {"id": "settings", "label": "Settings…", "value": ""},
            {"id": "quit", "label": "Quit", "value": ""},
        ]

    def _asset_value(self, key: str) -> str:
        path = self.settings.get(key)
        if not path:
            return "(not set)"
        if not Path(path).exists():
            return f"{path}  — MISSING"
        return f"{path}  ({ui.human_size(path)})"

    # -- drawing ----------------------------------------------------------
    def draw(self) -> None:
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        stdscr.erase()

        ui.fill_line(stdscr, 0, 0, width, ui.attr(ui.HEADER, True))
        ui.addstr(stdscr, 0, 1, " TUIray — xray control ", ui.attr(ui.HEADER, True))
        right = f"{'root' if os.geteuid() == 0 else os.environ.get('USER', '')} "
        ui.addstr(stdscr, 0, max(0, width - len(right) - 1), right, ui.attr(ui.HEADER))

        items = self.menu()
        body_height = min(len(items) + 6, max(9, height - 9))
        log_height = max(4, height - 1 - body_height - 1)
        if body_height + log_height + 2 > height:
            log_height = max(3, height - body_height - 2)

        self._draw_body(1, 0, body_height, width, items)
        self._draw_log(1 + body_height, 0, log_height, width)
        self._draw_traffic(body_height + log_height, 0, width)

        footer = ("Tab pane · Enter activate · s start/stop · R restart · p ping · "
                  "w share · h watchdog · g settings · c clear log · q quit")
        if self.message and time.time() < self.message_until:
            footer = self.message
        ui.fill_line(stdscr, height - 1, 0, width, ui.attr(ui.HEADER))
        ui.addstr(stdscr, height - 1, 1, ui.clip(footer, width - 2), ui.attr(ui.HEADER))
        stdscr.refresh()

    def _draw_body(self, top: int, left: int, height: int, width: int, items: list) -> None:
        stdscr = self.stdscr
        ui.draw_frame(stdscr, top, left, height, width, "Connection",
                      focused=self.focus == "menu")

        running = self.xray.is_running()
        marker, state_style = ("●", ui.OK) if running else ("○", ui.ERROR)
        state = "running" if running else "stopped"
        details = ""
        if running:
            details = (f"· pid {self.xray.pid} · up {self.xray.uptime()} · "
                       f"mode {self.xray.running_mode()}")
            config = self.xray.running_config()
            if config and config != self.settings.config_path:
                details += " · config changed, restart to apply"
        ui.addstr(stdscr, top + 1, left + 2, "Status", ui.attr(ui.DIM))
        ui.addstr(stdscr, top + 1, left + 16, f"{marker} {state}", ui.attr(state_style, True))
        ui.addstr(stdscr, top + 1, left + 16 + len(state) + 3, details, ui.attr(ui.DIM))
        ui.addstr(stdscr, top + 2, left + 2, "Access", ui.attr(ui.DIM))
        ui.addstr(stdscr, top + 2, left + 16, ui.clip(self.access_line(), width - 19))

        # On a short terminal the menu scrolls so the cursor stays visible.
        capacity = max(1, height - 5)
        if len(items) > capacity:
            if self.index < self.menu_top:
                self.menu_top = self.index
            elif self.index >= self.menu_top + capacity:
                self.menu_top = self.index - capacity + 1
            self.menu_top = max(0, min(self.menu_top, len(items) - capacity))
        else:
            self.menu_top = 0

        row = top + 4
        limit = top + height - 1
        for position, item in enumerate(items):
            if position < self.menu_top:
                continue
            if row >= limit:
                break
            if item["id"] == "sep":
                ui.addstr(stdscr, row, left + 2, "─" * (width - 4), ui.attr(ui.DIM))
                row += 1
                continue
            selected = position == self.index and self.focus == "menu"
            style = ui.attr(ui.SELECT) if selected else ui.attr(
                ui.KEYHINT if item.get("accent") else ui.NORMAL)
            ui.fill_line(stdscr, row, left + 1, width - 2, style)
            ui.addstr(stdscr, row, left + 2, ui.clip(item["label"], 14), style)
            value_x = left + 16
            ui.addstr(stdscr, row, value_x, ui.clip(item.get("value", ""),
                                                    width - value_x - 3), style)
            note = item.get("note")
            if note and not selected:
                start = value_x + len(item.get("value", "")) + 2
                ui.addstr(stdscr, row, start, ui.clip(f"[{note}]", width - start - 3),
                          ui.attr(ui.DIM))
            row += 1

    def _draw_log(self, top: int, left: int, height: int, width: int) -> None:
        stdscr = self.stdscr
        lines = self.log.snapshot()
        rows = height - 2
        max_offset = max(0, len(lines) - rows)
        self.log_offset = max(0, min(self.log_offset, max_offset))
        end = len(lines) - self.log_offset
        window = lines[max(0, end - rows): end]

        title = f"Xray log — {len(lines)} lines"
        if self.log_offset:
            title += f" (scrolled {self.log_offset})"
        ui.draw_frame(stdscr, top, left, height, width, title, focused=self.focus == "log")

        for offset, line in enumerate(window):
            style = ui.attr(ui.NORMAL)
            lowered = line.lower()
            if "[tuiray]" in line:
                style = ui.attr(ui.TITLE)
            elif "failed" in lowered or "[error]" in lowered or "rejected" in lowered:
                style = ui.attr(ui.ERROR)
            elif "[warning]" in lowered:
                style = ui.attr(ui.WARN)
            elif "started" in lowered:
                style = ui.attr(ui.OK)
            ui.addstr(stdscr, top + 1 + offset, left + 2, ui.clip(line, width - 4), style)

    def _draw_traffic(self, row: int, left: int, width: int) -> None:
        """Live rates and session totals, inlaid in the bottom-right border."""
        stats = self.xray.stats.snapshot()
        if not stats["ok"]:
            if not self.xray.is_running() or not self.settings.get("stats.enabled", True):
                return
            segments = [("traffic: waiting for the xray API", ui.attr(ui.DIM))]
        else:
            segments = [
                ("↓ ", ui.attr(ui.DIM)),
                (ui.human_rate(stats["down_rate"]), ui.attr(ui.OK, True)),
                ("  ↑ ", ui.attr(ui.DIM)),
                (ui.human_rate(stats["up_rate"]), ui.attr(ui.WARN, True)),
            ]
            totals = [
                ("  ·  total ↓ ", ui.attr(ui.DIM)),
                (ui.human_bytes(stats["downlink"]), ui.attr(ui.OK)),
                ("  ↑ ", ui.attr(ui.DIM)),
                (ui.human_bytes(stats["uplink"]), ui.attr(ui.WARN)),
            ]
            # Totals are the first thing to go on a narrow terminal.
            if sum(len(text) for text, _ in segments + totals) + 8 <= width:
                segments += totals

        span = sum(len(text) for text, _ in segments)
        x = left + width - 2 - span
        if x < left + 3:
            return
        ui.addstr(self.stdscr, row, x - 1, " ", ui.attr(ui.DIM))
        for text, style in segments:
            ui.addstr(self.stdscr, row, x, text, style)
            x += len(text)
        ui.addstr(self.stdscr, row, x, " ", ui.attr(ui.DIM))

    # -- actions ----------------------------------------------------------
    def start(self) -> None:
        if self.xray.is_running():
            return
        try:
            self.xray.start()
        except runner.NeedsPassword:
            password = ui.popup_input(
                self.stdscr, "sudo",
                "TUN mode needs root (or setcap on the xray binary). Password for sudo:",
                secret=True)
            if password is None:
                return
            try:
                self.xray.start(password=password)
            except (RuntimeError, runner.NeedsPassword) as exc:
                ui.popup_message(self.stdscr, "Cannot start", str(exc), ui.ERROR)
                return
        except RuntimeError as exc:
            ui.popup_message(self.stdscr, "Cannot start", str(exc), ui.ERROR)
            return

        time.sleep(0.8)
        if not self.xray.is_running():
            self.notify("xray exited immediately — see the log below", 8)
        else:
            self.notify(f"xray started in {self.settings.mode} mode")
        self.log_offset = 0

    def stop(self) -> None:
        if not self.xray.is_running():
            return
        if not self.xray.stop():
            # Root-owned instance and sudo no longer has a cached credential.
            password = ui.popup_input(self.stdscr, "sudo",
                                      "Stopping the TUN instance needs root. Password for sudo:",
                                      secret=True)
            if password is None or not self.xray.stop(password=password):
                ui.popup_message(self.stdscr, "Still running",
                                 f"xray (pid {self.xray.pid}) could not be stopped.\n"
                                 "Stop it manually with:  sudo pkill -f 'xray run -c'",
                                 ui.ERROR)
                return
        self.notify("xray stopped")

    def toggle(self) -> None:
        if self.xray.is_running():
            self.stop()
        else:
            self.start()

    def restart(self) -> None:
        self.stop()
        self.start()

    def pick_config(self) -> None:
        start = self.settings.config_path or self.settings.get("configs_dir")
        path = ui.file_browser(self.stdscr, start, "Select xray config", extensions=(".json",))
        if path:
            self.use_config(path)

    def use_config(self, path: str) -> None:
        try:
            xrayconf.load(path)
        except (OSError, ValueError) as exc:
            ui.popup_message(self.stdscr, "Bad config", str(exc), ui.ERROR)
            return
        self.settings.set("config_path", path)
        self.notify(f"config: {Path(path).name}")
        self.maybe_restart("Config changed.")

    def pick_mode(self) -> None:
        options = [
            ("Proxy — local SOCKS + HTTP",
             "apps must be pointed at the proxy (or use the GNOME proxy toggle)"),
            ("TUN — system-wide",
             "xray creates a TUN device and captures all traffic; needs root/setcap"),
        ]
        current = 1 if self.settings.mode == "tun" else 0
        picked = ui.popup_list(self.stdscr, "Mode", options, current)
        if picked is None:
            return
        mode = "tun" if picked == 1 else "proxy"
        if mode == self.settings.mode:
            return
        self.settings.set("mode", mode)
        if mode == "tun" and self.xray.needs_root():
            self.notify("TUN mode will ask for your sudo password on start", 6)
        self.maybe_restart("Mode changed.")

    def pick_asset(self, key: str, title: str) -> None:
        start = self.settings.get(key) or self.settings.get("configs_dir")
        path = ui.file_browser(self.stdscr, start, title, extensions=(".dat",))
        if path:
            self.settings.set(key, path)
            self.settings.asset_dir()
            self.maybe_restart(f"{title} changed.")

    def maybe_restart(self, reason: str) -> None:
        if self.xray.is_running() and ui.popup_confirm(
                self.stdscr, "Restart?", f"{reason} Restart xray now to apply?"):
            self.restart()

    def open_settings(self, jump_to: str = None) -> None:
        """Settings are live-saved, so offer a restart when they actually changed."""
        before = json.dumps(self.settings.data, sort_keys=True)
        screens.settings_screen(self.stdscr, self.settings, jump_to=jump_to)
        self.summary_for = None
        if json.dumps(self.settings.data, sort_keys=True) != before:
            self.maybe_restart("Settings changed.")

    def open_share(self) -> None:
        screens.share_screen(self.stdscr, self.settings, self.share,
                             self.xray, self.restart)

    def open_watchdog(self) -> None:
        """Toggle the watchdog, force a check, or jump to its settings."""
        state = self.watchdog.snapshot()
        on = self.watchdog.enabled
        detail = state["message"] or "-"
        picked = ui.popup_list(
            self.stdscr, "Connection watchdog",
            [("Turn the watchdog off" if on else "Turn the watchdog on",
              "checks the proxy every "
              f"{self.settings.get('health.interval')}s and switches config when it dies"),
             ("Check now", detail),
             ("Watchdog settings…", "URLs, interval, auto-switch")], 0)
        if picked == 0:
            self.notify("watchdog " + ("enabled" if self.watchdog.toggle() else "disabled"))
        elif picked == 1:
            if not self.watchdog.enabled:
                self.notify("watchdog is off — turn it on first", 5)
                return
            self.watchdog.check_now()
            self.notify("watchdog: checking…")
        elif picked == 2:
            self.open_settings("Connection watchdog")

    def ping(self) -> None:
        # One xray per probe and a port each: the watchdog stays out of the way.
        self.watchdog.pause()
        try:
            chosen = screens.ping_screen(self.stdscr, self.settings)
        finally:
            self.watchdog.resume()
        if chosen:
            self.use_config(chosen)

    def quit(self) -> bool:
        if self.xray.is_running():
            picked = ui.popup_list(
                self.stdscr, "xray is running",
                [("Stop xray and quit", "the tunnel/proxy goes away"),
                 ("Leave it running and quit", "reattach next time you start tuiray"),
                 ("Cancel", "")], 0)
            if picked is None or picked == 2:
                return False
            if picked == 0:
                self.stop()
        if self.share.is_active():
            picked = ui.popup_list(
                self.stdscr, "sharing is live",
                [("Stop sharing and quit", "the access point and DHCP go away"),
                 ("Leave it up and quit",
                  "profiles, rules and DHCP stay; tuiray picks them up next run"),
                 ("Cancel", "")], 0)
            if picked is None or picked == 2:
                return False
            if picked == 0:
                screens.share_stop(self.stdscr, self.share)
        return True

    def activate(self, item_id: str) -> bool:
        """Run a menu action; returns False when the app should exit."""
        if item_id == "config":
            self.pick_config()
        elif item_id == "mode":
            self.pick_mode()
        elif item_id == "geoip":
            self.pick_asset("geoip_path", "Select geoip.dat")
        elif item_id == "geosite":
            self.pick_asset("geosite_path", "Select geosite.dat")
        elif item_id == "start":
            self.toggle()
        elif item_id == "restart":
            self.restart()
        elif item_id == "routing":
            self.open_settings("Split routing")
        elif item_id == "share":
            self.open_share()
        elif item_id == "watchdog":
            self.open_watchdog()
        elif item_id == "ping":
            self.ping()
        elif item_id == "settings":
            self.open_settings()
        elif item_id == "quit":
            return not self.quit()
        return True

    # -- loop -------------------------------------------------------------
    def move(self, delta: int) -> None:
        items = self.menu()
        position = self.index
        for _ in range(len(items)):
            position = (position + delta) % len(items)
            if items[position]["id"] != "sep":
                break
        self.index = position

    def run(self) -> None:
        self.xray.adopt()
        self.watchdog.start()
        try:
            self._loop()
        finally:
            self.watchdog.stop()

    def _loop(self) -> None:
        while True:
            # Sub-screens set their own input timeout; restore ours each pass so
            # the status line and log keep refreshing without a keypress.
            self.stdscr.timeout(400)
            self.draw()
            key = ui.getkey(self.stdscr)
            if key is None:
                continue
            if key == curses.KEY_RESIZE:
                continue

            if key == "\t":
                self.focus = "log" if self.focus == "menu" else "menu"
                continue
            if key in ("q", "Q"):
                if self.quit():
                    return
                continue
            if key in ("s", "S"):
                self.toggle()
                continue
            if key == "R":
                self.restart()
                continue
            if key in ("p", "P"):
                self.ping()
                continue
            if key in ("w", "W"):
                self.open_share()
                continue
            if key in ("h", "H"):
                self.notify("watchdog " + ("enabled" if self.watchdog.toggle() else "disabled"))
                continue
            if key in ("g", "G"):
                self.open_settings()
                continue
            if key in ("c", "C"):
                self.log.clear()
                self.log_offset = 0
                continue

            if self.focus == "log":
                height = self.stdscr.getmaxyx()[0]
                page = max(1, height // 3)
                if key in (curses.KEY_UP, "k"):
                    self.log_offset += 1
                elif key in (curses.KEY_DOWN, "j"):
                    self.log_offset -= 1
                elif key == curses.KEY_PPAGE:
                    self.log_offset += page
                elif key == curses.KEY_NPAGE:
                    self.log_offset -= page
                elif key == curses.KEY_HOME:
                    self.log_offset = len(self.log)
                elif key == curses.KEY_END:
                    self.log_offset = 0
                elif ui.is_escape(key):
                    self.focus = "menu"
                self.log_offset = max(0, self.log_offset)
                continue

            if key in (curses.KEY_UP, "k"):
                self.move(-1)
            elif key in (curses.KEY_DOWN, "j"):
                self.move(1)
            elif ui.is_enter(key) or key == " ":
                items = self.menu()
                if not self.activate(items[self.index]["id"]):
                    return
            elif ui.is_escape(key):
                if self.quit():
                    return
