# tuiray

An `nmtui`-style terminal UI for [Xray](https://github.com/XTLS/Xray-core): pick a
config, start it as a local proxy or a system-wide tunnel, watch it, share it, and
keep subscriptions up to date.

Pure Python 3 + `curses` — no third-party packages.

```
  TUIray — xray control                                                 dmitry
┌─ Connection ───────────────────────────────────┐┌─ Status ───────────────────┐
│ ▸ Config          whit/00_Авто_Быстрый.json    ││ ● running                  │
│   Mode            Proxy — local SOCKS + HTTP   ││ pid    12345               │
│   System proxy    on · desktop says manual     ││ uptime 00:12:33            │
│   Routing         LAN/private direct           ││ mode   proxy               │
│   Share           wifi TUIray · stopped · lan… │└────────────────────────────┘
│   Watchdog        watching · proxy 46 ms · di… │┌─ Traffic ──────────────────┐
│                                                ││         rate     total     │
│ ────────────────────────────────────────────── ││ ↓   1.2 MB/s  472.0 MB     │
│   Stop xray                                    ││ ↑  43.9 KB/s   12.7 MB     │
│   Restart                                      │└────────────────────────────┘
│ ────────────────────────────────────────────── │┌─ Watchdog ─────────────────┐
│   Ping configs…                                ││ ● ok                in 22s │
│   Settings…                                    ││ proxy  46 ms               │
│   Quit                                         ││ direct 61 ms               │
└────────────────────────────────────────────────┘└────────────────────────────┘
┌─ Xray log — 4 lines ─────────────────────────────────────────────────────────┐
│ [tuiray] mode=proxy  config=…/configs/savevpn/02_Литва.json                  │
│ 2026/09/22 12:10:26 [Warning] core: Xray 26.7.28 started                     │
│ [tuiray] system proxy: set (GNOME)                                           │
│ [tuiray] watchdog: proxy 46 ms · direct 61 ms                                │
└──────────────────────────────────────────────────────────────────────────────┘
 Tab pane · Enter activate · s start/stop · R restart · p ping · u configs · w…
```

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/XuliGan4eg2006/tuiray/master/install.sh | sh
```

Or, if piping a script into a shell makes you uneasy — it should — read it first:

```bash
curl -fsSL https://raw.githubusercontent.com/XuliGan4eg2006/tuiray/master/install.sh -o install.sh
less install.sh && sh install.sh
```

It needs `git` and a `python3` that can `import curses`, installs nothing else,
and never asks for root:

| Path | What |
|------|------|
| `~/.local/share/tuiray/app/` | the checkout, cloned **with its submodule** |
| `~/.local/share/tuiray/` | where *Get xray* puts `xray`, `geoip.dat`, `geosite.dat` |
| `~/.local/bin/tuiray` | the launcher (it tells you if that is not on your `PATH`) |
| `~/.local/bin/tuiray-update` | self-contained updater |

```bash
tuiray                     # run it
tuiray-update              # or: sh install.sh --update
sh install.sh --uninstall  # program only; settings, profiles and configs stay
sh install.sh --purge      # everything, including ~/.config/tuiray
```

`TUIRAY_REPO`, `TUIRAY_REF`, `TUIRAY_DIR`, `TUIRAY_BIN` override where it comes
from and where it goes; `TUIRAY_FORCE=1` lets an update discard local edits.

**From source** instead — the submodule is the part people forget:

```bash
git clone --recursive https://github.com/XuliGan4eg2006/tuiray
python3 tuiray/main.py
# in an existing clone:  git submodule update --init
```

## Run

```bash
tuiray                          # installed
python3 tuiray/main.py          # from a checkout
```

On first start it finds the `xray` binary, `configs/`, `geoip.dat` and `geosite.dat`
next to the checkout. Everything is changeable from the UI — and if there is no xray
binary at all, the menu offers to **download one** for this machine (see below).

Command line extras:

```bash
tuiray -c configs/rfn/03_RFN-_Эстония.json -m tun   # preselect
tuiray --status                                     # is it up?
tuiray --stop                                       # stop it
```

## Main screen

The menu is on the left, read-only cards on the right: **Status** (or the
preflight problems that stop a start), **Traffic**, **Watchdog** and **Access**
— as many as the terminal has room for, most important first. Below ~78 columns
the cards collapse into one summary box above the menu, and the log always keeps
the bottom of the screen.

| Row | What it does |
|-----|--------------|
| **Get xray** | only shown when there is no binary: downloads the newest release for this machine (see below) |
| **Config** | the configs page: config files, URL subscriptions and protocol links, all on one page (see below) |
| **Mode** | Proxy (local SOCKS + HTTP) or TUN (system-wide) |
| **System proxy** | point the desktop's proxy settings at our inbounds while xray runs (see below) |
| **Routing** | split-routing rules layered on top of the config (see below) |
| **Share** | hand the tunnel to other machines over Wi-Fi or ethernet (see below) |
| **Watchdog** | periodic health check on the tunnel; swaps a dead config for a working one (see below) |
| **Start / Stop / Restart** | runs `xray run -c …` and follows its log |
| **Ping configs…** | latency test over every config in the configs directory |
| **Settings…** | paths (xray binary, configs dir, the `geoip.dat` / `geosite.dat` files), ports, TUN parameters, traffic stats, watchdog, ping options, log level |

Keys: `Tab` switch between menu and log · `Enter` activate · `s` start/stop ·
`R` restart · `p` ping · `w` share · `h` watchdog · `g` settings · `c` clear log ·
`q` quit.
With the log pane focused, `↑ ↓ PgUp PgDn Home End` scroll it; `End` returns to
following the tail.

Quitting does **not** kill xray unless you ask it to — the next run re-attaches to
the process through `~/.cache/tuiray/xray.pid` and keeps showing its log.

## Proxy vs TUN

**Proxy mode** rewrites the config's inbounds to a SOCKS5 (`10808`) and an HTTP
(`10809`) listener on `127.0.0.1`. Only apps pointed at the proxy use the tunnel.
The **System proxy** row points the desktop at those ports while xray runs
(see below).

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
  server leave by; without it the tunnel would route itself in a loop. `auto` reads
  the default routes at start time and takes the lowest-metric one, skipping `tun0`
  itself — `ip route get` would answer `tun0` once the tunnel is up — and skipping
  any interface currently handed to guests, which has no way out.

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

## Configs

`Enter` on the **Config** row (or `u` from the main screen) opens the page where
every source of configs lives — the three kinds side by side, with the number of
configs each one currently has on disk:

```
   #  type              name                 configs  where
   1  url subscription  whit                      43  https://example.com/sub1
   2  url subscription  savevpn                   23  https://example.com/sub2
   3  protocol link     pasted                     4  4 links
   4  configs folder    spare                     12  /home/you/configs/spare
   5  config file       lab                        1  /home/you/configs/lab.json
   ·  everything        configs directory         82  /home/you/subextractor/configs
 Enter use · a add · u update · e edit · d remove · s show links · c use as configs dir · Esc back
```

| Key | What it does |
|-----|--------------|
| `Enter` | pick a config out of that source and run it |
| `a` | add a **config file**, a **configs folder**, a **URL subscription** or a **protocol link** |
| `u` | update: fetch a subscription again, or rewrite a link source's configs |
| `e` | edit: name / URL / headers, the stored links, or the folder |
| `d` | remove the source, and optionally the configs it wrote |
| `s` | show its configs as share links, and write them to a file |
| `c` | make that folder the configs directory the ping tool scans |

The last row is the configs directory itself. It is always there and cannot be
removed, so nothing you already have can become invisible.

| Type | Where it is kept | What `update` does |
|------|------------------|--------------------|
| **config file** | a path in `settings.json` | nothing: it is read from disk as it is |
| **configs folder** | a path in `settings.json`; every `.json` under it counts, recursively | nothing, same reason |
| **URL subscription** | `profiles.json`, shared with the command-line converter | fetches it and rewrites `configs/<name>/` |
| **protocol link** | the links themselves, in `settings.json` | rewrites `configs/<name>/` from those links |

A folder is added with `d` in the browser ("use this dir"); a path that later
disappears is shown as `— MISSING` rather than quietly counting zero.

Adding a subscription asks for the name, URL (`happ://crypt` works) and the
request headers it needs, then offers to fetch it straight away. The headers
start from Happ's own set plus an **`X-Hwid`** — 16 hex characters identifying
this machine, made once and kept in `settings.json` (*Settings → Subscriptions →
Device id*), because a subscription that counts devices should see one. Edit it
per profile in the header editor, or globally in Settings. `e` on it
reopens the same editor; `e` on a link source reopens the paste box with the
links already in it.

### Where the work happens

The Happ-converter submodule in `Happ-converter/` does what it already does well,
and tuiray does not reimplement it:

| From the submodule | Used for |
|--------------------|----------|
| `export_configs.export_for_profile` | one Xray config per subscription entry, into `configs/<profile>/` |
| `parser.parse_response` | the reverse, for the **show links** view |
| `profiles.load_profiles` / `save_profiles` | reading and writing `profiles.json` |
| `happ_decrypt.decrypt` | `happ://crypt` subscription links |

The fetch itself is tuiray's, on `urllib`, so a machine with nothing installed can
still update a subscription — and because `urllib` does not undo `Content-Encoding`
the way `requests` does, the gzip the Happ profiles ask for is unpacked here.
`requests` and `cryptography` are only needed for the parts that use them
(`profiles.py` reaches for `requests` through `ipdetect`, `happ_decrypt` for
`cryptography`); without them the profile list falls back to reading the same JSON
directly, and only `happ://crypt` links stop working.

`profiles.json` lives next to the submodule by default. To share one list with the
command-line converter, point *Settings → Subscriptions → Profiles file* at that
checkout's `profiles.json` — the two then read and write the same file, so a
profile added in the TUI shows up in the converter and the other way round.

### Protocol links

Adding a **protocol link** source opens a paste box: `Enter` starts a new line,
so a terminal paste of forty links arrives whole, and the footer counts what it
recognises as you go. One config per link is written under the configs
directory, and a single link is selected right away. The same parser handles a
base64 blob, which is what a plain subscription URL returns.

| Scheme | Notes |
|--------|-------|
| `vless://` | reality, xtls-rprx-vision, ws / grpc / xhttp / httpupgrade / kcp / tcp-http, including the `extra` obfuscation object |
| `vmess://` | the v2rayN base64-JSON form |
| `trojan://` | with the same transports |
| `ss://` | SIP002 and the older all-base64 form |
| `socks://` | with or without credentials |
| `hysteria2://` / `hy2://` | parsed, but **upstream Xray-core has no hysteria outbound** — only the Happ fork runs these, and the import says how many it wrote |

Anything else (`wireguard://` …) is reported as unsupported rather than dropped
silently. A share link cannot carry routing rules, balancers, the observatory or
`enableSessionResumption`, which is exactly why a Happ subscription is imported as
whole configs instead: use the links path for servers that arrive as links, and
`Enter` on a profile for everything else.

## Getting xray

With no binary configured, the menu grows a **Get xray** row at the top, and
pressing Start offers the same thing. *Settings → Paths → Download / update xray*
does it at any time and shows the version currently installed.

It asks the GitHub API for
[XTLS/Xray-core](https://github.com/XTLS/Xray-core/releases) and takes the newest
entry, **release or prerelease** — Xray marks every build a prerelease, so
"latest stable" would find nothing at all.

The asset is picked from `uname`, because this tool is run on boards as often as
on laptops:

| `platform.machine()` | Asset |
|----------------------|-------|
| `x86_64` / `amd64` | `Xray-linux-64.zip` |
| `i386` … `i686` | `Xray-linux-32.zip` |
| `aarch64` / `arm64` | `Xray-linux-arm64-v8a.zip` |
| `armv7l` / `armhf` | `Xray-linux-arm32-v7a.zip` |
| `armv6l` | `Xray-linux-arm32-v6.zip` |
| `armv5tel` / `armv5l` | `Xray-linux-arm32-v5.zip` |
| `riscv64`, `loongarch64`, `ppc64`, `ppc64le`, `s390x` | the matching `Xray-linux-*.zip` |
| `mips*` | `mips32`/`mips64`, with `le` chosen from the interpreter's byte order rather than from the name |

macOS, FreeBSD, OpenBSD, Windows and Android names are mapped too, and a
combination upstream does not build (`Windows/armv7l`, say) is reported as
"*tag* has no *asset*" rather than downloading something that cannot run.

The zip is checked against the `SHA2-256` line of the `.dgst` file the release
publishes beside it; a mismatch installs nothing. It also carries `geoip.dat` and
`geosite.dat`: missing ones are always installed, and existing ones are only
replaced if you say so. Everything lands next to the repo (or in
`~/.cache/tuiray/bin` when the repo is not writable), the settings are pointed at
it, and the binary is written to a temporary name and renamed into place — you
cannot get `Text file busy` by updating xray while it runs.

The download is a background thread: the bar moves, and `Esc` cancels it.

## System proxy

`Enter` on the **System proxy** row hands the desktop our inbounds; it is applied
when xray starts and undone when it stops, and toggling it while xray is already
running takes effect immediately. It does nothing in TUN mode, where the device
captures the traffic anyway.

There is no single "system proxy" on Linux, so — following
[Throne](https://github.com/throneproj/Throne) and Qv2ray before it — every store
on the machine is written at once:

| Desktop | What is written |
|---------|-----------------|
| GNOME, Cinnamon, MATE, Budgie, Xfce, and every GTK app that asks `GProxyResolver` | `org.gnome.system.proxy` via `gsettings`: `socks` → the SOCKS port, `http` / `https` / `ftp` → the HTTP port, then `mode manual` |
| KDE Plasma and Trinity | `[Proxy Settings]` in `~/.config/kioslaverc` via `kwriteconfig6`/`kwriteconfig5`: `socksProxy`, `httpProxy`, `httpsProxy`, `ftpProxy`, `ProxyType 1` — then a `org.kde.KIO.Scheduler.reparseSlaveConfiguration` D-Bus signal so running KIO workers re-read it instead of waiting for a logout |

KDE is written only when the session says KDE/Plasma/Trinity, or — when
`XDG_CURRENT_DESKTOP` was stripped, as `sudo` tends to do — when the KDE tools are
installed at all. A desktop that fails is not fatal: a GNOME box has no
`kwriteconfig` and a Plasma box may have no gsettings schema, and neither is a
reason to leave the other one unset. The log line names what was actually applied
(`system proxy: set (GNOME, KDE)`).

Where tuiray differs from Throne: **the previous state is remembered** (in
`~/.cache/tuiray/sysproxy.prev`) and put back on stop, rather than blanket-setting
"no proxy". The one exception is a previous value of *manual* — restoring that
would hand the desktop back a proxy that is no longer listening, so `none` is used
instead.

Started with `sudo` (TUN mode, sharing), the desktop commands are run back as the
invoking user with their own session bus — root has no dconf or D-Bus session of
its own, and writing there would configure a desktop nobody is looking at.

## Traffic counters

The **Traffic** card shows the live rate and the running total in both directions:

```
┌─ Traffic ──────────────────┐
│         rate     total     │
│ ↓   1.2 MB/s  472.0 MB     │
│ ↑  43.9 KB/s   12.7 MB     │
└────────────────────────────┘
```

Down is what reached your applications, up is what they sent; the totals count from
the moment xray started (re-attaching to an instance left running keeps its totals).
On a terminal too narrow for the card column the rates move into the summary box at
the top.

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

## Sharing the tunnel

`w` from the main menu (or *Share* on it) turns this machine into a gateway: other
devices join a Wi-Fi access point it raises, or plug into its ethernet port, and
reach the internet through the same xray tunnel.

The share screen holds everything you touch with a guest standing next to you —
SSID, password, band, interface, gateway address and who runs DHCP, for both
segments — editable in place with `Enter`, `s` to start or stop, and a live list of
the guests holding a lease. `g` opens the rest (lease time, static leases, upstream
DNS, channel) in Settings. Changing a setting while sharing is live offers to
rebuild it, since the running share is still on the old values.

The datapath is deliberately boring:

```
guest → wlo1 (AP) / enp42s0 → forward → masquerade into tun0 → xray → exit
```

Nothing in the xray config changes — from xray's point of view the guests are local
traffic. That also means **sharing needs TUN mode**: proxy mode has no device to
route anyone into.

### Wi-Fi

The Wi-Fi card becomes the access point, which means it stops being a client of your
own network — so **the uplink has to be something else**, an ethernet cable or a USB
NIC. tuiray refuses to start rather than cutting the branch it is sitting on:

```
no uplink left once these interfaces are shared - plug in ethernet before
turning the Wi-Fi into an AP
```

The AP is a NetworkManager profile (`tuiray-ap`, WPA2/CCMP, `autoconnect no`), so no
hostapd is involved. If xray was reaching the internet through that card, the tunnel
is rebuilt against the remaining uplink as soon as the AP is up.

### DHCP and DNS

Each segment picks who hands out addresses:

| Setting | What happens |
|---------|--------------|
| `dnsmasq (tuiray)` | tuiray writes the config and supervises one dnsmasq for every segment. Range, lease time, static leases and upstream DNS are yours, and the *Guests* list shows live leases. |
| `NetworkManager shared` | `ipv4.method shared` does DHCP and NAT itself. Nothing to configure, but no client list and no static leases. |
| `nobody` | guests set an address by hand. |

Guest DNS goes to the segment's own address and dnsmasq forwards it to the resolvers
in *Guest DNS* (`1.1.1.1` by default). Those queries leave the host, so the tunnel
resolves them — the one thing worth getting right, since the obvious alternative is
guests quietly using the DNS of the network you are sitting on.

### What guests may and may not do

| Guard | Default | Effect |
|-------|---------|--------|
| Isolate guests | on | no guest-to-guest, no crossing between segments, and no reaching any private range — your LAN, the docker bridges and the host's own services stay out of reach. Guests get DHCP, DNS and ping from this machine, nothing else. Two clients of the same radio are bridged inside the AP, below the IP stack where nftables never sees them, so the hotspot also gets `802-11-wireless.ap-isolation`. |
| Kill-switch | on | a guest packet may leave only by the tunnel device. Stop xray and guests get nothing rather than a leak out of the naked uplink. |
| MSS clamp | on | the tunnel's MTU is not the guest's MTU. |
| IPv6 | off | shared segments are v4-only; a v6 address would be a second, untunnelled path. |

### What it touches, and how it is undone

| | |
|---|---|
| `nft` table `inet tuiray` | NAT, isolation, kill-switch, clamp — rendered to `~/.cache/tuiray/share.nft` and applied with `nft -f`, so it swings over atomically |
| NetworkManager profiles | `tuiray-ap`, `tuiray-lan` |
| one dnsmasq | config, leases and log under `~/.cache/tuiray/` |
| `net.ipv4.ip_forward` | set to 1, restored to whatever it was |
| two `iptables` rules per segment | only when the FORWARD policy is DROP (Docker does that) — an `accept` in our table cannot override a `drop` in another one |

Every step is written to `~/.cache/tuiray/share.json` the moment it succeeds, and
stopping undoes exactly what that file lists. A start that fails halfway therefore
cleans up after itself, and so does the next run after a crash — teardown never
depends on remembering anything in memory. Quitting asks whether to take the share
down, exactly as it does for xray.

### Privileges

Raising an interface, editing the firewall and binding DHCP all need root. Run the
whole TUI with `sudo` and it never asks; run it as your user and it asks for a sudo
password once, keeping it in memory for the session — the same deal TUN mode already
makes. Started through `sudo`, tuiray still reads and writes settings as *you*, not
as root, so nothing in `~/.config` or `~/.cache` changes hands.

### Not in this version

AP and client mode on the *same* card at once (it needs a second virtual interface
plus hostapd, and one shared channel), transparent proxying for guests while the host
stays in proxy mode, and per-client traffic counters.

## Watchdog

Off by default. Turn it on with `h`, or from the **Watchdog** row (`Enter` →
*Turn the watchdog on* / *Check now* / *Watchdog settings…*). Every
`health.interval` seconds it runs two probes and writes what it finds to the log
pane — and it checks a few seconds after you start or stop xray too, rather than
leaving the **Watchdog** card stale for a whole interval. The card counts down to
the next check (`checking…` while one is running, `paused` while the ping tool
has the machine):

| Probe | How | Setting |
|-------|-----|---------|
| **proxy** | a request through the running xray — via the local SOCKS inbound, or plain when TUN mode has no inbound to use | *Proxy check URL* |
| **direct** | the same request made *without* the proxy | *Direct check URL* |

The second one exists to place the blame. A dead proxy and a dead connection
look identical from inside the tunnel, and cycling through every config in the
directory because the Wi-Fi dropped is a waste. With *Only switch while the
direct link works* on (the default), no connection at all means the current
config is left alone.

> In TUN mode the device captures everything, so "without the proxy" is a white
> lie: unless the URL's host is matched by a direct-routing rule, that probe
> travels through the tunnel too and will simply agree with the first one.

After *Failures before acting* consecutive failed proxy checks (default 2) the
switch begins:

1. Candidates are the other `*.json` under the configs directory, ordered best
   bet first — ones that answered before, then untried ones, then the ones that
   failed most recently.
2. Each is probed in its own throwaway xray on a free port, exactly as the ping
   tool does. *Configs tried per switch* caps how many (`0` = all of them).
3. The first one that answers becomes the selected config and xray is
   restarted on it. If it starts but still does not answer within a few
   seconds, it is marked bad and the next candidate is tried.
4. If none answers, the original config is put back and the state reads
   *no working config*.

In **TUN mode** the tunnel is taken down before the candidates are probed:
the device would otherwise swallow the probes' own traffic and measure every
candidate through the tunnel that just died. It comes back up on the winner, or
on the original config when nothing works.

*Cooldown between switches* (default 60 s) keeps a flapping link from walking
through the whole directory in a minute. The ping tool suspends the watchdog
while it is open — both want a private xray and a port.

TUN mode needs root to restart xray. The watchdog will not take a working
tunnel down unless sudo can be used unattended (a credential cached by an
earlier start, or a passwordless sudo); otherwise it reports the problem and
leaves everything alone.

| Setting | Default |
|---------|---------|
| Watch the connection | off |
| Check interval | `30` s |
| Proxy check URL | `http://cp.cloudflare.com/generate_204` |
| Check the direct connection too | on |
| Direct check URL | `http://cp.cloudflare.com/generate_204` |
| Check timeout | `6` s |
| Failures before acting | `2` |
| Switch to a working config | on |
| Only switch while the direct link works | on |
| Cooldown between switches | `60` s |
| Configs tried per switch | all |

## Ping tool

`p` from the main menu tests **the group the selected config is in** — a group being
one folder under the configs directory, which is exactly what a subscription exports
into. Each probe is a process and a request, so testing all 200 configs on the
machine because you wanted to know how `whit` is doing today is not a good trade.

```
  Ping — whit — 43/43 tested · mode: proxy latency (starts xray per config)
   #  config                                                result  detail
   1  02_Эстония_Скоростной                                  118 ms
   2  00_Авто_Быстрый                                        141 ms
 fastest: whit/02_Эстония_Скоростной (118 ms) · 176 more in other groups (g)
```

| Key | What it does |
|-----|--------------|
| `g` | switch group — or *all groups*, the old behaviour |
| `p` | test the group again |
| `r` | test just the highlighted config |
| `Enter` | select the highlighted config |
| `f` | select the fastest one measured so far |
| `m` / `s` / `c` | probe mode, sort order, cancel what is running |

The status line says how many configs live in the other groups. If the selected
config sits outside the configs directory, its own folder comes along as a group.

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
| `tuiray/Happ-converter/profiles.json` | subscription profiles (URL + headers); the path is a setting |
| `~/.cache/tuiray/run.json` | the config actually handed to xray (original + rewritten inbounds) |
| `~/.cache/tuiray/xray.log` | xray output, tailed by the log pane |
| `~/.cache/tuiray/xray.pid` | pid/mode/config of the running instance, for re-attaching |
| `~/.cache/tuiray/assets/` | symlinks named `geoip.dat` / `geosite.dat`, passed as `XRAY_LOCATION_ASSET` |
| `~/.cache/tuiray/sysproxy.prev` | the desktop proxy settings from before we touched them |
| `~/.cache/tuiray/share.json` | what sharing changed on the machine, so it can be undone |
| `~/.cache/tuiray/share.nft` | the generated nft ruleset, exactly as applied |
| `~/.cache/tuiray/dnsmasq.{conf,leases,log}` | the DHCP/DNS server for the shared segments |

The selected config is never modified: it is copied, its `inbounds` replaced for the
mode, and the copy is what runs. Routing, DNS, balancers and outbounds are untouched.

## Layout

```
install.sh    one-line installer: clone, link, update, remove
main.py       entry point, CLI flags
app.py        main screen: status, menu, log pane
screens.py    ping tool and settings screens
ui.py         curses widgets: frames, popups, file browser
runner.py     process lifecycle, log tail, traffic stats poller, sudo/setcap, system proxy
pinger.py     SOCKS5 and TCP latency probes, worker pool
watchdog.py   periodic proxy/internet health checks and the config switch
share.py      Wi-Fi AP and wired sharing: NM profiles, dnsmasq, nft, teardown
xrayconf.py   config loading, inbound rewriting for proxy/TUN, stats API block
xrayget.py    platform detection and the release download for the xray binary
subs.py       the Happ-converter bridge: profiles, fetching, writing configs
links.py      protocol:// share links -> xray configs
Happ-converter/  the submodule: subscription parsing and config export
settings.py   persisted settings
```
