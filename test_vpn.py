"""VPN setup, client dispatch, and the tenant gate without opening real tunnels."""

import base64
import contextlib
import gc
import json
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QWidget

from app import vpn, vpn_connections as adapters

if QApplication.instance() is None:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
_APP = QApplication.instance() or QApplication(["rpmote-tests"])


def manual(kind="l2tp", **extra):
    return vpn.normalize({"kind": kind, "profile_name": "Contoso VPN", "profile_id": "a" * 32,
                          "server": "vpn.contoso.example", "username": "tech", "domain": "CONTOSO",
                          "password": ' password & "$() secret ', "psk": "key';&秘密", **extra})


@pytest.fixture
def tunnel(tmp_path):
    path = tmp_path / "office tunnel.ovpn"
    path.write_text("client\nremote vpn.example 1194\n")
    return path


def test_legacy_commands_keep_their_settings():
    saved = {"command": 'rasdial "Contoso VPN"', "disconnect_command": 'rasdial "Contoso VPN" /disconnect',
             "keep_connected": True, "skip_if_reachable": False, "wait_seconds": 90}
    dialog = vpn.TenantVpnDialog("Contoso", saved)
    try:
        restored = dialog.config()
        assert restored["kind"] == "custom"
        assert all(restored[key] == value for key, value in saved.items())
        assert adapters.configured(restored)
    finally:
        dialog.close()


@pytest.mark.parametrize("kind", ["pptp", "l2tp"])
def test_manual_fields_restore_and_hide_irrelevant_options(kind):
    saved = manual(kind, encryption="Optional", split_tunneling=True)
    dialog = vpn.TenantVpnDialog("Contoso", saved)
    try:
        assert not dialog.manual_panel.isHidden()
        assert dialog.file_panel.isHidden()
        assert dialog.command_panel.isHidden()
        assert dialog.psk.isHidden() == (kind != "l2tp")
        assert dialog.password.echoMode() == QLineEdit.EchoMode.Password
        assert dialog.psk.echoMode() == QLineEdit.EchoMode.Password
        assert dialog.config() == saved
        assert dialog.profile.text() == "Contoso VPN"
    finally:
        dialog.close()


def test_upload_selects_and_restores_a_config_without_starting_a_vpn(tunnel):
    dialog = vpn.TenantVpnDialog("Contoso", None)
    try:
        dialog.kind.setCurrentIndex(dialog.kind.findData("openvpn"))
        with patch.object(vpn.QFileDialog, "getOpenFileName", return_value=(str(tunnel), "")), \
             patch.object(vpn, "connect_vpn") as connect:
            dialog._upload_config()
        connect.assert_not_called()
        saved = dialog.config()
        assert Path(saved["config_file"]) == tunnel
        restored = vpn.TenantVpnDialog("Contoso", saved)
        try:
            assert restored.config() == saved
            assert not restored.file_panel.isHidden()
            assert restored.manual_panel.isHidden()
        finally:
            restored.close()
    finally:
        dialog.close()


def test_upload_cancel_keeps_the_saved_file(tunnel):
    dialog = vpn.TenantVpnDialog("Contoso", {"kind": "openvpn", "config_file": str(tunnel)})
    try:
        with patch.object(vpn.QFileDialog, "getOpenFileName", return_value=("", "")):
            dialog._upload_config()
        assert dialog.config_file.text() == str(tunnel)
    finally:
        dialog.close()


@pytest.mark.parametrize("key", ["profile_name", "server", "username", "password", "psk"])
def test_l2tp_requires_connection_details(key):
    with pytest.raises(adapters.VpnError):
        adapters.validate(manual(**{key: ""}))


def test_pptp_does_not_require_ipsec_key():
    adapters.validate(manual("pptp", psk=""))


@pytest.mark.parametrize("auth,encryption,ok", [("Pap", "Required", False), ("Chap", "Maximum", False),
                                                ("Pap", "Optional", True), ("MSChapv2", "Maximum", True)])
def test_unencryptable_login_methods_are_caught_before_windows_refuses_them(auth, encryption, ok):
    config = manual("pptp", authentication=auth, encryption=encryption)
    if ok:
        adapters.validate(config)
    else:
        with pytest.raises(adapters.VpnError, match="cannot encrypt"):
            adapters.validate(config)


def test_secrets_keep_whitespace_and_invalid_settings_get_defaults():
    config = vpn.normalize({"password": " p ", "psk": " k ", "wait_seconds": "bad",
                            "authentication": "evil", "encryption": "evil"})
    assert (config["password"], config["psk"]) == (" p ", " k ")
    assert (config["wait_seconds"], config["authentication"], config["encryption"]) == (45, "MSChapv2", "Required")


@pytest.mark.parametrize("name,content", [("bad.ovpn", ""), ("bad.ovpn", "\0binary"),
                                           ("bad.txt", "client")])
def test_invalid_uploads_are_rejected(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    with pytest.raises(adapters.VpnError):
        adapters.validate({"kind": "openvpn", "config_file": str(path)})


def test_missing_file_reports_error_at_connect_instead_of_silently_skipping(tmp_path):
    config = vpn.normalize({"kind": "wireguard", "config_file": str(tmp_path / "office.conf")})
    assert adapters.configured(config)
    with pytest.raises(adapters.VpnError, match="cannot be read"):
        adapters.validate(config)


@pytest.mark.parametrize("name", ["invalid tunnel", "x" * 33])
def test_invalid_wireguard_service_names_are_rejected(tmp_path, name):
    with pytest.raises(adapters.VpnError, match="config names"):
        adapters.validate({"kind": "wireguard", "config_file": str(tmp_path / f"{name}.conf")})


def test_save_validation_stays_open_on_missing_manual_details():
    dialog = vpn.TenantVpnDialog("Contoso", None)
    try:
        dialog.kind.setCurrentIndex(dialog.kind.findData("l2tp"))
        with patch.object(vpn.QMessageBox, "warning") as warning:
            dialog._accept()
        warning.assert_called_once()
        assert dialog.result() != QDialog.DialogCode.Accepted
        dialog.enabled.setChecked(False)
        dialog._accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_profile_setup_uses_stdin_and_keeps_secrets_out_of_script_and_command_line():
    config = manual()
    with patch.object(adapters.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        adapters._prepare_windows_profile(config)
    args, kwargs = run.call_args
    command = args[0]
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    assert config["password"] not in str(command) + script
    assert config["psk"] not in str(command) + script
    payload = json.loads(kwargs["input"])
    assert payload["psk"] == config["psk"]
    assert "password" not in payload
    assert payload["name"] != config["profile_name"]
    assert payload["tunnel"] == "L2tp"
    assert kwargs["shell"] is False


def test_ras_dial_receives_login_in_memory_and_releases_failed_handle():
    import win32ras

    config = manual()
    with patch.object(win32ras, "EnumConnections", return_value=[]), \
         patch.object(win32ras, "Dial", return_value=(123, 691)) as dial, \
         patch.object(win32ras, "HangUp") as hangup:
        with pytest.raises(adapters.VpnError, match="691") as error:
            adapters._dial(config)
    params = dial.call_args.args[2]
    assert params[3:] == (config["username"], config["password"], config["domain"])
    assert config["password"] not in str(error.value)
    hangup.assert_called_once_with(123)


def test_slow_hang_up_does_not_hide_the_dial_error():
    import pywintypes
    import win32ras

    config = manual("pptp", psk="")
    with patch.object(win32ras, "EnumConnections", return_value=[]), \
         patch.object(win32ras, "Dial", return_value=(123, 807)), \
         patch.object(win32ras, "HangUp", side_effect=pywintypes.error(0, "RasHangup", "")):
        with pytest.raises(adapters.VpnError, match="error 807.*interrupted"):
            adapters._dial(config)


def test_profile_errors_show_windows_message_without_the_key():
    config = manual()
    message = f"The parameter is incorrect for key {config['psk']} and café"
    encoded = base64.b64encode(message.encode("utf-8"))
    with patch.object(adapters.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=encoded)):
        with pytest.raises(adapters.VpnError) as error:
            adapters._prepare_windows_profile(config)
    assert "The parameter is incorrect" in str(error.value) and "café" in str(error.value)
    assert config["psk"] not in str(error.value)
    with patch.object(adapters.subprocess, "run", return_value=SimpleNamespace(returncode=2, stdout=b"")):
        with pytest.raises(adapters.VpnError, match="already connected with different settings"):
            adapters._prepare_windows_profile(config)


def test_ras_dial_reuses_its_connected_profile_without_another_reference():
    import win32ras

    config = manual()
    with patch.object(win32ras, "EnumConnections", return_value=[
        (1, "Someone else's VPN", "vpn", "device"),
        (123, adapters.windows_profile_name(config), "vpn", "device")]), \
         patch.object(win32ras, "Dial") as dial:
        assert adapters._dial(config) is False   # already up: not RPMote's to take down
    dial.assert_not_called()


def test_manual_connect_creates_a_profile_before_dialing():
    config = manual("pptp", psk="")
    calls = []
    with patch.object(adapters, "_prepare_windows_profile", side_effect=lambda _: calls.append("profile")), \
         patch.object(adapters, "_dial", side_effect=lambda _: calls.append("dial")):
        vpn.connect_vpn(config)
    assert calls == ["profile", "dial"]


def test_windows_profile_uses_one_literal_argument():
    config = vpn.normalize({"kind": "windows", "profile_name": 'Company & special " name'})
    with patch.object(adapters, "_run", return_value=SimpleNamespace(returncode=0)) as run, \
         patch.object(adapters, "_ras_connected", return_value=False):
        assert vpn.connect_vpn(config) is True
    assert run.call_args.args[0] == ["rasdial.exe", config["profile_name"]]


def test_windows_profile_already_connected_is_left_alone_and_not_claimed():
    with patch.object(adapters, "_run") as run, patch.object(adapters, "_ras_connected", return_value=True):
        assert adapters._connect_windows_profile("Contoso VPN") is False
    run.assert_not_called()


@pytest.mark.parametrize("code", sorted(adapters.NEEDS_SIGN_IN))
def test_windows_profile_without_a_saved_login_opens_the_windows_sign_in(code):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=code if args[0] == "rasdial.exe" else 0)
    with patch.object(adapters, "_run", side_effect=run), \
         patch.object(adapters, "_ras_connected", side_effect=[False, True]):
        assert adapters._connect_windows_profile("Contoso VPN") is True
    assert calls == [["rasdial.exe", "Contoso VPN"], ["rasphone.exe", "-d", "Contoso VPN"]]


def test_windows_sign_in_that_is_cancelled_reports_not_connected():
    with patch.object(adapters, "_run", return_value=SimpleNamespace(returncode=691)), \
         patch.object(adapters, "_ras_connected", return_value=False):
        with pytest.raises(adapters.VpnError, match="did not connect"):
            adapters._connect_windows_profile("Contoso VPN")


def test_windows_profile_errors_use_windows_wording():
    with patch.object(adapters, "_run", return_value=SimpleNamespace(returncode=623)) as run, \
         patch.object(adapters, "_ras_connected", return_value=False):
        with pytest.raises(adapters.VpnError, match="error 623.*phone book entry"):
            adapters._connect_windows_profile("Missing VPN")
    run.assert_called_once()


def wireguard_config(tmp_path, folder="files with spaces"):
    path = tmp_path / folder / "office.conf"
    path.parent.mkdir(exist_ok=True)
    path.write_text("[Interface]\nPrivateKey = test\n[Peer]\nPublicKey = test\n")
    return path, vpn.normalize({"kind": "wireguard", "config_file": str(path)})


def test_wireguard_first_connect_installs_with_approval_and_disconnect_stops_without(tmp_path):
    path, config = wireguard_config(tmp_path)
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_running", return_value=False), \
         patch.object(adapters, "_wireguard_start", return_value=False), \
         patch.object(adapters, "_install_wireguard") as install, \
         patch.object(adapters, "_wireguard_await_handshake", return_value=True), \
         patch.object(adapters, "_wireguard_stop", return_value=True) as stop, \
         patch.object(adapters, "_admin_checked") as checked:
        assert vpn.connect_vpn(config) is True
        vpn.disconnect_vpn(config)
    install.assert_called_once_with("wireguard.exe", path)
    stop.assert_called_once_with(path)
    checked.assert_not_called()


def test_wireguard_reconnect_starts_the_installed_tunnel_without_approval(tmp_path):
    path, config = wireguard_config(tmp_path)
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_running", return_value=False), \
         patch.object(adapters, "_wireguard_start", return_value=True) as start, \
         patch.object(adapters, "_wireguard_await_handshake", return_value=True), \
         patch.object(adapters, "_install_wireguard") as install:
        assert vpn.connect_vpn(config) is True
    start.assert_called_once_with(path)
    install.assert_not_called()


def test_wireguard_tunnel_without_stop_rights_is_removed_with_approval(tmp_path):
    path, config = wireguard_config(tmp_path)
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_stop", return_value=False), \
         patch.object(adapters, "_admin_checked") as checked:
        vpn.disconnect_vpn(config)
    checked.assert_called_once_with(["wireguard.exe", "/uninstalltunnelservice", "office"])


def test_wireguard_leaves_a_running_tunnel_alone(tmp_path):
    path, config = wireguard_config(tmp_path, "tunnels")
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_running", return_value=True), \
         patch.object(adapters, "_wireguard_start") as start, \
         patch.object(adapters, "_admin_checked") as checked:
        assert vpn.connect_vpn(config) is False
    start.assert_not_called()
    checked.assert_not_called()


def test_wireguard_install_is_one_elevated_powershell_that_grants_start_and_stop(tmp_path):
    path = tmp_path / "it's ’quoted’ $(evil)" / "office.conf"
    with patch.object(adapters, "_user_sid", return_value="S-1-5-21-1-2-3-1001"), \
         patch.object(adapters, "_admin_checked") as checked:
        adapters._install_wireguard("C:\\Program Files\\WireGuard\\wireguard.exe", path)
    args = checked.call_args.args[0]
    assert args[0].lower().endswith("powershell.exe") and args[-2] == "-EncodedCommand"
    script = base64.b64decode(args[-1]).decode("utf-16-le")
    assert f"$config = {adapters._ps_literal(str(path))};" in script
    assert "\\it''s ’’quoted’’ $(evil)\\" in script   # literal: nothing in the path runs
    assert "$service = 'WireGuardTunnel$office'" in script
    assert "$sid = 'S-1-5-21-1-2-3-1001'" in script
    assert f"$rights = {0x1 | 0x4 | 0x10 | 0x20 | 0x80}" in script
    assert "/installtunnelservice" in script and "start= demand" in script and "sdset" in script


def test_wireguard_stop_waits_until_the_service_has_stopped(tmp_path):
    import win32service

    path = tmp_path / "office.conf"
    service_config = (None, None, None, f'"C:\\WireGuard\\wireguard.exe" /tunnelservice "{path}"')
    states = iter([win32service.SERVICE_RUNNING, win32service.SERVICE_STOP_PENDING, win32service.SERVICE_STOPPED])
    with patch.object(win32service, "OpenSCManager", return_value=11), \
         patch.object(win32service, "OpenService", return_value=12) as open_service, \
         patch.object(win32service, "QueryServiceConfig", return_value=service_config), \
         patch.object(win32service, "QueryServiceStatus", side_effect=lambda _h: (None, next(states))), \
         patch.object(win32service, "ControlService") as control, \
         patch.object(adapters.time, "sleep"), \
         patch.object(win32service, "CloseServiceHandle") as close:
        assert adapters._wireguard_stop(path) is True
    assert open_service.call_args.args[2] & win32service.SERVICE_STOP
    control.assert_called_once_with(12, win32service.SERVICE_CONTROL_STOP)
    assert [call.args[0] for call in close.call_args_list] == [12, 11]


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_wireguard_without_granted_rights_falls_back_to_approval(tmp_path, operation):
    import pywintypes
    import win32service

    with patch.object(win32service, "OpenSCManager", return_value=11), \
         patch.object(win32service, "OpenService", side_effect=pywintypes.error(5, "OpenService", "")), \
         patch.object(win32service, "CloseServiceHandle"):
        function = adapters._wireguard_start if operation == "start" else adapters._wireguard_stop
        assert function(tmp_path / "office.conf") is False


def test_wireguard_stop_of_a_tunnel_that_is_not_installed_is_done(tmp_path):
    """The real Service Control Manager; nothing is installed or stopped."""
    assert adapters._wireguard_stop(tmp_path / "rpmotetestnotunnel.conf") is True
    assert adapters._wireguard_start(tmp_path / "rpmotetestnotunnel.conf") is False


def test_ras_disconnect_skips_a_profile_that_is_not_connected():
    config = manual("pptp", psk="")
    with patch.object(adapters, "_ras_connected", return_value=False), \
         patch.object(adapters, "_checked") as checked:
        vpn.disconnect_vpn(config)
    checked.assert_not_called()
    with patch.object(adapters, "_ras_connected", return_value=True), \
         patch.object(adapters, "_checked") as checked:
        vpn.disconnect_vpn(config)
    checked.assert_called_once_with(["rasdial.exe", adapters.windows_profile_name(config), "/disconnect"])


def test_wireguard_asks_for_approval_when_rpmote_is_not_elevated():
    args = ["wireguard.exe", "/installtunnelservice", "C:\\vpn\\office.conf"]
    with patch.object(adapters, "_is_admin", return_value=False), \
         patch.object(adapters, "_run_elevated", return_value=0) as elevated, \
         patch.object(adapters, "_checked") as checked:
        adapters._admin_checked(args)
    assert elevated.call_args.args[0] == args
    checked.assert_not_called()
    with patch.object(adapters, "_is_admin", return_value=False), \
         patch.object(adapters, "_run_elevated", return_value=1):
        with pytest.raises(adapters.VpnError, match="error 1"):
            adapters._admin_checked(args)
    with patch.object(adapters, "_is_admin", return_value=True), \
         patch.object(adapters, "_run_elevated") as elevated, \
         patch.object(adapters, "_checked") as checked:
        adapters._admin_checked(args)
    elevated.assert_not_called()
    assert checked.call_args.args[0] == args


@pytest.mark.parametrize("status,running", [(4, True), (2, True), (1, False)])
def test_wireguard_checks_its_service_with_query_rights_only(tmp_path, status, running):
    import win32service

    path = tmp_path / "office.conf"
    service_config = (None, None, None, f'"C:\\WireGuard\\wireguard.exe" /tunnelservice "{path}"')
    with patch.object(win32service, "OpenSCManager", return_value=11), \
         patch.object(win32service, "OpenService", return_value=12) as open_service, \
         patch.object(win32service, "QueryServiceConfig", return_value=service_config), \
         patch.object(win32service, "QueryServiceStatus", return_value=(None, status)), \
         patch.object(win32service, "StartService") as start, \
         patch.object(win32service, "CloseServiceHandle") as close:
        assert adapters._wireguard_running(path) == running
    assert not open_service.call_args.args[2] & win32service.SERVICE_START
    start.assert_not_called()
    assert [call.args[0] for call in close.call_args_list] == [12, 11]


@pytest.mark.parametrize("code", [1060, 5])
def test_wireguard_missing_service_is_installed_and_a_denied_check_is_explained(tmp_path, code):
    import pywintypes
    import win32service

    with patch.object(win32service, "OpenSCManager", return_value=11), \
         patch.object(win32service, "OpenService", side_effect=pywintypes.error(code, "OpenService", "")), \
         patch.object(win32service, "CloseServiceHandle") as close:
        if code == 1060:
            assert adapters._wireguard_running(tmp_path / "office.conf") is False
        else:
            with pytest.raises(adapters.VpnError, match="would not let"):
                adapters._wireguard_running(tmp_path / "office.conf")
    assert [call.args[0] for call in close.call_args_list] == [11]


def test_wireguard_service_check_works_without_administrator_rights(tmp_path):
    """The real Service Control Manager query; nothing is installed."""
    assert adapters._wireguard_running(tmp_path / "rpmotetestnotunnel.conf") is False


def test_wireguard_does_not_start_a_same_named_service_for_another_file(tmp_path):
    import win32service

    with patch.object(win32service, "OpenSCManager", return_value=11), \
         patch.object(win32service, "OpenService", return_value=12), \
         patch.object(win32service, "QueryServiceConfig", return_value=(None, None, None,
                           '"C:\\WireGuard\\wireguard.exe" /tunnelservice "C:\\other\\office.conf"')), \
         patch.object(win32service, "QueryServiceStatus", return_value=(None, win32service.SERVICE_RUNNING)), \
         patch.object(win32service, "StartService") as start, \
         patch.object(win32service, "CloseServiceHandle"):
        with pytest.raises(adapters.VpnError, match="different file") as error:
            adapters._wireguard_running(tmp_path / "office.conf")
        with pytest.raises(adapters.VpnError, match="different file"):
            adapters._wireguard_start(tmp_path / "office.conf")
    start.assert_not_called()
    assert error.value.other == "C:\\other\\office.conf" and error.value.running and not error.value.app_copy


def other_file(path, other, running):
    return adapters._OtherTunnelFile(path, other, running)


def test_wireguard_replaces_a_stopped_tunnel_left_by_the_configs_old_location(tmp_path):
    # Uploaded again from another folder: the old tunnel service still names the old file.
    path, config = wireguard_config(tmp_path)
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_running",
                      side_effect=other_file(path, "C:\\Downloads\\office.conf", running=False)), \
         patch.object(adapters, "_wireguard_start") as start, \
         patch.object(adapters, "_install_wireguard") as install, \
         patch.object(adapters, "_wireguard_await_handshake", return_value=True):
        assert vpn.connect_vpn(config) is True
    start.assert_not_called()
    install.assert_called_once_with("wireguard.exe", path)


def test_wireguard_uses_the_tunnel_switched_on_in_the_wireguard_app(tmp_path):
    path, config = wireguard_config(tmp_path)
    app_copy = "C:\\Program Files\\WireGuard\\Data\\Configurations\\office.conf.dpapi"
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_running", side_effect=other_file(path, app_copy, running=True)), \
         patch.object(adapters, "_install_wireguard") as install, \
         patch.object(adapters, "_wireguard_stop") as stop:
        assert vpn.connect_vpn(config) is False     # up, and not RPMote's to take down
    install.assert_not_called()
    stop.assert_not_called()


def test_wireguard_leaves_a_running_namesake_from_another_file_alone(tmp_path):
    path, config = wireguard_config(tmp_path)
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_running",
                      side_effect=other_file(path, "D:\\vpn\\office.conf", running=True)), \
         patch.object(adapters, "_install_wireguard") as install, \
         patch.object(adapters, "_wireguard_stop") as stop:
        with pytest.raises(adapters.VpnError, match=r"already running a tunnel named “office” from D:\\vpn"):
            vpn.connect_vpn(config)
    install.assert_not_called()
    stop.assert_not_called()


def test_wireguard_disconnect_leaves_a_namesake_alone(tmp_path):
    path, config = wireguard_config(tmp_path)
    with patch.object(adapters, "_wireguard_stop", side_effect=other_file(path, "D:\\office.conf", True)), \
         patch.object(adapters, "_admin_checked") as checked:
        vpn.disconnect_vpn(config)
    checked.assert_not_called()


def test_openvpn_imports_before_connecting_and_keeps_path_a_single_argument(tunnel):
    import win32gui

    config = vpn.normalize({"kind": "openvpn", "config_file": str(tunnel)})
    imported = tunnel.parent / "imported" / tunnel.name
    with patch.object(adapters, "_client", return_value="openvpn-gui.exe"), \
         patch.object(adapters, "_openvpn_imported", side_effect=[None, imported]), \
         patch.object(adapters, "_copy_openvpn_companions") as companions, \
         patch.object(win32gui, "FindWindow", return_value=1), \
         patch.object(adapters, "_checked") as checked, patch.object(adapters, "_popen") as popen:
        vpn.connect_vpn(config)
    assert checked.call_args.args[0] == ["openvpn-gui.exe", "--command", "import", str(tunnel)]
    assert popen.call_args.args[0] == ["openvpn-gui.exe", "--command", "connect", tunnel.stem]
    companions.assert_called_once_with(tunnel, imported)


def test_openvpn_brings_named_certificates_along_with_the_imported_config(tmp_path):
    source = tmp_path / "export" / "office.ovpn"
    (source.parent / "keys").mkdir(parents=True)
    for name in ("ca.crt", "keys/client.key", "ta.key"):
        (source.parent / name).write_text(name)
    (tmp_path / "outside.crt").write_text("not in the export folder")
    source.write_text("client\nremote vpn.example 1194\n"
                      "ca ca.crt\n"
                      'key "keys/client.key"\n'
                      "tls-auth ta.key 1 # comment\n"
                      "--cert ../outside.crt\n"
                      "pkcs12 [inline]\n"
                      f"crl-verify {(tmp_path / 'outside.crt').as_posix()}\n"
                      "<ca>\n-----BEGIN CERTIFICATE-----\n</ca>\n"
                      "auth-user-pass\n")
    imported = tmp_path / "OpenVPN" / "config" / "office" / "office.ovpn"
    imported.parent.mkdir(parents=True)
    imported.write_bytes(source.read_bytes())
    (imported.parent / "ta.key").write_text("already there")
    adapters._copy_openvpn_companions(source, imported)
    assert (imported.parent / "ca.crt").read_text() == "ca.crt"
    assert (imported.parent / "keys" / "client.key").read_text() == "keys/client.key"
    assert (imported.parent / "ta.key").read_text() == "already there"
    assert sorted(p.name for p in imported.parent.parent.rglob("*")) == [
        "ca.crt", "client.key", "keys", "office", "office.ovpn", "ta.key"]


def test_openvpn_never_connects_a_different_same_named_config(tunnel, tmp_path):
    imported = tmp_path / "client-configs" / tunnel.name
    imported.parent.mkdir()
    imported.write_text("client\nremote another.customer.example 1194\n")
    with patch.object(adapters, "_openvpn_dirs", return_value=[imported.parent]):
        with pytest.raises(adapters.VpnError, match="unique name"):
            adapters._openvpn_imported(tunnel)
        imported.write_bytes(tunnel.read_bytes())
        assert adapters._openvpn_imported(tunnel)


def test_client_error_and_timeout_are_reported_without_secret_output():
    with patch.object(adapters.subprocess, "run", return_value=SimpleNamespace(returncode=5)):
        with pytest.raises(adapters.VpnError, match="error 5"):
            adapters._checked(["client"])
    with patch.object(adapters.subprocess, "run", side_effect=adapters.subprocess.TimeoutExpired("client", 1)):
        with pytest.raises(adapters.VpnError, match="did not respond"):
            adapters._checked(["client"])


def test_custom_commands_still_dispatch_unchanged():
    config = vpn.normalize({"command": "custom connect", "disconnect_command": "custom stop"})
    with patch.object(vpn, "run_command") as run:
        vpn.connect_vpn(config)
        vpn.disconnect_vpn(config)
    assert [call.args[0] for call in run.call_args_list] == ["custom connect", "custom stop"]


def test_failed_custom_command_is_reported():
    config = vpn.normalize({"command": 'rasdial "Contoso"'})
    process = Mock(spec=vpn.subprocess.Popen)
    process.wait.return_value = 691
    relay = vpn._ProbeRelay()
    launched, exited = [], []
    relay.launched.connect(lambda *args: launched.append(args))
    relay.exited.connect(exited.append)
    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    with patch.object(vpn, "run_command", return_value=process), \
         patch.object(vpn.threading, "Thread", ImmediateThread):
        vpn._start_worker(config, relay)
    assert launched == [("", True)] and exited == [691]
    dialog = vpn.TenantVpnDialog("Contoso", config)
    try:
        dialog._test_exited(691)
        assert dialog.test_status.text() == "The connect command failed (exit code 691)."
    finally:
        dialog.close()


class FakeWindow(QWidget):
    """Just what the gate reads: the tenant settings and the open sessions."""

    def __init__(self, configs):
        super().__init__()
        self.vault = SimpleNamespace(settings={vpn.SETTINGS_KEY: configs})
        self.sessions = {}

    def _open_sessions(self):
        return dict(self.sessions)


_WIDGETS = []


@pytest.fixture(autouse=True)
def _delete_widgets_on_the_gui_thread():
    """A window and its gate refer to each other, so Python's cycle collector
    frees them, possibly on a worker thread, which Qt does not survive."""
    yield
    while _WIDGETS:
        widget = _WIDGETS.pop()
        if not sip.isdeleted(widget):
            sip.delete(widget)
    gc.collect()


def gate_for(config, **others):
    window = FakeWindow({"Contoso": config, **others})
    _WIDGETS.append(window)
    return window, vpn.VpnGate(window)


def vpn_dialog(config):
    dialog = vpn.TenantVpnDialog("Contoso", config)
    _WIDGETS.append(dialog)
    return dialog


def wait_until(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QApplication.processEvents()
        time.sleep(0.005)


def wait_dialogs(window):
    return [child for child in window.findChildren(vpn._WaitDialog) if child.isVisible()]


def opens_session(window, key, tenant="Contoso"):
    """A proceed() that opens a session, as MainWindow._open_now would."""
    return Mock(side_effect=lambda: window.sessions.update({key: tenant}))


def job(config, proceed=None, host="server", tenant="Contoso"):
    return {"config": config, "tenant": tenant, "host": host, "port": 22, "proceed": proceed or Mock()}


@pytest.fixture
def vpn_calls():
    """connect_vpn / disconnect_vpn / tcp_probe stand-ins; nothing real is dialled."""
    state = SimpleNamespace(answers=False, connect_result=True, connect_gate=None)

    def connect(_config):
        if state.connect_gate is not None:
            state.connect_gate.wait(5)
        if isinstance(state.connect_result, Exception):
            raise state.connect_result
        return state.connect_result

    with patch.object(vpn, "connect_vpn", side_effect=connect) as connect_mock, \
         patch.object(vpn, "disconnect_vpn") as disconnect, \
         patch.object(vpn, "tcp_probe", side_effect=lambda *a, **k: (state.answers, 1, "")):
        state.connect, state.disconnect = connect_mock, disconnect
        yield state


def test_gate_recognizes_structured_configs_without_commands():
    window, gate = gate_for(manual())
    assert gate.config_for("Contoso")["kind"] == "l2tp"
    assert gate.config_for("Missing") is None
    window.vault.settings[vpn.SETTINGS_KEY]["Contoso"]["enabled"] = False
    assert gate.config_for("Contoso") is None


def test_gate_connects_and_waits_for_reachability_before_opening(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    proceed = opens_session(window, "s1")
    gate._bring_up_and_wait(job(config, proceed))
    assert not proceed.called
    wait_until(lambda: gate._tunnels["Contoso"].state == "up")
    assert not proceed.called          # up, but the machine has not answered yet
    vpn_calls.answers = True
    wait_until(lambda: proceed.called)
    vpn_calls.connect.assert_called_once_with(config)
    assert gate._tunnels["Contoso"].owned
    vpn_calls.disconnect.assert_not_called()


def test_vpn_is_not_disconnected_while_the_machine_it_was_connected_for_is_still_answering(vpn_calls):
    """The bug: the 3-second session count saw no session yet and took the VPN down again."""
    config = manual()
    window, gate = gate_for(config)
    proceed = opens_session(window, "s1")
    gate._bring_up_and_wait(job(config, proceed))
    wait_until(lambda: gate._tunnels["Contoso"].state == "up")
    for _ in range(3):
        gate.sessions_changed()
    vpn_calls.disconnect.assert_not_called()
    vpn_calls.answers = True
    wait_until(lambda: proceed.called)
    gate.sessions_changed()
    vpn_calls.disconnect.assert_not_called()
    window.sessions.clear()             # the session that needed the VPN closes
    gate.sessions_changed()
    wait_until(lambda: "Contoso" not in gate._tunnels)
    vpn_calls.disconnect.assert_called_once_with(config)


def test_cancelling_the_wait_disconnects_the_vpn_it_brought_up(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    proceed = Mock()
    gate._bring_up_and_wait(job(config, proceed))
    wait_until(lambda: gate._tunnels["Contoso"].state == "up")
    dialog, = wait_dialogs(window)
    dialog.reject()
    wait_until(lambda: "Contoso" not in gate._tunnels)
    vpn_calls.disconnect.assert_called_once_with(config)
    proceed.assert_not_called()


def test_vpn_that_comes_up_after_the_wait_was_cancelled_is_disconnected(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    vpn_calls.connect_gate = threading.Event()
    gate._bring_up_and_wait(job(config))
    dialog, = wait_dialogs(window)
    dialog.reject()
    QApplication.processEvents()
    vpn_calls.disconnect.assert_not_called()
    vpn_calls.connect_gate.set()        # e.g. the UAC prompt approved late
    wait_until(lambda: vpn_calls.disconnect.called and "Contoso" not in gate._tunnels)


def test_open_anyway_keeps_the_vpn_for_that_session(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    proceed = opens_session(window, "s1")
    gate._bring_up_and_wait(job(config, proceed))
    wait_until(lambda: gate._tunnels["Contoso"].state == "up")
    dialog, = wait_dialogs(window)
    dialog.btn_anyway.click()
    wait_until(lambda: proceed.called)
    gate.sessions_changed()
    vpn_calls.disconnect.assert_not_called()


def test_vpn_that_was_already_up_is_never_disconnected(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    vpn_calls.connect_result = False
    vpn_calls.answers = True
    proceed = opens_session(window, "s1")
    gate._bring_up_and_wait(job(config, proceed))
    wait_until(lambda: proceed.called)
    window.sessions.clear()
    gate.sessions_changed()
    QApplication.processEvents()
    vpn_calls.disconnect.assert_not_called()
    assert "Contoso" not in gate._tunnels


def test_leave_connected_setting_keeps_the_vpn_up(vpn_calls):
    config = manual(keep_connected=True)
    window, gate = gate_for(config)
    vpn_calls.answers = True
    proceed = opens_session(window, "s1")
    gate._bring_up_and_wait(job(config, proceed))
    wait_until(lambda: proceed.called)
    window.sessions.clear()
    gate.sessions_changed()
    QApplication.processEvents()
    vpn_calls.disconnect.assert_not_called()
    assert config["keep_connected"] and not vpn.normalize({})["keep_connected"]


def test_sessions_open_before_the_vpn_do_not_keep_it_up(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    window.sessions.update({"cloud-console": "Contoso", "other": "Fabrikam"})
    vpn_calls.answers = True
    proceed = opens_session(window, "s1")
    gate._bring_up_and_wait(job(config, proceed))
    wait_until(lambda: proceed.called)
    gate.sessions_changed()
    vpn_calls.disconnect.assert_not_called()
    del window.sessions["s1"]
    gate.sessions_changed()
    wait_until(lambda: vpn_calls.disconnect.called)


def test_machines_opened_while_the_vpn_is_up_keep_it_up_until_the_last_closes(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    vpn_calls.answers = True
    gate._bring_up_and_wait(job(config, opens_session(window, "s1")))
    wait_until(lambda: "s1" in window.sessions)
    # A second machine answers straight away, through the VPN.
    second = opens_session(window, "s2")
    assert gate.before_open({"tenant": "Contoso", "host": "server2", "port": 22, "protocol": "ssh"}, second)
    wait_until(lambda: second.called)
    del window.sessions["s1"]
    gate.sessions_changed()
    vpn_calls.disconnect.assert_not_called()
    del window.sessions["s2"]
    gate.sessions_changed()
    wait_until(lambda: vpn_calls.disconnect.called)
    vpn_calls.connect.assert_called_once()


def test_machines_waiting_together_share_one_connect(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    vpn_calls.connect_gate = threading.Event()
    first, second = opens_session(window, "s1"), opens_session(window, "s2")
    gate._bring_up_and_wait(job(config, first))
    gate._bring_up_and_wait(job(config, second, host="server2"))
    assert len(wait_dialogs(window)) == 2
    vpn_calls.connect_gate.set()
    vpn_calls.answers = True
    wait_until(lambda: first.called and second.called)
    vpn_calls.connect.assert_called_once()
    vpn_calls.disconnect.assert_not_called()


def test_gate_reconnects_a_previously_started_vpn_when_the_host_is_unreachable(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    vpn_calls.answers = True
    gate._bring_up_and_wait(job(config, opens_session(window, "s1")))
    wait_until(lambda: "s1" in window.sessions)
    vpn_calls.answers = False
    gate._bring_up_and_wait(job(config, host="server2"))
    wait_until(lambda: vpn_calls.connect.call_count == 2)
    for dialog in wait_dialogs(window):
        dialog.reject()
    assert gate._tunnels["Contoso"].owned


def test_gate_failure_shows_the_error_without_marking_tunnel_started(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    vpn_calls.connect_result = adapters.VpnError("Bad login (error 691)")
    proceed = Mock()
    gate._bring_up_and_wait(job(config, proceed))
    dialog, = wait_dialogs(window)
    wait_until(lambda: dialog.detail.text() == "Bad login (error 691)")
    assert "Contoso" not in gate._tunnels
    dialog.reject()
    QApplication.processEvents()
    proceed.assert_not_called()
    vpn_calls.disconnect.assert_not_called()


def test_wait_dialog_grows_to_show_a_long_reason_in_full():
    dialog = vpn._WaitDialog("Contoso", "server:3389", 30)
    _WIDGETS.append(dialog)
    dialog.show()
    QApplication.processEvents()
    dialog.set_detail(adapters.wireguard_not_answering({"endpoint": "10.104.183.174:51820"}) * 2)
    QApplication.processEvents()
    label = dialog.detail
    assert label.height() >= label.heightForWidth(label.width())
    assert label.geometry().bottom() < dialog.bar.geometry().top()
    dialog.reject()


@pytest.mark.parametrize("kind,text", [("l2tp", "VPN connected"), ("custom", "VPN connection requested")])
def test_wait_dialog_says_connected_only_when_the_tunnel_is_confirmed(vpn_calls, kind, text):
    config = manual(kind, command="connect")
    window, gate = gate_for(config)
    if kind == "custom":
        vpn_calls.connect_result = Mock(spec=vpn.subprocess.Popen)
        vpn_calls.connect_result.wait.return_value = 0
    gate._bring_up_and_wait(job(config))
    dialog, = wait_dialogs(window)
    wait_until(lambda: dialog.label.text() == text)
    dialog.reject()


def test_quitting_disconnects_vpns_unless_a_remote_desktop_window_still_uses_them(vpn_calls):
    contoso, fabrikam = manual(), manual(profile_name="Fabrikam VPN", profile_id="b" * 32)
    window, gate = gate_for(contoso, Fabrikam=fabrikam)
    for tenant, config in (("Contoso", contoso), ("Fabrikam", fabrikam)):
        tunnel = vpn._Tunnel(tenant, config, set())
        tunnel.state, tunnel.owned = "up", True
        gate._tunnels[tenant] = tunnel
    gate.shutdown({"mstsc-window": "Fabrikam"})
    vpn_calls.disconnect.assert_called_once_with(contoso)


def test_quitting_while_a_vpn_is_still_connecting_disconnects_it_once_it_is_up(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    vpn_calls.connect_gate = threading.Event()
    gate._bring_up_and_wait(job(config))
    threading.Timer(0.2, vpn_calls.connect_gate.set).start()
    gate.shutdown({})
    vpn_calls.disconnect.assert_called_once_with(config)
    for dialog in wait_dialogs(window):
        dialog.reject()


def test_window_reports_tab_and_remote_desktop_sessions_by_tenant():
    from app.main_window import MainWindow

    tab = SimpleNamespace(connection_info={"tenant": "Contoso"}, _history_token="tab-token")
    broadcast = SimpleNamespace(connection_info={"tenant": "Fabrikam"})
    history = SimpleNamespace(_open={"tab-token": {"tenant": "Contoso"}, "rdp-token": {"tenant": "Contoso"}})
    window = SimpleNamespace(_session_tabs=lambda: [tab, broadcast], session_history=history,
                             _history_widgets={"tab-token": tab})
    sessions = MainWindow._open_sessions(window)
    assert sessions == {"tab-token": "Contoso", f"tab-{id(broadcast)}": "Fabrikam", "rdp-token": "Contoso"}
    assert MainWindow._external_sessions(window) == {"rdp-token": "Contoso"}


def test_connect_now_offers_disconnect_and_closing_the_window_disconnects(vpn_calls):
    dialog = vpn_dialog(manual())
    config = dialog.config()
    dialog._test_clicked()
    wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
    assert "disconnects when you close" in dialog.test_status.text()
    dialog.reject()
    wait_until(lambda: vpn_calls.disconnect.called)
    vpn_calls.disconnect.assert_called_once_with(config)


def test_connect_now_button_disconnects_on_request(vpn_calls):
    dialog = vpn_dialog(manual())
    try:
        dialog._test_clicked()
        wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
        dialog._test_clicked()
        wait_until(lambda: dialog.test_status.text() == "VPN disconnected.")
        assert dialog.btn_test.text() == "Connect VPN now"
        vpn_calls.disconnect.assert_called_once()
    finally:
        dialog.reject()
    QApplication.processEvents()
    vpn_calls.disconnect.assert_called_once()


def test_connect_now_leaves_the_vpn_up_when_set_to_stay_connected(vpn_calls):
    dialog = vpn_dialog(manual(keep_connected=True))
    dialog._test_clicked()
    wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
    dialog.reject()
    QApplication.processEvents()
    time.sleep(0.05)
    vpn_calls.disconnect.assert_not_called()


# ── Is anything getting through? ─────────────────────────────────


def write_wireguard(tmp_path, endpoint, name="office"):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    key = X25519PrivateKey.generate()
    private = base64.b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                 serialization.NoEncryption())).decode()
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw)).decode()
    path = tmp_path / f"{name}.conf"
    path.write_text(f"[Interface]\r\nPrivateKey = {private}\r\nAddress = 192.168.3.7/32\r\n"
                    f"DNS = 192.168.3.1, 192.168.3.2  # site DNS\r\n\r\n[Peer]\r\nPublicKey = x\r\n"
                    f"AllowedIPs = 192.168.3.0/24, 10.20.0.0/16\r\nEndpoint = {endpoint}\r\n")
    return path, public


def test_wireguard_settings_are_read_and_the_public_key_derived(tmp_path):
    path, public = write_wireguard(tmp_path, "96.94.208.73:51820")
    settings = adapters.wireguard_settings(path)
    assert settings["dns"] == ["192.168.3.1", "192.168.3.2"]
    assert settings["allowed_ips"] == ["192.168.3.0/24", "10.20.0.0/16"]
    assert settings["endpoint"] == "96.94.208.73:51820"
    message = adapters.wireguard_not_answering(settings)
    assert "96.94.208.73:51820 is not answering" in message
    assert public in message and "UDP port 51820" in message
    assert settings["private_key"] not in message


@pytest.mark.parametrize("endpoint,private", [("10.104.183.174:51820", "10.104.183.174"),
                                              ("100.72.1.9:51820", "100.72.1.9"),
                                              ("vpn.example.com:51820", ""),
                                              ("96.94.208.73:51820", "")])
def test_private_endpoints_are_called_out(tmp_path, endpoint, private):
    path, _public = write_wireguard(tmp_path, endpoint)
    assert adapters.private_endpoint(adapters.wireguard_settings(path)) == private
    warning = adapters.config_warning({"kind": "wireguard", "config_file": str(path)})
    assert (private in warning and "private address" in warning) if private else warning == ""


def test_upload_warns_about_a_private_endpoint_but_keeps_the_file(tmp_path):
    path, _public = write_wireguard(tmp_path, "10.104.183.174:51820")
    dialog = vpn_dialog(None)
    dialog.kind.setCurrentIndex(dialog.kind.findData("wireguard"))
    with patch.object(vpn.QFileDialog, "getOpenFileName", return_value=(str(path), "")), \
         patch.object(vpn.QMessageBox, "warning") as warning:
        dialog._upload_config()
    assert "10.104.183.174" in warning.call_args.args[2]
    assert dialog.config()["config_file"] == str(path.resolve())


def test_handshake_wait_sees_the_first_bytes_through_the_tunnel(tmp_path):
    path, _public = write_wireguard(tmp_path, "96.94.208.73:51820")
    traffic = iter([None, (0, 0), (0, 0), (148, 0)])
    sent = []
    with patch.object(adapters, "adapter_traffic", side_effect=lambda _alias: next(traffic)), \
         patch.object(adapters, "routed_through", return_value=True), \
         patch.object(adapters.time, "sleep"), \
         patch.object(adapters, "_nudge", lambda address, _held: sent.append(address)):
        assert adapters._wireguard_await_handshake(path) is True
    assert sent == ["192.168.3.1", "192.168.3.1"]


def test_handshake_nudge_sends_udp_and_tcp_but_never_to_the_dns_port():
    # Twingate (and other DNS filters) drop outbound UDP 53 before it reaches the
    # tunnel, so a DNS-shaped nudge never started a split tunnel's handshake.
    assert adapters._NUDGE_PORT != 53
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, \
         socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(5)
        port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", port))
        tcp.listen()
        tcp.settimeout(5)
        with patch.object(adapters, "_NUDGE_PORT", port), contextlib.ExitStack() as held:
            adapters._nudge("127.0.0.1", held)
            assert udp.recvfrom(64)[0]
            tcp.accept()[0].close()


def test_handshake_wait_reports_a_server_that_never_answers(tmp_path):
    path, _public = write_wireguard(tmp_path, "96.94.208.73:51820")
    with patch.object(adapters, "adapter_traffic", return_value=(0, 0)), \
         patch.object(adapters, "routed_through", return_value=True), \
         patch.object(adapters, "_nudge"):
        assert adapters._wireguard_await_handshake(path, timeout=0.3) is False


def test_handshake_nudge_handles_single_host_routes(tmp_path):
    # ip_network("x/32").hosts() is a list, not an iterator; it used to raise TypeError.
    path = tmp_path / "split.conf"
    path.write_text("[Interface]\nPrivateKey = x\nAddress = 192.168.4.220/32\n\n[Peer]\nPublicKey = y\n"
                    "AllowedIPs = 192.168.4.1/32, 10.9.0.0/31, 10.20.0.0/16\nEndpoint = 203.0.113.5:51820\n")
    settings = adapters.wireguard_settings(path)
    asked = []
    with patch.object(adapters, "routed_through", side_effect=lambda _alias, address: asked.append(address)
                      or address == "10.20.0.1"):
        assert adapters._nudge_address("split", settings) == "10.20.0.1"
    assert asked == ["192.168.4.1", "10.9.0.0", "10.20.0.1"]


def test_wireguard_is_stopped_when_the_handshake_wait_fails_unexpectedly(tmp_path):
    path, _public = write_wireguard(tmp_path, "96.94.208.73:51820")
    config = vpn.normalize({"kind": "wireguard", "config_file": str(path)})
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_running", return_value=False), \
         patch.object(adapters, "_wireguard_start", return_value=True), \
         patch.object(adapters, "_wireguard_await_handshake", side_effect=TypeError("boom")), \
         patch.object(adapters, "_wireguard_stop", return_value=True) as stop:
        with pytest.raises(TypeError):
            vpn.connect_vpn(config)
    stop.assert_called_once_with(path)


def test_handshake_wait_cannot_tell_without_a_routed_address_or_adapter(tmp_path):
    path, _public = write_wireguard(tmp_path, "96.94.208.73:51820")
    with patch.object(adapters, "adapter_traffic", return_value=(0, 0)), \
         patch.object(adapters, "routed_through", return_value=False):
        assert adapters._wireguard_await_handshake(path, timeout=0.3) is None
    with patch.object(adapters, "adapter_traffic", return_value=None), \
         patch.object(adapters, "_wireguard_running", return_value=False):
        with pytest.raises(adapters.VpnError, match="stopped as soon as it started"):
            adapters._wireguard_await_handshake(path, timeout=0.3)


def test_wireguard_that_never_handshakes_is_stopped_and_explained(tmp_path):
    path, public = write_wireguard(tmp_path, "96.94.208.73:51820")
    config = vpn.normalize({"kind": "wireguard", "config_file": str(path)})
    with patch.object(adapters, "_client", return_value="wireguard.exe"), \
         patch.object(adapters, "_wireguard_running", return_value=False), \
         patch.object(adapters, "_wireguard_start", return_value=True), \
         patch.object(adapters, "_wireguard_await_handshake", return_value=False), \
         patch.object(adapters, "_wireguard_stop", return_value=True) as stop:
        with pytest.raises(adapters.VpnError, match="not answering") as error:
            vpn.connect_vpn(config)
    stop.assert_called_once_with(path)          # a dead full tunnel would cut the PC off
    assert public in str(error.value)


def test_adapter_lookups_cope_with_an_adapter_that_does_not_exist():
    assert adapters.adapter_traffic("RPMote no such adapter") is None
    assert adapters.routed_through("RPMote no such adapter", "192.0.2.1") is None


def diagnose_with(config, *, traffic, routed=True, address="192.168.3.10", host="192.168.3.10",
                  port="silent", pings=False, local="10.100.20.2", conflict=()):
    with patch.object(adapters, "adapter_traffic", return_value=traffic), \
         patch.object(adapters, "routed_through", return_value=routed), \
         patch.object(adapters, "_ipv4", return_value=address), \
         patch.object(adapters, "_port_answer", return_value=port), \
         patch.object(adapters, "_echo_ttls", return_value=[127, 127] if pings else [None, None, None]), \
         patch.object(adapters, "address_conflict", return_value=conflict), \
         patch.object(adapters, "adapter_address", return_value=local):
        return adapters.diagnose(config, host, 3389)


def test_diagnosis_names_the_reason_a_machine_is_silent(tmp_path):
    path, public = write_wireguard(tmp_path, "96.94.208.73:51820")
    wireguard = {"kind": "wireguard", "config_file": str(path)}
    assert "not connected any more" in diagnose_with(wireguard, traffic=None)
    assert public in diagnose_with(wireguard, traffic=(0, 0))
    assert "outside the tunnel's AllowedIPs" in diagnose_with(wireguard, traffic=(10, 10), routed=False)
    assert "nothing comes back" in diagnose_with(wireguard, traffic=(500, 0))
    assert "refuses connections on port 3389" in diagnose_with(wireguard, traffic=(500, 900), port="refused")
    assert "answers ping over the VPN, but not on port 3389" in diagnose_with(wireguard, traffic=(500, 900),
                                                                              pings=True)
    silent = diagnose_with(manual(), traffic=(500, 900))
    assert "not even ping" in silent and "it gave this PC 10.100.20.2" in silent
    assert diagnose_with(wireguard, traffic=(500, 900), port="open") == ""   # it answers now
    assert "does not resolve" in diagnose_with(wireguard, traffic=(5, 5), address=None, host="dc01")
    split = manual("pptp", psk="", split_tunneling=True)
    assert "split tunneling" in diagnose_with(split, traffic=(5, 5), routed=False)
    assert diagnose_with(vpn.normalize({"command": "x"}), traffic=(0, 0)) == ""


def test_ttl_families_tell_kinds_of_device_apart():
    assert [adapters._ttl_family(ttl) for ttl in (128, 127, 120, 64, 63, 30, 255, 254)] == \
        [128, 128, 128, 64, 64, 32, 255, 255]


def test_two_kinds_of_device_at_one_address_are_a_conflict():
    """As seen at Epiphany on 2026-10-14: the DC (TTL 127) and a TTL-255 device both at 10.100.50.4."""
    now = adapters.time.monotonic()
    with patch.dict(adapters._TTL_SEEN, clear=True):
        adapters._TTL_SEEN["10.100.50.4"] = [(now, 127)] * 24 + [(now, 254)] * 6
        assert adapters.address_conflict("10.100.50.4") == (127, 254)
        # One machine whose replies take one route or another is not two machines.
        adapters._TTL_SEEN["10.0.0.9"] = [(now, 127), (now, 126), (now, 125)]
        assert adapters.address_conflict("10.0.0.9") == ()
        # What was seen long ago does not count any more.
        adapters._TTL_SEEN["10.0.0.8"] = [(now - 600, 254), (now, 127)]
        assert adapters.address_conflict("10.0.0.8") == ()
        assert adapters.address_conflict("10.0.0.7") == ()


def test_echo_samples_add_up_across_diagnoses():
    with patch.dict(adapters._TTL_SEEN, clear=True):
        adapters._TTL_SEEN["127.0.0.1"] = [(adapters.time.monotonic(), 254)]   # an earlier diagnosis
        assert adapters._echo_ttls("127.0.0.1", count=2, spacing=0) == [128, 128]   # the real ICMP API
        assert adapters.address_conflict("127.0.0.1") == (128, 254)


def test_echo_gives_up_on_an_address_that_never_answers():
    began = adapters.time.monotonic()
    assert adapters._echo_ttls("192.0.2.1", count=10, timeout_ms=200) == [None, None, None]
    assert adapters.time.monotonic() - began < 3


def test_diagnosis_puts_an_ip_conflict_first():
    text = diagnose_with(manual(), traffic=(500, 900), host="dc01", address="10.100.50.4",
                         port="open", conflict=(127, 254))
    assert "Two different devices answer at dc01 (10.100.50.4)" in text
    assert "TTL 127 and 254" in text and "IP address conflict" in text and "Tcpip event 4199" in text


def test_wait_dialog_explains_why_the_machine_is_not_answering(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    with patch.object(vpn, "_DIAGNOSE_AFTER", 1), \
         patch.object(adapters, "diagnose", return_value="Traffic for server does not go through the VPN."):
        gate._bring_up_and_wait(job(config))
        dialog, = wait_dialogs(window)
        wait_until(lambda: dialog.detail.text() == "Traffic for server does not go through the VPN.", timeout=10)
        dialog.reject()


# ── Errors that say what to change ───────────────────────────────


def test_l2tp_errors_say_what_to_change():
    assert "AssumeUDPEncapsulationContextOnSendRule" in str(adapters._ras_error(809, "l2tp"))
    assert "pre-shared key" in str(adapters._ras_error(789, "l2tp"))
    # 809 means other things for other tunnel types.
    assert "AssumeUDPEncapsulationContextOnSendRule" not in str(adapters._ras_error(809, "windows"))
    assert "authentication method the server expects" in str(adapters._ras_error(812, "pptp"))
    assert "TCP port 1723 and GRE" in str(adapters._ras_error(807, "pptp"))
    assert "1723" not in str(adapters._ras_error(807, "l2tp"))
    assert str(adapters._ras_error(691, "l2tp")).startswith("Windows could not connect the VPN (error 691): ")


def test_manual_dial_errors_carry_the_l2tp_hint():
    config = manual("l2tp")
    fake_ras = SimpleNamespace(EnumConnections=lambda: [], Dial=Mock(return_value=(0, 789)), HangUp=Mock(),
                               GetErrorString=lambda code: "The L2TP connection attempt failed.")
    with patch.dict("sys.modules", {"win32ras": fake_ras}):
        with pytest.raises(adapters.VpnError, match="Check the IPsec pre-shared key"):
            adapters._dial(config)


# ── Why the machine is silent ────────────────────────────────────


def test_port_answer_tells_a_refusal_from_silence():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert adapters._port_answer("127.0.0.1", listener.getsockname()[1]) == "open"
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]          # bound, never listening: refused
        assert adapters._port_answer("127.0.0.1", port, timeout=3) == "refused"
    assert adapters._port_answer("192.0.2.1", 3389, timeout=0.3) == "silent"   # TEST-NET-1: nothing there


def test_adapter_address_of_a_missing_adapter_is_empty():
    assert adapters.adapter_address("RPMote no such adapter") == ""


# ── Connect VPN now, in the settings window ──────────────────────


def gate_dialog(config, gate):
    dialog = vpn.TenantVpnDialog("Contoso", config, gate=gate)
    _WIDGETS.append(dialog)
    return dialog


def test_connect_now_shows_a_long_reason_in_full_and_lets_it_be_copied():
    dialog = vpn_dialog(manual())
    dialog.show()
    QApplication.processEvents()
    reason = adapters.wireguard_not_answering({"endpoint": "96.94.208.73:51820"})
    dialog._test_config = dialog.config()
    dialog._test_finished(reason, False)
    QApplication.processEvents()
    label = dialog.test_status
    assert label.text() == reason
    assert label.wordWrap() and label.geometry().right() <= dialog.width()
    assert label.height() >= label.heightForWidth(label.width())
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    dialog.reject()


def test_connect_now_goes_through_the_gate_and_closing_the_window_disconnects(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    dialog = gate_dialog(config, gate)
    dialog._test_clicked()
    wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
    tunnel = gate._tunnels["Contoso"]
    assert tunnel.owned and tunnel.waiting == 1      # held up by the window
    gate.sessions_changed()
    vpn_calls.disconnect.assert_not_called()
    dialog.reject()
    wait_until(lambda: vpn_calls.disconnect.called and "Contoso" not in gate._tunnels)
    vpn_calls.disconnect.assert_called_once_with(config)


def test_a_machine_opened_over_the_connect_now_vpn_keeps_it_up(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    dialog = gate_dialog(config, gate)
    dialog._test_clicked()
    wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
    vpn_calls.answers = True
    proceed = opens_session(window, "s1")
    assert gate.before_open({"tenant": "Contoso", "host": "server", "port": 22, "protocol": "ssh"}, proceed)
    wait_until(lambda: proceed.called)
    dialog.accept()
    QApplication.processEvents()
    vpn_calls.disconnect.assert_not_called()         # the session still needs it
    vpn_calls.connect.assert_called_once()           # and it was not dialled twice
    window.sessions.clear()
    gate.sessions_changed()
    wait_until(lambda: vpn_calls.disconnect.called)


def test_opening_a_machine_while_connect_now_disconnects_waits_and_reconnects(vpn_calls):
    """The race: the window's disconnect was invisible to the gate, so a machine
    that still answered opened without the VPN and lost it a moment later."""
    config = manual()
    window, gate = gate_for(config)
    dialog = gate_dialog(config, gate)
    dialog._test_clicked()
    wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
    slow = threading.Event()
    vpn_calls.disconnect.side_effect = lambda _config: slow.wait(5)
    vpn_calls.answers = True
    dialog.reject()                                   # disconnect under way
    assert gate._tunnels["Contoso"].state == "disconnecting"
    proceed = opens_session(window, "s1")
    assert gate.before_open({"tenant": "Contoso", "host": "server", "port": 22, "protocol": "ssh"}, proceed)
    QApplication.processEvents()
    assert not proceed.called                         # not opened on a VPN about to drop
    slow.set()
    wait_until(lambda: proceed.called)
    assert vpn_calls.connect.call_count == 2 and gate._tunnels["Contoso"].owned


def test_leave_connected_box_as_the_window_closes_wins(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    dialog = gate_dialog(config, gate)
    dialog._test_clicked()
    wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
    dialog.keep_connected.setChecked(True)            # ticked, then Save: not saved yet as it closes
    dialog.accept()
    QApplication.processEvents()
    time.sleep(0.05)
    vpn_calls.disconnect.assert_not_called()
    assert "Contoso" not in gate._tunnels


def test_connect_now_disconnect_button_goes_through_the_gate(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    dialog = gate_dialog(config, gate)
    dialog._test_clicked()
    wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
    dialog._test_clicked()
    wait_until(lambda: dialog.test_status.text() == "VPN disconnected.")
    assert "Contoso" not in gate._tunnels
    dialog.reject()
    QApplication.processEvents()
    vpn_calls.disconnect.assert_called_once_with(config)


def test_connect_now_failure_lets_go_of_the_gate(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    vpn_calls.connect_result = adapters.VpnError("Windows could not connect the VPN (error 691)")
    dialog = gate_dialog(config, gate)
    dialog._test_clicked()
    wait_until(lambda: "error 691" in dialog.test_status.text())
    assert dialog._held is None and "Contoso" not in gate._tunnels
    dialog.reject()
    vpn_calls.disconnect.assert_not_called()


def test_connect_now_of_a_different_vpn_than_the_one_in_use_still_waits_for_its_lock(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    tunnel = vpn._Tunnel("Contoso", manual(server="old.example"), set())
    tunnel.state, tunnel.owned = "up", True
    gate._tunnels["Contoso"] = tunnel
    dialog = gate_dialog(config, gate)
    with gate.lock("Contoso"):
        dialog._test_clicked()
        time.sleep(0.1)
        vpn_calls.connect.assert_not_called()         # queued behind the lock
    wait_until(lambda: dialog.btn_test.text() == "Disconnect VPN")
    assert gate._tunnels["Contoso"] is tunnel and dialog._held is None
    dialog.reject()


# ── The log ──────────────────────────────────────────────────────


def test_vpn_log_says_what_happened_without_secrets(caplog):
    config = manual("l2tp")
    caplog.set_level("INFO", logger="rpmote.vpn")
    with patch.object(adapters.sys, "platform", "win32"), \
         patch.object(adapters, "_prepare_windows_profile"), \
         patch.object(adapters, "_dial", side_effect=adapters._ras_error(789, "l2tp")):
        with pytest.raises(adapters.VpnError):
            adapters.connect(config)
    with patch.object(adapters.sys, "platform", "win32"), \
         patch.object(adapters, "_prepare_windows_profile"), \
         patch.object(adapters, "_dial", return_value=True), \
         patch.object(adapters, "adapter_address", return_value="10.100.20.2"):
        assert adapters.connect(config) is True
    text = caplog.text
    assert "connect failed | l2tp vpn.contoso.example" in text and "error 789" in text
    assert "brought up | address 10.100.20.2" in text
    assert config["password"].strip() not in text and config["psk"] not in text and "tech" not in text


def test_unexpected_errors_are_logged_by_type_only(caplog):
    caplog.set_level("INFO", logger="rpmote.vpn")
    error = OSError("RasDial('Contoso', 'tech', ' password & secret')")
    error.winerror = 87
    with patch.object(adapters, "_connect", side_effect=error):
        with pytest.raises(OSError):
            adapters.connect(manual("pptp", psk=""))
    assert "OSError 87" in caplog.text and "password" not in caplog.text


def test_wait_outcome_is_logged(vpn_calls, caplog):
    caplog.set_level("INFO", logger="rpmote.vpn")
    config = manual()
    window, gate = gate_for(config)
    gate._bring_up_and_wait(job(config))
    wait_until(lambda: gate._tunnels["Contoso"].state == "up")
    dialog, = wait_dialogs(window)
    dialog.reject()
    wait_until(lambda: "Contoso" not in gate._tunnels)
    assert "wait | server:22 cancelled after" in caplog.text
    assert "gate | Contoso: nothing uses the VPN now; disconnecting" in caplog.text


def test_wait_dialog_shows_a_tenant_name_as_text():
    dialog = vpn._WaitDialog("R&D <b>Lab</b>", "server:22", 30)
    _WIDGETS.append(dialog)
    assert "R&amp;D &lt;b&gt;Lab&lt;/b&gt;" in dialog.label.text()


# ── What this PC actually dials ──────────────────────────────────


def test_gate_waits_for_the_rd_gateway_not_the_machine_behind_it(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    probe = Mock(return_value=(True, 1, ""))
    proceed = Mock()
    conn = {"tenant": "Contoso", "protocol": "rdp", "host": "10.0.0.5", "port": 3389,
            "rdp_gateway": "gw.contoso.example:8443"}
    with patch.object(vpn, "tcp_probe", probe):
        assert gate.before_open(conn, proceed)
        wait_until(lambda: proceed.called)
    assert probe.call_args.args[:2] == ("gw.contoso.example", 8443)
    vpn_calls.connect.assert_not_called()         # the gateway answers as it is: no VPN needed


def test_gate_waits_for_the_outermost_jump_host(vpn_calls):
    config = manual()
    window, gate = gate_for(config)
    bastion = {"id": "b1", "name": "Bastion", "host": "bastion.contoso.example", "port": 2222, "protocol": "ssh"}
    inner = {"id": "j1", "name": "Inner", "host": "10.1.0.9", "port": 22, "protocol": "ssh", "jump_via": "b1"}
    window._safe_connections = lambda: [bastion, inner]
    conn = {"tenant": "Contoso", "protocol": "ssh", "host": "10.1.0.50", "port": 22, "jump_via": "j1"}
    assert gate.before_open(conn, Mock())
    wait_until(lambda: wait_dialogs(window))
    dialog, = wait_dialogs(window)
    assert "bastion.contoso.example:2222 (jump host)" in dialog.detail.text()
    dialog.reject()


def test_plain_connections_still_wait_for_the_machine_itself(vpn_calls):
    window, gate = gate_for(manual())
    assert gate._first_hop({"protocol": "rdp", "host": "10.0.0.5", "port": 3389}) == ("10.0.0.5", 3389, "")
    assert gate._first_hop({"protocol": "ssh", "host": "srv", "jump_via": "gone"}) == ("srv", 22, "")


# ── Two tenants set to one VPN ───────────────────────────────────


def shared_vpn():
    return vpn.normalize({"kind": "windows", "profile_name": "Shared VPN"})


def test_vpn_identity_tells_one_shared_vpn_from_two():
    assert vpn.vpn_identity(shared_vpn()) == vpn.vpn_identity(vpn.normalize(
        {"kind": "windows", "profile_name": "shared vpn"}))
    assert vpn.vpn_identity(vpn.normalize({"kind": "wireguard", "config_file": "C:\\a\\office.conf"})) == \
        vpn.vpn_identity(vpn.normalize({"kind": "wireguard", "config_file": "D:\\b\\Office.conf"}))
    assert vpn.vpn_identity(manual()) != vpn.vpn_identity(manual(profile_id="b" * 32))


def test_a_vpn_shared_by_two_tenants_stays_up_while_either_uses_it(vpn_calls):
    window, gate = gate_for(shared_vpn(), Fabrikam=shared_vpn())
    gate._bring_up_and_wait(job(shared_vpn(), opens_session(window, "s1")))
    vpn_calls.answers = True
    wait_until(lambda: "s1" in window.sessions)
    # Fabrikam's machine answers at once, over the VPN Contoso brought up.
    second = opens_session(window, "s2", tenant="Fabrikam")
    assert gate.before_open({"tenant": "Fabrikam", "host": "fab-server", "port": 22, "protocol": "ssh"}, second)
    wait_until(lambda: second.called)
    del window.sessions["s1"]
    gate.sessions_changed()
    QApplication.processEvents()
    vpn_calls.disconnect.assert_not_called()      # Fabrikam's session still runs over it
    del window.sessions["s2"]
    gate.sessions_changed()
    wait_until(lambda: vpn_calls.disconnect.called)
    vpn_calls.connect.assert_called_once()


def test_a_tenant_still_waiting_on_a_shared_vpn_takes_it_over(vpn_calls):
    window, gate = gate_for(shared_vpn(), Fabrikam=shared_vpn())
    vpn_calls.answers = True
    gate._bring_up_and_wait(job(shared_vpn(), opens_session(window, "s1")))
    wait_until(lambda: "s1" in window.sessions)
    vpn_calls.answers, vpn_calls.connect_result = False, False   # up already, for Fabrikam
    gate._bring_up_and_wait(job(shared_vpn(), tenant="Fabrikam", host="fab-server"))
    wait_until(lambda: gate._tunnels["Fabrikam"].state == "up")
    del window.sessions["s1"]
    gate.sessions_changed()                       # Contoso is done; Fabrikam is still waiting
    QApplication.processEvents()
    vpn_calls.disconnect.assert_not_called()
    assert "Contoso" not in gate._tunnels and gate._tunnels["Fabrikam"].owned
    for dialog in wait_dialogs(window):
        dialog.reject()                           # Fabrikam gives up: now it goes
    wait_until(lambda: vpn_calls.disconnect.called)


def test_quitting_keeps_a_shared_vpn_a_remote_desktop_window_still_uses(vpn_calls):
    window, gate = gate_for(shared_vpn(), Fabrikam=shared_vpn())
    tunnel = vpn._Tunnel("Contoso", shared_vpn(), set())
    tunnel.state, tunnel.owned = "up", True
    gate._tunnels["Contoso"] = tunnel
    gate.shutdown({"mstsc-window": "Fabrikam"})
    vpn_calls.disconnect.assert_not_called()
