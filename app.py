"""Main screen: status, menu, and the xray log pane at the bottom."""

import curses
import json
import os
import time
from pathlib import Path

import runner
import screens
import share
import subs
import ui
import watchdog
import xrayconf
import xrayget

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
        self._problems = []
        self._problems_at = 0.0
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

    def sources_label(self) -> str:
        """What the Config row shows next to the file name."""
        profiles = subs.profile_count(self.settings) if subs.installed() else 0
        added = len(subs.load_sources(self.settings))
        parts = []
        if profiles:
            parts.append(f"{profiles} subscription{'s' if profiles > 1 else ''}")
        if added:
            parts.append(f"{added} added source{'s' if added > 1 else ''}")
        return " · ".join(parts)

    def sysproxy_label(self) -> str:
        if not self.settings.get("proxy.set_system_proxy"):
            return "off"
        if self.settings.mode == "tun":
            return "on · TUN captures everything, not applied"
        if not self.xray.is_running():
            return "on · set when xray starts"
        return f"on · desktop says {runner.system_proxy_mode()}"

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

    def has_binary(self) -> bool:
        binary = self.settings.get("xray_bin") or ""
        return bool(binary) and Path(binary).is_file()

    def menu(self) -> list:
        running = self.xray.is_running()
        rows = []
        if not self.has_binary():
            # Nothing else on this screen can work without it, so it goes first.
            rows.append({"id": "getxray", "label": "Get xray",
                         "value": "download the newest release from GitHub",
                         "accent": True})
        return rows + [
            {"id": "config", "label": "Config", "value": self.short_config(),
             "note": " · ".join(part for part in (self.config_summary(),
                                                  self.sources_label()) if part)},
            {"id": "mode", "label": "Mode", "value": self.mode_label()},
            {"id": "sysproxy", "label": "System proxy", "value": self.sysproxy_label()},
            {"id": "routing", "label": "Routing", "value": self.routing_label()},
            {"id": "share", "label": "Share", "value": self.share.summary()},
            {"id": "watchdog", "label": "Watchdog", "value": self.watchdog.summary()},
            SEPARATOR,
            {"id": "start", "label": "Stop xray" if running else "Start xray",
             "value": "", "accent": True},
            {"id": "restart", "label": "Restart", "value": ""},
            SEPARATOR,
            {"id": "ping", "label": "Ping configs…", "value": ""},
            {"id": "settings", "label": "Settings…", "value": ""},
            {"id": "quit", "label": "Quit", "value": ""},
        ]

    def problems(self) -> list:
        """Cached preflight: the main screen repaints several times a second."""
        now = time.time()
        if now - self._problems_at > 2.0:
            self._problems = self.xray.preflight()
            self._problems_at = now
        return self._problems

    # -- drawing ----------------------------------------------------------
    #
    # Two columns when there is room for them: the menu on the left, a stack of
    # read-only cards on the right. Below ~78 columns the cards collapse into
    # one summary card above the menu, and the log keeps the rest.

    SIDEBAR = 30            # width of the card column
    SPLIT_AT = 78           # narrower than this -> single column
    MAX_LOG_SHARE = 6       # rows the log keeps when there is room for them

    def draw(self) -> None:
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        stdscr.erase()

        ui.fill_line(stdscr, 0, 0, width, ui.attr(ui.HEADER, True))
        ui.addstr(stdscr, 0, 1, " TUIray — xray control ", ui.attr(ui.HEADER, True))
        right = f"{'root' if os.geteuid() == 0 else os.environ.get('USER', '')} "
        ui.addstr(stdscr, 0, max(0, width - len(right) - 1), right, ui.attr(ui.HEADER))

        items = self.menu()
        body_top, body_height = 1, max(6, height - 2)
        split = width >= self.SPLIT_AT

        cards = self.cards() if split else []
        summary_height = 0 if split or body_height < 12 else 5
        # The band is as tall as the taller column needs; the log keeps a third
        # of the body, and never more than MAX_LOG_SHARE rows, so a short
        # terminal spends its rows on the menu rather than on empty log.
        keep_for_log = min(self.MAX_LOG_SHARE, max(3, body_height // 3))
        wanted = summary_height + max(len(items) + 2,
                                      sum(len(lines) + 2 for _, lines in cards))
        band_height = min(wanted, max(4, body_height - keep_for_log))
        menu_height = band_height - summary_height

        if split:
            menu_width = width - self.SIDEBAR
            self._draw_menu(body_top, 0, menu_height, menu_width, items)
            self._draw_cards(body_top, menu_width, band_height, self.SIDEBAR, cards)
        else:
            if summary_height:
                self._draw_summary(body_top, 0, summary_height, width)
            self._draw_menu(body_top + summary_height, 0, menu_height, width, items)

        log_top = body_top + band_height
        self._draw_log(log_top, 0, max(3, height - 1 - log_top), width)

        footer = ("Tab pane · Enter activate · s start/stop · R restart · p ping · "
                  "u configs · w share · h watchdog · g settings · c clear log · q quit")
        if self.message and time.time() < self.message_until:
            footer = self.message
        ui.fill_line(stdscr, height - 1, 0, width, ui.attr(ui.HEADER))
        ui.addstr(stdscr, height - 1, 1, ui.clip(footer, width - 2), ui.attr(ui.HEADER))
        stdscr.refresh()

    def _card(self, top: int, left: int, height: int, width: int,
              title: str, lines: list) -> None:
        """A framed box holding at most height-2 lines of (text, style) parts."""
        if height < 2 or width < 4:
            return
        ui.draw_frame(self.stdscr, top, left, height, width, title)
        edge = left + width - 2
        for offset, line in enumerate(lines[:max(0, height - 2)]):
            x = left + 2
            for text, style in line:
                if x >= edge:
                    break
                ui.addstr(self.stdscr, top + 1 + offset, x, ui.clip(text, edge - x), style)
                x += len(text)

    # -- cards ------------------------------------------------------------
    def status_lines(self) -> list:
        running = self.xray.is_running()
        if running:
            lines = [[("● ", ui.attr(ui.OK, True)), ("running", ui.attr(ui.OK, True))],
                     [("pid    ", ui.attr(ui.DIM)), (str(self.xray.pid), ui.attr(ui.NORMAL))],
                     [("uptime ", ui.attr(ui.DIM)), (self.xray.uptime(), ui.attr(ui.NORMAL))],
                     [("mode   ", ui.attr(ui.DIM)),
                      (self.xray.running_mode(), ui.attr(ui.NORMAL))]]
            config = self.xray.running_config()
            if config and config != self.settings.config_path:
                lines.append([("config changed — restart", ui.attr(ui.WARN))])
            return lines

        lines = [[("○ ", ui.attr(ui.ERROR, True)), ("stopped", ui.attr(ui.ERROR, True))]]
        problems = self.problems()
        if problems:
            lines += [[(problem, ui.attr(ui.ERROR))] for problem in problems[:3]]
        else:
            lines.append([("ready — press s to start", ui.attr(ui.DIM))])
        return lines

    def access_lines(self) -> list:
        listen = self.settings.get("proxy.listen", "127.0.0.1")
        socks = self.settings.get("proxy.socks_port")
        http = self.settings.get("proxy.http_port")
        dim, normal = ui.attr(ui.DIM), ui.attr(ui.NORMAL)
        if self.settings.mode == "tun":
            tun = self.xray.effective_tun()
            lines = [[("device ", dim), (str(tun.get("name")), normal)],
                     [("routes ", dim),
                      (", ".join(tun.get("routes") or []) or "(none)", normal)],
                     [("bypass ", dim), (tun.get("outbound_interface") or "auto", normal)]]
            if tun.get("keep_socks"):
                lines.append([("socks5 ", dim), (f"{listen}:{socks}", normal)])
            return lines

        lines = [[("socks5 ", dim), (f"{listen}:{socks}", normal)],
                 [("http   ", dim), (f"{listen}:{http}", normal)]]
        if self.settings.get("proxy.set_system_proxy"):
            lines.append([("system ", dim), (runner.system_proxy_mode(), normal)])
        return lines

    def traffic_lines(self) -> list:
        stats = self.xray.stats.snapshot()
        dim = ui.attr(ui.DIM)
        if not stats["ok"]:
            if not self.settings.get("stats.enabled", True):
                return [[("counters are off", dim)]]
            if not self.xray.is_running():
                return [[("idle", dim)]]
            return [[("waiting for the xray API", dim)]]
        return [
            [(f"{'rate':>12}{'total':>10}", dim)],
            [("↓ ", ui.attr(ui.OK, True)),
             (f"{ui.human_rate(stats['down_rate']):>10}", ui.attr(ui.OK)),
             (f"{ui.human_bytes(stats['downlink']):>10}", ui.attr(ui.DIM))],
            [("↑ ", ui.attr(ui.WARN, True)),
             (f"{ui.human_rate(stats['up_rate']):>10}", ui.attr(ui.WARN)),
             (f"{ui.human_bytes(stats['uplink']):>10}", ui.attr(ui.DIM))],
        ]

    def watchdog_lines(self) -> list:
        dim = ui.attr(ui.DIM)
        if not self.watchdog.enabled:
            return [[("○ ", dim), ("off", dim)],
                    [("press h to watch the tunnel", dim)]]
        state = self.watchdog.snapshot()
        style = {watchdog.OK: ui.attr(ui.OK, True),
                 watchdog.IDLE: ui.attr(ui.DIM)}.get(state["state"], ui.attr(ui.WARN, True))
        marker = "●" if state["state"] == watchdog.OK else "◐"
        # The countdown rides on the state line, right-aligned: a row of its own
        # would be the first thing a short card drops.
        head = f"{marker} {state['state']}"
        timer = self.watchdog.next_check_label()
        pad = max(1, self.SIDEBAR - 4 - len(head) - len(timer))
        lines = [[(head, style), (" " * pad, dim), (timer, dim)]]
        for label, ok, ms in (("proxy ", state["proxy_ok"], state["proxy_ms"]),
                              ("direct", state["internet_ok"], state["internet_ms"])):
            if ok is None:
                value, value_style = "—", dim
            elif ok:
                value, value_style = f"{ms:.0f} ms", ui.attr(ui.OK)
            else:
                value, value_style = "down", ui.attr(ui.ERROR)
            lines.append([(f"{label} ", dim), (value, value_style)])
        if state["message"]:
            lines.append([(state["message"], dim)])
        return lines

    def cards(self) -> list:
        """The card column, most important first -- the tail is dropped first."""
        return [("Status", self.status_lines()),
                ("Traffic", self.traffic_lines()),
                ("Watchdog", self.watchdog_lines()),
                ("Access", self.access_lines())]

    def _draw_cards(self, top: int, left: int, height: int, width: int,
                    cards: list) -> None:
        """Stack the cards that fit; leftover rows pad them from the bottom up."""
        placed, remaining = [], height
        for title, lines in cards:
            wanted = len(lines) + 2
            if remaining >= wanted:
                placed.append([title, lines, wanted])
                remaining -= wanted
            elif remaining >= 4:                 # room for a clipped card
                placed.append([title, lines, remaining])
                remaining = 0
            if not remaining:
                break
        # Bottoms have to line up with the menu box, so spread the slack instead
        # of leaving one stretched card with a hole in it.
        position = len(placed) - 1
        while remaining > 0 and placed:
            placed[position][2] += 1
            remaining -= 1
            position = (position - 1) % len(placed)

        row = top
        for title, lines, card_height in placed:
            self._card(row, left, card_height, width, title, lines)
            row += card_height

    def _draw_summary(self, top: int, left: int, height: int, width: int) -> None:
        """The narrow-terminal stand-in for the card column: one dense box."""
        dim = ui.attr(ui.DIM)
        status = self.status_lines()[0]
        if self.xray.is_running():
            status = status + [(f"  pid {self.xray.pid} · up {self.xray.uptime()} · "
                                f"{self.xray.running_mode()}", dim)]
        access = [part for line in self.access_lines()[:2] for part in line + [("  ", dim)]]

        stats = self.xray.stats.snapshot()
        live = []
        if stats["ok"]:
            live = [("↓ ", ui.attr(ui.OK, True)),
                    (ui.human_rate(stats["down_rate"]), ui.attr(ui.OK)),
                    ("  ↑ ", ui.attr(ui.WARN, True)),
                    (ui.human_rate(stats["up_rate"]), ui.attr(ui.WARN)),
                    ("  ", dim)]
        # The Watchdog row of the menu carries its state here, so the card only
        # adds what that row has no room for: when the next check lands.
        if self.watchdog.enabled:
            live.append((f"watchdog next {self.watchdog.next_check_label()}", dim))
        else:
            live.append(("watchdog off", dim))
        self._card(top, left, height, width, "Status", [status, access, live])

    # -- menu -------------------------------------------------------------
    def _draw_menu(self, top: int, left: int, height: int, width: int, items: list) -> None:
        stdscr = self.stdscr
        ui.draw_frame(stdscr, top, left, height, width, "Connection",
                      focused=self.focus == "menu")

        self.index = max(0, min(self.index, len(items) - 1))
        capacity = max(1, height - 2)
        if len(items) > capacity:
            if self.index < self.menu_top:
                self.menu_top = self.index
            elif self.index >= self.menu_top + capacity:
                self.menu_top = self.index - capacity + 1
            self.menu_top = max(0, min(self.menu_top, len(items) - capacity))
            rows_to_draw = list(enumerate(items))
        else:
            self.menu_top = 0
            rows_to_draw = self._spaced(items, capacity - len(items))

        row = top + 1
        limit = top + height - 1
        label_width = 14
        for position, item in rows_to_draw:
            if position < self.menu_top:
                continue
            if row >= limit:
                break
            if item is None:                     # breathing room around a rule
                row += 1
                continue
            if item["id"] == "sep":
                ui.addstr(stdscr, row, left + 2, "─" * (width - 4), ui.attr(ui.DIM))
                row += 1
                continue
            selected = position == self.index and self.focus == "menu"
            style = ui.attr(ui.SELECT) if selected else ui.attr(
                ui.KEYHINT if item.get("accent") else ui.NORMAL)
            ui.fill_line(stdscr, row, left + 1, width - 2, style)
            ui.addstr(stdscr, row, left + 2, "▸" if selected else " ", style)
            ui.addstr(stdscr, row, left + 4, ui.clip(item["label"], label_width), style)
            value_x = left + 4 + label_width + 2
            ui.addstr(stdscr, row, value_x, ui.clip(item.get("value", ""),
                                                    width - value_x + left - 2), style)
            note = item.get("note")
            if note and not selected:
                start = value_x + len(item.get("value", "")) + 2
                ui.addstr(stdscr, row, start,
                          ui.clip(f"[{note}]", left + width - 2 - start), ui.attr(ui.DIM))
            row += 1

    @staticmethod
    def _spaced(items: list, slack: int) -> list:
        """(index, item) rows, with blank ones spent on the group separators.

        A menu box stretched to the height of the card column would otherwise
        end in a hole; spacing the groups out makes the room look deliberate.
        """
        rows = []
        for position, item in enumerate(items):
            if item["id"] == "sep" and slack > 0:
                rows.append((position, None))
                slack -= 1
            rows.append((position, item))
            if item["id"] == "sep" and slack > 0:
                rows.append((position, None))
                slack -= 1
        return rows

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


    # -- actions ----------------------------------------------------------
    def start(self) -> None:
        if self.xray.is_running():
            return
        if not self.has_binary():
            if not ui.popup_confirm(self.stdscr, "No xray binary",
                                    "There is no xray binary to run.\n\n"
                                    "Download the newest release from GitHub now?"):
                return
            self.download_xray()
            if not self.has_binary():
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
            # Don't make the watchdog sit out a whole interval before saying
            # whether what was just started actually carries traffic.
            self.watchdog.kicked()
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
        self.watchdog.kicked(0.5)
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
        """The configs page: every source, and the three ways to add one."""
        self.watchdog.pause()
        try:
            chosen = screens.configs_screen(self.stdscr, self.settings)
        finally:
            self.watchdog.resume()
        self.summary_for = None
        if chosen:
            self.use_config(chosen)

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
             "apps must be pointed at the proxy (or use the System proxy row)"),
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

    def download_xray(self) -> None:
        path = screens.download_screen(self.stdscr, self.settings)
        if not path:
            return
        self._problems_at = 0.0          # the Status card is stale now
        self.notify(f"xray installed: {path}", 6)

    def toggle_system_proxy(self) -> None:
        """Hand the desktop our inbounds, or give it back what it had."""
        enable = not bool(self.settings.get("proxy.set_system_proxy"))
        self.settings.set("proxy.set_system_proxy", enable)
        if self.settings.mode == "tun":
            self.notify("saved — TUN mode captures everything, "
                        "so the desktop proxy is left alone", 6)
            return
        if not self.xray.is_running():
            self.notify("system proxy: "
                        + ("set when xray starts" if enable else "off"))
            return
        if enable:
            ok, detail = runner.set_system_proxy(
                self.settings.get("proxy.listen", "127.0.0.1"),
                int(self.settings.get("proxy.socks_port", 10808)),
                int(self.settings.get("proxy.http_port", 10809)))
            note = f"set ({detail})" if ok else f"failed ({detail})"
        else:
            ok, detail = runner.clear_system_proxy()
            note = f"restored ({detail})" if ok else f"restore failed ({detail})"
        self.log.note("system proxy: " + note)
        self.notify("system proxy: " + note, 6)

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
        elif item_id == "sysproxy":
            self.toggle_system_proxy()
        elif item_id == "getxray":
            self.download_xray()
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
            if key in ("u", "U"):
                self.pick_config()
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
