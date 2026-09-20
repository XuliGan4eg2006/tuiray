"""Share the tunnel with other machines: a Wi-Fi access point and/or a wired LAN.

The datapath is deliberately boring. Guests land on an interface this machine
owns, their packets are forwarded, and POSTROUTING masquerades them into the
device xray's TUN inbound listens on -- so from xray's point of view they are
indistinguishable from local traffic, and nothing in the xray config changes.
That also means sharing only works in TUN mode; proxy mode would need a
transparent-proxy inbound instead, which is a separate job.

Three things own state on the machine while sharing is up, and all three are
undone by stop(), which is safe to call at any time (including at startup,
against the state file a crashed session left behind):

  * NetworkManager connection profiles, one per shared segment. The Wi-Fi one
    turns the card into an AP, so it stops being a client of your own network
    -- the uplink has to be something else, which preflight() insists on.
  * one dnsmasq, serving DHCP and DNS to every segment at once (unless a
    segment delegates DHCP to NetworkManager's own shared mode).
  * one nft table, `inet tuiray`, holding NAT, guest isolation and the
    kill-switch. A single `nft delete table` removes the lot.

Docker complicates the last point: it sets the filter FORWARD policy to DROP,
and an `accept` in our own table cannot override a `drop` in another one. Where
that policy is DROP we insert two explicit iptables rules per segment and
remember them for teardown.
"""

import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import runner
from settings import CACHE_DIR, chown_back, mkdir_owned

NFT_TABLE = "tuiray"
NM_PROFILE = {"wifi": "tuiray-ap", "lan": "tuiray-lan"}

STATE_FILE = CACHE_DIR / "share.json"
NFT_RULES = CACHE_DIR / "share.nft"
DNSMASQ_CONF = CACHE_DIR / "dnsmasq.conf"
DNSMASQ_LEASES = CACHE_DIR / "dnsmasq.leases"
DNSMASQ_LOG = CACHE_DIR / "dnsmasq.log"

# Destinations a guest has no business reaching through us: every private range,
# which covers this machine's own LANs, the docker bridges and the other shared
# segment, without having to enumerate them.
PRIVATE_DESTINATIONS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                        "100.64.0.0/10", "169.254.0.0/16"]


class ShareError(RuntimeError):
    """Sharing cannot start (or could not be torn down) for a stated reason."""


# -- system helpers -------------------------------------------------------

def _run(command: list, timeout: int = 15) -> tuple:
    """Run an unprivileged command; (ok, output)."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout or f"exit {done.returncode}").strip()
    return True, done.stdout.strip()


def interfaces() -> list:
    """[(name, kind, state)] for every interface that could carry a segment."""
    ok, out = _run(["ip", "-o", "link", "show"])
    if not ok:
        return []
    found = []
    for line in out.splitlines():
        match = re.match(r"\d+:\s+([^:@]+)[:@]", line)
        if not match:
            continue
        name = match.group(1)
        if name == "lo" or name.startswith(("tun", "docker", "br-", "veth")):
            continue
        # "state UP" is not the test: a USB NIC that is perfectly up reports
        # UNKNOWN. The UP flag in <BROADCAST,MULTICAST,UP,LOWER_UP> is.
        flags = re.search(r"<([^>]*)>", line)
        flags = flags.group(1).split(",") if flags else []
        state = "up" if "UP" in flags else "down"
        kind = "wifi" if Path(f"/sys/class/net/{name}/wireless").exists() else "lan"
        found.append((name, kind, state))
    return found


def wifi_can_ap(iface: str) -> bool:
    """Does NetworkManager think this card can be an access point?"""
    ok, out = _run(["nmcli", "-f", "WIFI-PROPERTIES.AP", "device", "show", iface])
    return ok and "yes" in out.lower()


def uplink_interface(exclude=()) -> str:
    """What still reaches the internet once these interfaces are shared.

    The question is never "what is the best route now" but "what is left after
    the Wi-Fi card stops being a client", which is exactly what the runner's
    exclusion-aware lookup answers.
    """
    return runner.detect_default_interface(exclude, max_age=2.0)


def local_networks() -> list:
    """(network, interface) for every IPv4 subnet this machine is attached to."""
    ok, out = _run(["ip", "-4", "-o", "route", "show", "scope", "link"])
    if not ok:
        return []
    nets = []
    for line in out.splitlines():
        device = re.search(r"\bdev\s+(\S+)", line)
        try:
            network = ipaddress.ip_network(line.split()[0], strict=False)
        except (ValueError, IndexError):
            continue
        nets.append((network, device.group(1) if device else ""))
    return nets


def forward_policy_drops(password=None) -> bool:
    """True when something (Docker, usually) made the FORWARD policy DROP."""
    ok, out = runner.sudo_run(["iptables", "-S", "FORWARD"], password, timeout=10)
    return ok and out.splitlines()[:1] == ["-P FORWARD DROP"]


# -- one shared network ---------------------------------------------------

class Segment:
    """One shared network: an interface, its address, and how guests get one."""

    def __init__(self, kind: str, config: dict):
        self.kind = kind                                  # "wifi" | "lan"
        self.config = config
        self.iface = (config.get("iface") or "").strip()
        self.address = (config.get("address") or "").strip()
        self.dhcp = config.get("dhcp", "dnsmasq")         # dnsmasq | nm | off
        self.profile = NM_PROFILE[kind]

    @property
    def interface(self):
        return ipaddress.ip_interface(self.address)

    @property
    def network(self):
        return self.interface.network

    @property
    def ip(self) -> str:
        return str(self.interface.ip)

    @property
    def range(self) -> tuple:
        first, last = (self.config.get("range") or ["", ""])[:2]
        return first, last

    def label(self) -> str:
        if self.kind == "wifi":
            return f"{self.config.get('ssid') or '(no ssid)'} on {self.iface}"
        return f"wired {self.iface}"

    def problems(self) -> list:
        """Everything wrong with this segment's own settings."""
        issues = []
        if not self.iface:
            issues.append(f"{self.kind}: no interface selected")
        elif not Path(f"/sys/class/net/{self.iface}").exists():
            issues.append(f"{self.kind}: interface {self.iface} does not exist")
        try:
            network = self.network
        except ValueError:
            return issues + [f"{self.kind}: {self.address!r} is not an address/prefix"]
        if network.prefixlen > 30:
            issues.append(f"{self.kind}: {self.address} leaves no room for guests")

        if self.dhcp == "dnsmasq":
            for label, value in zip(("range start", "range end"), self.range):
                try:
                    if ipaddress.ip_address(value) not in network:
                        issues.append(f"{self.kind}: DHCP {label} {value} is outside {network}")
                except ValueError:
                    issues.append(f"{self.kind}: DHCP {label} {value!r} is not an address")

        if self.kind == "wifi":
            password = self.config.get("password") or ""
            if not 8 <= len(password) <= 63:
                issues.append("wifi: WPA2 needs a password of 8 to 63 characters")
            if not (self.config.get("ssid") or "").strip():
                issues.append("wifi: no SSID")
            if self.iface and not wifi_can_ap(self.iface):
                issues.append(f"wifi: {self.iface} does not support AP mode")
        return issues


def first_interface(kind: str) -> str:
    """The obvious interface of a kind, for a user who has not picked one."""
    for name, found_kind, _ in interfaces():
        if found_kind == kind:
            return name
    return ""


def enabled_segments(settings) -> list:
    """The segments the user turned on, in bring-up order."""
    found = []
    for kind in ("wifi", "lan"):
        config = dict(settings.get(f"share.{kind}", {}) or {})
        if not config.get("enabled"):
            continue
        if not (config.get("iface") or "").strip():
            config["iface"] = first_interface(kind)
        found.append(Segment(kind, config))
    return found


# -- firewall -------------------------------------------------------------

def _nft_set(values) -> str:
    return "{ " + ", ".join(values) + " }"


def render_ruleset(segments: list, tun: str, isolate: bool, killswitch: bool,
                   clamp: bool) -> str:
    """The whole `inet tuiray` table as one nft script.

    Written as a file and applied with `nft -f`, so the table swings over
    atomically and a half-applied ruleset cannot exist. The create-then-delete
    prologue is the idiomatic way to make that file safe to re-run.
    """
    shared = _nft_set(f'"{segment.iface}"' for segment in segments)
    lines = [
        "# generated by tuiray -- edits are overwritten on every start",
        f"table inet {NFT_TABLE}",
        f"delete table inet {NFT_TABLE}",
        "",
        f"table inet {NFT_TABLE} {{",
        "    chain input {",
        "        type filter hook input priority -10; policy accept;",
        f"        iifname {shared} ct state established,related accept",
        f"        iifname {shared} udp dport {{ 53, 67 }} accept",
        f"        iifname {shared} tcp dport 53 accept",
        f"        iifname {shared} icmp type echo-request accept",
        "        # guests get DHCP, DNS and ping from us, and nothing else",
        f"        iifname {shared} drop",
        "    }",
        "",
        "    chain forward {",
        "        type filter hook forward priority -10; policy accept;",
        "        # no IPv6 on a shared segment: it would route around the tunnel",
        f"        meta nfproto ipv6 iifname {shared} drop",
    ]
    if isolate:
        lines += [
            "        # guest to guest, and segment to segment, stay apart",
            f"        iifname {shared} oifname {shared} drop",
            "        # and no reaching the networks this machine is attached to",
            f"        iifname {shared} ip daddr {_nft_set(PRIVATE_DESTINATIONS)} drop",
        ]
    if killswitch:
        lines += [
            "        # without the tunnel a guest gets nothing, rather than a leak",
            f'        iifname {shared} oifname != "{tun}" drop',
        ]
    if clamp:
        lines += [
            "        # the tunnel's MTU is not the guest's MTU",
            f"        iifname {shared} tcp flags syn tcp option maxseg size set rt mtu",
        ]
    lines += ["    }", "", "    chain postrouting {",
              "        type nat hook postrouting priority srcnat; policy accept;"]
    for segment in segments:
        lines.append(f'        ip saddr {segment.network} oifname "{tun}" masquerade')
    lines += ["    }", "}", ""]
    return "\n".join(lines)


# -- dnsmasq --------------------------------------------------------------

def render_dnsmasq(segments: list) -> str:
    """One dnsmasq config serving every segment that wants our DHCP.

    bind-dynamic rather than bind-interfaces: it binds the shared interfaces
    only, which keeps systemd-resolved's 127.0.0.53 listener untouched, and it
    copes with an interface that appears after dnsmasq starts.
    """
    lines = ["# generated by tuiray -- edits are overwritten on every start",
             "bind-dynamic", "except-interface=lo", "no-resolv", "no-hosts",
             "domain-needed", "bogus-priv", "dhcp-authoritative",
             "cache-size=1000",
             f"dhcp-leasefile={DNSMASQ_LEASES}", f"log-facility={DNSMASQ_LOG}",
             "log-dhcp", ""]
    upstreams = []
    for segment in segments:
        for server in segment.config.get("dns") or []:
            if server not in upstreams:
                upstreams.append(server)
    # These queries leave the host, so the tunnel resolves them: the point of
    # not letting guests keep the DNS server of the network we are sitting on.
    lines += [f"server={server}" for server in upstreams] + [""]

    for segment in segments:
        tag = segment.kind
        first, last = segment.range
        netmask = segment.network.netmask
        lease = segment.config.get("lease") or "12h"
        lines += [
            f"# {segment.label()}",
            f"interface={segment.iface}",
            f"dhcp-range=set:{tag},{first},{last},{netmask},{lease}",
            f"dhcp-option=tag:{tag},option:router,{segment.ip}",
            f"dhcp-option=tag:{tag},option:dns-server,{segment.ip}",
        ]
        for reservation in segment.config.get("static") or []:
            lines.append(f"dhcp-host={reservation}")
        lines.append("")
    return "\n".join(lines)


def leases() -> list:
    """Current DHCP leases: [{mac, ip, name, expires}], newest first.

    dnsmasq writes the file as root; it is only ever read here.
    """
    try:
        raw = DNSMASQ_LEASES.read_text(encoding="utf-8")
    except OSError:
        return []
    found = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        expiry, mac, address, name = parts[0], parts[1], parts[2], parts[3]
        try:
            expires = int(expiry)
        except ValueError:
            continue
        found.append({"mac": mac, "ip": address,
                      "name": "" if name == "*" else name,
                      "expires": expires,
                      "seconds_left": max(0, expires - int(time.time()))})
    return sorted(found, key=lambda entry: entry["ip"])


def neighbours() -> dict:
    """MAC -> reachability, so the client list can show who is actually there."""
    ok, out = _run(["ip", "-4", "neigh", "show"])
    if not ok:
        return {}
    state = {}
    for line in out.splitlines():
        parts = line.split()
        if "lladdr" in parts:
            state[parts[parts.index("lladdr") + 1].lower()] = parts[-1].lower()
    return state


# -- the manager ----------------------------------------------------------

class ShareManager:
    """Brings the segments up and, just as importantly, takes them down again.

    Every privileged step is recorded in ~/.cache/tuiray/share.json as soon as
    it succeeds, and stop() undoes exactly what that file lists. A start that
    fails halfway therefore cleans up after itself, and so does the next run
    after a crash -- teardown never depends on remembering anything in memory.
    """

    def __init__(self, settings, log):
        self.settings = settings
        self.log = log
        self._password = None
        self.state = self._load_state()

    # -- privilege --------------------------------------------------------
    def needs_password(self) -> bool:
        return (os.geteuid() != 0 and not self._credential()
                and not runner.sudo_ready())

    def _credential(self):
        return self._password or runner.cached_password()

    def _root(self, command: list, timeout: int = 20, quiet: bool = False) -> tuple:
        ok, message = runner.sudo_run(command, self._credential(), timeout=timeout)
        if not ok and not quiet:
            self.log.note(f"share: {shlex.join(command)} -> {message}")
        return ok, message

    def _root_argv(self, command: list) -> tuple:
        """(argv, stdin) for a long-running privileged child."""
        if os.geteuid() == 0:
            return command, None
        password = self._credential()
        if password:
            return ["sudo", "-S", "-p", ""] + command, password + "\n"
        return ["sudo", "-n"] + command, None

    # -- state ------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        try:
            mkdir_owned(CACHE_DIR)
            STATE_FILE.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            chown_back(STATE_FILE)
        except OSError:
            pass

    def is_active(self) -> bool:
        return bool(self.state.get("segments"))

    def summary(self) -> str:
        """One line for the main menu."""
        parts = []
        for kind in ("wifi", "lan"):
            config = self.settings.get(f"share.{kind}", {}) or {}
            live = any(s["kind"] == kind for s in self.state.get("segments") or [])
            if not config.get("enabled"):
                parts.append(f"{kind} off")
            elif kind == "wifi":
                parts.append(f"wifi {config.get('ssid') or '?'}"
                             + (" · live" if live else " · stopped"))
            else:
                parts.append(f"lan {config.get('iface') or '?'}"
                             + (" · live" if live else " · stopped"))
        if self.is_active():
            count = len(leases())
            parts.append(f"{count} client{'' if count == 1 else 's'}")
        return " · ".join(parts)

    # -- preflight --------------------------------------------------------
    def preflight(self, xray, segments: list) -> list:
        problems = []
        if not segments:
            problems.append("no segment is enabled - turn on Wi-Fi AP or wired LAN")
        if not xray.is_running():
            problems.append("xray is not running - guests would have nowhere to go")
        elif xray.running_mode() != "tun":
            problems.append("sharing needs TUN mode (proxy mode has no device to "
                            "route guests into)")

        for segment in segments:
            problems += segment.problems()

        shared = {segment.iface for segment in segments}
        if not uplink_interface(exclude=shared):
            problems.append("no uplink left once these interfaces are shared - "
                            "plug in ethernet before turning the Wi-Fi into an AP")

        # A guest subnet overlapping something already routed here leaves the
        # machine unable to answer either network. Addresses on the interfaces
        # being shared do not count: they are about to go away, and one of them
        # is usually our own leftover from a previous run.
        existing = [(net, device) for net, device in local_networks()
                    if device not in shared]
        for segment in segments:
            try:
                network = segment.network
            except ValueError:
                continue
            for other, device in existing:
                if network.overlaps(other):
                    problems.append(f"{segment.kind}: {network} overlaps {other} "
                                    f"on {device}, which this machine already routes")

        for tool in ("nft", "nmcli"):
            if not _which(tool):
                problems.append(f"{tool} is not installed")
        if any(segment.dhcp == "dnsmasq" for segment in segments):
            if not _which("dnsmasq"):
                problems.append("dnsmasq is not installed "
                                "(apt install dnsmasq-base), or use DHCP: nm")
            elif _service_active("dnsmasq"):
                problems.append("the system dnsmasq service is running and would "
                                "fight ours - stop it, or use DHCP: nm")
        return problems

    # -- lifecycle --------------------------------------------------------
    def start(self, xray, password: str = None) -> None:
        if password:
            self._password = password
            runner.remember_password(password)
        segments = enabled_segments(self.settings)
        for segment in segments:
            # An "auto" interface is written back, so the rest of the app --
            # and the next run -- knows which device is in guest hands.
            if self.settings.get(f"share.{segment.kind}.iface") != segment.iface:
                self.settings.set(f"share.{segment.kind}.iface", segment.iface)
        problems = self.preflight(xray, segments)
        if problems:
            raise ShareError("; ".join(problems))
        if self.needs_password():
            raise runner.NeedsPassword("sharing needs root")

        self.stop()                      # leftovers from a crashed run, if any
        self.log.note("sharing: starting " + ", ".join(s.label() for s in segments))
        try:
            for segment in segments:
                self._profile_up(segment)
                self.state.setdefault("segments", []).append(
                    {"kind": segment.kind, "iface": segment.iface,
                     "profile": segment.profile, "network": str(segment.network),
                     "dhcp": segment.dhcp, "label": segment.label()})
                self._save_state()
            self._apply_firewall(segments)
            serve = [s for s in segments if s.dhcp == "dnsmasq"]
            if serve:
                self._start_dnsmasq(serve)
        except ShareError:
            self.stop()
            raise
        self.log.note(f"sharing: up · uplink {uplink_interface(exclude={s.iface for s in segments})}")

    def stop(self, password: str = None) -> None:
        """Undo whatever the state file says we did. Safe to call any time."""
        if password:
            self._password = password
            runner.remember_password(password)
        if not self.state:
            return
        self._stop_dnsmasq()
        self._clear_firewall()
        for record in list(self.state.get("segments") or []):
            self._profile_down(record.get("profile"))
        had = bool(self.state.get("segments"))
        self.state = {}
        self._save_state()
        if had:
            self.log.note("sharing: stopped")

    def xray_restart_needed(self, xray) -> bool:
        """Is the running xray bound to an interface we have just shared?

        Its own connections leave by that device; once the card is an access
        point there is no way out through it, so the tunnel has to be rebuilt
        against whatever uplink is left.
        """
        bound = (xray.meta.get("tun_interface") or "").strip()
        shared = {record.get("iface") for record in self.state.get("segments") or []}
        return bool(bound) and bound in shared

    # -- NetworkManager profiles ------------------------------------------
    def _profile_up(self, segment: Segment) -> None:
        self._profile_down(segment.profile)
        args = ["nmcli", "connection", "add",
                "type", "wifi" if segment.kind == "wifi" else "ethernet",
                "ifname", segment.iface, "con-name", segment.profile,
                "autoconnect", "no",
                # Never let a guest segment become this machine's way out.
                "ipv4.never-default", "yes",
                # No IPv6 either: guests would get a second, untunnelled path.
                "ipv6.method", "disabled"]
        if segment.kind == "wifi":
            args += ["ssid", segment.config.get("ssid") or "",
                     "802-11-wireless.mode", "ap",
                     "802-11-wireless.band", segment.config.get("band") or "bg",
                     "802-11-wireless.hidden",
                     "yes" if segment.config.get("hidden") else "no",
                     "802-11-wireless-security.key-mgmt", "wpa-psk",
                     "802-11-wireless-security.proto", "rsn",
                     "802-11-wireless-security.pairwise", "ccmp",
                     "802-11-wireless-security.group", "ccmp",
                     "802-11-wireless-security.psk", segment.config.get("password") or ""]
            channel = int(segment.config.get("channel") or 0)
            if channel:
                args += ["802-11-wireless.channel", str(channel)]
            if self.settings.get("share.isolate", True):
                # The nft rules cannot see two clients of one radio talking to
                # each other: the AP bridges those frames in mac80211, below
                # the IP stack. Only the AP itself can refuse to do it.
                args += ["802-11-wireless.ap-isolation", "yes"]
        if segment.dhcp == "nm":
            # NetworkManager runs its own dnsmasq and NAT for a shared method;
            # ipv4.dns is what it forwards guest queries to, and pointing that
            # at a public resolver is what keeps guest DNS inside the tunnel.
            args += ["ipv4.method", "shared", "ipv4.addresses", segment.address]
            servers = segment.config.get("dns") or []
            if servers:
                args += ["ipv4.dns", ",".join(servers)]
        else:
            args += ["ipv4.method", "manual", "ipv4.addresses", segment.address]

        ok, message = self._root(args)
        if not ok:
            raise ShareError(f"{segment.kind}: creating the profile failed: {message}")
        ok, message = self._root(["nmcli", "connection", "up", segment.profile],
                                 timeout=60)
        if not ok:
            raise ShareError(f"{segment.kind}: {segment.iface} would not come up: {message}")

    def _profile_down(self, profile: str) -> None:
        if not profile:
            return
        self._root(["nmcli", "connection", "down", profile], quiet=True)
        self._root(["nmcli", "connection", "delete", profile], quiet=True)

    # -- firewall ---------------------------------------------------------
    def _apply_firewall(self, segments: list) -> None:
        tun = self.settings.get("tun.name") or "tun0"
        text = render_ruleset(
            segments, tun,
            isolate=bool(self.settings.get("share.isolate", True)),
            killswitch=bool(self.settings.get("share.killswitch", True)),
            clamp=bool(self.settings.get("share.mss_clamp", True)))
        try:
            mkdir_owned(CACHE_DIR)
            NFT_RULES.write_text(text, encoding="utf-8")
            chown_back(NFT_RULES)
        except OSError as exc:
            raise ShareError(f"cannot write {NFT_RULES}: {exc}") from exc

        ok, message = self._root(["nft", "-f", str(NFT_RULES)])
        if not ok:
            raise ShareError(f"nft: {message}")
        self.state["nft"] = True
        self._save_state()

        previous = _sysctl_forward()
        if previous != "1":
            ok, _ = self._root(["sysctl", "-qw", "net.ipv4.ip_forward=1"])
            if not ok:
                raise ShareError("could not enable IPv4 forwarding")
            self.state["ip_forward_was"] = previous
            self._save_state()

        # Docker's FORWARD policy is DROP, and a drop in one table beats an
        # accept in ours, so the forwarded flow needs saying so explicitly.
        if forward_policy_drops(self._credential()):
            self.log.note("share: FORWARD policy is DROP (docker?), adding accepts")
            for segment in segments:
                for rule in (["-i", segment.iface, "-o", tun, "-j", "ACCEPT"],
                             ["-i", tun, "-o", segment.iface, "-m", "conntrack",
                              "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"]):
                    ok, _ = self._root(["iptables", "-I", "FORWARD", "1"] + rule)
                    if ok:
                        self.state.setdefault("iptables", []).append(rule)
                        self._save_state()

    def _clear_firewall(self) -> None:
        for rule in reversed(self.state.get("iptables") or []):
            self._root(["iptables", "-D", "FORWARD"] + rule, quiet=True)
        self.state.pop("iptables", None)
        if self.state.get("nft"):
            self._root(["nft", "delete", "table", "inet", NFT_TABLE], quiet=True)
            self.state.pop("nft", None)
        previous = self.state.pop("ip_forward_was", None)
        if previous is not None:
            self._root(["sysctl", "-qw", f"net.ipv4.ip_forward={previous}"], quiet=True)
        self._save_state()

    # -- dnsmasq ----------------------------------------------------------
    def _start_dnsmasq(self, segments: list) -> None:
        try:
            mkdir_owned(CACHE_DIR)
            DNSMASQ_CONF.write_text(render_dnsmasq(segments), encoding="utf-8")
            chown_back(DNSMASQ_CONF)
            DNSMASQ_LEASES.touch(exist_ok=True)
        except OSError as exc:
            raise ShareError(f"cannot write {DNSMASQ_CONF}: {exc}") from exc

        binary = _which("dnsmasq") or "/usr/sbin/dnsmasq"
        argv, stdin_data = self._root_argv(
            [binary, "--keep-in-foreground", f"--conf-file={DNSMASQ_CONF}"])
        try:
            sink = open(DNSMASQ_LOG, "a", buffering=1, encoding="utf-8")
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=sink,
                                       stderr=subprocess.STDOUT, text=True,
                                       start_new_session=True)
            sink.close()
        except OSError as exc:
            raise ShareError(f"cannot launch dnsmasq: {exc}") from exc
        if stdin_data:
            try:
                process.stdin.write(stdin_data)
                process.stdin.flush()
            except OSError:
                pass
        try:
            process.stdin.close()
        except OSError:
            pass

        time.sleep(0.6)
        if process.poll() is not None:
            raise ShareError("dnsmasq exited immediately: " + _tail(DNSMASQ_LOG))
        self.state["dnsmasq_pid"] = process.pid
        self._save_state()
        self.log.note("share: dnsmasq serving "
                      + ", ".join(s.iface for s in segments))

    def _dnsmasq_alive(self) -> bool:
        pid = self.state.get("dnsmasq_pid")
        return bool(pid) and runner.pid_alive(pid)

    def _stop_dnsmasq(self) -> None:
        pid = self.state.pop("dnsmasq_pid", None)
        if not pid:
            return
        if runner.pid_alive(pid):
            # Under sudo our child is sudo itself; dnsmasq is below it.
            targets = [str(child) for child in runner._descendants(pid)] + [str(pid)]
            ok, _ = self._root(["kill"] + targets, quiet=True)
            if not ok:
                self._root(["pkill", "-f", f"conf-file={DNSMASQ_CONF}"], quiet=True)
        self._save_state()

    # -- reporting --------------------------------------------------------
    def status(self) -> dict:
        segments = self.state.get("segments") or []
        return {
            "active": bool(segments),
            "segments": segments,
            "dnsmasq": self._dnsmasq_alive(),
            "clients": self.clients(),
        }

    def clients(self) -> list:
        """Leases, annotated with whether the kernel still sees the device."""
        reachable = neighbours()
        found = []
        for lease in leases():
            lease["state"] = reachable.get(lease["mac"].lower(), "")
            found.append(lease)
        return found


# -- small utilities ------------------------------------------------------

def _which(name: str) -> str:
    """shutil.which, plus the sbin directories a plain user does not have."""
    found = shutil.which(name)
    if found:
        return found
    for directory in ("/usr/sbin", "/sbin", "/usr/local/sbin"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def _service_active(unit: str) -> bool:
    ok, out = _run(["systemctl", "is-active", unit], timeout=5)
    return ok and out.strip() == "active"


def _sysctl_forward() -> str:
    try:
        return Path("/proc/sys/net/ipv4/ip_forward").read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def _tail(path: Path, lines: int = 3) -> str:
    try:
        return " / ".join(path.read_text(encoding="utf-8",
                                         errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no output)"
