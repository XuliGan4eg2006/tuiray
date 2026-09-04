# tuiray

An `nmtui`-style terminal UI for [Xray](https://github.com/XTLS/Xray-core), built for
the configs this repo exports (`export_configs.py` → `configs/<profile>/*.json`).

Pure Python 3 + `curses` — no third-party packages.

```
  TUIray — xray control                                                 dmitry
┌─ Connection ─────────────────────────────────────────────────────────────────┐
│ Status        ● running · pid 13837 · up 00:04:11 · mode proxy               │
│ Access        socks5 127.0.0.1:10808 · http 127.0.0.1:10809                  │
│                                                                              │
│ Config        rfn/03_RFN-_Эстония.json    [vless · tcp+xhttp · 3 servers]    │
│ Mode          Proxy — local SOCKS + HTTP                                     │
│ GeoIP data    /home/dmitry/subextractor/geoip.dat  (16.9 MB)                 │
│ GeoSite data  /home/dmitry/subextractor/geosite.dat  (10.0 MB)               │
│ ──────────────────────────────────────────────────────────────────────────── │
│ Stop xray                                                                    │
│ Restart                                                                      │
│ ──────────────────────────────────────────────────────────────────────────── │
│ Ping configs…                                                                │
│ Settings…                                                                    │
│ Quit                                                                         │
└──────────────────────────────────────────────────────────────────────────────┘
┌─ Xray log — 12 lines ────────────────────────────────────────────────────────┐
│ [tuiray] mode=proxy  config=…/configs/rfn/03_RFN-_Эстония.json               │
│ 2026/09/04 12:10:26 [Warning] core: Xray 26.7.28 started                     │
└──────────────────────────────────────────────────────────────────────────────┘
 Tab pane · Enter activate · s start/stop · R restart · p ping · g settings · q quit
```

## Run

```bash
python3 tuiray/main.py          # or ./tuiray/main.py
```

On first start it finds the `xray` binary, `configs/`, `geoip.dat` and `geosite.dat`
next to the repo. Everything is changeable from the UI.

Command line extras:

```bash
python3 tuiray/main.py -c configs/rfn/03_RFN-_Эстония.json -m tun   # preselect
python3 tuiray/main.py --status                                     # is it up?
python3 tuiray/main.py --stop                                       # stop it
```

## Main menu

| Row | What it does |
|-----|--------------|
| **Config** | file browser over `configs/` — the `.json` xray actually runs |
| **Mode** | Proxy (local SOCKS + HTTP) or TUN (system-wide) |
| **GeoIP / GeoSite data** | the `.dat` files; needed when a config uses `geoip:` / `geosite:` rules |
| **Start / Stop / Restart** | runs `xray run -c …` and follows its log |
| **Ping configs…** | latency test over every config in the configs directory |
| **Settings…** | ports, TUN parameters, ping options, log level |

Keys: `Tab` switch between menu and log · `Enter` activate · `s` start/stop ·
`R` restart · `p` ping · `g` settings · `c` clear log · `q` quit.
With the log pane focused, `↑ ↓ PgUp PgDn Home End` scroll it; `End` returns to
following the tail.

Quitting does **not** kill xray unless you ask it to — the next run re-attaches to
the process through `~/.cache/tuiray/xray.pid` and keeps showing its log.

## Proxy vs TUN

**Proxy mode** rewrites the config's inbounds to a SOCKS5 (`10808`) and an HTTP
(`10809`) listener on `127.0.0.1`. Only apps pointed at the proxy use the tunnel.
Settings has a *Set GNOME system proxy while running* toggle that flips
`org.gnome.system.proxy` to those ports on start and restores it on stop.

**TUN mode** uses the TUN inbound of Xray-core (26.x+): xray creates the device,
assigns the addresses, and installs the captured routes itself — no `tun2socks`.

Defaults (all editable in *Settings → TUN mode*):

| Setting | Default | Meaning |
|---------|---------|---------|
| Device name | `tun0` | `settings.name` |
| MTU | `1500` | `settings.mtu` |
| Device addresses | `10.10.0.1/24`, `fd00:10:10::1/64` | `settings.gateway` |
| DNS servers | `1.1.1.1`, `8.8.8.8` | `settings.dns` |
| Captured routes | `0.0.0.0/1`, `128.0.0.0/1`, `::/1`, `8000::/1` | `settings.autoSystemRoutingTable` |
| Bypass interface | `auto` | `settings.autoOutboundsInterface` |

Two details worth knowing:

* The routes are **split defaults**, not `0.0.0.0/0`. Xray installs its routes with
  metric 1, and NetworkManager gives Wi-Fi a default route with metric 600 — but a
  wired link can sit at metric 100 or lower, and then a `/0` from xray would lose.
  A `/1` is more specific than any default route, so capture works either way.
* **Bypass interface** is the physical interface xray's own connections to the proxy
  server leave by; without it the tunnel would route itself in a loop. `auto`
  resolves to whatever `ip route get 1.1.1.1` reports (here: `wlo1`) at start time.

### Privileges

Creating the device and editing the routing table needs `CAP_NET_ADMIN`. Either:

* **setcap (recommended)** — *Settings → xray has cap_net_admin → run setcap…*, which
  runs `sudo setcap cap_net_admin,cap_net_raw+ep <xray>` once. TUN then works with
  no password at all; or
* **sudo** — leave it as is and the TUI asks for your sudo password when starting in
  TUN mode (kept in memory only, for the lifetime of the TUI, so it can also stop
  the root-owned process).

## Ping tool

`p` from the main menu tests every `*.json` under the configs directory and sorts by
latency. `Enter` selects the highlighted config, `f` selects the fastest one.

* **proxy latency** (default) starts a private xray per config on a throwaway SOCKS
  port and times a real request to `http://cp.cloudflare.com/generate_204` through it.
  Slow but honest — it works for the balancer configs too, where the server is only
  picked at runtime. 4 configs are tested in parallel (Settings → *Parallel probes*).
* **tcp handshake** (`m` to switch) just measures `connect()` to the servers named in
  the config. Instant, but says nothing about whether the proxy itself works.

Every probe's xray is killed when it finishes, when you cancel (`c`), and when you
leave the screen.

## Files

| Path | What |
|------|------|
| `~/.config/tuiray/settings.json` | all settings; saved on every change |
| `~/.cache/tuiray/run.json` | the config actually handed to xray (original + rewritten inbounds) |
| `~/.cache/tuiray/xray.log` | xray output, tailed by the log pane |
| `~/.cache/tuiray/xray.pid` | pid/mode/config of the running instance, for re-attaching |
| `~/.cache/tuiray/assets/` | symlinks named `geoip.dat` / `geosite.dat`, passed as `XRAY_LOCATION_ASSET` |

The selected config is never modified: it is copied, its `inbounds` replaced for the
mode, and the copy is what runs. Routing, DNS, balancers and outbounds are untouched.

## Layout

```
main.py       entry point, CLI flags
app.py        main screen: status, menu, log pane
screens.py    ping tool and settings screens
ui.py         curses widgets: frames, popups, file browser
runner.py     process lifecycle, log tail, sudo/setcap, GNOME proxy
pinger.py     SOCKS5 and TCP latency probes, worker pool
xrayconf.py   config loading, inbound rewriting for proxy/TUN
settings.py   persisted settings
```
