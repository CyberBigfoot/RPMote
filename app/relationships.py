"""
RPMote Relationship Lenses — the structure already implied by the vault.

The graph draws three kinds of edge: the orb to a tenant, a tenant to its own
connections, and links the user drew by hand.  None of those say anything the
user did not already say themselves.  But the saved records *do* carry real
structure — the same box filed twice under different protocols, nine machines
sharing a subnet, one key that opens boxes in three customers, the management
server every reboot travels through — and none of it is visible.

This module works that structure out.  It is deliberately pure: connections in,
relationships out, no Qt and no I/O, so it can be tested at speed and reasoned
about without a canvas.

Two shapes come back, because the underlying relationships genuinely differ:

* **Groups** — "these N things share X".  Rendered as spokes to a small labelled
  hub rather than as every pair, because a /24 holding fifty machines is 1,225
  pairs and one hub with fifty spokes.  At 300+ connections that distinction is
  the difference between a diagram and a hairball.
* **Edges** — a directed relationship between exactly two records, which is what
  a restart dependency is: *this* server reboots *that* machine.

Anything spanning two tenants is flagged.  A host or a credential shared
between two customers is either deliberate shared infrastructure or a mistake,
and both are worth having drawn in a colour that makes you look.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

#: Lens names.  ``none`` is a real value, not an absence — it is what the menu
#: and the saved view state store when no lens is applied.
LENS_NONE = "none"
LENS_MACHINE = "machine"
LENS_SUBNET = "subnet"
LENS_CREDENTIAL = "credential"
LENS_RESTART = "restart"

LENSES = (LENS_NONE, LENS_MACHINE, LENS_SUBNET, LENS_CREDENTIAL, LENS_RESTART)

LENS_LABELS = {
    LENS_NONE: "No lens",
    LENS_MACHINE: "Same machine",
    LENS_SUBNET: "Same network",
    LENS_CREDENTIAL: "Shared credentials",
    LENS_RESTART: "Restart dependencies",
}

LENS_HINTS = {
    LENS_NONE: "Relationship lenses are off.",
    LENS_MACHINE: "Records pointing at one physical box — same host, or same MAC.",
    LENS_SUBNET: "Machines sharing a /24, worked out from literal IP addresses.",
    LENS_CREDENTIAL: "Machines opened by the same username and key, or the same password.",
    LENS_RESTART: "Which server reboots which machine, from your restart setup.",
}

#: Protocols with no network identity of their own to relate anything to.
_HOSTLESS = {"azure_ps"}

_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


# ── Results ────────────────────────────────────────────────────────

@dataclass
class RelationGroup:
    """N connections that share one thing."""

    kind: str
    key: str                       # stable identity, e.g. "192.168.10.0/24"
    label: str                     # what the canvas prints on the hub
    members: list = field(default_factory=list)   # connection ids
    tenants: list = field(default_factory=list)   # tenants represented, sorted

    @property
    def cross_tenant(self) -> bool:
        return len(self.tenants) > 1

    def __len__(self) -> int:
        return len(self.members)


@dataclass
class RelationEdge:
    """A directed relationship between exactly two connections."""

    kind: str
    source: str                    # connection id the relationship acts *from*
    target: str                    # connection id it acts *on*
    label: str = ""
    cross_tenant: bool = False


@dataclass
class RelationView:
    """Everything one lens found."""

    kind: str = LENS_NONE
    groups: list = field(default_factory=list)
    edges: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.groups and not self.edges

    def participants(self) -> set:
        """Every connection id this lens has something to say about."""
        found = set()
        for group in self.groups:
            found.update(group.members)
        for edge in self.edges:
            found.add(edge.source)
            found.add(edge.target)
        return found

    def cross_tenant_count(self) -> int:
        return (sum(1 for g in self.groups if g.cross_tenant)
                + sum(1 for e in self.edges if e.cross_tenant))

    def summary(self) -> str:
        """The one line the canvas banner shows."""
        if self.empty:
            return f"{LENS_LABELS.get(self.kind, self.kind)} — nothing found"
        parts = [LENS_LABELS.get(self.kind, self.kind)]
        if self.groups:
            count = len(self.groups)
            parts.append(f"{count} group{'' if count == 1 else 's'}")
        if self.edges:
            count = len(self.edges)
            parts.append(f"{count} link{'' if count == 1 else 's'}")
        crossing = self.cross_tenant_count()
        if crossing:
            parts.append(f"{crossing} crossing tenants")
        return "  ·  ".join(parts)


# ── Field readers ──────────────────────────────────────────────────

def _cid(conn: dict) -> str:
    return str((conn or {}).get("id", "") or "").strip()


def _tenant(conn: dict) -> str:
    return str((conn or {}).get("tenant", "Default") or "Default")


def _host(conn: dict) -> str:
    return str((conn or {}).get("host", "") or "").strip().lower()


def _protocol(conn: dict) -> str:
    return str((conn or {}).get("protocol", "") or "").strip().lower()


def _usable(conn) -> bool:
    """A record with an id and a network identity worth relating."""
    return (isinstance(conn, dict) and bool(_cid(conn))
            and _protocol(conn) not in _HOSTLESS)


def subnet_of(host: str, bits: int = 24) -> str | None:
    """``a.b.c.0/24`` for a literal IPv4 address, else None.

    Hostnames are deliberately *not* resolved: three hundred DNS lookups on the
    UI thread would freeze the window, and a lens is meant to be instant.
    """
    match = _IPV4.match(str(host or "").strip())
    if not match:
        return None
    octets = [int(o) for o in match.groups()]
    if any(o > 255 for o in octets):
        return None
    if bits == 24:
        return f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
    if bits == 16:
        return f"{octets[0]}.{octets[1]}.0.0/16"
    return None


def credential_key(conn: dict) -> tuple | None:
    """What identifies "the same credential", or None when there is none.

    A key file is the stronger signal and wins.  Passwords are keyed by digest
    so two records can be found to share one without the value ever being
    copied out of the vault and into a group label.
    """
    shared = str((conn or {}).get("credential_id", "") or "").strip()
    if shared:
        # Connections pointing at one shared credential obviously share it.
        return ("shared", shared, str((conn or {}).get("credential_name", "") or ""))
    username = str((conn or {}).get("username", "") or "").strip().lower()
    if not username:
        return None
    key_file = str((conn or {}).get("key_file", "") or "").strip()
    if key_file:
        # Basename, so the same key referenced by two different paths still
        # reads as one credential.
        leaf = re.split(r"[\\/]", key_file)[-1].strip().lower()
        return ("key", username, leaf)
    password = str((conn or {}).get("password", "") or "")
    if password:
        digest = hashlib.sha256(password.encode("utf-8", "replace")).hexdigest()[:16]
        return ("password", username, digest)
    return None


def credential_label(key: tuple) -> str:
    """Human wording for a credential group — never the secret itself."""
    if not key:
        return ""
    if key[0] == "key":
        return f"{key[1]}  ·  {key[2]}"
    if key[0] == "shared":
        return f"{key[2]}  ·  shared credential" if key[2] else "shared credential"
    return f"{key[1]}  ·  shared password"


# ── Union-find, for "same machine" ─────────────────────────────────

class _Union:
    """Small disjoint-set, so a host match and a MAC match merge into one."""

    def __init__(self):
        self._parent: dict = {}

    def find(self, item):
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:      # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def clusters(self) -> dict:
        out: dict = {}
        for item in list(self._parent):
            out.setdefault(self.find(item), []).append(item)
        return out


# ── The lenses ─────────────────────────────────────────────────────

def _make_group(kind: str, key: str, label: str, members: list,
                by_id: dict) -> RelationGroup:
    tenants = sorted({_tenant(by_id[m]) for m in members if m in by_id})
    return RelationGroup(kind=kind, key=key, label=label,
                         members=sorted(members), tenants=tenants)


def machine_groups(connections) -> list:
    """Records that point at one physical box — same host, or same MAC."""
    from app.wake_on_lan import connection_mac

    by_id = {}
    union = _Union()
    host_seed: dict = {}
    mac_seed: dict = {}
    for conn in connections or []:
        if not _usable(conn):
            continue
        cid = _cid(conn)
        by_id[cid] = conn
        union.find(cid)                       # every record is its own set first
        host = _host(conn)
        if host:
            if host in host_seed:
                union.union(host_seed[host], cid)
            else:
                host_seed[host] = cid
        mac = connection_mac(conn)
        if mac:
            if mac in mac_seed:
                union.union(mac_seed[mac], cid)
            else:
                mac_seed[mac] = cid

    groups = []
    for root, members in union.clusters().items():
        if len(members) < 2:
            continue
        hosts = sorted({_host(by_id[m]) for m in members if _host(by_id[m])})
        label = hosts[0] if hosts else str(root)
        if len(hosts) > 1:
            label += f"  (+{len(hosts) - 1})"
        groups.append(_make_group(LENS_MACHINE, f"machine:{root}", label,
                                  members, by_id))
    groups.sort(key=lambda g: (-len(g), g.label))
    return groups


def subnet_groups(connections, bits: int = 24) -> list:
    """Machines sharing a network, from literal IPv4 hosts only."""
    by_id = {}
    buckets: dict = {}
    for conn in connections or []:
        if not _usable(conn):
            continue
        subnet = subnet_of(_host(conn), bits)
        if not subnet:
            continue
        cid = _cid(conn)
        by_id[cid] = conn
        buckets.setdefault(subnet, []).append(cid)

    groups = [_make_group(LENS_SUBNET, subnet, subnet, members, by_id)
              for subnet, members in buckets.items() if len(members) > 1]
    groups.sort(key=lambda g: (-len(g), g.label))
    return groups


def credential_groups(connections) -> list:
    """Machines opened by the same username and key, or the same password."""
    by_id = {}
    buckets: dict = {}
    for conn in connections or []:
        if not _usable(conn):
            continue
        key = credential_key(conn)
        if key is None:
            continue
        cid = _cid(conn)
        by_id[cid] = conn
        buckets.setdefault(key, []).append(cid)

    groups = [
        _make_group(LENS_CREDENTIAL, "cred:" + "|".join(key),
                    credential_label(key), members, by_id)
        for key, members in buckets.items() if len(members) > 1
    ]
    groups.sort(key=lambda g: (-len(g), g.label))
    return groups


def restart_edges(connections, servers=None, overrides=None) -> list:
    """Directed "reboots" edges, straight out of the restart configuration.

    An override for one machine replaces its tenant's route rather than adding
    to it, exactly as the restart engine treats it — the picture has to agree
    with what would actually happen.
    """
    by_id = {}
    by_tenant: dict = {}
    for conn in connections or []:
        # An Azure PowerShell record is not a machine and cannot be rebooted,
        # so a tenant-wide restart route must not draw an arrow at it.
        if not _usable(conn):
            continue
        cid = _cid(conn)
        by_id[cid] = conn
        by_tenant.setdefault(_tenant(conn), []).append(cid)

    def server_of(config) -> str:
        if not isinstance(config, dict) or not config.get("enabled", True):
            return ""
        server_id = str(config.get("conn_id", "") or "").strip()
        return server_id if server_id in by_id else ""

    routes: dict = {}          # target id -> server id
    for tenant, config in (servers or {}).items():
        server_id = server_of(config)
        if not server_id:
            continue
        for cid in by_tenant.get(tenant, []):
            if cid != server_id:
                routes[cid] = server_id
    for cid, config in (overrides or {}).items():
        cid = str(cid or "").strip()
        if cid not in by_id:
            continue
        server_id = server_of(config)
        if not server_id:
            routes.pop(cid, None)      # an override that is off removes the route
        elif server_id != cid:
            routes[cid] = server_id

    edges = []
    for target, source in sorted(routes.items()):
        edges.append(RelationEdge(
            kind=LENS_RESTART, source=source, target=target,
            label=str(by_id[source].get("name", "") or "restart server"),
            cross_tenant=_tenant(by_id[source]) != _tenant(by_id[target])))
    return edges


def compute_relations(kind: str, connections, servers=None,
                      overrides=None) -> RelationView:
    """Run one lens.  Unknown or ``none`` kinds give an empty view."""
    kind = str(kind or LENS_NONE)
    if kind == LENS_MACHINE:
        return RelationView(kind, groups=machine_groups(connections))
    if kind == LENS_SUBNET:
        return RelationView(kind, groups=subnet_groups(connections))
    if kind == LENS_CREDENTIAL:
        return RelationView(kind, groups=credential_groups(connections))
    if kind == LENS_RESTART:
        return RelationView(
            kind, edges=restart_edges(connections, servers, overrides))
    return RelationView(LENS_NONE)


def describe_connection(conn: dict, connections, servers=None,
                        overrides=None) -> list:
    """Every relationship one connection is in, as tooltip-ready lines.

    Used for the hover text, so a machine can explain its own place in the
    estate without the user switching lenses one at a time.
    """
    cid = _cid(conn)
    if not cid:
        return []
    lines = []
    for lens in (LENS_MACHINE, LENS_SUBNET, LENS_CREDENTIAL):
        for group in compute_relations(lens, connections).groups:
            if cid not in group.members:
                continue
            others = len(group.members) - 1
            crossing = "  (across tenants)" if group.cross_tenant else ""
            lines.append(
                f"{LENS_LABELS[lens]}: {group.label} — with {others} "
                f"other{'' if others == 1 else 's'}{crossing}")
    view = compute_relations(LENS_RESTART, connections, servers, overrides)
    by_id = {_cid(c): c for c in connections or [] if isinstance(c, dict)}
    for edge in view.edges:
        if edge.target == cid:
            lines.append(f"{LENS_LABELS[LENS_RESTART]}: rebooted via {edge.label}")
        elif edge.source == cid:
            targets = sum(1 for e in view.edges if e.source == cid)
            lines.append(
                f"{LENS_LABELS[LENS_RESTART]}: reboots {targets} "
                f"machine{'' if targets == 1 else 's'}")
            break
    return lines
