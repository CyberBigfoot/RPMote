"""Guardrails for the machines you cannot afford to get wrong.

An MSP's estate is not uniform: a lab box and a customer's production database
server sit two rows apart in the same tree, and the terminal that opens on
them looks identical.  This module supplies the two things that make them look
different — knowing which connections are production, and knowing which
commands are worth stopping to think about — so the terminal can put a banner
up and ask before a command reaches many machines at once.

Nothing here blocks anything.  A guardrail that cannot be overridden is a
guardrail that gets worked around, and the point is to make the risky thing
deliberate, not impossible.

Three ways a connection is production, in order:

  environment   An explicit per-connection choice, made in the connection
                dialog.  Always wins, including to say "no, this one isn't".
  tenant        A tenant the user has marked production, kept in the vault so
                it applies to every connection under it.
  the name      "acme-prod-sql01" is production whether or not anyone got
                round to ticking a box, and an estate imported from
                ~/.ssh/config has nothing but names to go on.
"""

from __future__ import annotations

import re

#: Values the connection dialog offers. "" means "not stated", which is what
#: every connection saved before this existed will have.
ENVIRONMENTS = ("", "production", "staging", "development")

ENVIRONMENT_LABELS = {
    "": "Not specified",
    "production": "Production",
    "staging": "Staging / UAT",
    "development": "Development / lab",
}

#: Words that mean production in a hostname or a tenant name. Matched whole:
#: "prod-web-01" and "ACME (Prod)" count, "product-catalogue" does not.
PRODUCTION_WORDS = ("prod", "prods", "production", "prd", "live", "lve")

_PRODUCTION_RE = re.compile(
    r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % "|".join(PRODUCTION_WORDS)
)

#: (pattern, what it does). Ordered most-destructive first so the reason a
#: dialog leads with is the worst thing in the line, not the first thing.
DANGEROUS_PATTERNS = (
    # Any rm carrying a recursive or force flag, however the flags are spelt
    # or split up: -rf, -r -f, --recursive --force.
    (r"\brm\s+(?:-{1,2}[A-Za-z-]+\s+)*-{1,2}[A-Za-z-]*[rRfF]",
     "deletes recursively or without asking (rm)"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bmkfs(\.\w+)?\b", "formats a filesystem"),
    (r"\bdd\b[^\n]*\bof=/dev/", "writes straight to a device"),
    (r">\s*/dev/[sh]d[a-z]", "writes straight to a device"),
    (r"\bFormat-Volume\b", "formats a volume"),
    (r"\b(shutdown|halt|poweroff)\b", "shuts the machine down"),
    (r"\breboot\b|\binit\s+[06]\b|\bRestart-Computer\b|\bStop-Computer\b",
     "restarts the machine"),
    (r"\bDROP\s+(DATABASE|TABLE|SCHEMA)\b", "drops a database object"),
    (r"\bTRUNCATE\s+TABLE\b", "empties a table"),
    (r"\bRemove-Item\b[^\n]*-Recurse", "recursive delete (Remove-Item)"),
    (r"\bkubectl\s+delete\b", "deletes Kubernetes objects"),
    (r"\bdocker\s+(system\s+prune|rm\s+-f|volume\s+rm)\b",
     "removes Docker state"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "stops a service"),
    (r"\biptables\s+-F\b|\bufw\s+disable\b|\bnetsh\s+advfirewall\s+set\b",
     "changes the firewall"),
    (r"\b(userdel|deluser|Remove-LocalUser|Remove-ADUser)\b",
     "deletes a user account"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\b", "makes files world-writable"),
    (r"\b(chown|chmod)\s+-R\b", "changes ownership recursively"),
    (r"\bkillall\b|\bpkill\s+-9\b", "kills processes by name"),
    (r"\bgit\s+push\b[^\n]*(--force|-f)\b", "force-pushes over history"),
    (r"\btruncate\s+-s\s*0\b", "empties a file"),
)

_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in DANGEROUS_PATTERNS
)


def normalize_environment(value) -> str:
    """The environment string, or "" for anything unrecognised."""
    text = str(value or "").strip().lower()
    return text if text in ENVIRONMENTS else ""


def normalize_production_tenants(value) -> list:
    """Clean a saved tenant list: strings, stripped, deduplicated, in order."""
    if not isinstance(value, (list, tuple, set)):
        return []
    seen, out = set(), []
    for item in value:
        name = str(item or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def looks_production(text) -> bool:
    """True when a name reads like production to a human."""
    return bool(_PRODUCTION_RE.search(str(text or "").casefold()))


def is_production(conn, production_tenants=()) -> bool:
    """Whether *conn* should be treated as production."""
    if not isinstance(conn, dict):
        return False

    environment = normalize_environment(conn.get("environment"))
    if environment:
        # An explicit choice is the whole answer, including "development" on a
        # box whose name happens to say prod.
        return environment == "production"

    tenant = str(conn.get("tenant") or "Default")
    marked = {name.casefold() for name in normalize_production_tenants(production_tenants)}
    if tenant.casefold() in marked:
        return True

    return looks_production(tenant) or looks_production(conn.get("name"))


def production_reason(conn, production_tenants=()) -> str:
    """Why *conn* counts as production, for a tooltip. "" when it does not."""
    if not is_production(conn, production_tenants):
        return ""
    if normalize_environment(conn.get("environment")) == "production":
        return "Marked as production in the connection's settings."
    tenant = str(conn.get("tenant") or "Default")
    marked = {name.casefold() for name in normalize_production_tenants(production_tenants)}
    if tenant.casefold() in marked:
        return f"“{tenant}” is marked as a production tenant."
    if looks_production(tenant):
        return f"The tenant name “{tenant}” reads as production."
    return f"The connection name “{conn.get('name', '')}” reads as production."


def dangers(command) -> list:
    """What is alarming about *command*, worst first. Empty when nothing is."""
    text = str(command or "")
    if not text.strip():
        return []
    found, seen = [], set()
    for pattern, label in _COMPILED:
        if label not in seen and pattern.search(text):
            seen.add(label)
            found.append(label)
    return found


def describe_targets(conns) -> str:
    """"web01", "web01 and db01", "web01, db01 and 3 others"."""
    names = [str((c or {}).get("name") or (c or {}).get("host") or "?")
             for c in (conns or [])]
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    if len(names) <= 4:
        return f"{', '.join(names[:-1])} and {names[-1]}"
    return f"{', '.join(names[:3])} and {len(names) - 3} others"


class Confirmation:
    """A question worth asking before something runs.

    ``title`` and ``body`` are ready to put in a dialog; ``severity`` is
    "production", "dangerous" or "both", so a caller can decide how loud to
    be without re-deriving why it was asked.
    """

    __slots__ = ("title", "body", "severity", "targets", "reasons")

    def __init__(self, title, body, severity, targets, reasons):
        self.title = title
        self.body = body
        self.severity = severity
        self.targets = list(targets)
        self.reasons = list(reasons)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Confirmation {self.severity} targets={len(self.targets)}>"


def confirmation_for(command, conns, production_tenants=(),
                     always_confirm_many=2) -> "Confirmation | None":
    """The question to ask before *command* runs on *conns*, or None.

    Asked when any target is production, when the command itself is
    destructive, or when it is about to run on several machines at once —
    that last one because "type it once, run it everywhere" is exactly the
    feature that turns one typo into an outage.
    """
    targets = [c for c in (conns or []) if isinstance(c, dict)]
    production = [c for c in targets if is_production(c, production_tenants)]
    reasons = dangers(command)
    many = len(targets) >= max(2, int(always_confirm_many or 2))

    if not production and not reasons and not many:
        return None

    if production and reasons:
        severity = "both"
    elif production:
        severity = "production"
    elif reasons:
        severity = "dangerous"
    else:
        severity = "many"

    where = describe_targets(targets)
    lines = []
    if production:
        lines.append(
            f"{describe_targets(production)} "
            f"{'is' if len(production) == 1 else 'are'} production.")
    if reasons:
        lines.append("This command " + ", ".join(reasons) + ".")
    if many and not production:
        lines.append(f"It will run on {len(targets)} machines at once.")

    title = "Run on production?" if production else "Run this command?"
    body = (f"{' '.join(lines)}\n\nRunning on {where}:\n\n"
            f"{str(command or '').strip()}")
    return Confirmation(title, body, severity, targets, reasons)
