"""Snippets that know where they are running, and a way to find them fast.

A list of commands you have to scroll through and copy out by hand is a
notepad.  What makes it a tool is two things: the command can refer to the
session it is about to run in — ``systemctl status {service}`` on ``{host}``
— and you can get to it without taking your hands off the keyboard.

Substitution is literal replacement rather than ``str.format``, for the same
reason the restart templates are: a command full of shell braces
(``${VAR}``, an awk program, a PowerShell script block) has to survive
untouched, and a hostname must never reach a formatter.

Values that come from the estate — host, user, tenant, the shell's own
directory — are vetted before they are substituted, because they arrive from
somewhere other than the person about to press Enter.  Values the user types
into the prompt are not: they are authoring a command line, and second-
guessing their quoting would only teach them to work around it.
"""

from __future__ import annotations

import re

#: ``{name}`` — a letter, then letters, digits or underscores, and not
#: preceded by a ``$``. Deliberately narrow so ``${HOME}`` and ``${1}`` are
#: left alone: a snippet is a shell command, and shell braces are not ours.
PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([A-Za-z][A-Za-z0-9_]*)\}")

#: Placeholders RPMote fills in by itself, and what to call them in the UI.
SESSION_PLACEHOLDERS = {
    "host": "hostname or IP of this session",
    "name": "connection name",
    "port": "port",
    "user": "username",
    "tenant": "tenant name",
    "cwd": "directory the shell is in (OSC 7)",
}

#: Same rule as the restart engine: a value carrying these is not a value,
#: it is an attempt to append a second command.
_UNSAFE = re.compile(r"[;&|`$<>\r\n\"']|\$\(|\|\|")


class SnippetError(Exception):
    """A snippet could not be rendered."""


def placeholders_in(text) -> list:
    """Placeholder names in *text*, in order, without repeats."""
    seen, out = set(), []
    for name in PLACEHOLDER_RE.findall(str(text or "")):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def session_values(conn, cwd: str = "") -> dict:
    """What RPMote knows about the session a snippet is about to run in."""
    conn = conn if isinstance(conn, dict) else {}
    return {
        "host": str(conn.get("host", "") or ""),
        "name": str(conn.get("name", "") or ""),
        "port": str(conn.get("port", "") or ""),
        "user": str(conn.get("username", "") or ""),
        "tenant": str(conn.get("tenant", "") or "Default"),
        "cwd": str(cwd or ""),
    }


def missing_placeholders(text, values) -> list:
    """Placeholders in *text* that *values* has nothing to put in."""
    values = values or {}
    return [name for name in placeholders_in(text)
            if not str(values.get(name, "") or "").strip()]


def _vet(name: str, value: str) -> str:
    if _UNSAFE.search(value):
        raise SnippetError(
            f"Refusing to substitute {{{name}}}: “{value}” contains shell "
            f"control characters.")
    return value


def render(template, values, trusted=None) -> str:
    """Fill *template* from *values*.

    Anything named in *trusted* is substituted as typed; everything else is
    vetted first.  Callers pass the names the user typed by hand — by default
    nothing is trusted, which is the right answer for values that came out of
    the vault or off the wire.
    """
    values = values or {}
    trusted = set(trusted or ())

    def substitute(match):
        name = match.group(1)
        if name not in values:
            return match.group(0)
        value = str(values[name] if values[name] is not None else "")
        return value if name in trusted else _vet(name, value)

    # One pass with the same pattern that found them, so a ``${host}`` next
    # to a ``{host}`` keeps its dollar sign and its braces.
    return PLACEHOLDER_RE.sub(substitute, str(template or ""))


def is_parameterized(text) -> bool:
    """True when a snippet asks for anything at all."""
    return bool(placeholders_in(text))


# ── Finding things ──────────────────────────────────────────────────


def fuzzy_match(query, text):
    """Score *text* against *query*, or None when the letters aren't there.

    A subsequence match, scored so that the thing a person meant comes top:
    letters that land at the start of a word, and letters that run together,
    are what distinguish "restart nginx" from "read the manifest index".
    Returns ``(score, positions)``; higher scores are better matches.
    """
    query = str(query or "").strip()
    text = str(text or "")
    if not query:
        return (0, [])
    if not text:
        return None

    needle = query.casefold()
    hay = text.casefold()

    positions = []
    score = 0
    index = 0
    previous = -2
    for char in needle:
        if char == " ":
            # A space in the query means "next word", not a literal space.
            continue
        found = hay.find(char, index)
        if found < 0:
            return None
        positions.append(found)
        if found == previous + 1:
            score += 8            # consecutive letters
        if found == 0:
            score += 12           # matched the very start
        elif not hay[found - 1].isalnum():
            score += 10           # start of a word
        score += 1
        previous = found
        index = found + 1

    # Shorter haystacks win ties: an exact-ish name beats a long description
    # that happens to contain the same letters.
    score += max(0, 20 - len(hay) // 4)
    if hay.startswith(needle):
        score += 25
    return (score, positions)


def rank(query, items, fields=("name", "command", "description", "category")):
    """Order *items* by how well they match *query*.

    Each item is scored on its best field, so typing part of a command finds
    a snippet whose name is nothing like it.  With no query the list comes
    back in its own order, which is the order the user arranged it in.
    """
    items = list(items or [])
    query = str(query or "").strip()
    if not query:
        return [(item, 0, []) for item in items]

    scored = []
    for position, item in enumerate(items):
        best = None
        for weight, field in enumerate(fields):
            value = item.get(field, "") if isinstance(item, dict) else ""
            hit = fuzzy_match(query, value)
            if hit is None:
                continue
            # Later fields are worth less: a hit in the name beats a hit in
            # the description of a command you were not looking for.
            adjusted = (hit[0] - weight * 6, hit[1] if field == "name" else [])
            if best is None or adjusted[0] > best[0]:
                best = adjusted
        if best is not None:
            scored.append((-best[0], position, item, best[1]))

    scored.sort()
    return [(item, -negative, positions)
            for negative, _position, item, positions in scored]
