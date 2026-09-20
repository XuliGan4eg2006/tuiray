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
└──────────────── ↓ 1.2 MB/s  ↑ 339.8 KB/s  ·  total ↓ 4.4 GB  ↑ 5.1 MB ───────┘
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
| **Routing** | split-routing rules layered on top of the config (see below) |
| **GeoIP / GeoSite data** | the `.dat` files; needed when a config uses `geoip:` / `geosite:` rules |
| **Start / Stop / Restart** | runs `xray run -c …` and follows its log |
| **Ping configs…** | latency test over every config in the configs directory |
| **Settings…** | ports, TUN parameters, traffic stats, ping options, log level |

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

### What TUN does *not* capture

Directly-connected subnets keep working untouched: an on-link route like
`192.168.31.0/24 dev wlo1` is more specific than the `/1` routes xray installs, so
the kernel never hands that traffic to `tun0`. Same for the docker bridges. Only
traffic with no more-specific route — everything headed for the internet, plus any
private range you do *not* have a route for — enters the tunnel.

That second half is what *Split routing* below is for.

### Privileges

Creating the device and editing the routing table needs `CAP_NET_ADMIN`. Either:

* **setcap (recommended)** — *Settings → xray has cap_net_admin → run setcap…*, which
  runs `sudo setcap cap_net_admin,cap_net_raw+ep <xray>` once. TUN then works with
  no password at all; or
* **sudo** — leave it as is and the TUI asks for your sudo password when starting in
  TUN mode (kept in memory only, for the lifetime of the TUI, so it can also stop
  the root-owned process).

## Traffic counters

The bottom-right corner of the log frame shows the live rate and the running total
in both directions:

```
└──────────────── ↓ 1.2 MB/s  ↑ 339.8 KB/s  ·  total ↓ 4.4 GB  ↑ 5.1 MB ───────┘
```

Down is what reached your applications, up is what they sent; the totals count from
the moment xray started (re-attaching to an instance left running keeps its totals).
On a narrow terminal the totals are dropped and only the rates stay.

The numbers come from xray itself, not from an interface counter, so they are the
same in proxy and TUN mode. `run.json` gets three extra blocks for it:

* `stats: {}` and `policy.system.stats{Inbound,Outbound}{Up,Down}link` — the counters,
* `api` with `StatsService`, listening on `127.0.0.1:10815` (Settings → *Stats API port*).

The API binds its own loopback listener, so no extra inbound and no routing rule are
added, and nothing is reachable from outside the machine. The TUI polls it once a
second by running `xray api statsquery`, which keeps the whole thing dependency-free.
Turn it off with Settings → *Show traffic counters* and the blocks are left out of
`run.json` entirely.

One quirk worth knowing: traffic that leaves through a **direct** outbound is copied
with the kernel's `splice()`, which xray can only account for when the connection
closes. Proxied traffic — the point of the tool — is counted as it flows, but a large
direct download shows up as one jump at the end rather than as a live rate.

## Split routing

*Routing* on the main menu (or *Settings → Split routing*) adds rules **on top of**
the ones the subscription config already carries. They are prepended to
`routing.rules`, so they win.

| Setting | Default | Effect |
|---------|---------|--------|
| Bypass LAN / private ranges | **on** | `10/8`, `172.16/12`, `192.168/16`, `100.64/10`, `127/8`, `169.254/16`, multicast, `fc00::/7`, `fe80::/10` … → direct outbound |
| Direct domains | – | e.g. `regexp:.*\.ru$`, `geosite:category-ru`, `domain:example.com` |
| Direct IPs | – | e.g. `geoip:ru`, `203.0.113.0/24` |
| Force through proxy | – | domains that must use the proxy even if a direct rule matches |

**Why the private bypass matters.** Without it, a LAN address you have no route for
goes into the tunnel and is shipped to the VPN server, where it dies. With
`loglevel: info` you can watch the difference:

```
bypass off →  app/dispatcher: default route for tcp:192.168.1.5:80
              proxy/vless/outbound: tunneling request to tcp:192.168.1.5:80 via 80.76.44.75:443
bypass on  →  app/dispatcher: taking detour [direct] for tcp:192.168.1.5:80
```

**About `*.ru`.** The configs exported from these subscriptions already send
`regexp:.*\.ru$`, `.su`, `.by`, `.xn--p1ai` and ~280 named Russian domains to the
`direct` outbound — that split is built into the subscription, not something you
have to add. Use *Direct domains* for anything they missed (your own domains, a work
host, a service that blocks proxy exits), and *Force through proxy* to punch a hole
back through those built-in direct rules.

The direct outbound is found in the config itself (tag `direct`, else any `freedom`
outbound); if a config has none, a `freedom` outbound tagged `tuiray-direct` is added
to the generated run config. For *Force through proxy*, balancer configs get a
`balancerTag` rule and plain ones an `outboundTag` rule, so it works either way.

Split-routing rules are **not** applied to ping probes — a probe that slipped out
through the direct outbound would report the latency of not using the proxy at all.

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
runner.py     process lifecycle, log tail, traffic stats poller, sudo/setcap, GNOME proxy
pinger.py     SOCKS5 and TCP latency probes, worker pool
xrayconf.py   config loading, inbound rewriting for proxy/TUN, stats API block
settings.py   persisted settings
```
