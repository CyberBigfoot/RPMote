"""
Plain-English reasons for connection failures, and which kind each one is.

Every connection type fails the same few ways: the name does not resolve,
nothing answers, something answers and refuses, or the login is wrong.
Windows reports each as a Winsock number nobody remembers. The *kind* decides
what the failure panel offers next — Wake for a box that does not answer,
Edit for a login that was rejected — and the sentence says what happened.
"""

from __future__ import annotations

import errno
import socket

DNS = "dns"
TIMEOUT = "timeout"
REFUSED = "refused"
UNREACHABLE = "unreachable"
RESET = "reset"
AUTH = "auth"
HOST_KEY = "host_key"
PROTOCOL = "protocol"
OTHER = "other"

_UNREACHABLE = {10051, 10065, errno.ENETUNREACH, errno.EHOSTUNREACH}
_TIMED_OUT = {10060, errno.ETIMEDOUT}
_REFUSED = {10061, errno.ECONNREFUSED}
_RESET = {10053, 10054, errno.ECONNRESET, errno.ECONNABORTED}


def _class_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def classify(exc: BaseException) -> str:
    """Which of the kinds above *exc* is (paramiko's included, unimported)."""
    names = _class_names(exc)
    if names & {"HostKeyUnverified", "BadHostKeyException"}:
        return HOST_KEY
    if "AuthenticationException" in names:
        return AUTH
    if "NoValidConnectionsError" in names:
        # paramiko's "every address failed": go by what the addresses said.
        kinds = {classify(err) for err in (getattr(exc, "errors", None) or {}).values()}
        for kind in (TIMEOUT, UNREACHABLE, REFUSED):
            if kind in kinds:
                return kind
        return REFUSED
    if isinstance(exc, socket.gaierror):
        return DNS
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return TIMEOUT
    if isinstance(exc, ConnectionRefusedError):
        return REFUSED
    if isinstance(exc, OSError):
        code = getattr(exc, "winerror", None) or exc.errno
        if code in _UNREACHABLE:
            return UNREACHABLE
        if code in _TIMED_OUT:
            return TIMEOUT
        if code in _REFUSED:
            return REFUSED
        if code in _RESET:
            return RESET
    if "SSHException" in names:
        return PROTOCOL
    return OTHER


def describe_network_error(exc: Exception, host: str, port) -> str:
    """Say what actually went wrong, instead of quoting a Winsock number.

    ``[Errno 11001] getaddrinfo failed`` is the message Windows gives when a
    name does not resolve, and it tells the person reading it nothing about
    which name or what to do. The three failures that account for almost every
    test are worth spelling out.
    """
    if isinstance(exc, socket.gaierror):
        return (f"“{host}” did not resolve from this PC. Check the spelling, "
                f"or use its IP address if the name only exists on the "
                f"customer's DNS.")
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return (f"{host}:{port} did not answer within 5 seconds — it resolved, "
                f"but nothing is routing to it or a firewall is dropping it.")
    if isinstance(exc, ConnectionRefusedError):
        return (f"{host} refused the connection on port {port} — the machine "
                f"is reachable but nothing is listening there.")
    kind = classify(exc)
    if kind == UNREACHABLE:
        return (f"This PC has no route to {host} — usually a VPN that is not "
                f"connected, or an address on a network this PC is not on.")
    if kind == RESET:
        return (f"{host} cut the connection off — the service restarted, or a "
                f"firewall closed it.")
    return str(exc)


def describe_failure(exc: BaseException, host: str, port, username: str = "") -> tuple[str, str]:
    """``(kind, sentence)`` for a failed attempt to connect."""
    kind = classify(exc)
    if kind == AUTH:
        who = f" for “{username}”" if username else ""
        return kind, (f"{host} rejected the login{who}. Check the username and "
                      f"password — or, for a key, that the public key is in "
                      f"~/.ssh/authorized_keys for that user.")
    if kind == HOST_KEY:
        return kind, ("The server's identity (its host key) was not accepted, so "
                      "nothing was sent to it.")
    if kind == PROTOCOL:
        detail = str(exc).strip()
        return kind, (f"Something answered on {host}:{port} but the handshake did "
                      f"not finish — the wrong port, a service that is not what "
                      f"was expected, or a firewall cutting it short."
                      + (f" ({detail})" if detail else ""))
    if "NoValidConnectionsError" in _class_names(exc):
        errors = list((getattr(exc, "errors", None) or {}).values())
        if errors:
            return kind, describe_network_error(errors[0], host, port)
    text = describe_network_error(exc, host, port)
    return kind, (text or exc.__class__.__name__)


def failure_title(kind: str, name: str) -> str:
    """The headline over a failure, named after the machine."""
    name = name or "the server"
    return {
        DNS: f"Can't find {name}",
        TIMEOUT: f"{name} isn't answering",
        REFUSED: f"{name} refused the connection",
        UNREACHABLE: f"No route to {name}",
        RESET: f"{name} dropped the connection",
        AUTH: f"Sign-in to {name} was rejected",
        HOST_KEY: f"{name}'s identity wasn't accepted",
        PROTOCOL: f"{name} answered, but not as expected",
    }.get(kind, f"Couldn't connect to {name}")


#: Failures where the machine may simply be switched off.
WAKEABLE = {TIMEOUT, UNREACHABLE, OTHER}
