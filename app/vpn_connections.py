"""VPN client adapters. Structured fields never pass through a command shell.

Login secrets stay in the encrypted vault and are passed to Windows RAS in
memory. PowerShell receives the L2TP key over stdin, not its command line.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time

#: What each connect and disconnect did, in the app log. Never a secret.
_LOG = logging.getLogger("rpmote.vpn")


KINDS = (
    ("custom", "Custom command"),
    ("windows", "Existing Windows VPN profile"),
    ("openvpn", "OpenVPN — upload .ovpn config"),
    ("wireguard", "WireGuard — upload .conf config"),
    ("pptp", "PPTP — enter connection details"),
    ("l2tp", "L2TP/IPsec — enter connection details"),
)
MANUAL_KINDS = {"pptp", "l2tp"}
FILE_KINDS = {"openvpn", "wireguard"}
#: connect() returns only once these tunnels are up; the others are handed off.
CONFIRMED_KINDS = {"windows", "pptp", "l2tp", "wireguard"}
MAX_CONFIG_BYTES = 4 * 1024 * 1024
#: RAS errors that mean the profile needs a sign-in: no saved login, or an EAP prompt.
NEEDS_SIGN_IN = {691, 703}


class VpnError(OSError):
    pass


def validate(config: dict, *, check_file: bool = True) -> None:
    kind = config.get("kind", "custom")
    if kind == "custom":
        if not config.get("command"):
            raise VpnError("Enter the command that connects the VPN.")
    elif kind == "windows":
        if not config.get("profile_name"):
            raise VpnError("Enter the name of an existing Windows VPN profile.")
    elif kind in FILE_KINDS:
        path = Path(config.get("config_file", ""))
        extension = ".ovpn" if kind == "openvpn" else ".conf"
        if not config.get("config_file") or path.suffix.lower() != extension:
            raise VpnError(f"Upload a {extension} tunnel configuration file.")
        if kind == "wireguard" and not re.fullmatch(r"[a-zA-Z0-9_=+.-]{1,32}", path.stem):
            raise VpnError("WireGuard config names must use 1–32 letters, digits, or _=+.- characters.")
        if check_file:
            try:
                with path.open("rb") as source:
                    data = source.read(MAX_CONFIG_BYTES + 1)
            except OSError:
                raise VpnError("The config file cannot be read. Upload it again from its current location.") from None
            if not data.strip() or b"\0" in data or len(data) > MAX_CONFIG_BYTES:
                raise VpnError("Choose a non-empty text configuration file smaller than 4 MB.")
    elif kind in MANUAL_KINDS:
        for key, label in (("profile_name", "Connection name"), ("server", "Server address"),
                           ("username", "Username"), ("password", "Password")):
            if not config.get(key):
                raise VpnError(f"Enter the {label.lower()}.")
        if kind == "l2tp" and not config.get("psk"):
            raise VpnError("Enter the L2TP/IPsec pre-shared key.")
        if (config.get("authentication") in {"Pap", "Chap"}
                and config.get("encryption", "Required") in {"Required", "Maximum"}):
            # Windows refuses to create such a profile.
            raise VpnError("PAP and CHAP cannot encrypt the connection. Choose MSChapv2, "
                           "or set data encryption to optional.")
        for key, limit in (("username", 256), ("password", 256), ("domain", 15)):
            value = config.get(key, "")
            if "\0" in value or len(value.encode("utf-16-le")) // 2 > limit:
                raise VpnError(f"The {key} is too long or contains an invalid character.")
        if not re.fullmatch(r"[a-f0-9]{32}", config.get("profile_id", "")):
            raise VpnError("This Windows VPN configuration has no profile ID. Save it again.")
    else:
        raise VpnError("Choose a supported VPN type.")


def configured(config: dict) -> bool:
    """Include saved file profiles even if a removable drive is offline."""
    try:
        validate(config, check_file=False)
        return True
    except VpnError:
        return False


def windows_profile_name(config: dict) -> str:
    # Own only this profile; never overwrite a similarly named user profile.
    label = re.sub(r"[^a-zA-Z0-9_. -]", "_", config.get("profile_name", "VPN"))[:160]
    return f"RPMote-{label}-{config['profile_id']}"


def _client(executable: str, relative: str) -> str:
    found = shutil.which(executable)
    if found:
        return found
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root and (path := Path(root) / relative).is_file():
            return str(path)
    raise VpnError(f"Install {executable.removesuffix('.exe')} before connecting this VPN.")


def _popen(args):
    return subprocess.Popen(args, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run(args, *, input=None, timeout=40, capture=False):
    # A client that stops to prompt on stdin fails at once instead of at the timeout.
    stdin = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(args, **stdin, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, shell=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise VpnError("The VPN client did not respond in time. Check its window and try again.") from None


def _checked(args, *, input=None, timeout=40):
    if (code := _run(args, input=input, timeout=timeout).returncode):
        raise VpnError(f"The VPN client returned error {code}. Check its settings and permissions.")


#: What to change, for RAS errors whose Windows wording does not say.
_RAS_HINTS = {
    812: "Choose the authentication method the server expects.",
    741: "Change the data encryption setting to one the server supports.",
    742: "Change the data encryption setting to one the server supports.",
}
#: The same for the tunnel types these mean one thing for. A server that never
#: answers gives 807 (PPTP, after about 20 s) or 789 (L2TP, after about a minute).
_KIND_HINTS = {
    "pptp": {
        807: "If it happens every time, check that the server answers PPTP: TCP port 1723 and GRE.",
    },
    "l2tp": {
        789: ("Check the IPsec pre-shared key, and that the server answers IPsec "
              "(UDP ports 500 and 4500)."),
        809: ("If the server sits behind a NAT router, Windows only connects L2TP/IPsec once the "
              "registry value HKLM\\SYSTEM\\CurrentControlSet\\Services\\PolicyAgent\\"
              "AssumeUDPEncapsulationContextOnSendRule is 2, followed by a restart. Otherwise "
              "check that UDP ports 500 and 4500 reach the server."),
    },
}


def _ras_error(code, kind: str = "") -> VpnError:
    """Windows' own explanation of a RAS error number (691, 800, …)."""
    try:
        import win32ras
        text = " ".join(win32ras.GetErrorString(int(code)).split())
    except Exception:
        text = ""
    try:
        hint = _KIND_HINTS.get(kind, {}).get(int(code)) or _RAS_HINTS.get(int(code), "")
    except (TypeError, ValueError):
        hint = ""
    if not text:
        return VpnError(f"Windows could not connect the VPN (error {code}). "
                        + (hint or "Check the server and login details."))
    return VpnError(f"Windows could not connect the VPN (error {code}): {text}" + (f" {hint}" if hint else ""))


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _run_elevated(args, *, timeout=120) -> int:
    """Run *args* through the UAC prompt and return its exit code.

    subprocess cannot elevate: Windows only shows the prompt for ShellExecute.
    """
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long

    # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI; SW_HIDE.
    info = ShellExecuteInfo(cbSize=ctypes.sizeof(ShellExecuteInfo), fMask=0x40 | 0x100 | 0x400,
                            lpVerb="runas", lpFile=args[0],
                            lpParameters=subprocess.list2cmdline(args[1:]), nShow=0)
    # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE, as ShellExecuteEx asks of its callers.
    com = ole32.CoInitializeEx(None, 0x2 | 0x4) in (0, 1)
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            if ctypes.get_last_error() == 1223:  # ERROR_CANCELLED
                raise VpnError("Administrator approval was declined, so the tunnel was not changed.")
            raise VpnError("Windows could not start the VPN client with administrator rights.")
    finally:
        if com:
            ole32.CoUninitialize()
    try:
        if kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != 0:
            raise VpnError("The VPN client did not respond in time. Check its window and try again.")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def _admin_checked(args, *, timeout=120):
    """Run a client that changes Windows services, asking for approval if needed."""
    if _is_admin():
        _checked(args, timeout=timeout)
    elif (code := _run_elevated(args, timeout=timeout)):
        raise VpnError(f"The VPN client returned error {code}. Check its settings and permissions.")


_PROFILE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $data = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $parameters = @{
        Name = $data.name; ServerAddress = $data.server; TunnelType = $data.tunnel
        AuthenticationMethod = $data.authentication; EncryptionLevel = $data.encryption
        SplitTunneling = [bool]$data.split; RememberCredential = $false
        UseWinlogonCredential = $false; Force = $true
    }
    if ($data.tunnel -eq 'L2tp') { $parameters.L2tpPsk = $data.psk }
    $existing = Get-VpnConnection -Name $data.name -ErrorAction SilentlyContinue
    if ($existing -and $existing.ConnectionStatus -eq 'Connected') {
        if ($existing.ServerAddress -eq $data.server -and
            $existing.TunnelType -eq $data.tunnel -and
            $existing.SplitTunneling -eq [bool]$data.split -and
            $existing.EncryptionLevel -eq $data.encryption -and
            $existing.AuthenticationMethod -contains $data.authentication) { exit 0 }
        exit 2
    }
    if ($existing) {
        Set-VpnConnection @parameters | Out-Null
    } else {
        Add-VpnConnection @parameters | Out-Null
    }
    exit 0
} catch {
    # Base64 keeps Windows' (possibly localized) wording intact across code pages.
    [Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($_.Exception.Message)))
    exit 1
}
"""


def _powershell() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
               "System32/WindowsPowerShell/v1.0/powershell.exe")


def _prepare_windows_profile(config: dict):
    script = base64.b64encode(_PROFILE_SCRIPT.encode("utf-16-le")).decode("ascii")
    payload = {"name": windows_profile_name(config), "server": config["server"],
               "tunnel": "Pptp" if config["kind"] == "pptp" else "L2tp",
               "authentication": config.get("authentication", "MSChapv2"),
               "encryption": config.get("encryption", "Required"),
               "split": config.get("split_tunneling", False), "psk": config.get("psk", "")}
    result = _run([_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
                  input=json.dumps(payload, ensure_ascii=True).encode("ascii"), capture=True)
    if result.returncode == 2:
        raise VpnError("This VPN is already connected with different settings. Disconnect it, then try again.")
    if result.returncode:
        try:
            detail = base64.b64decode(result.stdout or b"").decode("utf-8", "replace")
        except ValueError:
            detail = ""
        if len(psk := config.get("psk", "")) >= 4:  # the only secret this script is given
            detail = detail.replace(psk, "•••")
        detail = " ".join(detail.split())[:300]
        raise VpnError(f"Windows could not create the VPN profile: {detail}" if detail else
                       f"Windows could not create the VPN profile (error {result.returncode}).")


def _dial(config: dict) -> bool:
    """Dial RPMote's profile; False when it was already connected."""
    import win32ras

    name = windows_profile_name(config)
    for _handle, entry, *_device in win32ras.EnumConnections():
        if entry == name:
            return False
    params = (name, "", "", config["username"],
              config["password"], config.get("domain", ""))
    try:
        handle, error = win32ras.Dial(None, None, params, None)
    except Exception as exc:
        # pywin32 error messages can include API arguments. Return only the code.
        raise _ras_error(getattr(exc, "winerror", None) or "unknown", config["kind"]) from None
    if error:
        if handle:
            try:
                win32ras.HangUp(handle)
            except Exception:
                # pywin32 raises when a failed link is slow to close; the dial error is the news.
                pass
        raise _ras_error(error, config["kind"])
    return True


def _ras_connected(name: str) -> bool:
    import win32ras

    return any(str(entry).casefold() == name.casefold()
               for _handle, entry, *_device in win32ras.EnumConnections())


def _connect_windows_profile(name: str) -> bool:
    """Dial an existing profile; False when it was already connected."""
    if _ras_connected(name):
        return False
    code = _run(["rasdial.exe", name], timeout=120).returncode
    if code in NEEDS_SIGN_IN:
        # No saved login, or the profile needs a prompt rasdial cannot show:
        # Windows' own connect dialog asks for it.
        _run(["rasphone.exe", "-d", name], timeout=600)
        if not _ras_connected(name):
            raise VpnError(f"“{name}” did not connect. Sign in from the Windows dialog to connect it.")
    elif code:
        raise _ras_error(code)
    return True


def _openvpn_dirs() -> list[Path]:
    import winreg

    folders = []
    defaults = (Path.home() / "OpenVPN/config",
                Path(_client("openvpn-gui.exe", "OpenVPN/bin/openvpn-gui.exe")).parent.parent / "config")
    for (hive, key), default in zip(
        ((winreg.HKEY_CURRENT_USER, r"SOFTWARE\OpenVPN-GUI"),
         (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\OpenVPN")), defaults
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                folder = Path(os.path.expandvars(winreg.QueryValueEx(handle, "config_dir")[0]))
        except OSError:
            folder = default
        folders.append(folder)
    return list(dict.fromkeys(folders))


def _openvpn_imported(path: Path) -> Path | None:
    matches = [candidate for folder in _openvpn_dirs() if folder.is_dir()
               for candidate in folder.rglob("*.ovpn") if candidate.name.lower() == path.name.lower()]
    if len(matches) > 1 or (matches and matches[0].read_bytes() != path.read_bytes()):
        raise VpnError("OpenVPN already has a different config with this filename. Give this file a unique name and upload it again.")
    return matches[0] if matches else None


#: Directives whose argument is a file OpenVPN opens relative to the config.
_OPENVPN_FILE_DIRECTIVES = {"ca", "cert", "key", "pkcs12", "tls-auth", "tls-crypt", "tls-crypt-v2",
                            "secret", "crl-verify", "extra-certs", "auth-user-pass", "askpass"}


def _openvpn_companions(path: Path) -> list[Path]:
    """Relative certificate/key files the config names, as paths under its folder."""
    folder = path.parent.resolve()
    found = []
    for line in path.read_text("utf-8", "replace").splitlines():
        try:
            words = shlex.split(line, comments=False)
        except ValueError:
            continue
        if len(words) < 2 or words[0].lstrip("-").lower() not in _OPENVPN_FILE_DIRECTIVES:
            continue
        name = words[1]
        if name.lower() == "[inline]" or Path(name).is_absolute() or Path(name).drive:
            continue
        candidate = (folder / name).resolve()
        if candidate.is_file() and candidate.is_relative_to(folder):
            found.append(candidate.relative_to(folder))
    return list(dict.fromkeys(found))


def _copy_openvpn_companions(source: Path, imported: Path):
    """OpenVPN GUI imports only the .ovpn, so bring the files it names along."""
    if source.parent.resolve() == imported.parent.resolve():
        return
    for relative in _openvpn_companions(source):
        target = imported.parent / relative
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source.parent / relative, target)
        except OSError:
            raise VpnError(f"OpenVPN needs “{relative}” next to the imported config, and it could not be copied there.") from None


def _connect_openvpn(config: dict):
    import win32gui

    path = Path(config["config_file"]).resolve()
    client = _client("openvpn-gui.exe", "OpenVPN/bin/openvpn-gui.exe")
    imported = _openvpn_imported(path)
    if imported is None:
        if win32gui.FindWindow("OpenVPN-GUI", None):
            _checked([client, "--command", "import", str(path)])
        else:
            _popen([client, "--command", "import", str(path)])
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if win32gui.FindWindow("OpenVPN-GUI", None) and (imported := _openvpn_imported(path)):
                break
            time.sleep(0.2)
        else:
            raise VpnError("OpenVPN did not import the config. Check the OpenVPN GUI window, then try again.")
    _copy_openvpn_companions(path, imported)
    # --command connect starts the GUI itself if it is not already running.
    return _popen([client, "--command", "connect", path.stem])


_SERVICE_QUERY = 0x1 | 0x4   # SERVICE_QUERY_CONFIG | SERVICE_QUERY_STATUS
#: What the signed-in user may do with a tunnel service RPMote installs:
#: query, start, stop and interrogate it, so later connects and disconnects
#: need no administrator approval.
_WIREGUARD_USER_RIGHTS = _SERVICE_QUERY | 0x10 | 0x20 | 0x80

_WIREGUARD_INSTALL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
& $client /installtunnelservice $config
if ($LASTEXITCODE) { exit $LASTEXITCODE }
try {
    # Started on demand only, so the tunnel does not come back by itself at
    # the next start-up; the user may start and stop it without a prompt.
    & sc.exe config $service start= demand | Out-Null
    $sddl = ((& sc.exe sdshow $service) -join '').Trim()
    $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new($sddl)
    $user = [Security.Principal.SecurityIdentifier]::new($sid)
    $ace = [Security.AccessControl.CommonAce]::new('None', 'AccessAllowed', $rights, $user, $false, $null)
    $descriptor.DiscretionaryAcl.InsertAce($descriptor.DiscretionaryAcl.Count, $ace)
    & sc.exe sdset $service $descriptor.GetSddlForm('Access') | Out-Null
} catch {
    # The tunnel is up either way; without these rights a disconnect asks for approval.
}
exit 0
"""


def _ps_literal(text: str) -> str:
    """A PowerShell single-quoted string; PowerShell also ends one at a curly quote."""
    return "'" + re.sub("(['‘’‚‛])", r"\1\1", text) + "'"


def _user_sid() -> str:
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return win32security.ConvertSidToStringSid(user)
    finally:
        token.Close()


def _wireguard_service_name(path: Path) -> str:
    return "WireGuardTunnel$" + path.stem


class _OtherTunnelFile(VpnError):
    """A WireGuard tunnel of this config's name runs a different file."""

    def __init__(self, path: Path, other: str, running: bool):
        super().__init__("WireGuard already has a tunnel with this name from a different file. "
                         "Give this config a unique filename.")
        self.other = other
        self.running = running
        # The WireGuard app keeps its own encrypted copy of each tunnel it imported.
        self.app_copy = Path(other).name.casefold() == f"{path.stem}.conf.dpapi".casefold()


@contextlib.contextmanager
def _wireguard_service(path: Path, access: int = 0):
    """This file's tunnel service opened for querying plus *access*, or None if not installed.

    Raises PermissionError when Windows refuses *access*, and _OtherTunnelFile
    when a tunnel of the same name runs a different file.
    """
    import win32api
    import win32service

    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        try:
            service = win32service.OpenService(manager, _wireguard_service_name(path), _SERVICE_QUERY | access)
        except win32service.error as exc:
            if exc.winerror == 5 and access:  # ERROR_ACCESS_DENIED
                raise PermissionError("WireGuard tunnel service") from None
            if exc.winerror != 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
                raise
        if service is not None:
            args = win32api.CommandLineToArgv(win32service.QueryServiceConfig(service)[3])
            if not args or os.path.normcase(str(Path(args[-1]).resolve())) != os.path.normcase(str(path.resolve())):
                state = win32service.QueryServiceStatus(service)[1]
                raise _OtherTunnelFile(path, args[-1] if args else "",
                                       state != win32service.SERVICE_STOPPED)
        yield service
    finally:
        if service is not None:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def _wireguard_running(path: Path) -> bool:
    """True when this file's tunnel is already up. Needs only query rights."""
    import win32service

    try:
        with _wireguard_service(path) as service:
            if service is None:
                return False
            status = win32service.QueryServiceStatus(service)[1]
    except win32service.error:
        raise VpnError("Windows would not let RPMote check this WireGuard tunnel.") from None
    if status in {win32service.SERVICE_RUNNING, win32service.SERVICE_START_PENDING}:
        return True
    if status == win32service.SERVICE_STOPPED:
        return False
    raise VpnError("The WireGuard tunnel is still stopping. Try connecting again in a moment.")


def _wireguard_start(path: Path) -> bool:
    """Start a stopped tunnel RPMote installed earlier; False when it must be (re)installed."""
    import win32service

    try:
        with _wireguard_service(path, win32service.SERVICE_START) as service:
            if service is None:
                return False
            try:
                win32service.StartService(service, None)
            except win32service.error as exc:
                if exc.winerror == 1072:  # ERROR_SERVICE_MARKED_FOR_DELETE
                    return False
                if exc.winerror != 1056:  # ERROR_SERVICE_ALREADY_RUNNING
                    raise
            return True
    except PermissionError:
        # Installed without the rights RPMote grants itself (older RPMote, or by hand).
        return False
    except win32service.error:
        raise VpnError("Windows could not start the WireGuard tunnel.") from None


def _wireguard_stop(path: Path, timeout: float = 30) -> bool:
    """Stop this file's tunnel and wait until it is down; False when that needs approval."""
    import win32service

    try:
        with _wireguard_service(path, win32service.SERVICE_STOP) as service:
            if service is None:
                return True
            deadline = time.monotonic() + timeout
            while (state := win32service.QueryServiceStatus(service)[1]) != win32service.SERVICE_STOPPED:
                if state != win32service.SERVICE_STOP_PENDING:
                    try:
                        win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                    except win32service.error as exc:
                        # Still starting (cannot take the control yet), or already stopping.
                        if exc.winerror not in (1061, 1062):
                            raise
                if time.monotonic() > deadline:
                    raise VpnError("The WireGuard tunnel did not stop in time. Check it in Windows Services.")
                time.sleep(0.2)
            return True
    except PermissionError:
        return False
    except win32service.error:
        raise VpnError("Windows could not stop the WireGuard tunnel.") from None


def _install_wireguard(client: str, path: Path):
    """Install the tunnel service; the one approval also covers later connects and disconnects."""
    script = (f"$client = {_ps_literal(client)}; $config = {_ps_literal(str(path))}; "
              f"$service = {_ps_literal(_wireguard_service_name(path))}; "
              f"$sid = {_ps_literal(_user_sid())}; $rights = {_WIREGUARD_USER_RIGHTS}\n"
              + _WIREGUARD_INSTALL_SCRIPT)
    _admin_checked([_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16-le")).decode("ascii")])


def _connect_wireguard(config: dict) -> bool:
    client = _client("wireguard.exe", "WireGuard/wireguard.exe")
    path = Path(config["config_file"]).resolve()
    try:
        if _wireguard_running(path):
            return False
        started = _wireguard_start(path)
    except _OtherTunnelFile as other:
        if other.running and other.app_copy:
            # Switched on in the WireGuard app: up, and not RPMote's to switch off.
            _LOG.info("wireguard | %s is already up in the WireGuard app", path.stem)
            return False
        if other.running:
            raise VpnError(f"WireGuard is already running a tunnel named “{path.stem}” from "
                           f"{other.other or 'another file'}. Disconnect it there first, "
                           "or give this config a unique filename.") from None
        # Stopped, and left by this config's old location (or a namesake):
        # installing replaces a stopped tunnel of the same name.
        _LOG.info("wireguard | replacing the stopped %s tunnel that ran %s", path.stem, other.other)
        started = False
    if not started:
        # Installing a tunnel service needs administrator rights.
        _install_wireguard(client, path)
    try:
        answered = _wireguard_await_handshake(path)
        if answered is False:
            raise VpnError(wireguard_not_answering(wireguard_settings(path)))
    except Exception:
        # Not left half up: a full tunnel that never connects cuts this PC off,
        # and a failed connect is not RPMote's to take down later.
        with contextlib.suppress(Exception):
            _disconnect_wireguard(config)
        raise
    return True


def _disconnect_wireguard(config: dict):
    path = Path(config["config_file"]).resolve()
    try:
        stopped = _wireguard_stop(path)
    except _OtherTunnelFile:
        # RPMote's tunnel is gone already; the one running now is somebody else's.
        _LOG.info("wireguard | %s now runs another file; left alone", path.stem)
        return
    if not stopped:
        # Installed before RPMote could stop it on its own: remove it, with approval.
        client = _client("wireguard.exe", "WireGuard/wireguard.exe")
        _admin_checked([client, "/uninstalltunnelservice", path.stem])


# ── Is traffic getting through? ─────────────────────────────────
#
# "Connected" only means Windows brought an adapter up. A WireGuard tunnel is
# "up" the moment its service starts, whether or not the server ever answers,
# so RPMote looks at what actually crosses the adapter and where the route to
# the machine goes. None of this needs administrator rights.

class _MibIfRow2(ctypes.Structure):
    _fields_ = [("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong),
                ("InterfaceGuid", ctypes.c_ubyte * 16), ("Alias", ctypes.c_wchar * 257),
                ("Description", ctypes.c_wchar * 257), ("PhysicalAddressLength", ctypes.c_ulong),
                ("PhysicalAddress", ctypes.c_ubyte * 32), ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
                ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong), ("TunnelType", ctypes.c_int),
                ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int), ("AccessType", ctypes.c_int),
                ("DirectionType", ctypes.c_int), ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
                ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int), ("MediaConnectState", ctypes.c_int),
                ("NetworkGuid", ctypes.c_ubyte * 16), ("ConnectionType", ctypes.c_int)]
    _fields_ += [(name, ctypes.c_uint64) for name in (
        "TransmitLinkSpeed", "ReceiveLinkSpeed", "InOctets", "InUcastPkts", "InNUcastPkts", "InDiscards",
        "InErrors", "InUnknownProtos", "InUcastOctets", "InMulticastOctets", "InBroadcastOctets", "OutOctets",
        "OutUcastPkts", "OutNUcastPkts", "OutDiscards", "OutErrors", "OutUcastOctets", "OutMulticastOctets",
        "OutBroadcastOctets", "OutQLen")]


def _adapter_row(alias: str):
    """MIB_IF_ROW2 of the adapter named *alias*, or None when there is none."""
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")
        luid = ctypes.c_uint64()
        if iphlpapi.ConvertInterfaceAliasToLuid(ctypes.c_wchar_p(alias), ctypes.byref(luid)):
            return None
        row = _MibIfRow2(InterfaceLuid=luid.value)
        return None if iphlpapi.GetIfEntry2(ctypes.byref(row)) else row
    except (AttributeError, OSError):
        return None


def adapter_traffic(alias: str) -> tuple[int, int] | None:
    """(bytes sent, bytes received) through the adapter *alias*, or None if it is not there."""
    row = _adapter_row(alias)
    return None if row is None else (row.OutOctets, row.InOctets)


class _MibIpAddrRow(ctypes.Structure):
    _fields_ = [("dwAddr", ctypes.c_uint32), ("dwIndex", ctypes.c_uint32), ("dwMask", ctypes.c_uint32),
                ("dwBCastAddr", ctypes.c_uint32), ("dwReasmSize", ctypes.c_uint32),
                ("unused1", ctypes.c_ushort), ("wType", ctypes.c_ushort)]


def adapter_address(alias: str) -> str:
    """The IPv4 address the adapter *alias* has ("" when none): what the VPN gave this PC."""
    row = _adapter_row(alias)
    if row is None:
        return ""
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")
        size = ctypes.c_ulong(0)
        iphlpapi.GetIpAddrTable(None, ctypes.byref(size), False)
        if not size.value:
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetIpAddrTable(buffer, ctypes.byref(size), False):
            return ""
    except (AttributeError, OSError):
        return ""
    count = struct.unpack_from("<I", buffer, 0)[0]
    rows = (_MibIpAddrRow * count).from_buffer_copy(buffer, 4) if count else []
    for entry in rows:
        if entry.dwIndex == row.InterfaceIndex and entry.dwAddr:
            return socket.inet_ntoa(struct.pack("<I", entry.dwAddr))
    return ""


def _ipv4(host: str) -> str | None:
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError, UnicodeError):
        return None


def routed_through(alias: str, address: str) -> bool | None:
    """Whether Windows sends traffic for IPv4 *address* through the adapter *alias*."""
    row = _adapter_row(alias)
    if row is None:
        return None
    try:
        index = ctypes.c_ulong()
        destination = struct.unpack("<I", socket.inet_aton(address))[0]
        if ctypes.WinDLL("iphlpapi").GetBestInterface(ctypes.c_ulong(destination), ctypes.byref(index)):
            return None
    except (AttributeError, OSError):
        return None
    return index.value == row.InterfaceIndex


def adapter_alias(config: dict) -> str:
    """The name Windows gives this VPN's network adapter ("" when unknown)."""
    kind = config.get("kind")
    if kind == "wireguard":
        return Path(config.get("config_file", "")).stem
    if kind in MANUAL_KINDS:
        return windows_profile_name(config)
    if kind == "windows":
        return config.get("profile_name", "")
    return ""


def wireguard_settings(path: Path) -> dict:
    """The parts of a WireGuard config RPMote reasons about: addresses, DNS, endpoint, routes, key."""
    settings = {"address": [], "dns": [], "endpoint": "", "allowed_ips": [], "private_key": ""}
    try:
        lines = Path(path).read_text("utf-8", "replace").splitlines()
    except OSError:
        return settings
    for line in lines:
        key, _sep, value = line.split("#", 1)[0].partition("=")
        key, value = key.strip().lower(), value.strip()
        values = [item.strip() for item in value.split(",") if item.strip()]
        if key == "address":
            settings["address"] += values
        elif key == "dns":
            settings["dns"] += values
        elif key == "allowedips":
            settings["allowed_ips"] += values
        elif key == "endpoint" and not settings["endpoint"]:
            settings["endpoint"] = value
        elif key == "privatekey":
            settings["private_key"] = value
    return settings


def _wireguard_public_key(private_key: str) -> str:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        key = X25519PrivateKey.from_private_bytes(base64.b64decode(private_key, validate=True))
        raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def _endpoint_parts(endpoint: str) -> tuple[str, str]:
    host, _sep, port = endpoint.rpartition(":")
    return (host.strip("[]"), port) if host else (endpoint, "")


def private_endpoint(settings: dict) -> str:
    """The endpoint's address when it is private (only reachable inside that network), else ""."""
    host, _port = _endpoint_parts(settings.get("endpoint", ""))
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    return "" if address.is_global else host


def wireguard_not_answering(settings: dict) -> str:
    endpoint = settings.get("endpoint") or "its server"
    text = f"The WireGuard server {endpoint} is not answering, so nothing gets through the tunnel."
    if (host := private_endpoint(settings)):
        return (f"{text} {host} is a private address that only works from inside that network; "
                "ask for a config that uses the site's public address.")
    _host, port = _endpoint_parts(settings.get("endpoint", ""))
    public = _wireguard_public_key(settings.get("private_key", ""))
    return (f"{text} Check that the server lists this PC's public key"
            + (f" {public}" if public else "") + f" and that UDP port {port or '51820'} is forwarded to it.")


def config_warning(config: dict) -> str:
    """A problem worth saying when the config is chosen, before anyone connects."""
    if config.get("kind") != "wireguard":
        return ""
    settings = wireguard_settings(Path(config.get("config_file", "")))
    if (host := private_endpoint(settings)):
        return (f"This config connects to {host}, a private address that only works from inside that "
                "network. From anywhere else the tunnel will start but never connect. Ask for a config "
                "that uses the site's public address.")
    return ""


def _nudge_address(alias: str, settings: dict) -> str | None:
    """An address the tunnel carries, to send a packet to: its DNS server, else a host on its routes."""
    candidates = [server for server in settings["dns"] if _is_ipv4(server)]
    for network in settings["allowed_ips"]:
        try:
            net = ipaddress.ip_network(network, strict=False)
        except ValueError:
            continue
        if net.version == 4:
            # hosts() is a list for a /32, a generator otherwise.
            candidates.append("1.1.1.1" if net.prefixlen == 0 else str(next(iter(net.hosts()), net.network_address)))
    return next((address for address in candidates if routed_through(alias, address)), None)


def _is_ipv4(text: str) -> bool:
    try:
        return ipaddress.ip_address(text).version == 4
    except ValueError:
        return False


#: Where the handshake nudges go: the discard port. Never 53: DNS filters such as
#: Twingate drop outbound DNS to any server but their own before it reaches the
#: tunnel, so a split tunnel's handshake never started and a working server was
#: reported as not answering.
_NUDGE_PORT = 9


def _nudge(address: str, held: contextlib.ExitStack):
    """Give WireGuard a packet to carry to *address*, so it starts its handshake.

    A UDP datagram and a TCP SYN: a local filter that drops one seldom drops
    both. The TCP attempt stays pending in *held*; closing it at once could
    cancel the SYN.
    """
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.sendto(b"RPMote", (address, _NUDGE_PORT))
    with contextlib.suppress(OSError):
        tcp = held.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        tcp.setblocking(False)
        tcp.connect_ex((address, _NUDGE_PORT))


def _wireguard_await_handshake(path: Path, timeout: float = 16) -> bool | None:
    """True once the server has answered; False if it never did; None if Windows cannot tell.

    WireGuard holds packets back until its handshake completes, so the first
    byte counted on the adapter means the server answered. Packets towards an
    address inside the tunnel give it something to send.
    """
    alias = path.stem
    settings = wireguard_settings(path)
    began = time.monotonic()
    deadline = began + timeout
    nudge = None
    with contextlib.ExitStack() as held:
        while time.monotonic() < deadline:
            traffic = adapter_traffic(alias)
            if traffic is not None:
                if traffic[0] or traffic[1]:
                    _LOG.info("wireguard | %s answered after %.1f s", settings["endpoint"] or alias,
                              time.monotonic() - began)
                    return True
                nudge = nudge or _nudge_address(alias, settings)
                if nudge is None:
                    _LOG.info("wireguard | %s: no address inside the tunnel to test with", alias)
                    return None     # nothing to send through it: cannot tell
                _nudge(nudge, held)
            time.sleep(0.5)
    if adapter_traffic(alias) is None:
        if not _wireguard_running(path):
            raise VpnError("The WireGuard tunnel stopped as soon as it started. "
                           "Import the config into the WireGuard app to see why.")
        _LOG.info("wireguard | %s: its adapter never appeared", alias)
        return None
    _LOG.warning("wireguard | %s never answered in %.0f s (sent packets for %s)",
                 settings["endpoint"] or alias, timeout, nudge)
    return False


def diagnose(config: dict, host: str, port: int) -> str:
    """Why *host* is not answering over this VPN, as far as Windows can tell ("" if unknown)."""
    alias = adapter_alias(config)
    if not alias:
        return ""
    traffic = adapter_traffic(alias)
    if traffic is None:
        return "The VPN is not connected any more."
    address = host if _is_ipv4(host) else _ipv4(host)
    if address is None:
        return (f"Windows cannot find {host}: its name does not resolve. Use the machine's IP "
                "address, or check the VPN's DNS server.")
    if routed_through(alias, address) is False:
        if config.get("kind") == "wireguard":
            hint = f"{address} is outside the tunnel's AllowedIPs."
        elif config.get("split_tunneling") or config.get("kind") == "windows":
            hint = "Turn off split tunneling for this VPN, or add a route to that network."
        else:
            hint = "Another network adapter is claiming that address."
        return f"Traffic for {host} does not go through the VPN. {hint}"
    if config.get("kind") == "wireguard" and traffic == (0, 0):
        return wireguard_not_answering(wireguard_settings(Path(config["config_file"])))
    if not traffic[1]:
        return "Traffic goes into the VPN, but nothing comes back through it."
    # The VPN carries traffic, so the question is the machine.
    replies = _echo_ttls(address)
    if (ttls := address_conflict(address)):
        who = host if host == address else f"{host} ({address})"
        return (f"Two different devices answer at {who}: their ping replies come back with TTL "
                f"{' and '.join(map(str, ttls))}. That is an IP address conflict on that network, so "
                "connections reach the machine only part of the time and drop when the other device "
                "takes over. Find the second device in the router's ARP or DHCP table, or in the "
                "Windows machine's System event log (Tcpip event 4199), and give it another address.")
    answer = _port_answer(address, port)
    if answer == "open":
        return ""
    if answer == "refused":
        return (f"{host} is reachable over the VPN but refuses connections on port {port}. "
                "The service may be off, or listen on another port.")
    if any(replies):
        return (f"{host} answers ping over the VPN, but not on port {port}. "
                "Its firewall may block that port, or the service is off.")
    local = adapter_address(alias)
    return (f"Nothing at {host} answers over the VPN, not even ping. Check the address: the machine "
            "may be off or firewalled, or the VPN may not reach its network"
            + (f" (it gave this PC {local})." if local else "."))


def _port_answer(address: str, port: int, timeout: float = 2.0) -> str:
    """"open", "refused" (the machine said no) or "silent"."""
    try:
        with socket.create_connection((address, int(port)), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except (OSError, ValueError):
        return "silent"


# ── Two devices on one address ──────────────────────────────────
#
# Every operating system starts its packets with a TTL of its own (Windows
# 128, Linux and macOS 64, much network gear and many printers 255), and each
# router on the way takes one off. Ping replies from one address in two of
# those families come from two machines: an IP address conflict, which lets a
# connection open and then cuts it when the network switches to the other.

class _IpOptionInformation(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class _IcmpEchoReply(ctypes.Structure):
    _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong), ("RoundTripTime", ctypes.c_ulong),
                ("DataSize", ctypes.c_ushort), ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                ("Options", _IpOptionInformation)]


#: address -> [(when, ttl)]: one wait diagnoses every ten seconds, and the
#: network may switch between the two devices only every few seconds.
_TTL_SEEN: dict[str, list[tuple[float, int]]] = {}
_TTL_WINDOW = 120.0


def _echo_ttls(address: str, count: int = 10, spacing: float = 0.25, timeout_ms: int = 600) -> list:
    """The TTL of each of *count* ping replies from IPv4 *address* (None where none came).

    Windows' own ICMP API: no administrator rights, and unlike ping.exe it
    gives each reply's TTL. Stops early when the first three get no reply.
    """
    ttls: list = []
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")
        iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
        iphlpapi.IcmpSendEcho.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ushort,
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        iphlpapi.IcmpSendEcho.restype = ctypes.c_ulong
        iphlpapi.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        destination = struct.unpack("<I", socket.inet_aton(address))[0]
        handle = iphlpapi.IcmpCreateFile()
    except (AttributeError, OSError):
        return ttls
    if not handle or handle == ctypes.c_void_p(-1).value:
        return ttls
    payload = b"RPMote ping " * 2
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_IcmpEchoReply) + len(payload) + 8 + 64)
    try:
        for index in range(count):
            if index:
                if index >= 3 and not any(ttls):
                    break
                time.sleep(spacing)
            got = iphlpapi.IcmpSendEcho(handle, destination, payload, len(payload), None,
                                        ctypes.addressof(buffer), len(buffer), timeout_ms)
            reply = _IcmpEchoReply.from_buffer_copy(buffer)
            # A router's "unreachable" comes back too, from the router's own address.
            ttls.append(reply.Options.Ttl if got and reply.Status == 0 and reply.Address == destination
                        else None)
    finally:
        iphlpapi.IcmpCloseHandle(handle)
    now = time.monotonic()
    seen = [(when, ttl) for when, ttl in _TTL_SEEN.get(address, []) if now - when < _TTL_WINDOW]
    _TTL_SEEN[address] = seen + [(now, ttl) for ttl in ttls if ttl]
    return ttls


def _ttl_family(ttl: int) -> int:
    """The TTL a reply most likely started out with."""
    return next((start for start in (32, 64, 128) if ttl <= start), 255)


def address_conflict(address: str) -> tuple[int, ...]:
    """One TTL per kind of device seen answering at *address* lately; () when only one kind answers."""
    now = time.monotonic()
    ttls = sorted({ttl for when, ttl in _TTL_SEEN.get(address, []) if now - when < _TTL_WINDOW})
    families: dict[int, int] = {}
    for ttl in ttls:
        families[_ttl_family(ttl)] = ttl            # the highest seen: the fewest hops
    return tuple(sorted(families.values())) if len(families) > 1 else ()


def describe(config: dict) -> str:
    """Which VPN this is, for the log. Never a login, a key or a command line."""
    kind = config.get("kind", "custom")
    if kind in MANUAL_KINDS:
        return f"{kind} {config.get('server', '')}"
    if kind in FILE_KINDS:
        return f"{kind} {Path(config.get('config_file', '')).name}"
    if kind == "windows":
        return f"windows profile “{config.get('profile_name', '')}”"
    return "custom command"


def _reason(exc: BaseException) -> str:
    # RPMote's own messages are written to be shown; anything else may carry API arguments.
    if isinstance(exc, VpnError):
        return str(exc)
    code = getattr(exc, "winerror", None)
    return type(exc).__name__ + (f" {code}" if code else "")


def connect(config: dict) -> bool:
    """Bring the tunnel up. Run on a worker: creating and dialing take seconds.

    Returns False when the tunnel was already up, so it is not RPMote's to
    take down again.
    """
    began = time.monotonic()
    _LOG.info("connect | %s", describe(config))
    try:
        result = _connect(config)
    except Exception as exc:
        _LOG.warning("connect failed | %s | %.1f s | %s", describe(config),
                     time.monotonic() - began, _reason(exc))
        raise
    alias = adapter_alias(config)
    address = adapter_address(alias) if alias else ""
    _LOG.info("connect done | %s | %.1f s | %s%s", describe(config), time.monotonic() - began,
              "brought up" if result else "was already up", f" | address {address}" if address else "")
    return result


def _connect(config: dict) -> bool:
    validate(config)
    if sys.platform != "win32":
        raise VpnError("These VPN clients are supported on Windows.")
    kind = config["kind"]
    if kind in MANUAL_KINDS:
        _prepare_windows_profile(config)
        return _dial(config)
    if kind == "windows":
        return _connect_windows_profile(config["profile_name"])
    if kind == "wireguard":
        return _connect_wireguard(config)
    if kind == "openvpn":
        # OpenVPN GUI cannot say whether it was already connected.
        _connect_openvpn(config)
        return True
    raise VpnError("Choose a supported VPN type.")


def disconnect(config: dict):
    began = time.monotonic()
    _LOG.info("disconnect | %s", describe(config))
    try:
        _disconnect(config)
    except Exception as exc:
        _LOG.warning("disconnect failed | %s | %.1f s | %s", describe(config),
                     time.monotonic() - began, _reason(exc))
        raise
    _LOG.info("disconnect done | %s | %.1f s", describe(config), time.monotonic() - began)


def _disconnect(config: dict):
    kind = config["kind"]
    if kind in MANUAL_KINDS or kind == "windows":
        name = windows_profile_name(config) if kind in MANUAL_KINDS else config["profile_name"]
        if _ras_connected(name):
            _checked(["rasdial.exe", name, "/disconnect"])
    elif kind == "wireguard":
        _disconnect_wireguard(config)
    elif kind == "openvpn":
        client = _client("openvpn-gui.exe", "OpenVPN/bin/openvpn-gui.exe")
        _checked([client, "--command", "disconnect", Path(config["config_file"]).stem])
