"""
Display-side coalescing for noisy terminal progress output.

Docker, package managers, and download tools often emit many transient status
rows. The SSH session log should keep the raw stream, but the terminal viewport
can stay calmer by rendering those rows as one updating status line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[()][A-B0-2]")
_SGR_RE = re.compile(r"\x1b\[[0-9:;]*m")
# Cursor movement (up/down/forward/back/next-line/prev-line/position). Its
# presence marks a TTY-style in-place redraw (Docker Compose's fancy progress),
# as opposed to "plain" progress that prints one line per refresh.
_CURSOR_MOVE_RE = re.compile(r"\x1b\[\d*[ABCDEFH]")


def _strip_terminal_codes(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "").replace("\n", "").strip()


@dataclass
class TerminalProgressCoalescer:
    """Compact known high-volume progress lines into one terminal row."""

    enabled: bool = True
    _active: bool = False
    _kind: str = "Progress"
    _current: str = ""
    _percent: int | None = None
    _seen_steps: set[str] = field(default_factory=set)
    _done_steps: set[str] = field(default_factory=set)
    _spinner: int = 0
    _in_docker_compose: bool = False

    def feed(self, text: str) -> str:
        if not self.enabled or not text:
            return text

        clean_text = _strip_terminal_codes(text).lower()
        if self._looks_like_shell_prompt(clean_text):
            self._in_docker_compose = False

        # Only treat this as a TTY compose stream (which redraws itself in place
        # with cursor movement, and which we pass through for the screen buffer
        # to emulate) when the stream actually contains cursor movement. Docker
        # Compose's "plain" progress mode has no cursor control and prints one
        # line per refresh — those fall through to the collapsing loop below so
        # they don't pile up as duplicate rows.
        if self._looks_like_docker_compose_stream(text) and _CURSOR_MOVE_RE.search(text):
            self._in_docker_compose = True

        if self._in_docker_compose:
            prefix = ""
            if self._active:
                prefix = self._status_line(final=True, newline=True)
                self.reset()
            self._in_docker_compose = True
            return prefix + self._sanitize_docker_compose_stream(text)

        out: list[str] = []
        just_status = False
        for piece in re.split(r"(?<=[\r\n])", text):
            if not piece:
                continue

            clean = _strip_terminal_codes(piece)

            # Plain-mode Docker Compose in-progress frame (e.g.
            # "[+] down 0/1 FlickVault Stopping 8.1s"). Overwrite it in place —
            # keeping Docker's own wording — so the ticking timer updates one
            # line instead of scrolling a new row every 100ms.
            if clean and self._is_compose_progress_frame(clean):
                self._active = True
                self._kind = "Compose"
                self._current = clean
                out.append(self._status_line(final=False))
                just_status = True
                continue

            if clean and self._is_compactable(clean):
                self._update(clean)
                final = self._looks_complete(clean)
                out.append(self._status_line(final=final))
                just_status = True
                continue

            if not clean:
                # A piece made up only of carriage returns / newlines. When it
                # immediately follows an in-place status update it is just the
                # terminator of the progress line we already rewrote — swallow
                # it so successive CRLF-terminated steps (apt Get/Hit, OS
                # updates) collapse onto one updating line instead of scrolling
                # a fresh line for every step.
                if just_status and set(piece) <= {"\r", "\n"}:
                    continue
                if self._active and "\n" in piece:
                    self.reset()
                out.append(piece)
                continue

            if self._active:
                out.append(self._status_line(final=True, newline=True))
                self.reset()

            just_status = False
            # Completed compose lines ("[+] down 2/2", "✔ Container … Removed")
            # stay as permanent output, but strip their noisy background colour
            # while leaving any following shell prompt's colours intact.
            if self._looks_like_docker_compose_stream(piece):
                out.append(_SGR_RE.sub("", piece))
            else:
                out.append(piece)

        return "".join(out)

    def _is_compose_progress_frame(self, line: str) -> bool:
        """True for a rolling, still-in-progress plain-mode Compose frame.

        Matches the ``[+] <verb> X/Y …`` header while X < Y (the operation is
        still running). Completed frames (X == Y) and per-resource result lines
        pass through as permanent output.
        """
        clean = re.sub(r"\s+", " ", line).strip()
        clean = re.sub(r"^[^\w\[]+", "", clean)
        m = re.match(r"^\[\+\]\s+\w+\s+(\d+)\s*/\s*(\d+)\b", clean)
        if not m:
            return False
        return int(m.group(1)) < int(m.group(2))

    def reset(self):
        self._active = False
        self._kind = "Progress"
        self._current = ""
        self._percent = None
        self._seen_steps.clear()
        self._done_steps.clear()
        self._spinner = 0
        self._in_docker_compose = False

    def flush(self) -> str:
        if not self._active:
            return ""
        line = self._status_line(final=True, newline=True)
        self.reset()
        return line

    def _is_compactable(self, line: str) -> bool:
        lowered = line.lower()
        normalized = re.sub(r"^[\s\W_]+", "", lowered)
        if re.match(r"^#\d+(?:\s|$)", line):
            return True
        if re.match(
            r"^[a-f0-9]{8,64}:\s+"
            r"(?:pulling fs layer|waiting|downloading|extracting|verifying checksum|"
            r"download complete|pull complete)",
            lowered,
        ):
            return True
        if re.match(
            r"^(?:pulling|pushed|pushing|building|built|creating|created|starting|"
            r"started|stopping|stopped|removing|removed|running|waiting|healthy|"
            r"downloading|extracting|verifying|restarting|recreating|killing|"
            r"pausing|unpausing)\b",
            lowered,
        ):
            return True
        if re.search(r"\b\d{1,3}(?:\.\d+)?%\b", line) and re.search(
            r"\b(?:download|extract|fetch|install|update|upgrade|build|pull|push|progress|working)\b",
            lowered,
        ):
            return True
        if re.match(r"^\s*(?:get|hit|ign|fetch|fetched):\d*\s+", lowered):
            return True
        if lowered in {
            "reading package lists...",
            "building dependency tree...",
            "reading state information...",
        }:
            return True
        return False

    def _update(self, line: str):
        self._active = True
        lowered = line.lower()
        normalized = re.sub(r"^[\s\W_]+", "", lowered)

        if re.match(r"^#\d+(?:\s|$)", line) or "docker" in lowered:
            self._kind = "Docker"
        elif re.match(r"^[a-f0-9]{8,64}:", lowered):
            self._kind = "Docker pull"
        elif re.match(r"^\s*(?:get|hit|ign|fetch|fetched):\d*\s+", lowered) or re.search(
            r"\b(?:apt|package|packages|repository|fetch|fetched|upgrade|update)\b",
            lowered,
        ):
            self._kind = "Update"

        step = re.match(r"^#(\d+)", line)
        if step:
            step_id = step.group(1)
            self._seen_steps.add(step_id)
            if "done" in lowered or "cached" in lowered:
                self._done_steps.add(step_id)

        bracket_step = re.search(r"\[(\d+)\s*/\s*(\d+)\]", line)
        if bracket_step:
            current, total = int(bracket_step.group(1)), int(bracket_step.group(2))
            if total > 0:
                self._percent = max(0, min(100, round(current / total * 100)))

        percentages = [float(p) for p in re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", line)]
        if percentages:
            self._percent = max(0, min(100, int(max(percentages))))
        elif self._seen_steps:
            total = max(len(self._seen_steps), len(self._done_steps), 1)
            self._percent = max(0, min(100, round(len(self._done_steps) / total * 100)))

        self._current = self._compact_label(line)

    def _compact_label(self, line: str) -> str:
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r"^#\d+\s+", "", line)
        line = re.sub(r"^[a-f0-9]{8,64}:\s+", "", line, flags=re.IGNORECASE)
        line = re.sub(r"^\[[+\-*/\\ ]+\]\s+", "", line)
        line = re.sub(r"^[^\w]+", "", line)
        line = line.strip()
        if len(line) > 84:
            line = line[:81].rstrip() + "..."
        return line or "working"

    def _status_line(self, final: bool = False, newline: bool = False) -> str:
        # Plain-mode Compose frames keep Docker's own wording (no synthetic bar),
        # just rewritten onto the same line so the ticking timer updates in place.
        if self._kind == "Compose":
            # Finalize with CR+LF: the bare LF the screen buffer receives here
            # does not reset the column, so a following line would be indented.
            suffix = "\r\n" if newline else ""
            return f"\r\x1b[0m\x1b[2K{self._current}\x1b[0m{suffix}"

        detail = self._current
        pct = 100 if final and self._percent is None and self._done_steps else self._percent
        bar = self._bar(pct, final)
        body = f"{self._kind} {bar} {detail}"

        if self._seen_steps:
            detail = f"{len(self._done_steps)}/{len(self._seen_steps)} steps - {detail}"
            pct = 100 if final and self._percent is None and self._done_steps else self._percent
            bar = self._bar(pct, final)
            body = f"{self._kind} {bar} {detail}"

        # Finalize with CR+LF so the following line starts at column 0 (the
        # screen buffer treats a bare LF as "move down", keeping the column).
        suffix = "\r\n" if newline else ""
        return f"\r\x1b[0m\x1b[2K{body}\x1b[0m{suffix}"

    def _sanitize_docker_compose_stream(self, text: str) -> str:
        """Remove noisy Compose SGR while preserving the prompt that follows it."""
        out: list[str] = []
        for piece in re.split(r"(?<=[\r\n])", text):
            if not piece:
                continue

            clean = _strip_terminal_codes(piece).lower()
            if clean and self._looks_like_shell_prompt(clean):
                self._in_docker_compose = False
                out.append(piece)
                continue

            if self._in_docker_compose:
                out.append(_SGR_RE.sub("", piece))
            else:
                out.append(piece)

        return "".join(out)

    def _bar(self, percent: int | None, final: bool) -> str:
        width = 18
        if percent is not None:
            done = max(0, min(width, round(width * percent / 100)))
            return f"[{'#' * done}{'.' * (width - done)}] {percent:3d}%"

        if final:
            return f"[{'#' * width}]"

        self._spinner = (self._spinner + 1) % width
        chars = ["."] * width
        chars[self._spinner] = "#"
        return f"[{''.join(chars)}]"

    @staticmethod
    def _looks_complete(line: str) -> bool:
        return bool(re.search(r"\b(?:done|complete|completed|cached|success|healthy|started|created|removed)\b", line, re.IGNORECASE))

    @staticmethod
    def _looks_like_shell_prompt(clean_text: str) -> bool:
        return bool(
            re.search(r"([#\$❯>])\s*$", clean_text)
            or re.search(r"(?:^|[\s|])[\w.-]+@[\w.-]+(?:\s|\||:).*?(?:/|~)", clean_text)
        )

    @staticmethod
    def _looks_like_docker_compose_stream(text: str) -> bool:
        clean = _strip_terminal_codes(text).lower()
        if not clean:
            return False
        if re.search(
            r"\[\+\]\s+(?:running|down|up|pulling|pushing|building|stopping|starting|"
            r"creating|removing|restarting|recreating|waiting|killing|pausing|unpausing)"
            r"\s+\d+\s*/\s*\d+",
            clean,
        ):
            return True
        return bool(re.search(
            r"\b(?:container|network|volume|image|service)\s+\S+.*\b"
            r"(?:creating|created|starting|started|stopping|stopped|removing|removed|"
            r"running|waiting|healthy|pulling|pulled|building|built|recreating|recreated|"
            r"restarting|restarted|killing|killed|pausing|paused|unpausing|exited|error)\b",
            clean,
        ))
