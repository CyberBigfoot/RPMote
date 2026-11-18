"""RD Gateway: what a connection knows about its gateway, and what mstsc is
told about it.

The customer sites that publish RDP straight to the internet are the ones an
MSP is trying to get rid of; the rest sit behind an RD Gateway, and a saved
connection that cannot describe its gateway is a saved connection you still
have to open mstsc by hand for.

Three settings, matching what mstsc calls them under Advanced → Connect from
anywhere, because that is the dialog the person filling this in has open in
their other hand:

  Server address                       gatewayhostname
  Bypass RD Gateway server for local   gatewayusagemethod 2 rather than 1 —
    addresses                          try the machine directly first, and
                                       only go through the gateway if that
                                       fails, which is what "local" means in
                                       practice from inside the network
  Use my RD Gateway credentials for    promptcredentialonce — one set of
    the remote computer                credentials answers both challenges

Nothing in here touches Qt: it is the part of the RDP launcher that can be
reasoned about, and tested, without a window.
"""

from __future__ import annotations

import logging
import re

from .windows_credentials import (
    CRED_TYPE_DOMAIN_PASSWORD,
    CredentialReport,
    store_credentials,
)

_LOG = logging.getLogger("rpmote.rdp.gateway")

#: Connection record fields. Flat, like ``rdp_embedded``, so an exported
#: connection stays readable and an older record simply has none of them.
RDP_GATEWAY_HOST_KEY = "rdp_gateway"
RDP_GATEWAY_BYPASS_KEY = "rdp_gateway_bypass_local"
RDP_GATEWAY_SHARE_CREDS_KEY = "rdp_gateway_use_for_remote"
RDP_GATEWAY_USERNAME_KEY = "rdp_gateway_username"
RDP_GATEWAY_PASSWORD_KEY = "rdp_gateway_password"

#: mstsc's own defaults for a connection whose gateway is enabled: bypass on
#: (a direct hop inside the network beats a round trip through the DMZ) and
#: one credential prompt rather than two.
_RDP_GATEWAY_DEFAULTS: dict = {
    "host": "",
    "bypass_local": True,
    "use_for_remote": True,
    "username": "",
    "password": "",
}

#: What an RD Gateway listens on unless it was moved. mstsc assumes it too, so
#: an address typed without a port means this one.
RDP_GATEWAY_DEFAULT_PORT = 443

#: ``gatewayusagemethod`` — when mstsc goes through the gateway.
GATEWAY_USAGE_NEVER = 0
GATEWAY_USAGE_ALWAYS = 1
GATEWAY_USAGE_IF_DIRECT_FAILS = 2   # "Bypass RD Gateway server for local addresses"
GATEWAY_USAGE_DEFAULT = 3
GATEWAY_USAGE_DETECT = 4            # mstsc's own "Automatically detect"

#: ``gatewaycredentialssource`` — how mstsc authenticates to the gateway.
GATEWAY_CREDS_PASSWORD_NTLM = 0
GATEWAY_CREDS_SMARTCARD = 1
GATEWAY_CREDS_LOGGED_ON_USER = 2
GATEWAY_CREDS_PASSWORD_BASIC = 3
GATEWAY_CREDS_SELECT_LATER = 4
GATEWAY_CREDS_COOKIE = 5


def normalize_gateway_host(text) -> str:
    """Reduce whatever was pasted to ``host`` or ``host:port``.

    Gateway addresses get copied out of browser bars and runbooks, so a
    ``https://`` prefix or a trailing ``/remoteDesktopGateway`` path is a
    normal thing to receive and not something to make the user delete.
    """
    host = str(text or "").strip()
    if not host:
        return ""
    if "://" in host:
        host = host.split("://", 1)[1]
    # Everything from the first slash on is a path, not an address. An IPv6
    # literal keeps its brackets — that is the form mstsc wants it in.
    return host.split("/", 1)[0].strip()


def gateway_host_without_port(host: str) -> str:
    """``gw:443`` → ``gw``, leaving an IPv6 literal's own colons alone."""
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def split_gateway_address(host: str,
                          default_port: int = RDP_GATEWAY_DEFAULT_PORT) -> tuple[str, int]:
    """``gw.contoso.com:8443`` → ``("gw.contoso.com", 8443)``.

    Needed wherever the gateway has to be reached as a socket rather than
    handed to mstsc as a string — testing it, mainly. A port that is not a
    number is ignored rather than raised on: the address is still worth
    trying on the default. An IPv6 literal comes back without its brackets,
    which is the form ``socket`` wants and the opposite of what mstsc wants.
    """
    host = normalize_gateway_host(host)
    if not host:
        return "", default_port
    bare = gateway_host_without_port(host)
    remainder = host[len(bare):]
    bare = bare.strip("[]")
    if remainder.startswith(":"):
        try:
            port = int(remainder[1:])
        except ValueError:
            port = default_port
        if 1 <= port <= 65535:
            return bare, port
    return bare, default_port


def normalize_rdp_gateway(saved) -> dict:
    """Coerce a connection record's gateway fields into a complete dict.

    Takes the whole connection (that is where the fields live) or an already
    extracted dict, and tolerates either missing entirely — a connection saved
    before gateways existed has no gateway, which is the right answer.
    """
    result = dict(_RDP_GATEWAY_DEFAULTS)
    if not isinstance(saved, dict):
        return result

    # Two shapes arrive here: a whole connection record, where every gateway
    # field carries the rdp_gateway prefix precisely so it cannot be confused
    # with the connection's own host, username and password; and a dict this
    # function already returned, being passed along. The prefix is not
    # optional — reading a bare "host" off a connection record would point the
    # gateway at the machine it is supposed to be reaching.
    already_normalized = "bypass_local" in saved or "use_for_remote" in saved
    keys = (
        ("host", "host" if already_normalized else RDP_GATEWAY_HOST_KEY),
        ("bypass_local", "bypass_local" if already_normalized else RDP_GATEWAY_BYPASS_KEY),
        ("use_for_remote",
         "use_for_remote" if already_normalized else RDP_GATEWAY_SHARE_CREDS_KEY),
        ("username", "username" if already_normalized else RDP_GATEWAY_USERNAME_KEY),
        ("password", "password" if already_normalized else RDP_GATEWAY_PASSWORD_KEY),
    )
    for field, source_key in keys:
        if source_key not in saved:
            continue
        value = saved[source_key]
        if field in ("bypass_local", "use_for_remote"):
            result[field] = bool(value)
        else:
            result[field] = str(value or "")

    result["host"] = normalize_gateway_host(result["host"])
    result["username"] = result["username"].strip()
    return result


def gateway_is_enabled(gateway) -> bool:
    """A gateway is in use exactly when an address was given for it."""
    return bool(normalize_rdp_gateway(gateway)["host"])


def rdp_gateway_lines(gateway) -> list[str]:
    """Render a gateway selection as .rdp directives.

    Always emits ``promptcredentialonce`` — it governs the remote computer's
    prompt as much as the gateway's, so leaving it to the caller is how the
    two end up disagreeing.  Emits nothing else when no gateway is configured,
    so a connection that does not use one is written exactly as it was before
    this existed, and a gateway pushed by policy still applies.
    """
    g = normalize_rdp_gateway(gateway)
    if not g["host"]:
        return ["promptcredentialonce:i:0"]

    usage = GATEWAY_USAGE_IF_DIRECT_FAILS if g["bypass_local"] else GATEWAY_USAGE_ALWAYS
    lines = [
        f"gatewayhostname:s:{g['host']}",
        # 1 = always via the gateway; 2 = direct first, gateway as the
        # fallback, which is how mstsc spells "bypass for local addresses".
        f"gatewayusagemethod:i:{usage}",
        # 0 = password over NTLM. mstsc looks in Credential Manager first and
        # only prompts when nothing is saved there, which beats failing
        # silently when the seeding did not happen.
        f"gatewaycredentialssource:i:{GATEWAY_CREDS_PASSWORD_NTLM}",
        # 1 = use the settings in this file rather than a stored profile.
        "gatewayprofileusagemethod:i:1",
        f"promptcredentialonce:i:{1 if g['use_for_remote'] else 0}",
    ]
    if g["username"]:
        lines.append(f"gatewayusername:s:{g['username']}")
    return lines


def gateway_login(gateway, fallback_user: str = "", fallback_password: str = "") -> tuple[str, str]:
    """Which credentials answer the gateway's challenge.

    "Use my RD Gateway credentials for the remote computer" says the two
    challenges take one answer, so with that ticked and no separate gateway
    login filled in, the connection's own username and password are what the
    gateway gets.  That is the case an MSP actually has: one AD account, two
    prompts on the way to the same desktop.
    """
    g = normalize_rdp_gateway(gateway)
    if g["username"]:
        return g["username"], g["password"]
    if g["use_for_remote"]:
        return str(fallback_user or "").strip(), str(fallback_password or "")
    return "", ""


def split_account(username: str) -> tuple[str, str]:
    """``CONTOSO\\admin`` → ``("CONTOSO", "admin")``; ``admin@contoso.com`` →
    ``("contoso.com", "admin")``; a bare name has no domain."""
    user = str(username or "").strip()
    if "\\" in user:
        domain, _, name = user.partition("\\")
        return domain.strip(), name.strip()
    if "@" in user:
        name, _, domain = user.rpartition("@")
        return domain.strip(), name.strip()
    return "", user


def account_has_domain(username: str) -> bool:
    """Whether a login names its domain.

    An RD Gateway authenticates over NTLM, and NTLM with no domain is a
    guess the gateway makes — usually its own domain, which is right for
    most sites and wrong for the one that matters.  Worth a hint in the
    dialog; not worth refusing to connect.
    """
    return bool(split_account(username)[0])


def gateway_credential_targets(host: str) -> list[str]:
    """The Credential Manager names mstsc looks a gateway up under.

    The bare address, exactly as it appears in ``gatewayhostname`` — that is
    the one mstsc consults (as a Domain Password credential; see
    ``windows_credentials``).  ``TERMSRV/<gateway>`` is not consulted for the
    gateway hop and is not written.  A gateway typed with a port is stored
    both ways, since which spelling mstsc asks for then has not been pinned
    down and a second write costs nothing.
    """
    host = normalize_gateway_host(host)
    if not host:
        return []
    bare = gateway_host_without_port(host)
    targets = [host]
    if bare and bare.lower() != host.lower():
        targets.append(bare)
    return targets


def seed_gateway_credentials(gateway, fallback_user: str = "",
                             fallback_password: str = "") -> CredentialReport:
    """Store the gateway login where mstsc will look for it.

    Returns an empty report when there is nothing to store — no gateway, or
    a gateway that is meant to ask on its own — so the caller can tell "did
    nothing" from "tried and failed".
    """
    g = normalize_rdp_gateway(gateway)
    if not g["host"]:
        return CredentialReport()
    user, password = gateway_login(g, fallback_user, fallback_password)
    if not user or not password:
        _LOG.info("Gateway %s: no login to store; mstsc will ask", g["host"])
        return CredentialReport()
    # Domain Password, not generic: the only kind mstsc's gateway prompt
    # reads.  Any username form is accepted for it — bare, DOMAIN\user or
    # user@domain — and whether the gateway then likes the form is its own
    # answer.
    report = store_credentials(gateway_credential_targets(g["host"]), user, password,
                               CRED_TYPE_DOMAIN_PASSWORD)
    if report.ok:
        _LOG.info("Gateway %s: login for %s stored under %s",
                  g["host"], user, ", ".join(report.targets_written))
    else:
        _LOG.warning("Gateway %s: could not store login for %s: %s",
                     g["host"], user, report.failure)
    return report


def describe_gateway_route(gateway, target_host: str) -> str:
    """One line for the status bar saying how this session is getting there."""
    g = normalize_rdp_gateway(gateway)
    if not g["host"]:
        return f"Connecting to {target_host}..."
    route = "direct first, then " if g["bypass_local"] else "always through "
    return f"Connecting to {target_host} ({route}RD Gateway {g['host']})..."


# ── What mstsc said, in words that lead somewhere ──────────────────────────
#
# mstsc reports a failed gateway hop in a message box the user may never see
# behind the app, and even seen, "the Remote Desktop Gateway server is
# temporarily unavailable" does not say what to do.  These are the messages
# an MSP actually meets, each paired with the thing to check.

# Each entry: (every one of these must appear, any one of these must appear,
# what to do).  Either tuple may be empty.
_MSTSC_GUIDANCE: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("certificate",), ("subject name", "do not match", "does not match"),
     "The gateway's certificate is issued to a different name than the "
     "address you entered. Use the name on the certificate as the gateway "
     "address."),
    (("certificate",), ("not trusted", "untrusted", "certification authority"),
     "This PC does not trust the gateway's certificate. Import the gateway's "
     "certificate chain into Trusted Root Certification Authorities, or fix "
     "the certificate on the gateway."),
    (("certificate",), ("expired", "revoked"),
     "The gateway's certificate has expired or been revoked. It has to be "
     "renewed on the gateway before anyone can connect through it."),
    ((), ("temporarily unavailable",),
     "The gateway did not answer. Check the address, that port 443 is open "
     "from here, and that the Remote Desktop Gateway service is running."),
    (("gateway",), ("server address", "unreachable", "could not be resolved",
                    "cannot be resolved"),
     "The gateway address could not be reached. Check the spelling and that "
     "it resolves from this network."),
    ((), ("connection authorization policy", "rd cap", "not authorized to connect"),
     "The gateway refused this account. Its connection authorization policy "
     "(RD CAP) does not include this user or group."),
    ((), ("resource authorization policy", "rd rap", "not authorized to access",
          "does not allow access to this computer"),
     "The gateway refused the destination. Its resource authorization policy "
     "(RD RAP) does not allow this account to reach that computer."),
    ((), ("credentials did not work", "logon attempt failed",
          "user name or password is incorrect", "credentials that were used"),
     "The username or password was rejected. Gateways authenticate over "
     "NTLM, so give the account as DOMAIN\\user or user@domain."),
    ((), ("network level authentication",
          "remote computer that you are trying to connect to requires"),
     "The remote computer requires Network Level Authentication and the "
     "account could not be verified for it. Check the username, domain and "
     "password."),
    (("gateway",), ("could not be authenticated", "cannot be authenticated"),
     "The gateway could not authenticate this PC's credentials. Give the "
     "account as DOMAIN\\user."),
    (("connections",), ("too many", "maximum number"),
     "The gateway or the remote computer is at its connection limit."),
    ((), ("internal error",),
     "mstsc reported an internal error. Reconnect; if it persists, try the "
     "connection in mstsc directly to see whether the gateway or the desktop "
     "is at fault."),
)


def explain_mstsc_message(text: str) -> str:
    """Turn the text of an mstsc message box into something to act on.

    Returns the original message, tidied, when nothing here matches — the
    user still gets to see it, which is already more than they had.
    """
    message = re.sub(r"\s+", " ", str(text or "")).strip()
    if not message:
        return ""
    lowered = message.lower()
    for all_of, any_of, guidance in _MSTSC_GUIDANCE:
        if all(n in lowered for n in all_of) and (not any_of or any(n in lowered for n in any_of)):
            return guidance
    return message


def is_gateway_message(text: str) -> bool:
    """Whether an mstsc message is about the gateway hop rather than the
    desktop behind it — decides which part of the status to blame."""
    lowered = str(text or "").lower()
    return "gateway" in lowered or "rd cap" in lowered or "rd rap" in lowered
