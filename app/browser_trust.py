"""Explicit certificate exceptions for individual web-console servers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QListWidget, QMessageBox, QPushButton,
    QVBoxLayout,
)

TRUST_SETTING = "web_certificate_trust"


@dataclass(frozen=True)
class CertificateIdentity:
    server: str
    fingerprint: str
    error_type: int

    @classmethod
    def from_error(cls, error):
        url = error.url()
        chain = error.certificateChain()
        if url.scheme().lower() != "https" or not url.host() or not chain:
            raise ValueError("The server did not provide an identifiable HTTPS certificate.")
        host = url.host().lower()
        if ":" in host:
            host = f"[{host}]"
        certificate = bytes(chain[0].toDer())
        if not certificate:
            raise ValueError("The certificate is empty.")
        kind = error.type()
        return cls(f"{host}:{url.port(443)}", hashlib.sha256(certificate).hexdigest(),
                   int(getattr(kind, "value", kind)))


class CertificateTrustStore:
    def __init__(self, vault=None):
        self.vault = vault
        self._session = set()

    @property
    def can_remember(self):
        return self.vault is not None and self.vault.is_unlocked

    def entries(self):
        if not self.can_remember:
            return {}
        return dict(self.vault.settings.get(TRUST_SETTING, {}))

    def trusts(self, identity):
        if identity in self._session:
            return True
        saved = self.entries().get(identity.server, {})
        return (saved.get("sha256") == identity.fingerprint
                and identity.error_type in saved.get("errors", []))

    def approve(self, identity, remember=False):
        if remember:
            if not self.can_remember:
                raise ValueError("Unlock the vault to remember a certificate.")
            entries = self.entries()
            previous = entries.get(identity.server, {})
            errors = (previous.get("errors", [])
                      if previous.get("sha256") == identity.fingerprint else [])
            entries[identity.server] = {
                "sha256": identity.fingerprint,
                "errors": sorted(set(errors) | {identity.error_type}),
            }
            self.vault.update_settings({TRUST_SETTING: entries})
        self._session.add(identity)

    def forget(self, server):
        entries = self.entries()
        if server in entries:
            del entries[server]
            self.vault.update_settings({TRUST_SETTING: entries})
        self._session = {entry for entry in self._session if entry.server != server}


class CertificatePrompts(QObject):
    """Keep Qt's deferred errors alive until the user makes a decision."""

    def __init__(self, store, parent):
        super().__init__(parent)
        self.store = store
        self._pending = {}
        self._closed = False

    def handle(self, error):
        if self._closed or not error.isOverridable():
            error.rejectCertificate()
            return
        try:
            identity = CertificateIdentity.from_error(error)
        except (ValueError, TypeError):
            error.rejectCertificate()
            return
        if self.store.trusts(identity):
            error.acceptCertificate()
            return
        error.defer()
        if identity in self._pending:
            self._pending[identity][1].append(error)
            return

        box = QMessageBox(self.parent())
        box.setWindowTitle("Verify Server Certificate")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        previous = self.store.entries().get(identity.server, {}).get("sha256")
        changed = bool(previous and previous != identity.fingerprint)
        box.setText(("The certificate has changed for " if changed else
                     "The certificate could not be verified for ") + identity.server)
        box.setInformativeText(
            f"{error.description()}\n\n"
            "Verify the fingerprint with the server administrator before trusting it. "
            "Remembering applies only to this server, port and certificate.")
        fingerprint = ":".join(identity.fingerprint[i:i + 2]
                               for i in range(0, len(identity.fingerprint), 2))
        box.setDetailedText(f"SHA-256: {fingerprint}"
                            + (f"\nPrevious SHA-256: {previous}" if changed else ""))
        once = box.addButton("Trust for This Session", QMessageBox.ButtonRole.AcceptRole)
        remember = box.addButton("Trust and Remember", QMessageBox.ButtonRole.AcceptRole)
        remember.setEnabled(self.store.can_remember)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        self._pending[identity] = (box, [error])

        def finished(_result):
            pending = self._pending.pop(identity, None)
            if pending is None:
                return
            accepted = not self._closed and box.clickedButton() in (once, remember)
            if accepted:
                try:
                    self.store.approve(identity, remember=box.clickedButton() is remember)
                except Exception:
                    # Consent still covers this session, but do not claim it
                    # was saved if the vault write failed.
                    self.store.approve(identity)
                    QMessageBox.warning(self.parent(), "Certificate Not Saved",
                                        "The vault could not save this approval. "
                                        "It applies only to this session.")
            for request in pending[1]:
                if accepted:
                    request.acceptCertificate()
                else:
                    request.rejectCertificate()
            box.deleteLater()

        box.finished.connect(finished)
        box.open()

    def close(self):
        self._closed = True
        for box, _errors in list(self._pending.values()):
            box.reject()


def manage_certificate_trust(store, parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Trusted Web Certificates")
    dialog.resize(640, 360)
    layout = QVBoxLayout(dialog)
    hint = QLabel("Saved exceptions for NAS and iDRAC servers. After removing one, "
                  "restart RPMote to clear Chromium's current session approvals.")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    listing = QListWidget()
    layout.addWidget(listing)

    def refresh():
        listing.clear()
        for server, entry in sorted(store.entries().items()):
            listing.addItem(f"{server}\nSHA-256: {entry.get('sha256', '')}")
            listing.item(listing.count() - 1).setData(Qt.ItemDataRole.UserRole, server)

    remove = QPushButton("Remove Selected Approval")
    remove.setEnabled(False)
    listing.currentRowChanged.connect(lambda row: remove.setEnabled(row >= 0))

    def forget():
        item = listing.currentItem()
        if item is not None:
            try:
                store.forget(item.data(Qt.ItemDataRole.UserRole))
            except Exception as exc:
                QMessageBox.warning(dialog, "Could Not Remove Approval", str(exc))
            refresh()

    remove.clicked.connect(forget)
    layout.addWidget(remove)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    refresh()
    dialog.exec()
