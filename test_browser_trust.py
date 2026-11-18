"""Trust is scoped to a server, port, fingerprint and approved error."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication, QWidget, QMessageBox
from app.browser_trust import CertificateIdentity, CertificatePrompts, CertificateTrustStore, TRUST_SETTING

APP = QApplication.instance() or QApplication(["rpmote-tests"])


class VaultStub:
    is_unlocked = True

    def __init__(self):
        self.settings = {}

    def update_settings(self, values):
        self.settings.update(values)


class ErrorStub:
    def __init__(self, url="https://nas.example:5001/", certificate=b"certificate", kind=-202,
                 overridable=True):
        self.address, self.certificate, self.kind = url, certificate, kind
        self.overridable = overridable
        self.accepted = self.rejected = self.deferred = False

    def url(self): return QUrl(self.address)
    def certificateChain(self): return [SimpleNamespace(toDer=lambda: self.certificate)]
    def type(self): return self.kind
    def isOverridable(self): return self.overridable
    def description(self): return "Self-signed certificate"
    def defer(self): self.deferred = True
    def acceptCertificate(self): self.accepted = True
    def rejectCertificate(self): self.rejected = True


class BrowserTrustTests(unittest.TestCase):
    def setUp(self):
        self.vault = VaultStub()
        self.store = CertificateTrustStore(self.vault)
        self.parent = QWidget()
        self.prompts = CertificatePrompts(self.store, self.parent)
        self.addCleanup(self.parent.deleteLater)
        self.addCleanup(self.prompts.close)

    def answer(self, text):
        box = next(iter(self.prompts._pending.values()))[0]
        button = next(b for b in box.buttons() if b.text() == text)
        button.click()
        APP.processEvents()

    def test_unknown_certificate_waits_for_approval_and_cancel_rejects(self):
        error = ErrorStub()
        self.prompts.handle(error)
        self.assertTrue(error.deferred)
        self.assertFalse(error.accepted)
        box = next(iter(self.prompts._pending.values()))[0]
        self.assertIs(box.defaultButton(), box.button(QMessageBox.StandardButton.Cancel))
        box.reject()
        self.assertTrue(error.rejected)
        self.assertEqual(self.store.entries(), {})

    def test_remember_survives_a_new_store_for_the_same_vault(self):
        error = ErrorStub()
        self.prompts.handle(error)
        self.answer("Trust and Remember")
        self.assertTrue(error.accepted)
        self.assertTrue(CertificateTrustStore(self.vault).trusts(CertificateIdentity.from_error(error)))

    def test_session_approval_does_not_persist(self):
        error = ErrorStub()
        self.prompts.handle(error)
        self.answer("Trust for This Session")
        self.assertTrue(error.accepted)
        self.assertEqual(self.vault.settings, {})
        self.assertFalse(CertificateTrustStore(self.vault).trusts(CertificateIdentity.from_error(error)))

    def test_changed_host_port_certificate_or_error_needs_fresh_approval(self):
        self.store.approve(CertificateIdentity.from_error(ErrorStub()), remember=True)
        for kwargs in ({"url": "https://other.example:5001/"},
                       {"url": "https://nas.example:443/"},
                       {"certificate": b"replacement"}, {"kind": -201}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(self.store.trusts(CertificateIdentity.from_error(ErrorStub(**kwargs))))

    def test_changed_certificate_prompt_identifies_the_change(self):
        self.store.approve(CertificateIdentity.from_error(ErrorStub()), remember=True)
        self.prompts.handle(ErrorStub(certificate=b"replacement"))
        box = next(iter(self.prompts._pending.values()))[0]
        self.assertIn("has changed", box.text())

    def test_non_overridable_error_is_rejected_even_if_previously_approved(self):
        error = ErrorStub(overridable=False)
        self.store.approve(CertificateIdentity.from_error(error), remember=True)
        self.prompts.handle(error)
        self.assertTrue(error.rejected)
        self.assertFalse(self.prompts._pending)

    def test_duplicate_requests_share_one_decision(self):
        first, second = ErrorStub(), ErrorStub()
        self.prompts.handle(first)
        self.prompts.handle(second)
        self.assertEqual(len(self.prompts._pending), 1)
        self.answer("Trust for This Session")
        self.assertTrue(first.accepted and second.accepted)

    def test_closing_tab_rejects_pending_requests(self):
        error = ErrorStub()
        self.prompts.handle(error)
        self.prompts.close()
        self.assertTrue(error.rejected)

    def test_removing_approval_clears_saved_and_session_trust(self):
        identity = CertificateIdentity.from_error(ErrorStub())
        self.store.approve(identity, remember=True)
        self.store.forget(identity.server)
        self.assertFalse(self.store.trusts(identity))
        self.assertEqual(self.vault.settings[TRUST_SETTING], {})

    def test_failed_vault_write_reports_session_only_approval(self):
        error = ErrorStub()
        self.prompts.handle(error)
        with patch.object(self.vault, "update_settings", side_effect=OSError("disk full")), \
                patch("app.browser_trust.QMessageBox.warning") as warning:
            self.answer("Trust and Remember")
        warning.assert_called_once()
        self.assertTrue(error.accepted)
        self.assertEqual(self.vault.settings, {})
