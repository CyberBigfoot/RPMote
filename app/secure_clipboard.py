"""Putting a secret on the clipboard without leaving it there.

Two things make the clipboard a worse place for a password than it looks.
Windows keeps a history of everything copied (Win+V) and, when signed in
with a Microsoft account, can sync that history to other devices — so
``clipboard.clear()`` thirty seconds later removes the live entry and
nothing else.  And a secret that is never cleared stays there until the
next copy, which on a technician's desk can be a long time.

Both are handled here.  The three extra clipboard formats are the ones
Windows itself documents for password managers: their presence tells the
history and cloud features to leave this entry alone.  Qt registers an
unknown MIME type as a Windows clipboard format of the same name, which is
exactly what the shell looks for.  On other platforms they are inert.
"""

from __future__ import annotations

from PyQt6.QtCore import QMimeData, QTimer
from PyQt6.QtWidgets import QApplication

#: How long a copied secret stays on the clipboard, in milliseconds.
CLEAR_AFTER_MS = 30_000

#: Formats the Windows shell checks before recording a clipboard entry.
#: ``ExcludeClipboardContentFromMonitorProcessing`` is honoured by presence;
#: the other two are DWORDs read as booleans, so a zero disables them.
_EXCLUDE_FORMATS = (
    ("ExcludeClipboardContentFromMonitorProcessing", b"\x01\x00\x00\x00"),
    ("CanIncludeInClipboardHistory", b"\x00\x00\x00\x00"),
    ("CanUploadToCloudClipboard", b"\x00\x00\x00\x00"),
)


def copy_secret(text: str, clear_after_ms: int = CLEAR_AFTER_MS) -> bool:
    """Copy ``text`` so it stays out of clipboard history, then clear it.

    The clear only happens if the clipboard still holds what was put there,
    so anything the user copied in the meantime is left alone.  Returns
    False when there is no clipboard to speak of (a headless test).
    """
    clipboard = QApplication.clipboard()
    if clipboard is None:
        return False
    mime = QMimeData()
    mime.setText(text)
    for fmt, payload in _EXCLUDE_FORMATS:
        mime.setData(fmt, payload)
    clipboard.setMimeData(mime)
    if clear_after_ms > 0:
        QTimer.singleShot(clear_after_ms, lambda: clear_if_still(text))
    return True


def clear_if_still(text: str) -> bool:
    """Clear the clipboard if it still holds ``text``.  True if it was cleared."""
    clipboard = QApplication.clipboard()
    if clipboard is None:
        return False
    try:
        if clipboard.text() == text:
            clipboard.clear()
            return True
    except RuntimeError:
        pass  # application already going away
    return False
