"""
RPMote Connection Manager — Dialog for adding/editing connections.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QPushButton, QLineEdit, QComboBox, QSpinBox, QFileDialog,
    QGroupBox, QDialogButtonBox, QMessageBox, QCheckBox, QColorDialog,
    QScrollArea, QWidget, QTextEdit, QApplication, QCompleter,
    QPlainTextEdit, QToolButton, QFrame, QSizePolicy, QAbstractSpinBox
)
from PyQt6.QtCore import Qt, QTimer, QThread, QSize, QEvent, QObject, pyqtSignal
from PyQt6.QtGui import QFont, QColor
import paramiko
import socket
import threading
import shutil
from pathlib import Path

from app import guardrails, host_keys, icons, totp
from app.widgets import CollapsibleSection, fit_to_screen, set_variant
from app.rdp_launcher import (
    RDP_GATEWAY_BYPASS_KEY,
    RDP_GATEWAY_HOST_KEY,
    RDP_GATEWAY_PASSWORD_KEY,
    RDP_GATEWAY_SHARE_CREDS_KEY,
    RDP_GATEWAY_USERNAME_KEY,
    account_has_domain,
    normalize_gateway_host,
    normalize_rdp_gateway,
    split_gateway_address,
)
from app.screen_buffer import DEFAULT_SCROLLBACK
from app.port_forwarding import ForwardError, describe as _describe_forward
from app.port_forwarding import parse_spec as _parse_forward_spec
from app.saved_passwords import attach_reveal_toggle, confirm_master_password


# Kept importable from here: older code and tests look for it here.
from app.net_errors import describe_network_error  # noqa: E402


class WheelGuard(QObject):
    """Keep the mouse wheel from changing a combo or spin box in passing.

    The connection form lives in a scroll area, and a wheel that lands on
    the Protocol combo or the Port box while someone is scrolling down would
    otherwise change it.  The event is ignored rather than swallowed so it
    still reaches the scroll area and the page scrolls as intended; the
    drop-down list and the arrow keys are unaffected.
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            event.ignore()
            return True
        return super().eventFilter(obj, event)


def guard_wheel(root: QWidget) -> None:
    """Install :class:`WheelGuard` on every combo and spin box under *root*."""
    guard = WheelGuard(root)
    for widget in root.findChildren((QComboBox, QAbstractSpinBox)):
        widget.installEventFilter(guard)


def parse_forward_list(text: str) -> list:
    """Parse a comma or newline separated list of forward specs."""
    specs = []
    for piece in str(text or "").replace("\n", ",").split(","):
        piece = piece.strip()
        if piece:
            specs.append(_parse_forward_spec(piece))
    return specs


def describe_forward_spec(forward: dict) -> str:
    """Render a stored forward back into the shape it was typed in."""
    if not isinstance(forward, dict):
        return str(forward)
    prefix = "R:" if forward.get("type") == "remote" else ""
    listen_host = forward.get("listen_host", "127.0.0.1")
    host_part = "" if listen_host == "127.0.0.1" else f"{listen_host}:"
    return (f"{prefix}{host_part}{forward.get('listen_port')}:"
            f"{forward.get('dest_host')}:{forward.get('dest_port')}")
from app.wake_on_lan import normalize_mac, wake_connection


def _load_ppk(p: Path, passphrase: str | None = None):
    """Pure-Python PPK v2/v3 loader (unencrypted, RSA/Ed25519/ECDSA).

    Paramiko's built-in loaders do not understand PuTTY's PPK format, so
    we parse it ourselves and hand the reconstructed key to paramiko via
    an OpenSSH/PEM serialization round-trip.
    """
    import base64
    import io
    import struct
    from cryptography.hazmat.primitives.asymmetric import rsa, ed25519, ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption
    )

    text = p.read_text()
    lines = text.splitlines()

    algorithm = None
    encryption = None
    pub_lines: list[str] = []
    priv_lines: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("PuTTY-User-Key-File-"):
            algorithm = line.split(":", 1)[1].strip()
        elif line.startswith("Encryption:"):
            encryption = line.split(":", 1)[1].strip()
        elif line.startswith("Public-Lines:"):
            n = int(line.split(":", 1)[1].strip())
            pub_lines = lines[i + 1:i + 1 + n]
            i += n
        elif line.startswith("Private-Lines:"):
            n = int(line.split(":", 1)[1].strip())
            priv_lines = lines[i + 1:i + 1 + n]
            i += n
        i += 1

    if algorithm is None:
        raise ValueError(f"'{p}' does not look like a PPK file.")
    if encryption and encryption != "none":
        raise ValueError(
            f"'{p.name}' is an encrypted PPK. Open it in PuTTYgen, either save "
            f"it without a passphrase, or export as OpenSSH (Conversions → "
            f"Export OpenSSH key), then select the exported file here."
        )

    pub_blob = base64.b64decode("".join(pub_lines))
    priv_blob = base64.b64decode("".join(priv_lines))

    def unpack_string(buf: bytes, offset: int) -> tuple[bytes, int]:
        (length,) = struct.unpack(">I", buf[offset:offset + 4])
        offset += 4
        return buf[offset:offset + length], offset + length

    def unpack_mpint(buf: bytes, offset: int) -> tuple[int, int]:
        data, offset = unpack_string(buf, offset)
        return (int.from_bytes(data, "big", signed=True) if data else 0), offset

    if algorithm == "ssh-rsa":
        _, off = unpack_string(pub_blob, 0)
        e, off = unpack_mpint(pub_blob, off)
        n, off = unpack_mpint(pub_blob, off)
        d, off = unpack_mpint(priv_blob, 0)
        prime_p, off = unpack_mpint(priv_blob, off)
        prime_q, off = unpack_mpint(priv_blob, off)
        dmp1 = d % (prime_p - 1)
        dmq1 = d % (prime_q - 1)
        iqmp = pow(prime_q, -1, prime_p)
        pub_nums = rsa.RSAPublicNumbers(e=e, n=n)
        priv_key = rsa.RSAPrivateNumbers(
            p=prime_p, q=prime_q, d=d,
            dmp1=dmp1, dmq1=dmq1, iqmp=iqmp,
            public_numbers=pub_nums,
        ).private_key()
        pem = priv_key.private_bytes(
            Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
        )
        return paramiko.RSAKey.from_private_key(io.StringIO(pem.decode()))

    if algorithm == "ssh-ed25519":
        priv_bytes, _ = unpack_string(priv_blob, 0)
        priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
        pem = priv_key.private_bytes(
            Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()
        )
        return paramiko.Ed25519Key.from_private_key(io.StringIO(pem.decode()))

    if algorithm.startswith("ecdsa-sha2-nistp"):
        curve_map = {
            "nistp256": ec.SECP256R1(),
            "nistp384": ec.SECP384R1(),
            "nistp521": ec.SECP521R1(),
        }
        _, off = unpack_string(pub_blob, 0)
        curve_name_b, _ = unpack_string(pub_blob, off)
        curve = curve_map.get(curve_name_b.decode())
        if curve is None:
            raise ValueError(f"Unsupported ECDSA curve: {curve_name_b!r}")
        d, _ = unpack_mpint(priv_blob, 0)
        priv_key = ec.derive_private_key(d, curve)
        pem = priv_key.private_bytes(
            Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
        )
        return paramiko.ECDSAKey.from_private_key(io.StringIO(pem.decode()))

    raise ValueError(f"Unsupported PPK algorithm: {algorithm}")


def load_ssh_private_key(path: str, passphrase: str | None = None):
    """Load an SSH private key, auto-detecting format (OpenSSH, PEM, PPK).

    If `path` points to a `.pub` file, transparently switches to the
    private counterpart next to it.
    """
    p = Path(path).expanduser()
    if p.suffix == ".pub":
        priv = p.with_suffix("")
        if priv.exists():
            p = priv
        else:
            raise ValueError(
                f"'{path}' is a public key (.pub). Select the matching "
                f"private key instead (same path without .pub)."
            )
    if not p.exists():
        raise FileNotFoundError(f"Key file not found: {p}")

    pw = passphrase or None

    # Detect PuTTY PPK by header — paramiko can't parse these natively.
    try:
        head = p.read_bytes()[:24]
    except OSError as e:
        raise RuntimeError(f"Could not read {p}: {e}") from e
    if head.startswith(b"PuTTY-User-Key-File-"):
        return _load_ppk(p, pw)

    first_err: Exception | None = None

    # paramiko >= 3.2 unified loader (OpenSSH + legacy PEM)
    if hasattr(paramiko.PKey, "from_path"):
        try:
            return paramiko.PKey.from_path(str(p), passphrase=pw)
        except paramiko.PasswordRequiredException:
            raise
        except Exception as e:
            first_err = e

    last_err: Exception | None = first_err
    key_classes = [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]
    if hasattr(paramiko, "DSSKey"):
        key_classes.append(paramiko.DSSKey)
    for key_cls in key_classes:
        try:
            return key_cls.from_private_key_file(str(p), password=pw)
        except paramiko.PasswordRequiredException:
            raise
        except Exception as e:
            last_err = e
    # Surface the first (most informative) error — class-specific loaders
    # all say "not a valid X key", which hides the real cause.
    raise RuntimeError(f"Could not load key {p.name}: {first_err or last_err}")


def save_as_ppk(key, filepath: str, comment: str = "") -> None:
    """Save a paramiko key as PuTTY PPK v2 format (unencrypted)."""
    import struct, base64, hmac as _hmac, hashlib
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption
    )

    def _pack(data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + data

    def _pack_mpint(n: int) -> bytes:
        if n == 0:
            return struct.pack(">I", 0)
        length = (n.bit_length() + 8) // 8  # +8 ensures leading 0x00 if high bit set
        return _pack(n.to_bytes(length, "big"))

    if isinstance(key, paramiko.RSAKey):
        keytype = "ssh-rsa"
        pub_blob = key.asbytes()
        nums = key.key.private_numbers()
        priv_blob = (
            _pack_mpint(nums.d) + _pack_mpint(nums.p) +
            _pack_mpint(nums.q) + _pack_mpint(nums.iqmp)
        )
    elif isinstance(key, paramiko.Ed25519Key):
        keytype = "ssh-ed25519"
        pub_blob = key.asbytes()
        priv_raw = key._signing_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        pub_raw = key._signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        priv_blob = struct.pack(">I", 64) + priv_raw + pub_raw
    else:
        raise ValueError(f"Unsupported key type for PPK: {type(key).__name__}")

    encryption = "none"
    mac_key = hashlib.sha1(b"putty-private-key-file-mac-key").digest()
    mac_data = (
        _pack(keytype.encode()) + _pack(encryption.encode()) +
        _pack(comment.encode()) + _pack(pub_blob) + _pack(priv_blob)
    )
    mac_hex = _hmac.new(mac_key, mac_data, hashlib.sha1).hexdigest()

    def _b64_lines(data: bytes):
        b64 = base64.b64encode(data).decode()
        lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
        return len(lines), "\n".join(lines)

    pub_n, pub_b64 = _b64_lines(pub_blob)
    priv_n, priv_b64 = _b64_lines(priv_blob)

    content = (
        f"PuTTY-User-Key-File-2: {keytype}\n"
        f"Encryption: {encryption}\n"
        f"Comment: {comment}\n"
        f"Public-Lines: {pub_n}\n"
        f"{pub_b64}\n"
        f"Private-Lines: {priv_n}\n"
        f"{priv_b64}\n"
        f"Private-MAC: {mac_hex}\n"
    )
    with open(filepath, "w") as f:
        f.write(content)


class GenerateKeyDialog(QDialog):
    """PuTTYgen-style dialog for generating SSH key pairs."""

    #: (pub_text, status, colour, private path) from the worker thread.
    _generated = pyqtSignal(str, str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._generated.connect(self._on_generated)
        self.setWindowTitle("Generate SSH Key Pair")
        self.setMinimumWidth(500)
        self.setModal(True)
        self.generated_priv_path = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("Generate SSH Key Pair")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)

        self.key_type = QComboBox()
        self.key_type.addItems(["Ed25519 (recommended)", "RSA 2048", "RSA 4096"])
        form.addRow("Key Type:", self.key_type)

        self.comment_input = QLineEdit()
        self.comment_input.setPlaceholderText("rpmote")
        form.addRow("Comment:", self.comment_input)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["PuTTY (.ppk)", "OpenSSH (no extension)"])
        form.addRow("Save Format:", self.format_combo)

        save_row = QHBoxLayout()
        self.save_path = QLineEdit()
        self.save_path.setPlaceholderText("Choose where to save the private key…")
        save_row.addWidget(self.save_path)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_save)
        save_row.addWidget(btn_browse)
        form.addRow("Save To:", save_row)

        layout.addLayout(form)

        self.btn_generate = QPushButton("Generate key pair")
        self.btn_generate.setIcon(icons.icon("key"))
        self.btn_generate.setFixedHeight(36)
        set_variant(self.btn_generate, "primary")
        self.btn_generate.clicked.connect(self._generate)
        layout.addWidget(self.btn_generate)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size: 12px;")
        layout.addWidget(self.status_label)

        pub_header = QLabel("Public Key (paste this into your server's authorized_keys):")
        pub_header.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        layout.addWidget(pub_header)

        self.pub_key_display = QTextEdit()
        self.pub_key_display.setReadOnly(True)
        self.pub_key_display.setFixedHeight(80)
        self.pub_key_display.setPlaceholderText("Public key appears here after generation…")
        self.pub_key_display.setStyleSheet("font-family: monospace; font-size: 11px;")
        layout.addWidget(self.pub_key_display)

        copy_row = QHBoxLayout()
        self.btn_copy = QPushButton("Copy public key")
        self.btn_copy.setIcon(icons.icon("copy"))
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_pub_key)
        copy_row.addWidget(self.btn_copy)
        copy_row.addStretch()
        layout.addLayout(copy_row)

        # Divider
        divider = QLabel("─── or load an existing private key ───")
        divider.setStyleSheet("color: #6b7280; font-size: 11px;")
        divider.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(divider)

        load_row = QHBoxLayout()
        self.load_path = QLineEdit()
        self.load_path.setPlaceholderText("Path to existing private key (.ppk, .pem, id_rsa, id_ed25519…)")
        load_row.addWidget(self.load_path)
        btn_browse_load = QPushButton("Browse…")
        btn_browse_load.clicked.connect(self._browse_load)
        load_row.addWidget(btn_browse_load)
        layout.addLayout(load_row)

        self.btn_load = QPushButton("Load key")
        self.btn_load.setIcon(icons.icon("folder"))
        self.btn_load.setFixedHeight(34)
        self.btn_load.clicked.connect(self._load_key)
        layout.addWidget(self.btn_load)

        self.load_status = QLabel("")
        self.load_status.setStyleSheet("font-size: 12px;")
        layout.addWidget(self.load_status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_save(self):
        is_ppk = "ppk" in self.format_combo.currentText().lower()
        is_ed = "Ed25519" in self.key_type.currentText()
        default_name = ("id_ed25519" if is_ed else "id_rsa") + (".ppk" if is_ppk else "")
        filt = "PuTTY Key (*.ppk)" if is_ppk else "All Files (*)"
        start = str(Path.home() / ".ssh" / default_name)
        path, _ = QFileDialog.getSaveFileName(self, "Save Private Key As", start, filt)
        if path:
            self.save_path.setText(path)

    def _generate(self):
        path = self.save_path.text().strip()
        if not path:
            self.status_label.setText("✗ Choose a save location first")
            self.status_label.setStyleSheet("color: #f87171; font-size: 12px;")
            return
        self.btn_generate.setEnabled(False)
        self.status_label.setText("Generating…")
        self.status_label.setStyleSheet("color: #fbbf24; font-size: 12px;")
        # Read on the GUI thread; the worker only computes.
        self._gen_comment = self.comment_input.text().strip() or "rpmote"
        self._gen_key_type = self.key_type.currentText()
        self._gen_is_ppk = "ppk" in self.format_combo.currentText().lower()
        threading.Thread(target=self._do_generate, args=(path,), daemon=True).start()

    def _do_generate(self, path: str):
        try:
            comment = self._gen_comment
            key_type = self._gen_key_type
            is_ppk = self._gen_is_ppk

            if "Ed25519" in key_type:
                key = paramiko.Ed25519Key.generate()
            elif "2048" in key_type:
                key = paramiko.RSAKey.generate(2048)
            else:
                key = paramiko.RSAKey.generate(4096)

            if is_ppk:
                save_as_ppk(key, path, comment)
            else:
                key.write_private_key_file(path)

            pub_text = f"{key.get_name()} {key.get_base64()} {comment}"
            pub_path = Path(path).with_suffix(".pub") if not path.endswith(".pub") else Path(path)
            with open(pub_path, "w") as f:
                f.write(pub_text + "\n")

            self._generated.emit(
                pub_text, f"✓ Saved to {Path(path).name}  (public key: {pub_path.name})",
                "#4ade80", path)
        except Exception as e:
            self._generated.emit("", f"✗ {e}", "#f87171", "")

    def _on_generated(self, pub_text: str, status: str, color: str, path: str):
        if path:
            self.generated_priv_path = path
            self.pub_key_display.setPlainText(pub_text)
            self.btn_copy.setEnabled(True)
        self.status_label.setText(status)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.btn_generate.setEnabled(True)

    def _copy_pub_key(self):
        QApplication.clipboard().setText(self.pub_key_display.toPlainText())

    def _browse_load(self):
        start = str(Path.home() / ".ssh") if (Path.home() / ".ssh").exists() else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Existing Private Key", start,
            "Key Files (*.ppk *.pem *.key *.pub);;All Files (*)"
        )
        if path:
            self.load_path.setText(path)

    def _load_key(self):
        path = self.load_path.text().strip()
        if not path:
            self.load_status.setText("✗ Choose a key file first")
            self.load_status.setStyleSheet("color: #f87171; font-size: 12px;")
            return
        try:
            key = load_ssh_private_key(path)
            pub_text = f"{key.get_name()} {key.get_base64()} (loaded)"
            self.pub_key_display.setPlainText(pub_text)
            self.btn_copy.setEnabled(True)
            self.generated_priv_path = path
            self.load_status.setText(f"✓ Loaded {key.get_name()} key from {Path(path).name}")
            self.load_status.setStyleSheet("color: #4ade80; font-size: 12px;")
            self.status_label.setText("")
        except Exception as e:
            self.load_status.setText(f"✗ {e}")
            self.load_status.setStyleSheet("color: #f87171; font-size: 12px;")


#: How each RDP connection opens. "default" follows the app-wide setting
#: (View › Open RDP Inside Tabs); the others pin it for this connection.
RDP_OPEN_MODES = (
    ("default", "App default (View › Open RDP Inside Tabs)"),
    ("embedded", "Inside a tab in RPMote"),
    ("external", "Its own Remote Desktop window"),
)


def rdp_open_mode(conn: dict) -> str:
    """How *conn* asked to open: "default", "embedded" or "external".

    Older records only have the ``rdp_embedded`` tick box, which was never
    honoured — a ticked one meant "inside a tab", an unticked one simply
    left it to the app.
    """
    mode = str((conn or {}).get("rdp_open_mode", "") or "").strip().lower()
    if mode in ("default", "embedded", "external"):
        return mode
    return "embedded" if (conn or {}).get("rdp_embedded") else "default"


class ConnectionDialog(QDialog):
    """Dialog to add or edit a connection."""

    #: (challenge, what-to-retry) from a worker thread to the GUI thread.
    host_key_challenge = pyqtSignal(object, str)
    #: A callable to run on the GUI thread (status updates from workers).
    _ui_call = pyqtSignal(object)

    #: Protocols in the order the picker lists them, with their default port.
    PROTOCOL_PORTS = {
        "SSH": 22,
        "Telnet": 23,
        "Serial": 0,          # no network port: the COM port is the address
        "SFTP": 22,
        "FTP": 21,
        "RDP": 3389,
        "VNC": 5900,
        "Web": 443,
        "iDRAC": 443,
        "NAS": 5001,
        "Azure PowerShell": 443,
    }
    PROTOCOL_KEYS = {
        "SSH": "ssh",
        "Telnet": "telnet",
        "Serial": "serial",
        "SFTP": "sftp",
        "FTP": "ftp",
        "RDP": "rdp",
        "VNC": "vnc",
        "Web": "web",
        "iDRAC": "idrac",
        "NAS": "synology",
        "Azure PowerShell": "azure_ps",
    }
    #: Which form rows each protocol uses. A row not listed is hidden, not
    #: just greyed out — a disabled field still reads as "fill me in".
    PROTOCOL_ROWS = {
        "SSH": {"host", "port", "username", "password", "key", "pubkey", "install"},
        "Telnet": {"host", "port", "username", "password"},
        "Serial": set(),
        "SFTP": {"host", "port", "username", "password", "key", "pubkey", "install"},
        "FTP": {"host", "port", "username", "password"},
        "RDP": {"host", "port", "username", "password", "rdp_mode"},
        "VNC": {"host", "port", "password"},
        "Web": {"host", "port", "username", "password"},
        "iDRAC": {"host", "port", "username", "password"},
        "NAS": {"host", "port", "username", "password"},
        "Azure PowerShell": set(),
    }
    #: Consoles RPMote drives in its own browser, so it can type the login
    #: and the 2FA code.
    WEB_CONSOLES = ("Web", "iDRAC", "NAS")
    #: SSH-option rows that only mean something for an interactive shell.
    SHELL_ONLY_ROWS = {"forwards", "on_drop", "scrollback"}
    #: Width of the label column, shared by every section so fields line up.
    LABEL_WIDTH = 128

    def __init__(self, connection=None, parent=None, default_tenant: str | None = None,
                 vault=None):
        super().__init__(parent)
        self.connection = connection or {}
        self._host_key_prompt_open = False
        self.host_key_challenge.connect(
            self._on_host_key_challenge, Qt.ConnectionType.QueuedConnection
        )
        self.default_tenant = (default_tenant or "").strip()
        # Needed to re-authenticate before an existing saved secret is revealed.
        self.vault = vault
        self._tenant_names = {}
        self._tenant_colors = {}
        self._tenant_color_choices = {}
        records = list(getattr(vault, "connections", []) or [])
        if connection:
            records.insert(0, connection)
        for record in records:
            name = str(record.get("tenant", "Default") or "Default").strip()
            self._tenant_names.setdefault(name.casefold(), name)
            color = record.get("tenant_color", "")
            if color and QColor(color).isValid():
                self._tenant_colors.setdefault(name.casefold(), color)
        # Tenant settings are authoritative; older connection records can
        # still carry the color the tenant had when they were created.
        for name, color in (getattr(vault, "settings", {}) or {}).get("tenant_colors", {}).items():
            name = str(name).strip()
            self._tenant_names[name.casefold()] = name
            if color and QColor(color).isValid():
                self._tenant_colors[name.casefold()] = color
        self._reveal_authorised = False
        self._ui_call.connect(lambda fn: fn(), Qt.ConnectionType.QueuedConnection)
        self._rows: dict[str, tuple] = {}
        self._last_protocol = None
        editing = bool(connection and connection.get("id"))
        self.setWindowTitle("Edit Connection" if editing else "New Connection")
        self.setMinimumWidth(600)
        self.setModal(True)
        self._setup_ui()
        if connection:
            self._populate(connection)
        else:
            if self.default_tenant:
                self.tenant_input.setText(self.default_tenant)
        self._sync_tenant_color()
        self.resize(680, 760)
        fit_to_screen(self)
        if not connection and self.default_tenant:
            # Tenant is already answered, so start typing in Name.
            self.name_input.setFocus()
        else:
            self.tenant_combo.setFocus()

    def _post(self, fn):
        """Run *fn* on the GUI thread.

        Test, Install key, Wake and the like do their network work on a
        worker thread; Qt widgets may only be touched from the GUI thread, so
        their results come back through here.
        """
        if QThread.currentThread() is self.thread():
            fn()
        else:
            self._ui_call.emit(fn)

    def _set_status(self, label: QLabel, text: str, color: str,
                    tooltip: str | None = None, size: int = 12):
        def apply():
            label.setText(text)
            label.setStyleSheet(f"color: {color}; font-size: {size}px;")
            if tooltip is not None:
                label.setToolTip(tooltip)
        self._post(apply)

    def _form(self, parent: QWidget) -> QFormLayout:
        form = QFormLayout(parent)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        return form

    def _row(self, form: QFormLayout, key: str, label: str, field):
        """Add a row whose label column is the same width in every section."""
        if not isinstance(field, QWidget):
            holder = QWidget()
            field.setContentsMargins(0, 0, 0, 0)
            holder.setLayout(field)
            field = holder
        caption = QLabel(label)
        caption.setFixedWidth(self.LABEL_WIDTH)
        caption.setMinimumHeight(32)
        caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        caption.setProperty("role", "form-label")
        form.addRow(caption, field)
        if key:
            self._rows[key] = (form, field, caption)
        return field

    def _set_row_visible(self, key: str, visible: bool):
        entry = self._rows.get(key)
        if entry is None:
            return
        form, field, _caption = entry
        form.setRowVisible(field, bool(visible))

    def _setup_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll = scroll

        content = QWidget()
        content.setObjectName("connection_form")
        scroll.setWidget(content)
        outer_layout.addWidget(scroll, 1)

        layout = QVBoxLayout(content)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 20, 24, 16)

        # Title, with the favourite star where the eye starts.
        title_row = QHBoxLayout()
        title = QLabel("Edit Connection" if self.connection.get("id") else "New Connection")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title_row.addWidget(title)
        title_row.addStretch()
        self.favorite_toggle = QToolButton()
        self.favorite_toggle.setCheckable(True)
        self.favorite_toggle.setText("Favorite")
        self.favorite_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.favorite_toggle.setIconSize(QSize(16, 16))
        self.favorite_toggle.setToolTip("Favorites are listed first in the sidebar, "
                                        "on the dashboard and in Go to (Ctrl+P)")
        self.favorite_toggle.toggled.connect(self._sync_favorite_icon)
        self._sync_favorite_icon(False)
        title_row.addWidget(self.favorite_toggle)
        layout.addLayout(title_row)

        # ── Connection ─────────────────────────────────────────────────
        basic = QGroupBox("Connection")
        basic_layout = self._form(basic)

        self.tenant_combo = QComboBox()
        self.tenant_combo.setEditable(True)
        self.tenant_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.tenant_combo.addItems(sorted(self._tenant_names.values(), key=str.casefold))
        self.tenant_combo.setCurrentIndex(-1)
        self.tenant_combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.tenant_combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.tenant_combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.tenant_input = self.tenant_combo.lineEdit()
        self.tenant_input.setPlaceholderText("Customer or site — pick one or type a new name")
        tenant_row = QHBoxLayout()
        tenant_row.setSpacing(8)
        tenant_row.addWidget(self.tenant_combo, 1)
        self._tenant_color = "#a78bfa"  # default purple
        self.tenant_color_btn = QPushButton()
        self.tenant_color_btn.setFixedSize(34, 34)
        self.tenant_color_btn.setToolTip("Tenant colour — used for this tenant everywhere")
        self.tenant_color_btn.setAccessibleName("Tenant colour")
        self.tenant_color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_color_btn()
        self.tenant_color_btn.clicked.connect(self._pick_tenant_color)
        tenant_row.addWidget(self.tenant_color_btn)
        self._row(basic_layout, "tenant", "Tenant", tenant_row)
        self.tenant_input.textChanged.connect(self._sync_tenant_color)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. DC01, web01, Backup NAS")
        self._row(basic_layout, "name", "Name", self.name_input)
        self.setTabOrder(self.tenant_combo, self.name_input)

        self.protocol = QComboBox()
        for label, key in self.PROTOCOL_KEYS.items():
            self.protocol.addItem(icons.protocol_icon(key), label)
        self.protocol.setIconSize(QSize(16, 16))
        self._row(basic_layout, "protocol", "Protocol", self.protocol)
        self.setTabOrder(self.name_input, self.protocol)

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("192.168.1.100 or hostname.com")
        self._host_caption = self._row(basic_layout, "host", "Host", self.host_input)

        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(22)
        self.port_input.setMaximumWidth(140)
        self._row(basic_layout, "port", "Port", self.port_input)

        # Which environment this machine belongs to. Left unset, RPMote falls
        # back to the tenant's own marking and then to the name — this is here
        # for the box called "web01" that happens to be the live one.
        self.environment = QComboBox()
        for value in guardrails.ENVIRONMENTS:
            self.environment.addItem(
                guardrails.ENVIRONMENT_LABELS.get(value, value or "Not specified"),
                value)
        self.environment.setToolTip(
            "Production sessions wear a PROD badge and ask before running "
            "anything destructive.\nLeave this unset to go by the tenant and "
            "the connection's name instead.")
        self._row(basic_layout, "environment", "Environment", self.environment)

        layout.addWidget(basic)

        # ── Sign-in ────────────────────────────────────────────────────
        auth = QGroupBox("Sign-in")
        auth_layout = self._form(auth)

        # A shared credential (File › Shared Credentials) instead of a login
        # of its own: rotate the password once, every connection follows.
        self.credential_combo = QComboBox()
        self.credential_combo.setToolTip(
            "Use a shared credential instead of typing this connection's own login.")
        self._fill_credentials()
        self.credential_combo.currentIndexChanged.connect(self._on_credential_change)
        self._row(auth_layout, "credential", "Login", self.credential_combo)
        self.credential_hint = QLabel("")
        self.credential_hint.setWordWrap(True)
        self.credential_hint.setProperty("role", "hint")
        self._row(auth_layout, "credential_hint", "", self.credential_hint)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("username (DOMAIN\\user for Windows)")
        self._row(auth_layout, "username", "Username", self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        attach_reveal_toggle(
            self.password_input,
            can_reveal=self._can_reveal_secret,
            tooltip="Show the saved password",
        )
        self._row(auth_layout, "password", "Password", self.password_input)

        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self.key_file_input = QLineEdit()
        self.key_file_input.setPlaceholderText("Optional — .ppk, .pem, id_rsa, id_ed25519…")
        key_row.addWidget(self.key_file_input, 1)
        btn_browse = QPushButton("Browse…")
        btn_browse.setToolTip("Choose a private key file")
        btn_browse.clicked.connect(self._browse_key)
        key_row.addWidget(btn_browse)
        btn_gen = QPushButton("Generate…")
        btn_gen.setToolTip("Generate a new RSA or Ed25519 key pair (PPK or OpenSSH format)")
        btn_gen.clicked.connect(self._open_generate_dialog)
        key_row.addWidget(btn_gen)
        self._row(auth_layout, "key", "Private key", key_row)

        pub_row = QHBoxLayout()
        pub_row.setSpacing(8)
        self.pub_key_input = QLineEdit()
        self.pub_key_input.setPlaceholderText("Optional — found next to the private key if blank")
        pub_row.addWidget(self.pub_key_input, 1)
        btn_browse_pub = QPushButton("Browse…")
        btn_browse_pub.setToolTip("Choose a public key (.pub) file")
        btn_browse_pub.clicked.connect(self._browse_pub_key)
        pub_row.addWidget(btn_browse_pub)
        self._row(auth_layout, "pubkey", "Public key", pub_row)

        install_row = QHBoxLayout()
        install_row.setSpacing(10)
        self.btn_install_key = QPushButton("Install public key on server")
        self.btn_install_key.setIcon(icons.icon("key"))
        self.btn_install_key.setToolTip(
            "Uses the password above to upload your public key to the server's "
            "~/.ssh/authorized_keys, so future connections can use key auth."
        )
        self.btn_install_key.clicked.connect(self._install_public_key)
        install_row.addWidget(self.btn_install_key)

        self.install_status = QLabel("")
        self.install_status.setStyleSheet("font-size: 12px;")
        self.install_status.setWordWrap(True)
        install_row.addWidget(self.install_status, 1)
        self._row(auth_layout, "install", "", install_row)

        self.rdp_open_mode = QComboBox()
        for value, label in RDP_OPEN_MODES:
            self.rdp_open_mode.addItem(label, value)
        self.rdp_open_mode.setToolTip(
            "Where this desktop opens. \"App default\" follows "
            "View › Open RDP Inside Tabs.")
        self._row(auth_layout, "rdp_mode", "Open in", self.rdp_open_mode)

        layout.addWidget(auth)
        self.auth_group = auth

        layout.addWidget(self._build_gateway_group())
        layout.addWidget(self._build_totp_group())
        layout.addWidget(self._build_serial_group())

        # ── SSH options (folded: most connections never need them) ────
        ssh_opts = QGroupBox()
        ssh_opts.setFlat(True)
        ssh_layout = self._form(ssh_opts)

        self.passphrase_input = QLineEdit()
        self.passphrase_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase_input.setPlaceholderText(
            "Only if the private key has one")
        self.passphrase_input.setToolTip(
            "Only unlocks the key file. The password above is what is sent to "
            "the server; before this field existed one box had to do both."
        )
        self._row(ssh_layout, "passphrase", "Key passphrase", self.passphrase_input)

        self.use_agent = QCheckBox("Offer keys held by the SSH agent")
        self.use_agent.setChecked(True)
        self.use_agent.setToolTip(
            "Pageant or the Windows OpenSSH agent, if one is running."
        )
        self._row(ssh_layout, "agent", "Agent", self.use_agent)

        self.jump_combo = QComboBox()
        self.jump_combo.setToolTip(
            "Reach this machine through another saved connection, the way "
            "ssh -J does. The bastion is verified and authenticated in its "
            "own right."
        )
        self._populate_jump_hosts()
        self._row(ssh_layout, "jump", "Jump host", self.jump_combo)

        self.forwards_input = QLineEdit()
        self.forwards_input.setPlaceholderText(
            "8080:127.0.0.1:80, R:9000:127.0.0.1:22")
        self.forwards_input.setToolTip(
            "Port forwards brought up with the session. Same shapes as ssh: "
            "listen:host:port for a local forward, R:listen:host:port for a "
            "reverse one. Local forwards bind 127.0.0.1 unless you give an "
            "address. Comma separated."
        )
        self._row(ssh_layout, "forwards", "Port forwards", self.forwards_input)

        self.auto_reconnect = QCheckBox("Reconnect automatically if the session drops")
        self.auto_reconnect.setChecked(True)
        self._row(ssh_layout, "on_drop", "On drop", self.auto_reconnect)

        self.scrollback_input = QSpinBox()
        self.scrollback_input.setRange(0, 1_000_000)
        self.scrollback_input.setSingleStep(1000)
        self.scrollback_input.setGroupSeparatorShown(True)
        self.scrollback_input.setValue(DEFAULT_SCROLLBACK)
        self.scrollback_input.setSuffix(" lines")
        self.scrollback_input.setMaximumWidth(200)
        self.scrollback_input.setToolTip(
            "How much history to keep. Rows in history are stored packed, so "
            "a hundred thousand lines costs a few tens of megabytes rather "
            "than the gigabytes it would take to keep them as live cells. "
            "Zero keeps no history at all."
        )
        self._row(ssh_layout, "scrollback", "Scrollback", self.scrollback_input)

        self.ssh_options_group = ssh_opts
        self.ssh_section = CollapsibleSection("SSH options", ssh_opts)
        layout.addWidget(self.ssh_section)

        # ── Azure ──────────────────────────────────────────────────────
        azure_auth = QGroupBox("Microsoft 365 / Azure sign-in")
        azure_layout = self._form(azure_auth)

        self.azure_auth_mode = QComboBox()
        self.azure_auth_mode.addItems(["Interactive", "Device Code", "Service Principal"])
        self.azure_auth_mode.currentTextChanged.connect(self._on_azure_auth_mode_change)
        self._row(azure_layout, "azure_mode", "Sign-in", self.azure_auth_mode)

        # Microsoft services this connection should sign in to. Lets an admin
        # work a customer tenant across Exchange, Graph, Teams, etc.
        self.azure_service_checks: dict[str, QCheckBox] = {}
        services_box = QWidget()
        services_grid = QHBoxLayout(services_box)
        services_grid.setContentsMargins(0, 0, 0, 0)
        services_col_a = QVBoxLayout()
        services_col_b = QVBoxLayout()
        service_defs = [
            ("azure", "Azure (Az)"),
            ("exchange", "Exchange Online (mailboxes)"),
            ("graph", "Microsoft Graph (users/licensing)"),
            ("teams", "Microsoft Teams"),
            ("spo", "SharePoint / OneDrive (PnP)"),
            # "&&": a single & would be eaten as a keyboard mnemonic.
            ("compliance", "Security && Compliance"),
        ]
        for idx, (key, label) in enumerate(service_defs):
            cb = QCheckBox(label)
            if key in ("azure", "exchange", "graph"):
                cb.setChecked(True)
            self.azure_service_checks[key] = cb
            (services_col_a if idx % 2 == 0 else services_col_b).addWidget(cb)
        services_grid.addLayout(services_col_a)
        services_grid.addLayout(services_col_b)
        self._row(azure_layout, "azure_services", "Services", services_box)

        self.azure_tenant_id = QLineEdit()
        self.azure_tenant_id.setPlaceholderText("Tenant ID or domain (customer tenant)")
        self._row(azure_layout, "azure_tenant", "Tenant ID", self.azure_tenant_id)

        self.azure_organization = QLineEdit()
        self.azure_organization.setPlaceholderText("customer.onmicrosoft.com")
        self.azure_organization.setToolTip("Used for Exchange, Security & Compliance and SharePoint")
        self._row(azure_layout, "azure_org", "Organization", self.azure_organization)
        self.azure_use_delegated_org = QCheckBox("Use Organization for delegated customer access")
        self.azure_use_delegated_org.setToolTip("Enable for Exchange/Compliance customer access through GDAP/delegated admin permissions. Leave off when signing into your own tenant.")
        self._row(azure_layout, "azure_delegated", "Delegated (GDAP)", self.azure_use_delegated_org)

        self.azure_sharepoint_url = QLineEdit()
        self.azure_sharepoint_url.setPlaceholderText("https://customer-admin.sharepoint.com (optional override)")
        self._row(azure_layout, "azure_spo", "SharePoint URL", self.azure_sharepoint_url)

        self.azure_pnp_client_id = QLineEdit()
        self.azure_pnp_client_id.setPlaceholderText("Registered PnP app client ID (required for SharePoint)")
        self._row(azure_layout, "azure_pnp", "PnP client ID", self.azure_pnp_client_id)

        self.azure_upn = QLineEdit()
        self.azure_upn.setPlaceholderText("Admin UPN to sign in as (optional)")
        self._row(azure_layout, "azure_upn", "Admin UPN", self.azure_upn)

        self.azure_subscription_id = QLineEdit()
        self.azure_subscription_id.setPlaceholderText("Subscription ID (optional)")
        self._row(azure_layout, "azure_sub", "Subscription", self.azure_subscription_id)

        self.azure_client_id = QLineEdit()
        self.azure_client_id.setPlaceholderText("Required for Service Principal; optional Graph app")
        self._row(azure_layout, "azure_client", "Client ID", self.azure_client_id)

        self.azure_client_secret = QLineEdit()
        self.azure_client_secret.setPlaceholderText("Client secret")
        self.azure_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        attach_reveal_toggle(
            self.azure_client_secret,
            can_reveal=self._can_reveal_secret,
            tooltip="Show the saved client secret",
        )
        self._row(azure_layout, "azure_secret", "Client secret", self.azure_client_secret)

        self.azure_cloud = QComboBox()
        self.azure_cloud.addItems(["AzureCloud", "AzureUSGovernment", "AzureChinaCloud", "AzureGermanCloud"])
        self._row(azure_layout, "azure_cloud", "Cloud", self.azure_cloud)

        azure_hint = QLabel(
            "Interactive & Device Code prompt an admin sign-in per service. "
            "Service Principal (client ID + secret) covers Azure and Graph; "
            "other services need a separate interactive/device-code connection. "
            "SharePoint needs PowerShell 7.4+ and a registered PnP app client ID. "
            "Enable delegated customer access only when using delegated admin permissions."
        )
        azure_hint.setWordWrap(True)
        azure_hint.setProperty("role", "hint")
        self._row(azure_layout, "azure_hint", "", azure_hint)

        layout.addWidget(azure_auth)
        self.azure_auth_group = azure_auth

        # ── Wake-on-LAN (folded unless a MAC is saved) ─────────────────
        # A MAC is the one thing RPMote cannot work out for itself once the
        # machine is off, so it is asked for here rather than at the moment
        # someone urgently needs the box back.
        wake_box = QWidget()
        wake_layout = self._form(wake_box)
        mac_row = QHBoxLayout()
        mac_row.setSpacing(8)
        self.mac_input = QLineEdit()
        self.mac_input.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        mac_row.addWidget(self.mac_input, 1)
        self.btn_wake_test = QPushButton("Wake now")
        self.btn_wake_test.setIcon(icons.icon("power"))
        self.btn_wake_test.setToolTip(
            "Send a magic packet to this MAC now, to check Wake-on-LAN works.\n"
            "The machine must have WoL enabled in its BIOS and NIC settings, "
            "and RPMote must be on the same network segment.")
        self.btn_wake_test.clicked.connect(self._send_wake_test)
        mac_row.addWidget(self.btn_wake_test)
        self._row(wake_layout, "mac", "MAC address", mac_row)

        self.wake_status = QLabel("")
        self.wake_status.setWordWrap(True)
        self.wake_status.setStyleSheet("color:#94a3b8;font-size:11px;")
        self._row(wake_layout, "wake_status", "", self.wake_status)
        self.wake_section = CollapsibleSection("Wake-on-LAN", wake_box)
        layout.addWidget(self.wake_section)

        # ── Notes ──────────────────────────────────────────────────────
        notes_box = QWidget()
        notes_layout = QVBoxLayout(notes_box)
        notes_layout.setContentsMargins(0, 0, 0, 0)
        self.notes_input = QPlainTextEdit()
        self.notes_input.setPlaceholderText(
            "Anything worth knowing next time: VPN needed first, call before "
            "rebooting, who to ask…")
        self.notes_input.setFixedHeight(96)
        self.notes_input.setObjectName("notes_input")
        notes_layout.addWidget(self.notes_input)
        self.notes_section = CollapsibleSection("Notes", notes_box)
        layout.addWidget(self.notes_section)

        layout.addStretch(1)

        # ── Pinned footer: always on screen, however long the form ─────
        footer = QFrame()
        footer.setObjectName("dialog_footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(24, 12, 24, 14)
        footer_layout.setSpacing(10)

        self.btn_test = QPushButton("Test connection")
        self.btn_test.setIcon(icons.icon("plug"))
        self.btn_test.setToolTip("Check the host answers (and, for SSH, that the login works) "
                                 "without saving")
        self.btn_test.clicked.connect(self._test_connection)
        footer_layout.addWidget(self.btn_test)

        self.test_status = QLabel("")
        self.test_status.setStyleSheet("font-size: 12px;")
        self.test_status.setWordWrap(True)
        self.test_status.setMinimumWidth(80)
        self.test_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer_layout.addWidget(self.test_status, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        set_variant(buttons.button(QDialogButtonBox.StandardButton.Save), "primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        footer_layout.addWidget(buttons)
        outer_layout.addWidget(footer)

        self.protocol.currentTextChanged.connect(self._on_protocol_change)
        self._on_protocol_change(self.protocol.currentText())
        guard_wheel(content)

    _MANAGE = "__manage__"

    def _credentials(self) -> list:
        try:
            return list(getattr(self.vault, "credentials", []) or [])
        except Exception:
            return []

    def _fill_credentials(self, select: str | None = None):
        combo = self.credential_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(icons.icon("user"), "This connection's own login", "")
        for cred in sorted(self._credentials(), key=lambda c: str(c.get("name", "")).casefold()):
            combo.addItem(icons.icon("key"),
                          f"{cred.get('name', '')}  —  {cred.get('username', '')}",
                          str(cred.get("id", "")))
        if self.vault is not None:
            combo.addItem(icons.icon("settings"), "Manage shared credentials…", self._MANAGE)
        index = combo.findData(select or "")
        combo.setCurrentIndex(max(0, index))
        self._last_credential = combo.currentData() or ""
        combo.blockSignals(False)

    def _selected_credential(self) -> dict | None:
        cred_id = self.credential_combo.currentData() or ""
        if not cred_id or cred_id == self._MANAGE:
            return None
        for cred in self._credentials():
            if str(cred.get("id", "")) == cred_id:
                return cred
        return None

    def _on_credential_change(self, _index=None):
        data = self.credential_combo.currentData() or ""
        if data == self._MANAGE:
            from app.credentials_dialog import CredentialsManagerDialog
            CredentialsManagerDialog(self.vault, self).exec()
            self._fill_credentials(select=self._last_credential)
        else:
            self._last_credential = data
        self._apply_login_rows()

    def _apply_login_rows(self):
        """Hide the own-login rows while a shared credential stands in for them."""
        protocol = self.protocol.currentText()
        rows = self.PROTOCOL_ROWS.get(protocol, self.PROTOCOL_ROWS["SSH"])
        uses_login = "username" in rows
        cred = self._selected_credential() if uses_login else None
        self._set_row_visible("credential", uses_login and (bool(self._credentials())
                                                            or self.vault is not None))
        self._set_row_visible("credential_hint", cred is not None)
        for key in ("username", "password", "key", "pubkey", "install"):
            self._set_row_visible(key, key in rows and cred is None)
        if hasattr(self, "passphrase_input"):
            self._set_row_visible("passphrase", cred is None)
        if cred is not None:
            when = cred.get("updated_at")
            from datetime import datetime
            changed = (datetime.fromtimestamp(float(when)).strftime("%Y-%m-%d")
                       if when else "unknown")
            key = " and a private key" if cred.get("key_file") else ""
            self.credential_hint.setText(
                f"Signs in as {cred.get('username', '')}{key}. Password last changed "
                f"{changed} — edit it once under File › Shared Credentials.")

    def _sync_favorite_icon(self, checked: bool):
        self.favorite_toggle.setIcon(
            icons.icon("star-filled", "#fbbf24") if checked else icons.icon("star"))

    def _build_gateway_group(self) -> QGroupBox:
        """RD Gateway settings, laid out the way mstsc lays them out.

        Someone filling this in is copying from a runbook written against
        Remote Desktop Connection's own "Connect from anywhere" dialog, so the
        wording here is that dialog's wording rather than the .rdp file's.
        """
        group = QGroupBox("RD Gateway  (Connect from anywhere)")
        form = self._form(group)

        self.gateway_host = QLineEdit()
        self.gateway_host.setPlaceholderText(
            "gateway.contoso.com — leave blank to connect directly")
        self.gateway_host.setToolTip(
            "The RD Gateway this machine is published through. Add :443 or "
            "another port if the gateway does not listen on the default.")
        self._row(form, "gw_host", "Server address", self.gateway_host)

        self.gateway_bypass_local = QCheckBox(
            "Bypass RD Gateway server for local addresses")
        self.gateway_bypass_local.setChecked(True)
        self.gateway_bypass_local.setToolTip(
            "Try the machine directly first and fall back to the gateway. "
            "Leave this on for anything you also reach from inside the "
            "customer's network — it saves a round trip through the DMZ when "
            "you are already on the LAN or on VPN.")
        self._row(form, "gw_bypass", "", self.gateway_bypass_local)

        self.gateway_use_for_remote = QCheckBox(
            "Use my RD Gateway credentials for the remote computer")
        self.gateway_use_for_remote.setChecked(True)
        self.gateway_use_for_remote.setToolTip(
            "One set of credentials answers both the gateway and the desktop "
            "behind it. With this on, the username and password above are "
            "what the gateway is given unless a separate gateway login is "
            "filled in below.")
        self.gateway_use_for_remote.toggled.connect(self._on_gateway_creds_toggle)
        self._row(form, "gw_share", "", self.gateway_use_for_remote)

        self.gateway_username = QLineEdit()
        self.gateway_username.setPlaceholderText(
            "DOMAIN\\user — optional, only if the gateway wants a different login")
        self._row(form, "gw_user", "Gateway user", self.gateway_username)

        self.gateway_password = QLineEdit()
        self.gateway_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.gateway_password.setPlaceholderText(
            "Password for the gateway login — optional")
        attach_reveal_toggle(
            self.gateway_password,
            can_reveal=self._can_reveal_secret,
            tooltip="Show the saved gateway password",
        )
        self._row(form, "gw_password", "Gateway password", self.gateway_password)

        self.gateway_hint = QLabel("")
        self.gateway_hint.setWordWrap(True)
        self.gateway_hint.setProperty("role", "hint")
        self._row(form, "gw_hint", "", self.gateway_hint)

        self.gateway_host.textChanged.connect(self._on_gateway_host_change)
        self.gateway_username.textChanged.connect(
            lambda _text: self._on_gateway_creds_toggle(
                self.gateway_use_for_remote.isChecked()))
        self.username_input.textChanged.connect(
            lambda _text: self._on_gateway_creds_toggle(
                self.gateway_use_for_remote.isChecked()))
        self.rdp_gateway_group = group
        self._on_gateway_host_change(self.gateway_host.text())
        return group

    def _on_gateway_host_change(self, text: str):
        """Enable the rest of the block only once there is a gateway to use."""
        enabled = bool(normalize_gateway_host(text))
        for widget in (self.gateway_bypass_local, self.gateway_use_for_remote,
                       self.gateway_username, self.gateway_password):
            widget.setEnabled(enabled)
        self._on_gateway_creds_toggle(self.gateway_use_for_remote.isChecked())

    def _on_gateway_creds_toggle(self, shared: bool):
        """Say what the gateway will actually be handed, in words."""
        if not normalize_gateway_host(self.gateway_host.text()):
            self.gateway_hint.setText(
                "No gateway — RPMote connects straight to the host above.")
            return
        gateway_user = self.gateway_username.text().strip()
        if gateway_user:
            hint = ("The gateway login below is used for the gateway; the "
                    "credentials above are used for the desktop.")
            login_for_gateway = gateway_user
        elif shared:
            hint = ("The username and password above are used for both the "
                    "gateway and the desktop.")
            login_for_gateway = self.username_input.text().strip()
        else:
            hint = ("Windows will ask for gateway credentials separately when the "
                    "session opens.")
            login_for_gateway = ""
        # A gateway authenticates over NTLM, where a login with no domain is
        # a guess the gateway makes. Worth saying before the first attempt
        # fails with "your credentials did not work".
        if login_for_gateway and not account_has_domain(login_for_gateway):
            hint += (f"  Gateways need the domain: save \"{login_for_gateway}\" as "
                     f"DOMAIN\\{login_for_gateway} or {login_for_gateway}@domain.")
        self.gateway_hint.setText(hint)

    def _build_totp_group(self) -> QGroupBox:
        """Somewhere to keep the second factor a NAS or web console asks for."""
        group = QGroupBox("Two-Step Verification")
        form = self._form(group)

        self.totp_secret = QLineEdit()
        self.totp_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.totp_secret.setPlaceholderText(
            "Base32 secret or otpauth:// link — leave blank if 2FA is off")
        self.totp_secret.setToolTip(
            "The secret shown next to the QR code when 2-step verification is "
            "set up on the NAS or console. RPMote generates the six-digit code from it "
            "so the console can be opened without reaching for a phone.")
        attach_reveal_toggle(
            self.totp_secret,
            can_reveal=self._can_reveal_secret,
            tooltip="Show the saved 2FA secret",
        )
        self.totp_secret.textChanged.connect(self._refresh_totp_preview)
        self._row(form, "totp_secret", "Secret", self.totp_secret)

        self.totp_preview = QLabel("")
        self.totp_preview.setWordWrap(True)
        self.totp_preview.setStyleSheet("font-size:12px;color:#94a3b8;")
        self._row(form, "totp_preview", "Code now", self.totp_preview)

        warning = QLabel(
            "Kept in this vault beside the password, so anyone who can open "
            "the vault has both factors. Worth it when the alternative is a "
            "code passed around the team; leave blank to keep the phone in "
            "the loop."
        )
        warning.setWordWrap(True)
        warning.setProperty("role", "hint")
        self._row(form, "totp_warning", "", warning)

        # Only ticks while the dialog is open, and only for the preview.
        self._totp_timer = QTimer(self)
        self._totp_timer.setInterval(1000)
        self._totp_timer.timeout.connect(self._refresh_totp_preview)
        self.finished.connect(lambda _result: self._totp_timer.stop())
        self.totp_group = group
        return group

    def _build_serial_group(self) -> QGroupBox:
        """Line settings for a console cable: which COM port, and how to talk on it."""
        from app.stream_transports import BAUD_RATES, FLOW_CONTROLS, PARITIES

        group = QGroupBox("Serial line")
        form = self._form(group)

        port_row = QHBoxLayout()
        port_row.setSpacing(8)
        self.serial_port_combo = QComboBox()
        self.serial_port_combo.setEditable(True)
        self.serial_port_combo.lineEdit().setPlaceholderText("COM3")
        self.serial_port_combo.setToolTip(
            "The COM port of the console cable or USB adapter. Device Manager › "
            "Ports (COM & LPT) shows the number Windows gave it.")
        port_row.addWidget(self.serial_port_combo, 1)
        refresh = QPushButton("Refresh")
        refresh.setIcon(icons.icon("refresh"))
        refresh.setToolTip("Look for COM ports again (after plugging an adapter in)")
        refresh.clicked.connect(self._fill_serial_ports)
        port_row.addWidget(refresh)
        self._row(form, "serial_port", "COM port", port_row)

        self.serial_baud = QComboBox()
        self.serial_baud.setEditable(True)
        for rate in BAUD_RATES:
            self.serial_baud.addItem(str(rate))
        self.serial_baud.setCurrentText("9600")
        self.serial_baud.setMaximumWidth(160)
        self.serial_baud.setToolTip(
            "9600 for most switches, routers, firewalls and UPS consoles; "
            "115200 for many newer devices.")
        self._row(form, "serial_baud", "Speed (baud)", self.serial_baud)

        self.serial_data_bits = QComboBox()
        self.serial_data_bits.addItems(["8", "7", "6", "5"])
        self.serial_data_bits.setMaximumWidth(160)
        self._row(form, "serial_data_bits", "Data bits", self.serial_data_bits)

        self.serial_parity = QComboBox()
        for value in PARITIES:
            self.serial_parity.addItem(value.capitalize(), value)
        self.serial_parity.setMaximumWidth(160)
        self._row(form, "serial_parity", "Parity", self.serial_parity)

        self.serial_stop_bits = QComboBox()
        self.serial_stop_bits.addItems(["1", "1.5", "2"])
        self.serial_stop_bits.setMaximumWidth(160)
        self._row(form, "serial_stop_bits", "Stop bits", self.serial_stop_bits)

        flow_labels = {"none": "None", "rts/cts": "Hardware (RTS/CTS)",
                       "xon/xoff": "Software (XON/XOFF)"}
        self.serial_flow = QComboBox()
        for value in FLOW_CONTROLS:
            self.serial_flow.addItem(flow_labels.get(value, value), value)
        self.serial_flow.setMaximumWidth(220)
        self._row(form, "serial_flow", "Flow control", self.serial_flow)

        hint = QLabel("Most network gear wants 9600 baud, 8 data bits, no parity, "
                      "1 stop bit and no flow control (9600 8N1).")
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        self._row(form, "serial_hint", "", hint)

        self._fill_serial_ports()
        self.serial_group = group
        return group

    def _fill_serial_ports(self):
        from app.stream_transports import list_serial_ports
        current = self.serial_port_combo.currentText().strip()
        ports = list_serial_ports()
        self.serial_port_combo.clear()
        self.serial_port_combo.addItems(ports)
        if current:
            self.serial_port_combo.setCurrentText(current)
        elif ports:
            self.serial_port_combo.setCurrentIndex(0)
        if hasattr(self, "test_status") and self.protocol.currentText() == "Serial":
            found = ", ".join(ports) if ports else "none — is the adapter plugged in?"
            self._set_status(self.test_status, f"COM ports: {found}", "#94a3b8")

    def _serial_settings(self) -> dict:
        """The serial line as typed; a baud rate of 0 means it is not a number."""
        try:
            baud = int(self.serial_baud.currentText().strip() or 9600)
        except ValueError:
            baud = 0
        return {
            "serial_port": self.serial_port_combo.currentText().strip().upper(),
            "baud_rate": baud,
            "data_bits": int(self.serial_data_bits.currentText() or 8),
            "parity": self.serial_parity.currentData() or "none",
            "stop_bits": float(self.serial_stop_bits.currentText() or 1),
            "flow_control": self.serial_flow.currentData() or "none",
        }

    def _set_serial_settings(self, conn: dict):
        port = str(conn.get("serial_port", "") or "")
        if not port and str(conn.get("protocol", "")).lower() == "serial":
            port = str(conn.get("host", "") or "")
        if port:
            self.serial_port_combo.setCurrentText(port)
        self.serial_baud.setCurrentText(str(conn.get("baud_rate", 9600) or 9600))
        self.serial_data_bits.setCurrentText(str(conn.get("data_bits", 8) or 8))
        index = self.serial_parity.findData(str(conn.get("parity", "none") or "none").lower())
        self.serial_parity.setCurrentIndex(max(0, index))
        try:
            stop = float(conn.get("stop_bits", 1) or 1)
        except (TypeError, ValueError):
            stop = 1.0
        self.serial_stop_bits.setCurrentText("1.5" if stop == 1.5 else str(int(stop)))
        index = self.serial_flow.findData(str(conn.get("flow_control", "none") or "none").lower())
        self.serial_flow.setCurrentIndex(max(0, index))

    def _refresh_totp_preview(self, *_args):
        """Show the code the stored secret produces right now, as proof it works."""
        text = self.totp_secret.text().strip()
        if not text:
            self.totp_preview.setText("—")
            self.totp_preview.setStyleSheet("font-size:12px;color:#94a3b8;")
            return
        try:
            code, seconds = totp.current_code(text)
        except totp.TOTPError as exc:
            self.totp_preview.setText(f"✗ {exc}")
            self.totp_preview.setStyleSheet("font-size:12px;color:#f87171;")
            return
        spaced = f"{code[:len(code) // 2]} {code[len(code) // 2:]}"
        self.totp_preview.setText(f"{spaced}   ·   {seconds}s left")
        self.totp_preview.setStyleSheet(
            "font-size:14px;font-weight:700;color:#4ade80;"
            "font-family:'MesloLGS Nerd Font Mono',monospace;")

    def _on_protocol_change(self, protocol):
        previous = self._last_protocol
        self._last_protocol = protocol
        default_port = self.PROTOCOL_PORTS.get(protocol, 22)
        current_port = self.port_input.value()
        # Move the port with the protocol only when it is still the old
        # protocol's default: a custom 2222 survives a switch SSH -> SFTP.
        if protocol != "Serial" and (
                previous in (None, "Serial") or current_port < 1
                or current_port == self.PROTOCOL_PORTS.get(previous, -1)):
            self.port_input.setMinimum(1)
            self.port_input.setSpecialValueText("")
            self.port_input.setValue(default_port)

        is_rdp = protocol == "RDP"
        is_nas = protocol == "NAS"
        is_azure = protocol == "Azure PowerShell"
        is_serial = protocol == "Serial"
        is_ssh_like = protocol in ("SSH", "SFTP")
        is_web_console = protocol in self.WEB_CONSOLES

        rows = self.PROTOCOL_ROWS.get(protocol, self.PROTOCOL_ROWS["SSH"])
        for key in ("host", "port", "username", "password", "key", "pubkey",
                    "install", "rdp_mode"):
            self._set_row_visible(key, key in rows)
        for key in self.SHELL_ONLY_ROWS:
            self._set_row_visible(key, protocol == "SSH")

        # Nothing to sign in with here: Azure signs in its own way, and a
        # serial console asks at its own prompt.
        self.auth_group.setVisible(bool(rows & {"username", "password", "rdp_mode"}))
        self.rdp_gateway_group.setVisible(is_rdp)
        # 2FA is offered where RPMote can actually type the code: the web
        # consoles it drives in an embedded browser.
        self.totp_group.setVisible(is_web_console)
        if is_web_console:
            self._refresh_totp_preview()
            self._totp_timer.start()
        else:
            self._totp_timer.stop()
        self.serial_group.setVisible(is_serial)
        self.ssh_section.setVisible(is_ssh_like)
        self.azure_auth_group.setVisible(is_azure)
        # Neither an Azure PowerShell record nor a console cable has a NIC of
        # its own to wake.
        self.wake_section.setVisible(not is_azure and not is_serial)

        host_caption = self._rows["host"][2]
        if is_nas:
            host_caption.setText("Host or ID")
            self.host_input.setPlaceholderText(
                "192.168.1.20, nas.example.com or a QuickConnect ID")
        elif protocol == "Web":
            host_caption.setText("URL or host")
            self.host_input.setPlaceholderText(
                "https://10.0.0.1:4444/ or firewall.example.com")
        else:
            host_caption.setText("Host")
            self.host_input.setPlaceholderText("192.168.1.100 or hostname.com")

        if not hasattr(self, "_default_user_placeholder"):
            self._default_user_placeholder = self.username_input.placeholderText()
        if protocol == "Telnet":
            self.username_input.setPlaceholderText("optional — typed at the login: prompt")
            self.password_input.setPlaceholderText("optional — typed at the Password: prompt")
        elif protocol == "VNC":
            self.password_input.setPlaceholderText(
                "VNC password (servers only use the first 8 characters)")
        else:
            self.username_input.setPlaceholderText(self._default_user_placeholder)
            self.password_input.setPlaceholderText("password")

        self.username_input.setEnabled(not is_azure)
        self.password_input.setEnabled(not is_azure)
        self.key_file_input.setEnabled(is_ssh_like)
        self.pub_key_input.setEnabled(is_ssh_like)

        self.azure_auth_mode.setEnabled(is_azure)
        self.azure_tenant_id.setEnabled(is_azure)
        self.azure_organization.setEnabled(is_azure)
        self.azure_use_delegated_org.setEnabled(is_azure)
        self.azure_sharepoint_url.setEnabled(is_azure)
        self.azure_pnp_client_id.setEnabled(is_azure)
        self.azure_upn.setEnabled(is_azure)
        self.azure_subscription_id.setEnabled(is_azure)
        self.azure_cloud.setEnabled(is_azure)
        for cb in self.azure_service_checks.values():
            cb.setEnabled(is_azure)
        self.azure_client_id.setEnabled(is_azure)
        self.azure_client_secret.setEnabled(is_azure and self.azure_auth_mode.currentText() == "Service Principal")

        self.mac_input.setEnabled(not is_azure)
        self.btn_wake_test.setEnabled(not is_azure)

        self.btn_test.setVisible(not is_azure)
        if is_azure:
            self.test_status.setText("Azure sign-in is checked when the session opens")
            self.test_status.setStyleSheet("color: #94a3b8; font-size: 12px;")
        else:
            self.test_status.setText("")
        # NAS and Web: a URL carries its own port, so the port box steps aside.
        if protocol in ("NAS", "Web"):
            try:
                self.host_input.textChanged.disconnect(self._on_host_change)
            except Exception:
                pass
            self.host_input.textChanged.connect(self._on_host_change)
            self._on_host_change(self.host_input.text())
        else:
            try:
                self.host_input.textChanged.disconnect(self._on_host_change)
            except Exception:
                pass
            self.port_input.setEnabled(True)

        if is_azure:
            self.port_input.setEnabled(False)
        elif protocol not in ("NAS", "Web"):
            self.port_input.setEnabled(True)

        self._on_azure_auth_mode_change(self.azure_auth_mode.currentText())
        if hasattr(self, "credential_combo"):
            self._apply_login_rows()

    def _on_azure_auth_mode_change(self, mode: str):
        if not hasattr(self, "auth_group"):
            return
        is_azure = self.protocol.currentText() == "Azure PowerShell"
        is_sp = mode == "Service Principal" and is_azure
        self.azure_client_id.setEnabled(is_azure)
        self.azure_client_secret.setEnabled(is_sp)
        for service, checkbox in self.azure_service_checks.items():
            supported = not is_sp or service in ("azure", "graph")
            checkbox.setEnabled(is_azure and supported)
            if is_sp and not supported:
                checkbox.setChecked(False)

    def _on_host_change(self, host_text: str):
        """NAS and Web: no port of its own when the host is given as a URL."""
        protocol = self.protocol.currentText()
        if protocol not in ("NAS", "Web"):
            return
        host = host_text.strip()
        if protocol == "Web":
            is_url = "://" in host
        else:
            is_url = "://" in host or ("." in host and not host.replace(".", "").replace(":", "").isdigit())
        if is_url:
            self.port_input.setEnabled(False)
            self.port_input.setSpecialValueText("—")
            self.port_input.setMinimum(0)
            self.port_input.setValue(0)
        else:
            self.port_input.setEnabled(True)
            self.port_input.setMinimum(1)
            self.port_input.setSpecialValueText("")
            if self.port_input.value() < 1:
                self.port_input.setValue(self.PROTOCOL_PORTS.get(protocol, 443))

    def _populate_jump_hosts(self):
        """List the saved SSH-capable connections this one could go through."""
        self.jump_combo.clear()
        self.jump_combo.addItem("— connect directly —", "")
        own_id = str((self.connection or {}).get("id", "") or "")
        connections = []
        if self.vault is not None:
            try:
                connections = self.vault.connections
            except Exception:
                connections = []
        for candidate in connections:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("protocol", "")).lower() not in ("ssh", "sftp"):
                continue
            candidate_id = str(candidate.get("id", "") or "")
            # A connection cannot be its own bastion.
            if not candidate_id or candidate_id == own_id:
                continue
            label = f"{candidate.get('name', '?')}  ({candidate.get('host', '?')})"
            self.jump_combo.addItem(label, candidate_id)

    def _populate(self, conn):
        self.name_input.setText(conn.get("name", ""))
        # Stored keys are lower case ("idrac"), labels are not ("iDRAC"):
        # go through the table rather than guessing at capitalisation.
        key = str(conn.get("protocol", "ssh") or "ssh").strip().lower()
        key = {"nas": "synology", "azure powershell": "azure_ps",
               "azure-powershell": "azure_ps"}.get(key, key)
        labels = {value: label for label, value in self.PROTOCOL_KEYS.items()}
        idx = self.protocol.findText(labels.get(key, "SSH"))
        if idx >= 0:
            self.protocol.setCurrentIndex(idx)
        self.host_input.setText(conn.get("host", ""))
        self._set_serial_settings(conn)
        port_val = int(conn.get("port", 0) or 0)
        if port_val > 0:
            self.port_input.setValue(port_val)
        else:
            # Port was saved as 0 (URL mode for NAS)
            self.port_input.setMinimum(0)
            self.port_input.setValue(0)
        self.mac_input.setText(conn.get("mac", "") or conn.get("mac_address", ""))
        self.username_input.setText(conn.get("username", ""))
        self.password_input.setText(conn.get("password", ""))
        self.key_file_input.setText(conn.get("key_file", ""))
        self.passphrase_input.setText(conn.get("key_passphrase", ""))
        self.use_agent.setChecked(bool(conn.get("use_agent", True)))
        self.auto_reconnect.setChecked(bool(conn.get("auto_reconnect", True)))
        environment = self.environment.findData(
            guardrails.normalize_environment(conn.get("environment")))
        self.environment.setCurrentIndex(max(0, environment))
        history = conn.get("scrollback_lines")
        try:
            history = DEFAULT_SCROLLBACK if history in (None, "") else int(history)
        except (TypeError, ValueError):
            history = DEFAULT_SCROLLBACK
        self.scrollback_input.setValue(max(0, min(1_000_000, history)))
        forwards = conn.get("port_forwards") or []
        if isinstance(forwards, str):
            forwards = [forwards]
        self.forwards_input.setText(", ".join(
            f if isinstance(f, str) else describe_forward_spec(f) for f in forwards))
        jump_index = self.jump_combo.findData(str(conn.get("jump_via", "") or ""))
        self.jump_combo.setCurrentIndex(max(0, jump_index))
        self.tenant_input.setText(conn.get("tenant", "Default"))
        self._tenant_color = conn.get("tenant_color", "#a78bfa")
        self._update_color_btn()
        self._fill_credentials(select=str(conn.get("credential_id", "") or ""))
        self._apply_login_rows()
        mode_index = self.rdp_open_mode.findData(rdp_open_mode(conn))
        self.rdp_open_mode.setCurrentIndex(max(0, mode_index))
        self.favorite_toggle.setChecked(bool(conn.get("favorite", False)))
        notes = str(conn.get("notes", "") or "")
        self.notes_input.setPlainText(notes)
        self.notes_section.setExpanded(bool(notes.strip()))
        gateway = normalize_rdp_gateway(conn)
        self.gateway_host.setText(gateway["host"])
        self.gateway_bypass_local.setChecked(gateway["bypass_local"])
        self.gateway_use_for_remote.setChecked(gateway["use_for_remote"])
        self.gateway_username.setText(gateway["username"])
        self.gateway_password.setText(gateway["password"])
        self._on_gateway_host_change(gateway["host"])
        self.totp_secret.setText(str(conn.get(totp.TOTP_SECRET_KEY, "") or ""))
        auth_mode = str(conn.get("azure_auth_mode", "interactive")).strip().lower()
        mode_label = {
            "interactive": "Interactive",
            "device_code": "Device Code",
            "service_principal": "Service Principal",
        }.get(auth_mode, "Interactive")
        self.azure_auth_mode.setCurrentText(mode_label)
        self.azure_tenant_id.setText(conn.get("azure_tenant_id", ""))
        self.azure_organization.setText(conn.get("azure_organization", ""))
        self.azure_use_delegated_org.setChecked(bool(conn.get("azure_use_delegated_org", False)))
        self.azure_sharepoint_url.setText(conn.get("azure_sharepoint_url", ""))
        self.azure_pnp_client_id.setText(conn.get("azure_pnp_client_id", ""))
        self.azure_upn.setText(conn.get("azure_upn", ""))
        self.azure_subscription_id.setText(conn.get("azure_subscription_id", ""))
        self.azure_client_id.setText(conn.get("azure_client_id", ""))
        self.azure_client_secret.setText(conn.get("azure_client_secret", ""))
        self.azure_cloud.setCurrentText(conn.get("azure_cloud", "AzureCloud"))
        saved_services = conn.get("azure_services")
        if isinstance(saved_services, str):
            saved_services = [s.strip() for s in saved_services.split(",") if s.strip()]
        if saved_services:
            for key, cb in self.azure_service_checks.items():
                cb.setChecked(key in saved_services)
        self._on_azure_auth_mode_change(self.azure_auth_mode.currentText())
        # Open the folded sections that have something in them, so nothing
        # saved is ever out of sight.
        self.wake_section.setExpanded(bool(self.mac_input.text().strip()))
        self.ssh_section.setExpanded(bool(
            self.passphrase_input.text()
            or self.forwards_input.text().strip()
            or self.jump_combo.currentIndex() > 0
            or not self.use_agent.isChecked()
            or not self.auto_reconnect.isChecked()
            or self.scrollback_input.value() != DEFAULT_SCROLLBACK))

    def _can_reveal_secret(self) -> bool:
        """Gate the eye toggles behind the master password.

        A secret the user just typed is theirs already and needs no challenge;
        one loaded from the vault does, since revealing it is what lets the
        credential leave this machine. Asked once per dialog.
        """
        if not self.connection:
            return True
        if self._reveal_authorised or self.vault is None:
            return True
        if confirm_master_password(
            self, self.vault, "You are about to view a saved password."
        ):
            self._reveal_authorised = True
            return True
        return False

    def _browse_key(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Private Key", "",
            "Key Files (*.pem *.key *.ppk *.pub);;All Files (*)"
        )
        if path:
            self.key_file_input.setText(path)

    def _browse_pub_key(self):
        start_dir = str(Path.home() / ".ssh") if (Path.home() / ".ssh").exists() else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Public Key", start_dir,
            "Public Keys (*.pub);;All Files (*)"
        )
        if path:
            self.pub_key_input.setText(path)

    def _open_generate_dialog(self):
        dlg = GenerateKeyDialog(self)
        dlg.exec()
        if dlg.generated_priv_path:
            self.key_file_input.setText(dlg.generated_priv_path)

    def _send_wake_test(self):
        """Fire a magic packet at whatever MAC is typed, right now.

        Deliberately does not require the dialog to be saved first: the point
        is to find out whether the MAC is right while the person is still
        looking at the box.
        """
        mac = normalize_mac(self.mac_input.text())
        if mac is None:
            self.wake_status.setText(
                "✗ Enter a MAC address first (AA:BB:CC:DD:EE:FF)")
            self.wake_status.setStyleSheet("color:#f87171;font-size:11px;")
            return

        self.wake_status.setText("Sending magic packet…")
        self.wake_status.setStyleSheet("color:#fbbf24;font-size:11px;")
        self.btn_wake_test.setEnabled(False)
        conn = {"mac": mac,
                "host": self.host_input.text().strip(),
                "name": self.name_input.text().strip()}
        threading.Thread(target=self._do_wake_test, args=(conn,),
                         daemon=True).start()

    def _do_wake_test(self, conn: dict):
        try:
            result = wake_connection(conn)
            if result.ok:
                self._set_status(self.wake_status, f"✓ {result.summary()}", "#4ade80",
                                 "Sent to:\n" + "\n".join(result.sent), size=11)
            else:
                self._set_status(self.wake_status, f"✗ {result.summary()}", "#f87171",
                                 "\n".join(result.errors), size=11)
        finally:
            self._post(lambda: self.btn_wake_test.setEnabled(True))

    def _test_connection(self):
        self._set_status(self.test_status, "Testing…", "#fbbf24", "")
        self.btn_test.setEnabled(False)
        params = self._test_params()
        threading.Thread(target=self._do_test, args=(params,), daemon=True).start()

    def _test_params(self) -> dict:
        """Everything a test needs, read on the GUI thread."""
        params = {
            "host": self.host_input.text().strip(),
            "port": self.port_input.value(),
            "protocol": self.protocol.currentText(),
            "username": self.username_input.text(),
            "password": self.password_input.text(),
            "key_file": self.key_file_input.text().strip(),
            "gateway": self._gateway_from_ui(),
        }
        params.update(self._serial_settings())
        cred = self._selected_credential() if hasattr(self, "credential_combo") else None
        if cred is not None:
            params["username"] = str(cred.get("username", "") or "")
            params["password"] = str(cred.get("password", "") or "")
            params["key_file"] = str(cred.get("key_file", "") or "")
        return params

    def _gateway_from_ui(self) -> dict:
        """The gateway exactly as it is typed right now, saved or not.

        Same principle as the Wake test: the point of pressing Test is to find
        out whether what is on screen is right, while the person is still
        looking at it.
        """
        return {
            RDP_GATEWAY_HOST_KEY: self.gateway_host.text(),
            RDP_GATEWAY_BYPASS_KEY: self.gateway_bypass_local.isChecked(),
            RDP_GATEWAY_SHARE_CREDS_KEY: self.gateway_use_for_remote.isChecked(),
            RDP_GATEWAY_USERNAME_KEY: self.gateway_username.text(),
            RDP_GATEWAY_PASSWORD_KEY: self.gateway_password.text(),
        }

    def _do_test(self, params: dict | None = None):
        if params is None:
            params = self._test_params()
        host = params["host"]
        port = params["port"]
        protocol = params["protocol"]

        try:
            if protocol == "Azure PowerShell":
                shell = shutil.which("pwsh") or shutil.which("powershell")
                if not shell:
                    raise RuntimeError("PowerShell not found (install pwsh or enable powershell)")
                self._set_status(self.test_status, "✓ PowerShell available", "#4ade80")
                return

            # A machine published through an RD Gateway is, by design, not
            # reachable from here — its name usually does not even resolve on
            # this side. Probing it directly is the wrong test, so the gateway
            # is tested instead and the desktop is reported on separately.
            if protocol == "RDP":
                gateway = normalize_rdp_gateway(params["gateway"])
                if gateway["host"]:
                    self._test_via_gateway(gateway, host, port)
                    return

            if protocol == "Serial":
                self._test_serial(params)
                return
            if protocol == "Web":
                # A pasted URL names its own host and port.
                from app.reachability import connection_host, connection_port
                probe = {"protocol": "web", "host": host, "port": port}
                host, port = connection_host(probe), connection_port(probe) or 443

            sock = socket.create_connection((host, port), timeout=5)
            if protocol == "VNC":
                # Answering on the port is not enough: plenty of things listen
                # on 5900. A VNC server speaks first, with its RFB version.
                sock.settimeout(5)
                try:
                    banner = sock.recv(12)
                finally:
                    sock.close()
                if not banner.startswith(b"RFB "):
                    raise RuntimeError("The port answered, but not with VNC (no RFB banner).")
                version = banner.decode("ascii", "replace").strip()
                self._set_status(self.test_status, f"✓ VNC server answered ({version})",
                                 "#4ade80", "")
                return
            sock.close()
            # For SSH/SFTP, try paramiko
            if protocol in ("SSH", "SFTP"):
                client = paramiko.SSHClient()
                kw = {"username": params["username"], "timeout": 10}
                pw = params["password"]
                kf = params["key_file"]
                if kf:
                    try:
                        kw["pkey"] = load_ssh_private_key(kf, pw)
                    except Exception:
                        if pw:
                            kw["password"] = pw
                        else:
                            raise
                elif pw:
                    kw["password"] = pw
                # Testing a connection is where a host is usually met for the
                # first time, so it is also where its key gets checked.
                host_keys.connect(client, host, port, **kw)
                client.close()

            self._set_status(self.test_status, "✓ Connection successful!", "#4ade80", "")
        except host_keys.HostKeyUnverified as challenge:
            self.host_key_challenge.emit(challenge, "test")
        except Exception as e:
            msg = describe_network_error(e, host, port)
            # Hover text shows the full message; status bar shows a hint.
            tooltip = f"{msg}\n\n{e!r}" if msg != str(e) else msg
            if isinstance(e, paramiko.AuthenticationException):
                hint = (
                    "Auth failed. Verify:\n"
                    "  • Username matches the one PuTTY uses (e.g. 'ubuntu' on Ubuntu cloud images)\n"
                    "  • Public key is in ~/.ssh/authorized_keys on the server for that user\n"
                    "  • Key file path is correct and not passphrase-protected"
                )
                tooltip = f"{msg}\n\n{hint}"
            self._set_status(self.test_status, f"✗ {msg[:80]}", "#f87171", tooltip)
        finally:
            self._post(lambda: self.btn_test.setEnabled(True))

    def _test_serial(self, params: dict):
        """Open the COM port and let go of it: proves it exists and is free."""
        from app.stream_transports import SerialChannel, describe_serial_error
        name = params.get("serial_port", "")
        if not name:
            self._set_status(self.test_status, "✗ Choose a COM port first", "#f87171", "")
            return
        try:
            SerialChannel(name, baud=params.get("baud_rate") or 9600,
                          data_bits=params.get("data_bits", 8),
                          parity=params.get("parity", "none"),
                          stop_bits=params.get("stop_bits", 1),
                          flow=params.get("flow_control", "none")).close()
        except Exception as exc:
            message = describe_serial_error(exc, name)
            self._set_status(self.test_status, f"✗ {message[:80]}", "#f87171", message)
            return
        self._set_status(self.test_status, f"✓ {name} opened — the port is free",
                         "#4ade80", "Whether anything answers on it shows once "
                         "the session opens.")

    def _test_via_gateway(self, gateway: dict, target_host: str, target_port: int):
        """Test the hop RPMote can actually make, and describe the other one.

        Two probes, and neither of them is "connect to the desktop":

        * the gateway itself, on 443 unless the address says otherwise. This
          one has to work — if it does not, nothing else will.
        * the desktop, directly, best effort. Failing is the normal answer for
          a machine that is only published through the gateway, so it is
          reported as information rather than as an error. Succeeding matters
          too: with "bypass for local addresses" ticked, that is the route the
          session will take, and it is worth knowing which one you are on.

        What neither probe does is authenticate. A gateway answering on 443 is
        as far as a socket gets; whether the account may pass through it is
        mstsc's answer to give.
        """
        gw_host, gw_port = split_gateway_address(gateway["host"])

        try:
            sock = socket.create_connection((gw_host, gw_port), timeout=5)
            sock.close()
        except Exception as exc:
            reason = describe_network_error(exc, gw_host, gw_port)
            self._set_status(
                self.test_status, f"✗ RD Gateway: {reason[:70]}", "#f87171",
                f"The RD Gateway {gw_host}:{gw_port} could not be reached.\n\n"
                f"{reason}\n\n"
                "This is the hop that has to work — the desktop behind it is "
                "not reachable from here at all until the gateway answers.")
            return

        direct_ok = False
        direct_error = ""
        if target_host:
            try:
                sock = socket.create_connection((target_host, target_port), timeout=3)
                sock.close()
                direct_ok = True
            except Exception as exc:
                direct_error = describe_network_error(exc, target_host, target_port)

        reached = f"✓ RD Gateway {gw_host}:{gw_port} reachable"
        if direct_ok and gateway["bypass_local"]:
            text = (f"{reached} — {target_host} also answers directly, so this "
                    f"session will bypass it")
            detail = ("Both routes are open. With \"Bypass RD Gateway server "
                      "for local addresses\" ticked, mstsc will take the "
                      "direct one and keep the gateway as the fallback.")
        elif direct_ok:
            text = f"{reached} — {target_host} also answers directly"
            detail = ("Both routes are open. Bypass is off, so every session "
                      "goes through the gateway anyway.")
        else:
            text = f"{reached} — {target_host} only answers through it"
            detail = (
                f"{target_host}:{target_port} is not reachable directly from "
                f"this PC, which is the normal answer for a machine published "
                f"through a gateway.\n\n{direct_error}")

        self._set_status(
            self.test_status, text, "#4ade80",
            f"{detail}\n\nA socket test cannot check credentials — whether "
            "this account may pass through the gateway is answered when the "
            "session opens.")

    def _install_public_key(self):
        host = self.host_input.text().strip()
        username = self.username_input.text().strip()
        password = self.password_input.text()
        if not host or not username or not password:
            self._set_status(self.install_status, "✗ Need host, username, and password", "#f87171")
            return
        self._set_status(self.install_status, "Installing…", "#fbbf24")
        self.btn_install_key.setEnabled(False)
        params = {
            "host": host,
            "port": self.port_input.value(),
            "username": username,
            "password": password,
            "pub_key": self.pub_key_input.text().strip(),
            "key_file": self.key_file_input.text().strip(),
        }
        threading.Thread(target=self._do_install_key, args=(params,), daemon=True).start()

    def _do_install_key(self, params: dict):
        try:
            host = params["host"]
            port = params["port"]
            username = params["username"]
            password = params["password"]

            ssh_dir = Path.home() / ".ssh"
            ssh_dir.mkdir(exist_ok=True)

            pub_path = None
            priv_path = None
            pub_key_text = None

            custom_pub = params["pub_key"]
            selected_priv = params["key_file"]
            if custom_pub:
                p = Path(custom_pub).expanduser()
                if not p.exists():
                    raise RuntimeError(f"Public key not found: {p}")
                pub_path = p
                guess_priv = p.with_suffix("") if p.suffix == ".pub" else None
                if guess_priv and guess_priv.exists():
                    priv_path = guess_priv
            elif selected_priv:
                # Derive the public key from whatever private key the user
                # picked in the Private Key field — crucial for PPK files
                # that have no sibling .pub on disk.
                priv_obj = load_ssh_private_key(selected_priv, password)
                pub_key_text = (
                    f"{priv_obj.get_name()} {priv_obj.get_base64()} "
                    f"rpmote-imported\n"
                )
                priv_path = Path(selected_priv).expanduser()
            else:
                for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
                    p = ssh_dir / f"{name}.pub"
                    if p.exists():
                        pub_path = p
                        priv_path = ssh_dir / name
                        break

                if pub_path is None:
                    priv_path = ssh_dir / "id_rsa"
                    pub_path = ssh_dir / "id_rsa.pub"
                    key = paramiko.RSAKey.generate(2048)
                    key.write_private_key_file(str(priv_path))
                    with open(pub_path, "w") as f:
                        f.write(f"{key.get_name()} {key.get_base64()} rpmote-generated\n")

            if pub_key_text is None:
                pub_key_text = pub_path.read_text().strip()
            else:
                pub_key_text = pub_key_text.strip()
            if not pub_key_text:
                raise RuntimeError("Public key file is empty")

            client = paramiko.SSHClient()
            host_keys.connect(client, host, port, username=username,
                              password=password, timeout=10)

            try:
                sftp = client.open_sftp()
                try:
                    try:
                        sftp.stat(".ssh")
                    except IOError:
                        sftp.mkdir(".ssh")
                    try:
                        sftp.chmod(".ssh", 0o700)
                    except Exception:
                        pass

                    existing = b""
                    try:
                        with sftp.open(".ssh/authorized_keys", "rb") as f:
                            existing = f.read()
                    except IOError:
                        pass

                    if pub_key_text.encode() in existing:
                        already = True
                    else:
                        already = False
                        new_content = existing
                        if new_content and not new_content.endswith(b"\n"):
                            new_content += b"\n"
                        new_content += pub_key_text.encode() + b"\n"
                        with sftp.open(".ssh/authorized_keys", "wb") as f:
                            f.write(new_content)
                        try:
                            sftp.chmod(".ssh/authorized_keys", 0o600)
                        except Exception:
                            pass
                finally:
                    sftp.close()
            finally:
                client.close()

            if priv_path and not selected_priv:
                found = str(priv_path)
                self._post(lambda: self.key_file_input.setText(found))

            msg = "✓ Key already installed" if already else "✓ Public key installed!"
            self._set_status(self.install_status, msg, "#4ade80")
        except host_keys.HostKeyUnverified as challenge:
            self.host_key_challenge.emit(challenge, "install")
        except Exception as e:
            self._set_status(self.install_status, f"✗ {str(e)[:60]}", "#f87171", str(e))
        finally:
            self._post(lambda: self.btn_install_key.setEnabled(True))

    def _on_host_key_challenge(self, challenge, retry: str):
        """Trust prompt for the Test / Install buttons, then run them again."""
        if self._host_key_prompt_open:
            return
        self._host_key_prompt_open = True
        status = self.test_status if retry == "test" else self.install_status
        try:
            from app.host_key_dialog import ask_about_host_key

            store = host_keys.store()
            if store.policy == host_keys.POLICY_STRICT:
                status.setText("✗ Host key not trusted (policy: strict)")
                status.setStyleSheet("color: #f87171; font-size: 12px;")
                return
            decision = ask_about_host_key(
                challenge, self.name_input.text().strip(), self)
            if not decision.connect:
                status.setText("✗ Host key not trusted")
                status.setToolTip(str(challenge))
                status.setStyleSheet("color: #f87171; font-size: 12px;")
                return
            if decision.remember:
                store.trust(challenge.host, challenge.port, challenge.key,
                            replace=decision.replace)
            else:
                status.setText("✗ Host key must be trusted to continue here")
                status.setStyleSheet("color: #fbbf24; font-size: 12px;")
                return
        finally:
            self._host_key_prompt_open = False

        if retry == "test":
            self._test_connection()
        else:
            self._install_public_key()

    def _save(self):
        name = self.name_input.text().strip()
        host = self.host_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation", "Please enter a connection name.")
            return
        protocol_label = self.protocol.currentText()
        serial = self._serial_settings() if protocol_label == "Serial" else {}
        if serial:
            if not serial["serial_port"]:
                QMessageBox.warning(self, "Validation",
                                    "Choose the COM port the console cable is on.")
                return
            if serial["baud_rate"] <= 0:
                QMessageBox.warning(self, "Validation",
                                    "The speed must be a number, such as 9600 or 115200.")
                return
            host = serial["serial_port"]
        elif not host and protocol_label != "Azure PowerShell":
            QMessageBox.warning(self, "Validation", "Please enter a host address.")
            return

        mac_text = self.mac_input.text().strip()
        mac = normalize_mac(mac_text)
        if mac_text and mac is None:
            QMessageBox.warning(
                self, "Validation",
                f"“{mac_text}” is not a MAC address.\n\n"
                "Use a form like AA:BB:CC:DD:EE:FF, AA-BB-CC-DD-EE-FF, or "
                "aabbccddeeff — or leave it blank if you do not need "
                "Wake-on-LAN.")
            return

        protocol_key = self.PROTOCOL_KEYS.get(protocol_label, protocol_label.lower())

        azure_auth_mode = {
            "Interactive": "interactive",
            "Device Code": "device_code",
            "Service Principal": "service_principal",
        }.get(self.azure_auth_mode.currentText(), "interactive")

        if protocol_key == "azure_ps":
            services = [key for key, cb in self.azure_service_checks.items() if cb.isChecked()]
            if not services:
                QMessageBox.warning(self, "Azure Authentication", "Select at least one Microsoft service.")
                return
            if azure_auth_mode == "service_principal" and not all((
                self.azure_tenant_id.text().strip(), self.azure_client_id.text().strip(),
                self.azure_client_secret.text(),
            )):
                QMessageBox.warning(self, "Azure Authentication", "Service Principal requires Tenant ID, Client ID, and Client Secret.")
                return
            if self.azure_use_delegated_org.isChecked() and not self.azure_organization.text().strip():
                QMessageBox.warning(self, "Azure Authentication", "Delegated customer access requires the customer Organization domain.")
                return
            if "spo" in services:
                from app.azure_shell import AzureShellConfig
                config = AzureShellConfig({
                    "azure_organization": self.azure_organization.text().strip(),
                    "azure_sharepoint_url": self.azure_sharepoint_url.text().strip(),
                    "azure_cloud": self.azure_cloud.currentText(),
                })
                try:
                    url = config.sharepoint_url()
                except ValueError as exc:
                    QMessageBox.warning(self, "SharePoint", str(exc))
                    return
                if not url or not self.azure_pnp_client_id.text().strip():
                    QMessageBox.warning(self, "SharePoint", "Provide the registered PnP Client ID and a SharePoint URL (or the original onmicrosoft domain in Organization).")
                    return

        try:
            forwards = parse_forward_list(self.forwards_input.text())
        except Exception as exc:
            QMessageBox.warning(self, "Port Forwards", str(exc))
            return

        # A secret that cannot make a code is a connection that stops at the
        # 2FA box with no way to tell why, so it is caught here instead.
        totp_secret = self.totp_secret.text().strip()
        if totp_secret and protocol_label in self.WEB_CONSOLES:
            try:
                totp.parse_secret(totp_secret)
            except totp.TOTPError as exc:
                QMessageBox.warning(self, "Two-Step Verification", str(exc))
                return

        self.result_data = {
            "name": name,
            "tenant": self._tenant_name(),
            "environment": self.environment.currentData() or "",
            "protocol": protocol_key,
            "host": host,
            "port": 0 if serial else self.port_input.value(),
            "mac": mac or "",
            "username": self.username_input.text(),
            "password": self.password_input.text(),
            "key_file": self.key_file_input.text().strip(),
            "key_passphrase": self.passphrase_input.text(),
            "use_agent": self.use_agent.isChecked(),
            "auto_reconnect": self.auto_reconnect.isChecked(),
            "scrollback_lines": self.scrollback_input.value(),
            "jump_via": self.jump_combo.currentData() or "",
            "port_forwards": forwards,
            "rdp_open_mode": self.rdp_open_mode.currentData() or "default",
            "rdp_embedded": (self.rdp_open_mode.currentData() == "embedded"),
            "favorite": self.favorite_toggle.isChecked(),
            "notes": self.notes_input.toPlainText().strip(),
            RDP_GATEWAY_HOST_KEY: normalize_gateway_host(self.gateway_host.text()),
            RDP_GATEWAY_BYPASS_KEY: self.gateway_bypass_local.isChecked(),
            RDP_GATEWAY_SHARE_CREDS_KEY: self.gateway_use_for_remote.isChecked(),
            RDP_GATEWAY_USERNAME_KEY: self.gateway_username.text().strip(),
            RDP_GATEWAY_PASSWORD_KEY: self.gateway_password.text(),
            totp.TOTP_SECRET_KEY: totp_secret,
            "tenant_color": self._tenant_color,
            "azure_auth_mode": azure_auth_mode,
            "azure_tenant_id": self.azure_tenant_id.text().strip(),
            "azure_organization": self.azure_organization.text().strip(),
            "azure_use_delegated_org": self.azure_use_delegated_org.isChecked(),
            "azure_sharepoint_url": self.azure_sharepoint_url.text().strip(),
            "azure_pnp_client_id": self.azure_pnp_client_id.text().strip(),
            "azure_upn": self.azure_upn.text().strip(),
            "azure_subscription_id": self.azure_subscription_id.text().strip(),
            "azure_client_id": self.azure_client_id.text().strip(),
            "azure_client_secret": self.azure_client_secret.text(),
            "azure_cloud": self.azure_cloud.currentText().strip() or "AzureCloud",
            "azure_services": [
                key for key, cb in self.azure_service_checks.items() if cb.isChecked()
            ] or ["azure"],
        }
        self.result_data.update(serial)
        cred = self._selected_credential()
        self.result_data["credential_id"] = str(cred.get("id", "")) if cred else ""
        if cred is not None:
            # The shared credential is the one copy of this secret.
            self.result_data["password"] = ""
            self.result_data["key_passphrase"] = ""
        if self.connection and "id" in self.connection:
            self.result_data["id"] = self.connection["id"]

        self.accept()

    def get_result(self) -> dict:
        return getattr(self, "result_data", {})

    def _tenant_name(self) -> str:
        name = self.tenant_input.text().strip() or "Default"
        return self._tenant_names.get(name.casefold(), name)

    def _sync_tenant_color(self, _text=None):
        key = self._tenant_name().casefold()
        self._tenant_color = self._tenant_color_choices.get(
            key, self._tenant_colors.get(key, "#a78bfa"))
        self._update_color_btn()

    def _pick_tenant_color(self):
        """Open a color dialog for the tenant text color."""
        color = QColorDialog.getColor(QColor(self._tenant_color), self, "Tenant Color")
        if color.isValid():
            self._tenant_color = color.name()
            self._tenant_color_choices[self._tenant_name().casefold()] = color.name()
            self._update_color_btn()

    def _update_color_btn(self):
        """Show the tenant's colour as a swatch — the button *is* the colour."""
        color = self._tenant_color if QColor(self._tenant_color).isValid() else "#a78bfa"
        self.tenant_color_btn.setStyleSheet(
            f"QPushButton {{ background-color: {color}; padding: 0;"
            f" border: 2px solid rgba(255, 255, 255, 0.28); border-radius: 8px; }}"
            f"QPushButton:hover {{ border-color: rgba(255, 255, 255, 0.85); }}"
        )
        self.tenant_color_btn.setToolTip(
            f"Tenant colour {color} — used for this tenant everywhere. Click to change.")
