"""One way to open an SSH connection, shared by every part of RPMote.

Before this, each feature built its own ``SSHClient.connect`` call with its own
subset of the options, which is why key passphrases were fed in as passwords,
why an agent was never mentioned anywhere, and why a machine behind a bastion
simply could not be reached.

What this adds over a bare connect:

  jump hosts      a chain of bastions, each one verified and authenticated in
                  its own right, with the next hop opened as a direct-tcpip
                  channel through the previous one.
  agent keys      Pageant and the Windows OpenSSH agent, via paramiko.
  passphrases     a key passphrase that is separate from the login password.
  MFA             a keyboard-interactive fallback that asks the caller, so a
                  one-time code can come from a dialog instead of stdin.

Host keys go through ``app.host_keys`` for every hop, so a bastion gets the
same scrutiny as the machine behind it.
"""

from __future__ import annotations

import socket

import paramiko

from app import host_keys
from app.connection_manager import load_ssh_private_key

DEFAULT_TIMEOUT = 15.0

#: Connection fields that describe how to reach a host. Kept in one place so
#: the jump-chain resolver and the dialog agree on the vocabulary.
HOST_FIELDS = ("host", "port", "username", "password", "key_file",
               "key_passphrase", "use_agent")


class SSHConnectError(Exception):
    """A connection could not be opened, with a message worth showing."""


class MFAChallenge(Exception):
    """The server wants keyboard-interactive answers nobody supplied."""

    def __init__(self, host, port, title, instructions, prompts):
        super().__init__(f"{host}:{port} requires additional authentication")
        self.host = host
        self.port = port
        self.title = title
        self.instructions = instructions
        #: [(prompt_text, echo_bool), ...]
        self.prompts = list(prompts)


def agent_keys() -> list:
    """Keys the local SSH agent is holding, or an empty list.

    paramiko finds Pageant and the Windows OpenSSH agent by itself; an agent
    that is simply not running is not an error worth surfacing.
    """
    try:
        return list(paramiko.Agent().get_keys())
    except Exception:
        return []


def has_agent() -> bool:
    return bool(agent_keys())


# ── Jump chains ─────────────────────────────────────────────────────


def resolve_jump_chain(conn: dict, connections: list, _seen=None) -> list:
    """Expand ``jump_via`` into the list of hops to traverse, nearest last.

    A hop may itself sit behind a bastion, so this follows the chain. A cycle
    is broken rather than followed: a mis-set pair of connections pointing at
    each other must not hang the app.
    """
    chain: list = []
    seen = set(_seen or ())
    current = conn
    by_id = {}
    for candidate in connections or []:
        if isinstance(candidate, dict):
            key = str(candidate.get("id", "") or "").strip()
            if key:
                by_id[key] = candidate

    while True:
        jump_id = str(current.get("jump_via", "") or "").strip()
        if not jump_id or jump_id in seen:
            break
        seen.add(jump_id)
        hop = by_id.get(jump_id)
        if hop is None:
            raise SSHConnectError(
                "This connection is set to go through a jump host that is no "
                "longer saved. Clear or re-pick it in the connection settings."
            )
        chain.append(hop)
        current = hop

    # Nearest bastion first is the order they must be dialled in.
    return chain


def _auth_kwargs(conn: dict, password: str | None = None) -> dict:
    """Auth arguments for one host, from its saved fields."""
    kwargs: dict = {}
    secret = conn.get("password", "") if password is None else password
    key_file = str(conn.get("key_file", "") or "").strip()
    # A key passphrase is its own field now. Falling back to the login
    # password preserves how older saved connections were set up, where the
    # one box had to serve both purposes.
    passphrase = str(conn.get("key_passphrase", "") or "") or secret or None

    if key_file:
        try:
            kwargs["pkey"] = load_ssh_private_key(key_file, passphrase)
        except Exception as exc:
            if secret:
                # The key did not load but there is a password to try; the
                # server gets the final say on whether that is enough.
                kwargs["password"] = secret
            else:
                raise SSHConnectError(f"Key auth failed: {exc}") from exc
    elif secret:
        kwargs["password"] = secret

    # An agent is consulted unless the connection says not to. paramiko does
    # this by default; naming it makes the behaviour visible and settable.
    kwargs["allow_agent"] = bool(conn.get("use_agent", True))
    kwargs["look_for_keys"] = bool(conn.get("look_for_keys", True))
    return kwargs


def _host_port(conn: dict) -> tuple:
    host = str(conn.get("host", "") or "").strip()
    if not host:
        raise SSHConnectError("This connection has no hostname.")
    try:
        port = int(conn.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    return host, port


def _connect_one(conn: dict, *, password=None, sock=None, sock_factory=None,
                 timeout=DEFAULT_TIMEOUT, extra_host_keys=None, compress=True,
                 interactive=None):
    """Open and authenticate one hop, host key verified.

    ``sock_factory`` produces the carrier socket on demand. A retry cannot
    reuse the first attempt's channel — it died with that attempt — so a hop
    behind a bastion hands in a factory rather than a socket.
    """
    host, port = _host_port(conn)
    if sock is None and sock_factory is not None:
        sock = sock_factory()
    client = paramiko.SSHClient()
    kwargs = _auth_kwargs(conn, password)
    kwargs.update({
        "username": str(conn.get("username", "") or ""),
        "timeout": timeout,
        "auth_timeout": timeout,
        "banner_timeout": timeout,
        "compress": compress,
    })
    if sock is not None:
        kwargs["sock"] = sock

    try:
        host_keys.connect(client, host, port,
                          extra_keys=extra_host_keys, **kwargs)
    except paramiko.AuthenticationException:
        if interactive is None:
            client.close()
            raise
        # Every other method was refused. A server asking for a one-time code
        # gets one more attempt, this time answering its questions — on a
        # fresh carrier, because the first attempt closed the old one.
        client.close()
        retry_sock = sock_factory() if sock_factory is not None else None
        if retry_sock is None and sock is not None:
            # A hop with no way to reopen its channel cannot be retried.
            raise
        return _connect_keyboard_interactive(
            conn, host, port, sock=retry_sock, timeout=timeout,
            extra_host_keys=extra_host_keys, interactive=interactive)
    except Exception:
        client.close()
        raise
    return client


def _connect_keyboard_interactive(conn, host, port, *, sock, timeout,
                                  extra_host_keys, interactive):
    """Authenticate by answering the server's questions.

    paramiko's own fallback reads the answers from stdin, which a windowed
    application does not have; this hands the prompts to a callback instead.
    """
    if sock is None:
        sock = socket.create_connection((host, port), timeout=timeout)
    transport = paramiko.Transport(sock)
    transport.banner_timeout = timeout
    try:
        transport.start_client(timeout=timeout)
    except Exception as exc:
        transport.close()
        raise SSHConnectError(f"SSH handshake failed: {exc}") from exc

    server_key = transport.get_remote_server_key()
    store = host_keys.store()
    trusted = store.is_trusted(host, port, server_key) or any(
        key.asbytes() == server_key.asbytes() for key in (extra_host_keys or ())
    )
    if not trusted:
        transport.close()
        if store.is_known_host(host, port):
            raise host_keys.HostKeyMismatch(
                host, port, server_key, None, store.fingerprints_for(host, port))
        raise host_keys.UnknownHostKey(host, port, server_key)

    username = str(conn.get("username", "") or "")

    def handler(title, instructions, prompt_list):
        return interactive(MFAChallenge(host, port, title, instructions,
                                        prompt_list))

    try:
        transport.auth_interactive(username, handler)
    except Exception as exc:
        transport.close()
        raise paramiko.AuthenticationException(str(exc)) from exc

    client = paramiko.SSHClient()
    client._transport = transport
    return client


def _chain_close(client, hops: list):
    """Make closing the far end close every bastion behind it."""
    original_close = client.close

    def close():
        try:
            original_close()
        finally:
            for hop in reversed(hops):
                try:
                    hop.close()
                except Exception:
                    pass

    client.close = close


def open_client(conn: dict, *, connections=None, password=None,
                timeout=DEFAULT_TIMEOUT, extra_host_keys=None,
                compress=True, interactive=None):
    """Open an SSH client for ``conn``, through its jump hosts if it has any.

    Raises ``host_keys.HostKeyUnverified`` for any hop whose key is unknown or
    has changed, so the caller can ask about that specific machine.
    """
    chain = conn.get("jump_chain")
    if chain is None:
        chain = resolve_jump_chain(conn, connections or [])
    chain = [hop for hop in chain if isinstance(hop, dict)]

    hops: list = []
    sock = None
    try:
        # Dial outward: the furthest bastion first, each one carrying the next.
        factory = None
        for hop in reversed(chain):
            hop_client = _connect_one(
                hop, sock_factory=factory, timeout=timeout, compress=False,
                interactive=interactive)
            hops.append(hop_client)
            target_host, target_port = _next_hop_address(chain, hop, conn)
            transport = hop_client.get_transport()
            if transport is None:
                raise SSHConnectError(
                    f"Jump host {hop.get('name') or hop.get('host')} did not "
                    f"open a usable session.")
            factory = _channel_factory(transport, target_host, target_port)

        client = _connect_one(
            conn, password=password, sock_factory=factory, timeout=timeout,
            extra_host_keys=extra_host_keys, compress=compress,
            interactive=interactive)
    except Exception:
        for hop_client in reversed(hops):
            try:
                hop_client.close()
            except Exception:
                pass
        raise

    if hops:
        _chain_close(client, hops)
        client._rpmote_jump_hops = hops
    return client


def _channel_factory(transport, host, port):
    """A callable that opens a fresh channel through ``transport`` each time."""
    def open_channel():
        return transport.open_channel(
            "direct-tcpip", (host, port), ("127.0.0.1", 0))

    return open_channel


def _next_hop_address(chain: list, hop: dict, final: dict) -> tuple:
    """Where ``hop`` should open its channel to: the next hop, or the target."""
    index = chain.index(hop)
    following = chain[index - 1] if index > 0 else final
    return _host_port(following)


def describe_route(conn: dict, connections=None) -> str:
    """Human-readable route, for a tooltip or a confirmation line."""
    try:
        chain = conn.get("jump_chain")
        if chain is None:
            chain = resolve_jump_chain(conn, connections or [])
    except SSHConnectError:
        return str(conn.get("host", "") or "")
    names = [str(hop.get("name") or hop.get("host") or "?")
             for hop in reversed(chain or [])]
    names.append(str(conn.get("host", "") or "?"))
    return " → ".join(names)
