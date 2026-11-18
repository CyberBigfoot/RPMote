"""Import connections from an OpenSSH ``~/.ssh/config``.

Anyone arriving at RPMote from the command line already has their estate
written down once. Retyping thirty hosts to try the app is the reason they
would not try the app.

What is read: HostName, Port, User, IdentityFile and ProxyJump. Wildcard
blocks (``Host *``) are settings, not machines, so they are applied to the
hosts they match rather than imported as connections of their own — which
paramiko's own config parser already does for us.
"""

from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from pathlib import Path

import paramiko
import paramiko.config

DEFAULT_TENANT = "SSH Config"


@contextmanager
def _without_fqdn_lookup(config_text: str):
    """Skip the local-FQDN lookup unless the config actually uses ``%l``.

    Expanding a config value makes paramiko resolve the local FQDN, and
    ``socket.getfqdn()`` blocks for the full reverse-DNS timeout on a machine
    where that is misconfigured — ten seconds a host, on a VPN, is normal.
    The value is only ever substituted into ``%l``, so when the file does not
    contain one the answer cannot matter.
    """
    if "%l" in config_text:
        yield
        return
    original = paramiko.config.LazyFqdn.__str__
    paramiko.config.LazyFqdn.__str__ = lambda self: socket.gethostname()
    try:
        yield
    finally:
        paramiko.config.LazyFqdn.__str__ = original


def default_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def _looks_like_a_pattern(name: str) -> bool:
    """True for ``Host *`` style entries, which name no single machine."""
    return any(ch in name for ch in "*?!") or name.strip() == ""


def parse_config(path=None) -> list:
    """Read an ssh config into connection dicts, in the order it lists them."""
    path = Path(path) if path else default_config_path()
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8", errors="replace")
    config = paramiko.SSHConfig()
    config.parse(text.splitlines())

    entries = []
    seen = set()
    with _without_fqdn_lookup(text):
        for name in config.get_hostnames():
            if _looks_like_a_pattern(name) or name in seen:
                continue
            seen.add(name)
            entries.append(_entry_for(config, name))
    # get_hostnames() is a set; sort so an import is reproducible.
    entries.sort(key=lambda item: item["name"].lower())
    return entries


def _entry_for(config, name: str) -> dict:
    values = config.lookup(name)
    identity = values.get("identityfile") or []
    if isinstance(identity, str):
        identity = [identity]
    key_file = os.path.expanduser(identity[0]) if identity else ""

    try:
        port = int(values.get("port", 22) or 22)
    except (TypeError, ValueError):
        port = 22

    proxy = values.get("proxyjump", "") or ""
    return {
        "name": name,
        "host": values.get("hostname", name) or name,
        "port": port,
        "username": values.get("user", "") or "",
        "key_file": key_file,
        "protocol": "SSH",
        "tenant": DEFAULT_TENANT,
        # Kept as written; it is resolved against the imported set below.
        "proxy_jump": proxy.split(",")[0].strip() if proxy else "",
    }


def link_jump_hosts(entries: list) -> list:
    """Turn ProxyJump names into jump_via links between imported entries.

    A ProxyJump naming a host that is not in the file is left alone rather
    than guessed at — it will show as unset, which is honest.
    """
    by_name = {entry["name"]: entry for entry in entries}
    by_host = {entry["host"]: entry for entry in entries}
    for entry in entries:
        target = entry.pop("proxy_jump", "")
        if not target:
            continue
        # ProxyJump may be written as user@host:port.
        bare = target.split("@")[-1].split(":")[0]
        hop = by_name.get(target) or by_name.get(bare) or by_host.get(bare)
        if hop is not None and hop is not entry:
            entry["jump_via"] = hop.get("id") or hop["name"]
    return entries


def _identity(conn: dict) -> tuple:
    return (
        str(conn.get("host", "") or "").strip().lower(),
        int(conn.get("port", 22) or 22),
        str(conn.get("username", "") or "").strip().lower(),
    )


def new_entries(entries: list, existing: list) -> list:
    """The entries that are not already saved, matched on host/port/user."""
    known = {_identity(conn) for conn in existing or [] if isinstance(conn, dict)}
    fresh = []
    for entry in entries:
        if _identity(entry) in known:
            continue
        known.add(_identity(entry))
        fresh.append(entry)
    return fresh


def read_importable(path=None, existing=None) -> list:
    """Everything in the config file that is not already a saved connection."""
    return new_entries(link_jump_hosts(parse_config(path)), existing or [])
