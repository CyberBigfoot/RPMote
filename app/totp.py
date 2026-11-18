"""Time-based one-time codes for the consoles that ask for a second factor.

A NAS with 2-step verification turned on is the one connection RPMote could
not open on its own: it holds the username and the password, then stops at a
six-digit box and sends the person who owns the estate off to find their
phone.  Storing the shared secret alongside the password puts that last step
in the same vault as the first two.

That is a deliberate trade.  A secret kept next to the password it protects is
no longer a second *factor* in the strict sense — anyone who opens the vault
has both.  It is still worth having: the vault is master-password protected
and encrypted at rest, the alternative in practice is a code the technician
copies out of a shared phone, and the NAS keeps enforcing the code against
everyone who does not have RPMote.  Whether to store it is the user's call,
one connection at a time; nothing here turns itself on.

The implementation is RFC 6238 (TOTP) over RFC 4226 (HOTP), in the standard
library.  It accepts either the bare base32 secret a NAS shows next to the QR
code — spaces, lowercase and missing padding included, because that is how it
is displayed and how it gets pasted — or the whole ``otpauth://`` URI behind
the QR code, which carries the digit count and period with it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
import time
from urllib.parse import parse_qs, unquote, urlsplit

#: What almost every authenticator uses, and what the parameters fall back to
#: when a bare secret is pasted with nothing to say otherwise.
DEFAULT_DIGITS = 6
DEFAULT_PERIOD = 30
DEFAULT_ALGORITHM = "SHA1"

#: Connection record field holding the secret, as typed. Kept in whatever form
#: it was pasted so an ``otpauth://`` URI does not lose its parameters.
TOTP_SECRET_KEY = "totp_secret"

_ALGORITHMS = {
    "SHA1": hashlib.sha1,
    "SHA256": hashlib.sha256,
    "SHA512": hashlib.sha512,
}

#: Base32 as RFC 4648 spells it, before padding is worked out.
_BASE32_RE = re.compile(r"^[A-Z2-7]+$")

#: Separators a secret is displayed with. Synology groups it in fours, Google
#: Authenticator in fours, others use hyphens; none of them are data.
_NOISE_RE = re.compile(r"[\s\-_]+")


class TOTPError(ValueError):
    """A secret that cannot produce codes, with a reason worth showing."""


def looks_like_uri(text: str) -> bool:
    """True for the ``otpauth://`` form, which carries its own parameters."""
    return str(text or "").strip().lower().startswith("otpauth://")


def parse_secret(text: str) -> dict:
    """Turn what the user pasted into everything needed to make a code.

    Returns ``{"secret", "digits", "period", "algorithm", "issuer", "label"}``
    with the secret as clean base32.  Raises :class:`TOTPError` with a message
    written for the person who did the pasting.
    """
    raw = str(text or "").strip()
    if not raw:
        raise TOTPError("No secret entered.")

    digits = DEFAULT_DIGITS
    period = DEFAULT_PERIOD
    algorithm = DEFAULT_ALGORITHM
    issuer = ""
    label = ""

    if looks_like_uri(raw):
        parts = urlsplit(raw)
        if parts.netloc.lower() not in ("totp", ""):
            raise TOTPError(
                f"“{parts.netloc}” codes are not time-based — RPMote can only "
                "generate otpauth://totp/… codes.")
        query = parse_qs(parts.query)
        secret_text = (query.get("secret") or [""])[0]
        if not secret_text:
            raise TOTPError("That otpauth:// link has no secret= parameter.")
        label = unquote(parts.path.lstrip("/"))
        issuer = (query.get("issuer") or [""])[0]
        if not issuer and ":" in label:
            issuer = label.split(":", 1)[0].strip()
        digits = _positive_int(query.get("digits"), DEFAULT_DIGITS, "digits")
        period = _positive_int(query.get("period"), DEFAULT_PERIOD, "period")
        algorithm = ((query.get("algorithm") or [DEFAULT_ALGORITHM])[0] or "").upper()
        if algorithm not in _ALGORITHMS:
            raise TOTPError(
                f"“{algorithm}” is not an algorithm RPMote can use "
                f"({', '.join(sorted(_ALGORITHMS))}).")
    else:
        secret_text = raw

    if not 6 <= digits <= 10:
        raise TOTPError(f"A {digits}-digit code is outside what TOTP allows (6–10).")

    return {
        "secret": normalize_secret(secret_text),
        "digits": digits,
        "period": period,
        "algorithm": algorithm,
        "issuer": issuer,
        "label": label,
    }


def normalize_secret(text: str) -> str:
    """Strip the display formatting off a base32 secret and check it decodes.

    Padding is dropped rather than preserved: a secret is displayed without
    it, so re-adding it at decode time is more reliable than trusting what
    came in.
    """
    cleaned = _NOISE_RE.sub("", str(text or "")).upper().rstrip("=")
    if not cleaned:
        raise TOTPError("No secret entered.")
    if not _BASE32_RE.match(cleaned):
        bad = sorted({c for c in cleaned if not _BASE32_RE.match(c)})
        raise TOTPError(
            "That is not a base32 secret — it contains "
            f"{', '.join(repr(c) for c in bad[:4])}. A NAS shows the secret "
            "next to the QR code as letters A–Z and digits 2–7.")
    decode_secret(cleaned)  # raises if the length is not a whole number of bytes
    return cleaned


def decode_secret(secret: str) -> bytes:
    """Base32-decode a normalized secret into the HMAC key."""
    padded = secret + "=" * (-len(secret) % 8)
    try:
        return base64.b32decode(padded, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise TOTPError(f"That secret is not valid base32 ({exc}).") from exc


def generate(text: str, at: float | None = None) -> str:
    """The code for ``text`` right now, or at the Unix time ``at``."""
    parsed = parse_secret(text)
    return generate_parsed(parsed, at)


def generate_parsed(parsed: dict, at: float | None = None) -> str:
    """Same as :func:`generate` for an already-parsed secret."""
    now = time.time() if at is None else float(at)
    counter = int(now // parsed["period"])
    key = decode_secret(parsed["secret"])
    digest = hmac.new(
        key, struct.pack(">Q", counter), _ALGORITHMS[parsed["algorithm"]]
    ).digest()
    # RFC 4226 dynamic truncation: the low nibble of the last byte picks which
    # four bytes of the digest become the code.
    offset = digest[-1] & 0x0F
    (truncated,) = struct.unpack(">I", digest[offset:offset + 4])
    return str((truncated & 0x7FFFFFFF) % (10 ** parsed["digits"])).zfill(parsed["digits"])


def seconds_remaining(parsed: dict, at: float | None = None) -> int:
    """How long this code stays valid, in whole seconds, at least 1."""
    now = time.time() if at is None else float(at)
    period = parsed["period"]
    return max(1, int(period - (now % period)))


def current_code(text: str, at: float | None = None) -> tuple[str, int]:
    """``(code, seconds left)`` — what a countdown next to the code needs."""
    parsed = parse_secret(text)
    return generate_parsed(parsed, at), seconds_remaining(parsed, at)


def has_totp(connection) -> bool:
    """True when this connection carries a second factor RPMote can supply."""
    if not isinstance(connection, dict):
        return False
    return bool(str(connection.get(TOTP_SECRET_KEY, "") or "").strip())


def code_for_connection(connection, at: float | None = None) -> tuple[str, int] | None:
    """``(code, seconds left)`` for a connection, or ``None`` if it has no secret.

    A stored secret that has since gone bad — hand-edited, or truncated on the
    way in — returns ``None`` rather than raising: a broken second factor must
    not stop the console from opening at all, it just leaves the box empty.
    """
    if not has_totp(connection):
        return None
    try:
        return current_code(connection[TOTP_SECRET_KEY], at)
    except TOTPError:
        return None


def _positive_int(values, fallback: int, what: str) -> int:
    if not values:
        return fallback
    try:
        number = int(str(values[0]).strip())
    except (TypeError, ValueError):
        raise TOTPError(f"“{values[0]}” is not a number of {what}.") from None
    if number <= 0:
        raise TOTPError(f"The {what} must be greater than zero.")
    return number
