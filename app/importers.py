"""
Bring connections in from other tools.

* **.rdp files** — what mstsc saves; drag several in at once.
* **CSV** — any spreadsheet or tool export (Devolutions RDM, Royal TS, a
  customer's asset list…). Columns are matched by name: Host/Address/URI,
  Name, Group/Folder/Customer, Username, Password, Port, Protocol/Type…
* **mRemoteNG** — confCons.xml, including its encrypted passwords (the
  default master password, or one you type).
* **PuTTY** — the saved sessions in the registry.

Everything goes through one preview where you pick what to import and which
tenant it lands in; nothing touches the vault until you press Import.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout,
)

from app import icons
from app.widgets import fit_to_screen, set_variant

DEFAULT_PORTS = {"ssh": 22, "sftp": 22, "ftp": 21, "rdp": 3389, "vnc": 5900,
                 "telnet": 23, "web": 443, "idrac": 443, "synology": 5001}


class ImportError_(Exception):
    """A file that cannot be read as what it claims to be."""


# ── protocol names from other tools ─────────────────────────────────────

_PROTOCOL_WORDS = (
    ("rdp", ("rdp", "remote desktop", "rdpconfiguredconnection", "terminal server", "mstsc")),
    ("sftp", ("sftp", "scp", "winscp")),
    ("ssh", ("ssh", "ssh1", "ssh2", "putty", "shell")),
    ("ftp", ("ftp", "ftps")),
    ("vnc", ("vnc", "rfb", "tightvnc", "realvnc", "ultravnc")),
    ("telnet", ("telnet", "rlogin", "raw")),
    ("serial", ("serial", "com")),
    ("idrac", ("idrac", "ilo", "ipmi", "drac")),
    ("synology", ("synology", "nas", "dsm")),
    ("azure_ps", ("azure", "powershell")),
    ("web", ("http", "https", "web", "website", "url", "webbrowser")),
)


def protocol_from(text: str, port: int | None = None) -> str:
    word = str(text or "").strip().lower()
    if word:
        for key, names in _PROTOCOL_WORDS:
            if word in names or any(word.startswith(n) for n in names if len(n) > 3):
                return key
    by_port = {22: "ssh", 3389: "rdp", 21: "ftp", 23: "telnet", 5900: "vnc",
               443: "web", 80: "web", 5001: "synology"}
    return by_port.get(port or 0, "ssh")


def _int(value, default=None):
    try:
        number = int(str(value).strip())
        return number if 0 < number < 65536 else default
    except (TypeError, ValueError):
        return default


def _split_host(host: str):
    """"host:3390" -> ("host", 3390); a URL keeps its scheme for Web."""
    host = str(host or "").strip()
    if "://" in host:
        parsed = urlparse(host)
        return host, parsed.port
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        if _int(port):
            return name.strip(), _int(port)
    return host, None


def _record(name, host, protocol, port=None, username="", password="", tenant="",
            notes="", mac="", domain="", **extra) -> dict:
    host = str(host or "").strip()
    protocol = protocol or "ssh"
    user = str(username or "").strip()
    if domain and user and "\\" not in user and "@" not in user:
        user = f"{domain}\\{user}"
    record = {
        "name": str(name or host or "Imported").strip(),
        "tenant": str(tenant or "").strip(),
        "protocol": protocol,
        "host": host,
        "port": port or DEFAULT_PORTS.get(protocol, 22),
        "username": user,
        "password": str(password or ""),
        "notes": str(notes or "").strip(),
        "mac": str(mac or "").strip(),
    }
    record.update({k: v for k, v in extra.items() if v not in (None, "")})
    return record


# ── .rdp files ──────────────────────────────────────────────────────────

def parse_rdp_file(path) -> dict:
    """One mstsc .rdp file. Passwords are never in them (DPAPI blobs only)."""
    path = Path(path)
    raw = path.read_bytes()
    for encoding in ("utf-16", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            if "address" in text:
                break
        except UnicodeDecodeError:
            continue
    values = {}
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            values[parts[0].strip().lower()] = parts[2].strip()
    address = values.get("full address", "")
    if not address:
        raise ImportError_(f"{path.name} has no 'full address'")
    host, port = _split_host(address)
    extra = {}
    gateway = values.get("gatewayhostname", "")
    if gateway and values.get("gatewayusagemethod", "0") not in ("0", "4"):
        from app.rdp_gateway import RDP_GATEWAY_BYPASS_KEY, RDP_GATEWAY_HOST_KEY
        extra[RDP_GATEWAY_HOST_KEY] = gateway
        extra[RDP_GATEWAY_BYPASS_KEY] = values.get("gatewayusagemethod") == "2"
    return _record(path.stem, host, "rdp", port or 3389,
                   username=values.get("username", ""), domain=values.get("domain", ""),
                   **extra)


# ── CSV ─────────────────────────────────────────────────────────────────

_COLUMNS = {
    "name": ("name", "connection", "connection name", "title", "display name", "entry",
             "entry name", "label", "computer name"),
    "host": ("host", "hostname", "host name", "address", "ip", "ip address", "computername",
             "computer", "server", "url", "uri", "fqdn", "target"),
    "port": ("port",),
    "protocol": ("protocol", "type", "connection type", "kind", "connectiontype"),
    "username": ("username", "user", "user name", "login", "credentialusername", "account"),
    "password": ("password", "pass", "pwd", "secret", "credentialpassword"),
    "domain": ("domain", "credentialdomain"),
    "tenant": ("tenant", "group", "folder", "customer", "client", "site", "company",
               "organization", "organisation", "path", "parent"),
    "notes": ("notes", "description", "comment", "comments", "descr"),
    "mac": ("mac", "mac address", "macaddress"),
}


def _match_columns(header: list[str]) -> dict:
    mapping = {}
    cleaned = [re.sub(r"[^a-z0-9 ]", "", str(h or "").strip().lower()) for h in header]
    for field, names in _COLUMNS.items():
        for index, col in enumerate(cleaned):
            if col in names or col.replace(" ", "") in [n.replace(" ", "") for n in names]:
                mapping.setdefault(field, index)
                break
    return mapping


def parse_csv(path) -> list[dict]:
    path = Path(path)
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(text.splitlines(), dialect))
    if not rows:
        return []
    mapping = _match_columns(rows[0])
    if "host" not in mapping:
        raise ImportError_("No column looks like a host or address. Name one of them "
                           "“Host”, “Address”, “IP” or “URI”.")
    records = []
    for row in rows[1:]:
        def get(field):
            index = mapping.get(field)
            return row[index].strip() if index is not None and index < len(row) else ""
        raw_host = get("host")
        if not raw_host:
            continue
        host, port = _split_host(raw_host)
        port = _int(get("port"), port)
        protocol = protocol_from(get("protocol"), port)
        if "://" in host:
            protocol = "web" if protocol not in ("idrac", "synology") else protocol
        tenant = get("tenant").replace("\\", "/").strip("/").split("/")[0] if get("tenant") else ""
        records.append(_record(get("name") or host, host, protocol, port,
                               username=get("username"), password=get("password"),
                               domain=get("domain"), tenant=tenant, notes=get("notes"),
                               mac=get("mac")))
    return records


# ── mRemoteNG ───────────────────────────────────────────────────────────

MREMOTE_DEFAULT_PASSWORD = "mR3m"


def _mremote_decrypt(value: str, password: str, gcm: bool, iterations: int) -> str:
    if not value:
        return ""
    data = base64.b64decode(value)
    if gcm:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt, nonce, body = data[:16], data[16:32], data[32:]
        key = PBKDF2HMAC(algorithm=hashes.SHA1(), length=32, salt=salt,
                         iterations=iterations).derive(password.encode("utf-8"))
        return AESGCM(key).decrypt(nonce, body, salt).decode("utf-8")
    # Pre-1.76 files: AES-CBC, key = MD5(password), IV in front.
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = hashlib.md5(password.encode("utf-8")).digest()
    iv, body = data[:16], data[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(body) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def mremoteng_needs_password(path) -> bool:
    """True when the file is protected by a master password other than the default."""
    root = ET.parse(path).getroot()
    protected = root.get("Protected", "")
    if not protected:
        return False
    gcm = root.get("BlockCipherMode", "").upper() == "GCM"
    try:
        _mremote_decrypt(protected, MREMOTE_DEFAULT_PASSWORD, gcm,
                         int(root.get("KdfIterations", "1000") or 1000))
        return False
    except Exception:
        return True


def parse_mremoteng(path, password: str = MREMOTE_DEFAULT_PASSWORD) -> list[dict]:
    root = ET.parse(path).getroot()
    gcm = root.get("BlockCipherMode", "").upper() == "GCM"
    iterations = int(root.get("KdfIterations", "1000") or 1000)
    if root.get("FullFileEncryption", "false").lower() == "true":
        inner = _mremote_decrypt((root.text or "").strip(), password, gcm, iterations)
        root = ET.fromstring(f"<Connections>{inner}</Connections>")

    records = []
    failed = {"count": 0}

    def secret(node):
        try:
            return _mremote_decrypt(node.get("Password", ""), password, gcm, iterations)
        except Exception:
            if node.get("Password"):
                failed["count"] += 1
            return ""

    def walk(node, path_names):
        for child in node:
            if child.tag != "Node":
                continue
            kind = child.get("Type", "")
            name = child.get("Name", "")
            if kind == "Container":
                walk(child, path_names + [name])
                continue
            if kind != "Connection":
                continue
            proto = protocol_from(child.get("Protocol", ""), _int(child.get("Port")))
            host = child.get("Hostname", "")
            if not host:
                continue
            port = _int(child.get("Port"))
            if proto == "web" and not host.startswith("http"):
                scheme = "http" if child.get("Protocol", "").upper() == "HTTP" else "https"
                host = f"{scheme}://{host}"
            records.append(_record(
                name, host, proto, port, username=child.get("Username", ""),
                password=secret(child), domain=child.get("Domain", ""),
                tenant=path_names[0] if path_names else "",
                notes=child.get("Descr", "")))

    walk(root, [])
    if failed["count"]:
        for record in records:
            record.setdefault("_warning", f"{failed['count']} password(s) could not be decrypted")
    return records


# ── PuTTY ───────────────────────────────────────────────────────────────

def putty_sessions() -> list[dict]:
    try:
        import winreg
    except ImportError:
        return []
    records = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\SimonTatham\PuTTY\Sessions")
    except OSError:
        return []
    with root:
        index = 0
        while True:
            try:
                key_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, key_name) as key:
                    def value(name, default=""):
                        try:
                            return winreg.QueryValueEx(key, name)[0]
                        except OSError:
                            return default
                    proto = str(value("Protocol", "ssh") or "ssh").lower()
                    host = str(value("HostName", "") or "")
                    if proto == "serial":
                        line = str(value("SerialLine", "") or "")
                        if not line:
                            continue
                        records.append(_record(unquote(key_name), line, "serial", None,
                                               baud_rate=int(value("SerialSpeed", 9600) or 9600)))
                        continue
                    if not host:
                        continue
                    user = str(value("UserName", "") or "")
                    if "@" in host and not user:
                        user, host = host.split("@", 1)
                    records.append(_record(
                        unquote(key_name), host, "telnet" if proto in ("telnet", "raw", "rlogin") else "ssh",
                        _int(value("PortNumber", 22), 22), username=user,
                        key_file=str(value("PublicKeyFile", "") or "")))
            except OSError:
                continue
    return [r for r in records if r["name"].lower() != "default settings"]


# ── preview ─────────────────────────────────────────────────────────────

def identity(conn: dict) -> tuple:
    return (str(conn.get("protocol", "")).lower(), str(conn.get("host", "")).lower(),
            str(conn.get("port", "")))


class ImportPreviewDialog(QDialog):
    """Choose what to import, and which tenant it goes to."""

    def __init__(self, records: list[dict], existing: list[dict], source: str,
                 tenants: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Import — {source}")
        self.setWindowIcon(icons.icon("import"))
        self.setMinimumSize(760, 420)
        self._records = records
        known = {identity(c) for c in existing if isinstance(c, dict)}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        warning = next((r.get("_warning") for r in records if r.get("_warning")), "")
        intro = QLabel(f"{len(records)} connection{'s' if len(records) != 1 else ''} found in "
                       f"{source}. Untick anything you do not want; machines already saved "
                       "are unticked for you." + (f"  Warning: {warning}." if warning else ""))
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.use_tenant = QCheckBox("Put everything in tenant")
        self.tenant = QComboBox()
        self.tenant.setEditable(True)
        self.tenant.addItems(tenants)
        self.tenant.setCurrentText("Imported")
        self.tenant.setMinimumWidth(200)
        self.use_tenant.setChecked(not any(r.get("tenant") for r in records))
        row.addWidget(self.use_tenant)
        row.addWidget(self.tenant)
        row.addStretch()
        self.select_all = QCheckBox("Select all")
        self.select_all.setChecked(False)
        self.select_all.toggled.connect(self._toggle_all)
        row.addWidget(self.select_all)
        layout.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Tenant", "Protocol", "Host", "User", "Password", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for record in records:
            duplicate = identity(record) in known
            item = QTreeWidgetItem([
                record["name"], record.get("tenant") or "—", record["protocol"].upper(),
                f"{record['host']}:{record['port']}" if record.get("port") and "://" not in record["host"] else record["host"],
                record.get("username", "") or "—",
                "yes" if record.get("password") else "—",
                "already saved" if duplicate else "new"])
            item.setIcon(0, icons.protocol_icon(record["protocol"]))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked if duplicate else Qt.CheckState.Checked)
            self.tree.addTopLevelItem(item)
        layout.addWidget(self.tree, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.btn_import = self.buttons.addButton("Import", QDialogButtonBox.ButtonRole.AcceptRole)
        set_variant(self.btn_import, "primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.tree.itemChanged.connect(lambda *_: self._sync())
        self._sync()
        self.resize(940, 560)
        fit_to_screen(self)

    def _toggle_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, state)

    def _sync(self):
        count = sum(1 for i in range(self.tree.topLevelItemCount())
                    if self.tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked)
        self.btn_import.setText(f"Import {count}")
        self.btn_import.setEnabled(count > 0)

    def chosen(self) -> list[dict]:
        tenant = self.tenant.currentText().strip() or "Imported"
        result = []
        for i in range(self.tree.topLevelItemCount()):
            if self.tree.topLevelItem(i).checkState(0) != Qt.CheckState.Checked:
                continue
            record = {k: v for k, v in self._records[i].items() if not k.startswith("_")}
            if self.use_tenant.isChecked() or not record.get("tenant"):
                record["tenant"] = tenant
            result.append(record)
        return result
