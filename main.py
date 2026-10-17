#!/usr/bin/env python3
"""tuiray -- an nmtui-style terminal UI for xray.

  python3 tuiray/main.py            # or ./tuiray/main.py

Main menu: pick the config and the geoip/geosite data files, switch between
proxy and TUN mode, start/stop xray and watch its log. The ping tool measures
every config in the configs directory so the fastest one can be selected.
"""

import argparse
import curses
import locale
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app          # noqa: E402  (import after sys.path bootstrap)
import runner       # noqa: E402
import settings as settings_module   # noqa: E402

VERSION = "1.0"


def parse_args():
    parser = argparse.ArgumentParser(description="nmtui-style TUI for xray")
    parser.add_argument("-c", "--config", help="xray config to preselect")
    parser.add_argument("-d", "--configs-dir", help="where subscriptions and links are written")
    parser.add_argument("-m", "--mode", choices=("proxy", "tun"), help="start-up mode")
    parser.add_argument("--stop", action="store_true",
                        help="stop a running xray started by tuiray, then exit")
    parser.add_argument("--status", action="store_true",
                        help="print whether xray is running, then exit")
    parser.add_argument("--version", action="version", version=f"tuiray {VERSION}")
    return parser.parse_args()


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args()
    config = settings_module.Settings.load()

    if args.config:
        config.set("config_path", str(Path(args.config).expanduser().resolve()))
    if args.configs_dir:
        config.set("configs_dir", str(Path(args.configs_dir).expanduser().resolve()))
    if args.mode:
        config.set("mode", args.mode)

    if args.status or args.stop:
        log = runner.LogBuffer()
        xray = runner.XrayRunner(config, log)
        attached = xray.adopt()
        if args.status:
            if attached:
                print(f"running: pid {xray.pid}, mode {xray.running_mode()}, "
                      f"config {xray.running_config()}")
            else:
                print("not running")
            return 0 if attached else 1
        if not attached:
            print("not running")
            return 1
        if not xray.stop():
            print(f"could not stop pid {xray.pid}; a TUN instance runs as root:\n"
                  "  sudo pkill -f 'xray run -c'", file=sys.stderr)
            return 1
        print("stopped")
        return 0

    if not config.get("xray_bin"):
        print("No xray binary found. The TUI offers to download the newest "
              "release for this machine; you can also put one next to this "
              "repo or on $PATH, or set it in Settings.", file=sys.stderr)

    os.environ.setdefault("ESCDELAY", "25")     # snappier Esc handling
    curses.wrapper(_run, config)
    return 0


def _run(stdscr, config) -> None:
    curses.curs_set(0)
    import ui
    ui.init_colors()
    stdscr.keypad(True)
    app.App(stdscr, config).run()


if __name__ == "__main__":
    sys.exit(main())
