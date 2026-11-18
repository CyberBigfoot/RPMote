"""
Vault backups — rolling copies of the encrypted vault files.

The vault is one file; an atomic write protects it from a power cut, but not
from a mistaken "Delete Tenant", a bad import or a disk that eats the file.
A backup is simply a copy of the encrypted files (vault.enc, vault.key,
vault.salt, vault.hash and the recovery wrap) in a dated folder next to the
vault, so it is exactly as safe as the vault itself and opens with the
master password that was in use when it was taken.

Taken automatically once a day on unlock and before anything destructive
(deleting a tenant, deleting several connections, importing, restoring).
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import vault as vault_module

#: How many snapshots to keep; the oldest go first.
KEEP = 20
_REASON_FILE = "reason.txt"


@dataclass
class BackupInfo:
    path: Path
    created: datetime
    reason: str
    size: int

    @property
    def label(self) -> str:
        return self.created.strftime("%Y-%m-%d %H:%M:%S")


def backup_dir() -> Path:
    return Path(vault_module.VAULT_DIR) / "backups"


def _vault_files() -> list[Path]:
    return [vault_module.VAULT_FILE, vault_module.SALT_FILE, vault_module.HASH_FILE,
            vault_module.KEY_FILE, vault_module.RECOVERY_FILE]


def create_backup(reason: str = "") -> Path | None:
    """Copy the vault files into a new dated folder. None if there is no vault."""
    if not Path(vault_module.VAULT_FILE).exists():
        return None
    root = backup_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = root / stamp
    n = 2
    while target.exists():
        target = root / f"{stamp}-{n}"
        n += 1
    target.mkdir()
    for f in _vault_files():
        f = Path(f)
        if f.exists():
            shutil.copy2(f, target / f.name)
    try:
        (target / _REASON_FILE).write_text(str(reason or ""), encoding="utf-8")
    except OSError:
        pass
    prune()
    return target


def list_backups() -> list[BackupInfo]:
    root = backup_dir()
    if not root.exists():
        return []
    result = []
    for folder in root.iterdir():
        if not folder.is_dir() or not (folder / Path(vault_module.VAULT_FILE).name).exists():
            continue
        try:
            created = datetime.strptime(folder.name[:15], "%Y%m%d-%H%M%S")
        except ValueError:
            created = datetime.fromtimestamp(folder.stat().st_mtime)
        try:
            reason = (folder / _REASON_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            reason = ""
        size = sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
        result.append(BackupInfo(folder, created, reason, size))
    result.sort(key=lambda b: (b.created, b.path.name), reverse=True)
    return result


def prune(keep: int = KEEP):
    for info in list_backups()[keep:]:
        shutil.rmtree(info.path, ignore_errors=True)


def daily_backup_if_due() -> Path | None:
    """One snapshot per calendar day, taken the first time the vault opens."""
    today = datetime.now().date()
    if any(b.created.date() == today for b in list_backups()):
        return None
    return create_backup("daily")


def restore_backup(path: Path) -> None:
    """Put a snapshot back in place of the current vault.

    The current vault is snapshotted first, so a restore can itself be
    undone. The caller must lock and re-open the vault afterwards: the
    restored files answer to the password that was current back then.
    """
    path = Path(path)
    if not (path / Path(vault_module.VAULT_FILE).name).exists():
        raise FileNotFoundError(f"{path} does not contain a vault")
    create_backup("before restore")
    for f in _vault_files():
        f = Path(f)
        source = path / f.name
        if source.exists():
            tmp = f.with_suffix(f.suffix + ".restore")
            shutil.copy2(source, tmp)
            tmp.replace(f)
        elif f.exists():
            # A file the snapshot did not have (a recovery key made later)
            # belongs to the other password; leaving it would break unlock.
            f.unlink()
    # Touch so the restored files sort as "now" in Explorer.
    now = time.time()
    for f in _vault_files():
        if Path(f).exists():
            try:
                import os
                os.utime(f, (now, now))
            except OSError:
                pass


def format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
