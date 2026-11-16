#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""zabbix-matrix-rain: "digital rain" in a terminal, driven by Zabbix.

Every monitored host owns a band of screen columns:

* CPU load      -> how many streams fall in the band and how fast;
* network load  -> how long the streams are;
* open problems -> the band is tinted with the severity colour;
* a NEW problem -> the screen glitches for a moment and the problem
                   is shown in the status line.

Now and then a stream "decodes" and spells the host name and its load.

Only the Python 3.8+ standard library is used. Run with --demo to see it
without a Zabbix server, or with --check to test the connection.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import locale
import math
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__version__ = "0.1.0"

SEVERITY_NAMES = ["not classified", "information", "warning", "average", "high", "disaster"]
SEVERITY_SHORT = ["N", "I", "W", "A", "H", "D"]

# Percent CPU utilisation: Linux/Windows agent "system.cpu.util" and SNMP
# templates "system.cpu.util[hrProcessorLoad.N]"; per-mode keys such as
# "system.cpu.util[,user]" are skipped because of the comma.
DEFAULT_CPU_KEY = r"^system\.cpu\.util(\[[^,\]]*\])?$"
# Interface traffic with a single parameter: net.if.in["eth0"],
# net.if.out[ifHCOutOctets.3]; errors/drops (second parameter) are skipped.
DEFAULT_NET_KEY = r"^net\.if\.(in|out)\[[^,\]]+\]$"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Host:
    hostid: str
    name: str
    cpu: Optional[float] = None      # percent, 0..100; None = no fresh data
    net: Optional[float] = None      # bits per second, in + out
    severity: int = -1               # highest open problem, -1 = none
    problems: int = 0


@dataclass
class ProblemEvent:
    host: str
    severity: int
    description: str


@dataclass
class Snapshot:
    source: str
    hosts: List[Host]
    new_problems: List[ProblemEvent] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# Zabbix API
# --------------------------------------------------------------------------

class ZabbixError(Exception):
    pass


class ZabbixClient:
    """Minimal JSON-RPC client using an API token (Zabbix 5.4+)."""

    def __init__(self, url: str, token: str, verify: bool = True,
                 ca_file: Optional[str] = None, timeout: float = 10.0):
        if not url.endswith(".php"):
            url = url.rstrip("/") + "/api_jsonrpc.php"
        self.url = url
        self._token = token
        self.timeout = timeout
        self.version: Optional[str] = None
        self._legacy_auth = False
        self._id = 0
        if not verify:
            self._ssl = ssl._create_unverified_context()
        else:
            self._ssl = ssl.create_default_context(cafile=ca_file)

    def call(self, method: str, params, auth: bool = True):
        self._id += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
        headers = {
            "Content-Type": "application/json-rpc",
            "User-Agent": "zabbix-matrix-rain/" + __version__,
        }
        if auth:
            if self._legacy_auth:
                body["auth"] = self._token
            else:
                headers["Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            raise ZabbixError("HTTP %s from %s" % (e.code, self.url)) from None
        except urllib.error.URLError as e:
            hint = ""
            if isinstance(e.reason, ssl.SSLError):
                hint = " (private CA: --ca-file; self-signed: --insecure)"
            raise ZabbixError("cannot reach %s: %s%s" % (self.url, e.reason, hint)) from None
        except (OSError, ValueError) as e:
            raise ZabbixError("%s: %s" % (self.url, e)) from None
        if not isinstance(data, dict):
            raise ZabbixError("unexpected answer from %s" % self.url)
        if "error" in data:
            err = data["error"]
            raise ZabbixError("%s: %s" % (method, err.get("data") or err.get("message")))
        return data.get("result")

    def connect(self) -> str:
        self.version = str(self.call("apiinfo.version", {}, auth=False))
        nums = [int(x) for x in re.findall(r"\d+", self.version)[:2]] + [0, 0]
        # The Authorization header appeared in 6.4; before that the token
        # goes into the "auth" field of the request body.
        self._legacy_auth = (nums[0], nums[1]) < (6, 4)
        return self.version


def literal_prefix(regex: str) -> str:
    """Leading literal part of a regex: narrows item.get on the server side."""
    out = []
    i = 1 if regex.startswith("^") else 0
    while i < len(regex):
        ch = regex[i]
        if ch == "\\" and i + 1 < len(regex) and not regex[i + 1].isalnum():
            out.append(regex[i + 1])
            i += 2
            continue
        if ch in ".^$*+?{}[]|()\\":
            break
        out.append(ch)
        i += 1
    # A quantifier after the last literal makes that literal optional.
    if i < len(regex) and regex[i] in "*?{" and out:
        out.pop()
    return "".join(out)


class ZabbixSource:
    def __init__(self, client: ZabbixClient, groups: List[str], min_severity: int = 2,
                 cpu_key: str = DEFAULT_CPU_KEY, net_key: str = DEFAULT_NET_KEY,
                 stale: float = 900.0):
        self.client = client
        self.groups = groups
        self.min_severity = min_severity
        self.cpu_re = re.compile(cpu_key)
        self.net_re = re.compile(net_key)
        self.cpu_prefix = literal_prefix(cpu_key)
        self.net_prefix = literal_prefix(net_key)
        self.stale = stale
        self._seen: Optional[set] = None   # trigger ids already reported

    def label(self) -> str:
        return "Zabbix %s" % (self.client.version or "?")

    def _item_values(self, hostids: List[str], prefix: str, key_re, now: float
                     ) -> Dict[str, List[float]]:
        params = {
            "output": ["hostid", "key_", "lastvalue", "lastclock"],
            "hostids": hostids,
            "monitored": True,
            "filter": {"value_type": [0, 3]},   # numeric float / unsigned
        }
        if prefix:
            params["search"] = {"key_": prefix}
            params["startSearch"] = True
        values: Dict[str, List[float]] = {}
        for item in self.client.call("item.get", params) or []:
            if not key_re.search(item.get("key_", "")):
                continue
            clock = int(item.get("lastclock") or 0)
            # lastclock 0 means "never collected": lastvalue is then a fake 0.
            if clock == 0 or now - clock > self.stale:
                continue
            try:
                value = float(item.get("lastvalue"))
            except (TypeError, ValueError):
                continue
            values.setdefault(item["hostid"], []).append(value)
        return values

    def fetch(self) -> Snapshot:
        if self.client.version is None:
            self.client.connect()
        params = {"output": ["hostid", "name"], "monitored_hosts": True}
        if self.groups:
            found = self.client.call("hostgroup.get", {
                "output": ["groupid", "name"], "filter": {"name": self.groups}}) or []
            missing = set(self.groups) - {g["name"] for g in found}
            if missing:
                raise ZabbixError("host group not found: " + ", ".join(sorted(missing)))
            params["groupids"] = [g["groupid"] for g in found]
        hosts = {h["hostid"]: Host(h["hostid"], h["name"])
                 for h in self.client.call("host.get", params) or []}
        if not hosts:
            return Snapshot(self.label(), [])
        ids = list(hosts)
        now = time.time()

        for hostid, vals in self._item_values(ids, self.cpu_prefix, self.cpu_re, now).items():
            if hostid in hosts:
                hosts[hostid].cpu = max(0.0, min(100.0, sum(vals) / len(vals)))
        for hostid, vals in self._item_values(ids, self.net_prefix, self.net_re, now).items():
            if hostid in hosts:
                hosts[hostid].net = max(0.0, sum(vals))

        triggers = self.client.call("trigger.get", {
            "output": ["triggerid", "priority", "description"],
            "hostids": ids,
            "filter": {"value": 1},
            "monitored": True,
            "skipDependent": True,
            "min_severity": self.min_severity,
            "expandDescription": True,
            "selectHosts": ["hostid"],
        }) or []
        new: List[ProblemEvent] = []
        current = set()
        for trig in triggers:
            sev = int(trig.get("priority") or 0)
            current.add(trig["triggerid"])
            fresh = self._seen is not None and trig["triggerid"] not in self._seen
            for h in trig.get("hosts") or []:
                host = hosts.get(h.get("hostid"))
                if host is None:
                    continue
                host.problems += 1
                host.severity = max(host.severity, sev)
                if fresh:
                    new.append(ProblemEvent(host.name, sev, trig.get("description", "")))
        self._seen = current
        return Snapshot(self.label(), sorted(hosts.values(), key=lambda h: h.name.lower()), new)


# --------------------------------------------------------------------------
# Demo data
# --------------------------------------------------------------------------

DEMO_ROLES = ["web", "db", "cache", "mq", "app", "lb", "k8s", "backup", "mail", "dns", "vpn", "ci"]
DEMO_PROBLEMS = [
    "High CPU utilization", "Disk space is low", "Interface link down",
    "Service is not running", "Too many processes", "Certificate expires soon",
    "Replication lag is too high", "Host has been restarted", "Memory is low",
]


class DemoSource:
    def __init__(self, count: int = 12, seed: Optional[int] = None):
        self.rng = random.Random(seed)
        self.hosts = []
        for i in range(count):
            name = "%s-%02d" % (DEMO_ROLES[i % len(DEMO_ROLES)], i // len(DEMO_ROLES) + 1)
            host = Host(str(i + 1), name)
            host.cpu = self.rng.uniform(3, 60)
            host.net = 10 ** self.rng.uniform(4, 8)
            self.hosts.append(host)
        self.active: Dict[str, List[Tuple[int, str]]] = {}

    def label(self) -> str:
        return "demo"

    def fetch(self) -> Snapshot:
        rng = self.rng
        new = []
        for h in self.hosts:
            cpu = h.cpu + rng.gauss(0, 9) + (25.0 - h.cpu) * 0.08
            if rng.random() < 0.05:
                cpu += rng.uniform(30, 70)            # a burst of work
            h.cpu = max(1.0, min(100.0, cpu))
            h.net = max(1e3, min(1e9, h.net * 10 ** rng.gauss(0, 0.25)))
            probs = self.active.setdefault(h.hostid, [])
            if probs and rng.random() < 0.2:
                probs.pop(0)
            if rng.random() < 0.03 or (h.cpu > 90 and not probs and rng.random() < 0.5):
                sev = rng.choice([2, 2, 2, 3, 3, 4, 5])
                desc = rng.choice(DEMO_PROBLEMS)
                probs.append((sev, desc))
                new.append(ProblemEvent(h.name, sev, desc))
            h.severity = max((s for s, _ in probs), default=-1)
            h.problems = len(probs)
        return Snapshot("demo", [dataclasses.replace(h) for h in self.hosts], new)


def anonymize(snap: Snapshot) -> Snapshot:
    """Replace host names with host-01, host-02... and hide problem texts."""
    names = {h.name: "host-%02d" % (i + 1) for i, h in enumerate(snap.hosts)}
    hosts = [dataclasses.replace(h, name=names[h.name]) for h in snap.hosts]
    new = [ProblemEvent(names.get(p.host, "host"), p.severity,
                        SEVERITY_NAMES[p.severity] + " problem") for p in snap.new_problems]
    return Snapshot(snap.source, hosts, new, snap.fetched_at)


class Poller(threading.Thread):
    """Fetches a snapshot every `interval` seconds in the background."""

    def __init__(self, source, interval: float, hide_names: bool = False):
        super().__init__(daemon=True)
        self.source = source
        self.interval = interval
        self.hide_names = hide_names
        self.lock = threading.Lock()
        self.snapshot: Optional[Snapshot] = None
        self.error: Optional[str] = None
        self.seq = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                snap = self.source.fetch()
                if self.hide_names:
                    snap = anonymize(snap)
                with self.lock:
                    self.snapshot = snap
                    self.error = None
                    self.seq += 1
            except Exception as e:  # keep raining whatever happens
                with self.lock:
                    self.error = str(e) or e.__class__.__name__
                    self.seq += 1
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def get(self):
        with self.lock:
            return self.seq, self.snapshot, self.error


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

CHARSETS = {
    "katakana": [chr(c) for c in range(0xFF66, 0xFF9E)] + list("0123456789:.=*+-<>|"),
    "ascii": list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ@#$%&*+=<>?:."),
    "binary": list("01"),
    "hex": list("0123456789ABCDEF"),
}

# (body, dim) colour numbers for 256-colour and 8-colour terminals.
PALETTE_256 = {"ok": (40, 22), 0: (250, 240), 1: (39, 24), 2: (220, 136),
               3: (208, 130), 4: (196, 88), 5: (201, 90)}


class Drop:
    __slots__ = ("col", "y", "speed", "length", "fade", "style", "label", "cells", "head")

    def __init__(self, col, speed, length, style, label=None):
        self.col = col
        self.y = -1.0
        self.speed = speed
        self.length = length
        self.fade = max(2, int(length * 0.6))
        self.style = style        # key of Rain.styles
        self.label = label        # text to spell, or None for random glyphs
        self.cells: Dict[int, str] = {}
        self.head = -1


class HostParams:
    __slots__ = ("hostid", "spawn", "speed", "length", "tint", "style", "label")

    def __init__(self, host: Optional[Host], fps: float):
        self.hostid = None if host is None else host.hostid
        k = 20.0 / fps                    # tuned at 20 fps
        if host is None or host.cpu is None:
            act = 0.08
        else:
            act = host.cpu / 100.0
        self.spawn = (0.004 + 0.07 * act ** 0.8) * k
        self.speed = (0.25 + 0.85 * act) * k
        if host is None or host.net is None:
            self.length = 9.0
        else:
            self.length = 5.0 + 2.2 * math.log10(1.0 + host.net / 1000.0)
        sev = -1 if host is None else host.severity
        self.style = "ok" if sev < 0 else sev
        self.tint = 0.0 if sev < 0 else min(0.95, 0.55 + 0.1 * host.problems)
        self.label = None
        if host is not None:
            parts = [host.name]
            if host.cpu is not None:
                parts.append("%d%%" % round(host.cpu))
            if host.problems:
                parts.append("!%d" % host.problems)
            self.label = " ".join(parts)


class Rain:
    def __init__(self, scr, curses, args, poller: Poller):
        self.scr = scr
        self.c = curses
        self.args = args
        self.poller = poller
        self.rng = random.Random(args.seed)
        self.glyphs = CHARSETS[args.charset]
        self.hud = not args.no_hud
        self.labels = not args.no_labels
        self.paused = False
        self.seq = -1
        self.snapshot: Optional[Snapshot] = None
        self.error: Optional[str] = None
        self.message: Optional[Tuple[str, object, float]] = None   # text, style, until
        self.glitch_frames = 0
        self.glitch_style: object = 4
        self.glitch_cells: List[Tuple[int, int]] = []
        self.banner: Optional[tuple] = None    # row, x, text, attr, until
        self.test_severity = 1                 # key t cycles 2..5
        self.test_tint: Dict[str, Tuple[int, float]] = {}
        self.setup_colors()
        self.resize()

    # -- colours -----------------------------------------------------------

    def setup_colors(self):
        c = self.c
        self.styles: Dict[object, Tuple[int, int]] = {}
        bold = c.A_BOLD
        if not c.has_colors():
            for key in ["ok", "label", 0, 1, 2, 3, 4, 5]:
                self.styles[key] = (bold if key != "ok" else 0, c.A_DIM)
            self.head_attr = bold
            self.hud_attr = c.A_REVERSE
            self.err_attr = c.A_REVERSE | bold
            return
        c.start_color()
        bg = c.COLOR_BLACK
        if self.args.transparent:
            try:
                c.use_default_colors()
                bg = -1
            except c.error:
                pass
        pair = [0]

        def mk(fg, attr=0):
            pair[0] += 1
            c.init_pair(pair[0], fg, bg)
            return c.color_pair(pair[0]) | attr

        if c.COLORS >= 256:
            for key, (body, dim) in PALETTE_256.items():
                self.styles[key] = (mk(body), mk(dim))
            self.styles["label"] = (mk(231, bold), mk(120, bold))
            self.head_attr = mk(231, bold)
        else:
            basic = {"ok": c.COLOR_GREEN, 0: c.COLOR_WHITE, 1: c.COLOR_CYAN, 2: c.COLOR_YELLOW,
                     3: c.COLOR_YELLOW, 4: c.COLOR_RED, 5: c.COLOR_MAGENTA}
            for key, fg in basic.items():
                self.styles[key] = (mk(fg, bold if key == 3 else 0), mk(fg, c.A_DIM))
            self.styles["label"] = (mk(c.COLOR_WHITE, bold), mk(c.COLOR_GREEN, bold))
            self.head_attr = mk(c.COLOR_WHITE, bold)
        pair[0] += 1
        c.init_pair(pair[0], c.COLOR_BLACK, c.COLOR_GREEN)
        self.hud_attr = c.color_pair(pair[0])
        pair[0] += 1
        c.init_pair(pair[0], c.COLOR_WHITE, c.COLOR_RED)
        self.err_attr = c.color_pair(pair[0]) | bold
        if bg == c.COLOR_BLACK:
            self.scr.bkgd(" ", self.styles["ok"][0])

    # -- layout ------------------------------------------------------------

    def resize(self):
        self.h, self.w = self.scr.getmaxyx()
        self.scr.erase()
        self.rows = max(1, self.h - (1 if self.hud else 0))
        self.cols = list(range(0, max(1, self.w - 1), self.args.spacing))
        self.drops: List[Drop] = []
        self.col_top: Dict[int, Drop] = {}
        self.col_count: Dict[int, int] = {}
        self.label_cols: Dict[int, int] = {}
        self.glitch_cells = []
        self.banner = None
        self.layout()

    def layout(self):
        """Give every host a contiguous band of columns."""
        fps = self.args.fps
        hosts = list(self.snapshot.hosts) if self.snapshot else []
        if len(hosts) > len(self.cols):          # keep the most interesting ones
            hosts.sort(key=lambda h: (h.severity, h.cpu or 0), reverse=True)
            hosts = sorted(hosts[:len(self.cols)], key=lambda h: h.name.lower())
        self.host_params = [HostParams(h, fps) for h in hosts] or [HostParams(None, fps)]
        n = len(self.host_params)
        ncols = len(self.cols)
        self.col_params = [self.host_params[i * n // ncols] for i in range(ncols)]
        self.band = {}                           # params -> its columns
        for x, p in zip(self.cols, self.col_params):
            self.band.setdefault(id(p), []).append(x)
        now = time.monotonic()
        every = self.args.label_every
        self.next_label = {id(p): now + self.rng.uniform(0.05, 0.6) * every
                           for p in self.host_params if p.label}

    # -- drawing primitives -------------------------------------------------

    def put(self, y, x, ch, attr=0):
        try:
            self.scr.addstr(y, x, ch, attr)
        except self.c.error:
            pass  # bottom-right cell or a width mismatch: not worth dying for

    def glyph(self):
        return self.rng.choice(self.glyphs)

    # -- animation ---------------------------------------------------------

    def spawn(self, x, p: HostParams, label=None):
        rng = self.rng
        if label:
            speed = 0.3 * 20.0 / self.args.fps     # slow enough to read
            below = self.col_top.get(x)
            if below is not None:                  # never catch up with it
                speed = min(speed, below.speed * 0.95)
            # the name is spelled from the top row and erased when the head is
            # `length` rows further: it stays whole for about four seconds
            length = len(label) + int(4.0 * speed * self.args.fps)
            style = "label" if p.style == "ok" else p.style
        else:
            speed = max(0.08, p.speed * rng.uniform(0.7, 1.3))
            length = max(3, int(p.length * rng.uniform(0.6, 1.4)))
            style = p.style if rng.random() < p.tint else "ok"
        d = Drop(x, speed, min(length, self.rows + len(label or "")), style, label)
        if label:
            d.fade = d.length          # a name does not fade before it is erased
            self.label_cols[x] = self.label_cols.get(x, 0) + 1
        self.drops.append(d)
        self.col_top[x] = d
        self.col_count[x] = self.col_count.get(x, 0) + 1

    def step_drop(self, d: Drop) -> bool:
        body, dim = self.styles[d.style]
        if d.label:
            body |= self.c.A_BOLD
        rows = self.rows
        d.y += d.speed
        new_head = int(d.y)
        for r in range(d.head + 1, new_head + 1):
            if r < rows:
                if d.label is None:
                    ch = self.glyph()
                elif r < len(d.label):
                    ch = d.label[r]
                else:
                    ch = None
                if ch is not None:
                    d.cells[r] = ch
                    self.put(r, d.col, ch, self.head_attr)
            prev = r - 1
            if prev in d.cells and prev < rows:
                self.put(prev, d.col, d.cells[prev], body)
            faded = r - d.fade
            if faded in d.cells:
                self.put(faded, d.col, d.cells[faded], dim)
            tail = r - d.length
            if tail in d.cells:
                self.put(tail, d.col, " ")
                del d.cells[tail]
        d.head = max(d.head, new_head)
        if d.label is None and d.cells and self.rng.random() < 0.1:
            r = self.rng.choice(list(d.cells))
            if r < d.head:
                ch = self.glyph()
                d.cells[r] = ch
                self.put(r, d.col, ch, body if r > d.head - d.fade else dim)
        return d.head - d.length < rows

    def apply_snapshot(self):
        seq, snap, err = self.poller.get()
        if seq == self.seq:
            return
        self.seq = seq
        self.error = err
        if snap is None or snap is self.snapshot:
            return
        old = self.snapshot
        self.snapshot = snap
        if old is None or [h.hostid for h in old.hosts] != [h.hostid for h in snap.hosts]:
            self.layout()
        else:
            # same hosts: refresh the parameters in place, keep the bands
            fps = self.args.fps
            hosts = {h.hostid: h for h in snap.hosts}
            for p in self.host_params:
                if p.hostid in hosts:
                    fresh = HostParams(hosts[p.hostid], fps)
                    for slot in HostParams.__slots__:
                        setattr(p, slot, getattr(fresh, slot))
        self.apply_test_tint()
        if snap.new_problems:
            self.announce(snap.new_problems)

    def announce(self, events: List[ProblemEvent]):
        worst = max(events, key=lambda e: e.severity)
        self.glitch_frames = int(self.args.fps * 0.6)
        self.glitch_style = worst.severity
        text = "NEW %s: %s: %s" % (SEVERITY_NAMES[worst.severity].upper(),
                                    worst.host, worst.description)
        if len(events) > 1:
            text += "  (+%d more)" % (len(events) - 1)
        self.message = (text, worst.severity, time.monotonic() + 30)
        self.show_banner("%s  %s" % (worst.host, worst.description), worst.severity)

    def test_problem(self):
        """Key t: a fake problem shown locally; nothing is sent to Zabbix."""
        self.test_severity = 2 if self.test_severity >= 5 else self.test_severity + 1
        sev = self.test_severity
        names = {h.hostid: h.name for h in self.snapshot.hosts} if self.snapshot else {}
        candidates = [p for p in self.host_params if p.hostid in names]
        if candidates:
            p = self.rng.choice(candidates)
            host = names[p.hostid]
            self.test_tint[p.hostid] = (sev, time.monotonic() + 30)
            self.apply_test_tint()
        else:
            host = "test-host"
        self.announce([ProblemEvent(host, sev, "TEST problem (local only, not in Zabbix)")])

    def apply_test_tint(self):
        now = time.monotonic()
        self.test_tint = {k: v for k, v in self.test_tint.items() if v[1] > now}
        for p in self.host_params:
            if p.hostid in self.test_tint:
                sev = self.test_tint[p.hostid][0]
                if p.style == "ok" or sev > p.style:
                    p.style = sev
                p.tint = max(p.tint, 0.8)

    def show_banner(self, text, style):
        text = " %s " % text
        width = min(len(text), self.w - 2)
        if width < 6 or self.rows < 3:
            return
        row = self.rows // 2
        x = (self.w - width) // 2
        attr = self.styles[style][0] | self.c.A_REVERSE | self.c.A_BOLD
        self.banner = (row, x, text[:width], attr, time.monotonic() + 4.0)

    def step_glitch(self):
        if self.glitch_frames > 0:
            self.glitch_frames -= 1
            attr = self.styles[self.glitch_style][0] | self.c.A_REVERSE
            for _ in range(max(1, self.rows * len(self.cols) // 60)):
                y = self.rng.randrange(self.rows)
                x = self.rng.randrange(max(1, self.w - 1))
                self.put(y, x, self.glyph(), attr)
                self.glitch_cells.append((y, x))
        elif self.glitch_cells:
            for y, x in self.glitch_cells:
                self.put(y, x, " ")
            self.glitch_cells = []
        if self.banner:
            row, x, text, attr, until = self.banner
            if time.monotonic() > until:
                self.put(row, x, " " * len(text))
                self.banner = None
            else:   # redrawn every frame so the rain flows behind it
                self.put(row, x, text, attr)

    def step(self):
        rng = self.rng
        for x, p in zip(self.cols, self.col_params):
            if self.col_count.get(x, 0) >= 2 or self.label_cols.get(x):
                continue   # nothing may overtake a name and eat its letters
            top = self.col_top.get(x)
            if top is not None and top.head - top.length < 1:
                continue
            if rng.random() < p.spawn:
                self.spawn(x, p)
        if self.labels:
            now = time.monotonic()
            for p in self.host_params:
                due = self.next_label.get(id(p))
                if due is None or now < due:
                    continue
                if len(p.label) + 2 > self.rows:
                    self.next_label[id(p)] = now + self.args.label_every
                    continue
                # the name surfaces at a random height in the upper half;
                # leading blanks keep the rows above it empty
                start = rng.randrange(max(1, (self.rows - len(p.label)) // 2))
                text = " " * start + p.label
                # a column whose newest stream is already well below the
                # name: the name falls slower and never meets it
                need = len(text) + 2
                free = []
                for x in self.band.get(id(p), []):
                    top = self.col_top.get(x)
                    if self.label_cols.get(x) or self.col_count.get(x, 0) >= 2:
                        continue
                    if top is None or top.head - top.length > need:
                        free.append(x)
                if free:
                    self.spawn(rng.choice(free), p, label=text)
                    self.next_label[id(p)] = now + rng.uniform(0.6, 1.4) * self.args.label_every
                else:
                    self.next_label[id(p)] = now + 0.3     # try again shortly
        alive = []
        for d in self.drops:
            if self.step_drop(d):
                alive.append(d)
            else:
                if d.label:
                    self.label_cols[d.col] -= 1
                    if not self.label_cols[d.col]:
                        del self.label_cols[d.col]
                self.col_count[d.col] -= 1
                if self.col_top.get(d.col) is d:
                    del self.col_top[d.col]
        self.drops = alive
        self.step_glitch()

    # -- status line -------------------------------------------------------

    def draw_hud(self):
        if not self.hud:
            return
        snap = self.snapshot
        now = time.time()
        attr = self.hud_attr
        if self.error:
            text = " NO CARRIER: %s " % self.error
            attr = self.err_attr
        elif snap is None:
            text = " connecting... "
        else:
            hosts = snap.hosts
            cpus = [h.cpu for h in hosts if h.cpu is not None]
            counts = [0] * 6
            for h in hosts:
                if h.severity >= 0:
                    counts[h.severity] += 1
            sev = " ".join("%s:%d" % (SEVERITY_SHORT[i], counts[i])
                           for i in range(5, -1, -1) if counts[i])
            text = " %s | hosts %d | cpu avg %s | hosts with problems %d%s | %ds ago " % (
                snap.source, len(hosts),
                "%d%%" % round(sum(cpus) / len(cpus)) if cpus else "n/a",
                sum(counts), " (%s)" % sev if sev else "", now - snap.fetched_at)
        keys = " q quit  n names  t test  h hud  l labels  space pause "
        if self.message and time.monotonic() < self.message[2] and snap is not None \
                and not self.error:
            text = " %s |%s" % (self.message[0], text)
            attr = self.styles[self.message[1]][0] | self.c.A_REVERSE
            keys = ""
        if self.paused:
            keys = " PAUSED " + keys
        width = self.w - 1
        line = text[:max(0, width - len(keys))]
        line = line + " " * (width - len(line) - len(keys)) + keys if width > len(keys) else line
        self.put(self.h - 1, 0, line[:width], attr)

    # -- main loop ---------------------------------------------------------

    def run(self):
        c = self.c
        frame = 1.0 / self.args.fps
        while True:
            start = time.monotonic()
            key = self.scr.getch()
            if key in (ord("q"), ord("Q"), 27):
                return
            if self.args.screensaver and key not in (-1, c.KEY_RESIZE):
                return
            if key == c.KEY_RESIZE:
                self.resize()
            elif key in (ord("h"), ord("H")):
                self.hud = not self.hud
                self.resize()
            elif key in (ord("l"), ord("L")):
                self.labels = not self.labels
            elif key in (ord("n"), ord("N")):
                self.labels = True
                now = time.monotonic()
                for k in self.next_label:
                    self.next_label[k] = now
            elif key in (ord("t"), ord("T")):
                self.test_problem()
            elif key == ord(" "):
                self.paused = not self.paused
            self.apply_snapshot()
            if not self.paused and self.h > 2 and self.w > 4:
                self.step()
            self.draw_hud()
            self.scr.refresh()
            time.sleep(max(0.0, frame - (time.monotonic() - start)))


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="zabbix-matrix-rain",
        description="Matrix-style digital rain in the terminal, driven by Zabbix metrics.",
        epilog="The API token is read from --token-file, ZABBIX_TOKEN_FILE or ZABBIX_TOKEN "
               "(it is never accepted on the command line, where other users could see it).")
    src = p.add_argument_group("data source")
    src.add_argument("--url", default=os.environ.get("ZABBIX_URL"),
                     help="Zabbix frontend URL, e.g. https://zabbix.example.com "
                          "(env ZABBIX_URL)")
    src.add_argument("--token-file", default=os.environ.get("ZABBIX_TOKEN_FILE"),
                     help="file with the API token (env ZABBIX_TOKEN_FILE)")
    src.add_argument("--group", action="append", default=None,
                     help="only hosts of this host group; repeat for several "
                          "(env ZABBIX_GROUPS, comma separated)")
    src.add_argument("--min-severity", type=int, default=2, choices=range(6),
                     help="ignore problems below this severity (default 2 = warning)")
    src.add_argument("--interval", type=float, default=None,
                     help="seconds between API polls (default 30, demo 3)")
    src.add_argument("--insecure", action="store_true", default=env_flag("ZABBIX_INSECURE"),
                     help="do not verify the TLS certificate (env ZABBIX_INSECURE=1)")
    src.add_argument("--ca-file", default=os.environ.get("ZABBIX_CA_FILE"),
                     help="CA bundle for a private certificate authority (env ZABBIX_CA_FILE)")
    src.add_argument("--timeout", type=float, default=10.0, help="API timeout, seconds")
    src.add_argument("--cpu-key", default=DEFAULT_CPU_KEY,
                     help="regex for CPU utilisation item keys (percent, averaged per host)")
    src.add_argument("--net-key", default=DEFAULT_NET_KEY,
                     help="regex for traffic item keys (bits per second, summed per host)")
    src.add_argument("--stale", type=float, default=900.0,
                     help="ignore values older than this many seconds (default 900)")
    src.add_argument("--demo", action="store_true", help="simulated hosts, no Zabbix needed")
    src.add_argument("--demo-hosts", type=int, default=12, help="number of simulated hosts")
    src.add_argument("--check", action="store_true",
                     help="poll once, print what was found and exit")

    look = p.add_argument_group("look")
    look.add_argument("--charset", choices=sorted(CHARSETS), default=None,
                      help="glyphs (default katakana on UTF-8 terminals, otherwise ascii)")
    look.add_argument("--fps", type=float, default=20.0, help="frames per second (default 20)")
    look.add_argument("--spacing", type=int, default=1,
                      help="use every N-th column; 2 gives the airy look of the film")
    look.add_argument("--label-every", type=float, default=30.0,
                      help="average seconds between host name streams of one host (default 30)")
    look.add_argument("--no-labels", action="store_true", help="never spell host names")
    look.add_argument("--no-hud", action="store_true", help="hide the status line")
    look.add_argument("--anonymize", action="store_true",
                      help="show host-01, host-02... instead of names, hide problem texts")
    look.add_argument("--screensaver", action="store_true", help="exit on any key")
    look.add_argument("--transparent", action="store_true",
                      help="keep the terminal background instead of black")
    look.add_argument("--seed", type=int, default=None, help="random seed")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = p.parse_args(argv)

    if args.group is None:
        env = os.environ.get("ZABBIX_GROUPS", "")
        args.group = [g.strip() for g in env.split(",") if g.strip()]
    if args.interval is None:
        args.interval = 3.0 if args.demo else 30.0
    args.interval = max(1.0, args.interval)
    args.fps = max(2.0, min(60.0, args.fps))
    args.spacing = max(1, args.spacing)
    args.label_every = max(1.0, args.label_every)
    if args.charset is None:
        enc = (locale.getpreferredencoding(False) or "").lower().replace("-", "")
        args.charset = "katakana" if enc == "utf8" else "ascii"
    return p, args


def read_token(args) -> Optional[str]:
    if args.token_file:
        with open(os.path.expanduser(args.token_file), encoding="utf-8") as f:
            return f.read().strip() or None
    return os.environ.get("ZABBIX_TOKEN", "").strip() or None


def build_source(parser, args):
    if args.demo:
        return DemoSource(args.demo_hosts, args.seed)
    if not args.url:
        parser.error("set --url or ZABBIX_URL (or use --demo)")
    try:
        token = read_token(args)
    except OSError as e:
        parser.error("cannot read token file: %s" % e)
    if not token:
        parser.error("no API token: set ZABBIX_TOKEN_FILE or ZABBIX_TOKEN")
    client = ZabbixClient(args.url, token, verify=not args.insecure,
                          ca_file=args.ca_file, timeout=args.timeout)
    return ZabbixSource(client, args.group, args.min_severity,
                        args.cpu_key, args.net_key, args.stale)


def human_bps(v: Optional[float]) -> str:
    if v is None:
        return "-"
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if v < 1000:
            return "%.0f %s" % (v, unit)
        v /= 1000.0
    return "%.1f Tbps" % v


def run_check(source, hide_names: bool) -> int:
    try:
        snap = source.fetch()
    except ZabbixError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    if hide_names:
        snap = anonymize(snap)
    print("source: %s, hosts: %d" % (snap.source, len(snap.hosts)))
    width = max([4] + [len(h.name) for h in snap.hosts])
    print("%-*s  %5s  %10s  %s" % (width, "host", "cpu", "traffic", "problems"))
    for h in snap.hosts:
        cpu = "-" if h.cpu is None else "%d%%" % round(h.cpu)
        prob = "-" if h.severity < 0 else "%d, worst %s" % (h.problems, SEVERITY_NAMES[h.severity])
        print("%-*s  %5s  %10s  %s" % (width, h.name, cpu, human_bps(h.net), prob))
    no_cpu = sum(1 for h in snap.hosts if h.cpu is None)
    if no_cpu:
        print("\n%d host(s) without a fresh CPU value: their streams stay slow. "
              "See --cpu-key." % no_cpu)
    return 0


def main(argv=None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    parser, args = parse_args(argv)
    source = build_source(parser, args)
    if args.check:
        return run_check(source, args.anonymize)
    try:
        import curses
    except ImportError:
        print("The curses module is missing. On Windows use WSL or "
              "`pip install windows-curses`.", file=sys.stderr)
        return 1
    poller = Poller(source, args.interval, args.anonymize)
    poller.start()

    def ui(scr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        scr.nodelay(True)
        scr.keypad(True)
        Rain(scr, curses, args, poller).run()

    try:
        curses.wrapper(ui)
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
