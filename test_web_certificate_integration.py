"""Exercise the real Qt certificate signal against an isolated HTTPS server."""
import datetime
import http.server
import ipaddress
import os
from pathlib import Path
import ssl
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QThread
from PyQt6.QtWidgets import QApplication, QMessageBox

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
APP = QApplication.instance() or QApplication(["rpmote-certificate-tests"])
from app.web_browser import WebBrowserTab, _ensure_webengine
from app.browser_trust import CertificateTrustStore


def pump(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        QThread.msleep(10)
    return bool(predicate())


class VaultStub:
    is_unlocked = True

    def __init__(self): self.settings = {}
    def update_settings(self, values): self.settings.update(values)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body>verified-local-console</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args): pass


@unittest.skipUnless(_ensure_webengine(), "Qt WebEngine unavailable")
class CertificateIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        cls.temporary = tempfile.TemporaryDirectory(prefix="rpmote-https-test-")
        root = Path(cls.temporary.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
        cert_path, key_path = root / "cert.pem", root / "key.pem"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.url = f"https://127.0.0.1:{cls.server.server_port}/"
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temporary.cleanup()

    def setUp(self):
        self.opened = []
        self.vault = VaultStub()

    def tearDown(self):
        for tab, profile, _loaded in self.opened:
            tab.cleanup()
            tab.close()
            tab.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        for _tab, profile, _loaded in self.opened:
            profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def open_tab(self):
        from PyQt6.QtWebEngineCore import QWebEngineProfile
        profile = QWebEngineProfile()  # off-the-record: never touches real browser storage
        tab = WebBrowserTab({"host": self.url, "port": 0, "protocol": "idrac"},
            _skip_navigate=True, _shared_profile=profile,
            certificate_trust=CertificateTrustStore(self.vault))
        loaded = []
        tab._page.loadFinished.connect(loaded.append)
        self.opened.append((tab, profile, loaded))
        tab._navigate()
        return tab, loaded

    def test_untrusted_real_certificate_is_blocked_until_approved(self):
        tab, loaded = self.open_tab()
        self.assertTrue(pump(lambda: bool(tab._certificate_prompts._pending)),
                        "Qt never delivered the certificate approval request")
        self.assertNotIn(True, loaded)
        box = next(iter(tab._certificate_prompts._pending.values()))[0]
        box.button(QMessageBox.StandardButton.Cancel).click()
        self.assertTrue(pump(lambda: bool(loaded)))
        self.assertFalse(loaded[-1])
        self.assertEqual(self.vault.settings, {})

    def test_remembered_certificate_works_with_a_fresh_chromium_profile(self):
        tab, loaded = self.open_tab()
        self.assertTrue(pump(lambda: bool(tab._certificate_prompts._pending)))
        box = next(iter(tab._certificate_prompts._pending.values()))[0]
        next(button for button in box.buttons() if button.text() == "Trust and Remember").click()
        self.assertTrue(pump(lambda: True in loaded))
        new_tab, new_loaded = self.open_tab()
        self.assertTrue(pump(lambda: bool(new_loaded)))
        self.assertTrue(new_loaded[-1])
        self.assertFalse(new_tab._certificate_prompts._pending)
