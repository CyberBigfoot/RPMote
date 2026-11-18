"""Azure connection state, isolated PowerShell execution, and clean sign-out."""

import base64
import codecs
import os
import re
import shutil
import subprocess
import time
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QProcess, Qt
from PyQt6.QtWidgets import QApplication, QListWidgetItem
from PyQt6.QtTest import QTest

from app.azure_commands_library import get_azure_command_library, get_disconnect_commands
from app.azure_powershell_terminal import AzurePowerShellTerminal
from app.azure_shell import AzureShellConfig, encode_command, host_arguments
from app.connection_manager import ConnectionDialog

APP = QApplication.instance() or QApplication([])


@pytest.fixture
def terminal():
    with patch.object(AzurePowerShellTerminal, "_start_shell"):
        widget = AzurePowerShellTerminal({"azure_services": ["azure", "graph"]})
    widget._token = "test"
    widget._marker_re = re.compile(r"__RPMOTE_test\|(.+?)__")
    widget._stdout_decoder = codecs.getincrementaldecoder("utf-8")("replace")
    widget._stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
    widget._stdout_buffer = widget._stderr_buffer = ""
    widget.process = Mock()
    widget.process.state.return_value = QProcess.ProcessState.Running
    widget.process.write.return_value = 1
    yield widget
    widget.process = None
    widget._shutdown_timer.stop()
    widget.deleteLater()


def test_shell_start_is_not_authentication_success(terminal):
    established = Mock()
    terminal.connection_established.connect(established)
    terminal._on_started()
    assert not terminal._connected
    established.assert_not_called()
    terminal._handle_marker("SERVICE|azure|OK")
    terminal._handle_marker("SERVICE|graph|FAILED")
    terminal._handle_marker("CONNECT_DONE")
    assert terminal._connected
    assert "Partially connected" in terminal.status_text.text()
    established.assert_called_once()


def test_failed_sign_in_stays_open_and_can_retry(terminal):
    terminal._shell_ready = True
    lost, closed = Mock(), Mock()
    terminal.connection_lost.connect(lost)
    terminal.session_closed.connect(closed)
    terminal._handle_marker("SERVICE|azure|FAILED")
    terminal._handle_marker("SERVICE|graph|FAILED")
    terminal._handle_marker("CONNECT_DONE")
    assert not terminal._connected
    assert terminal.btn_connect.isEnabled()
    lost.assert_called_once()
    closed.assert_not_called()


def test_split_status_markers_and_secrets_are_not_displayed(terminal):
    terminal.connection_info["azure_client_secret"] = "hidden-secret"
    terminal._consume_output("ordinary output\n__RPMOTE_te")
    terminal._consume_output("st|SERVICE|azure|OK__\nhidden-")
    terminal._consume_output("secret\n__RPMOTE_test|CONNECT_DONE__\n")
    assert terminal._connected
    output = terminal.output.toPlainText()
    assert "ordinary output" in output
    assert "[redacted]" in output
    assert "hidden-secret" not in output
    assert "__RPMOTE" not in output


def test_commands_need_filled_placeholders_and_explicit_run(terminal):
    terminal._shell_ready = True
    terminal.command_input.setText("Start-AzVM -Name '<vm-name>'")
    terminal._run_user_command()
    terminal.process.write.assert_not_called()
    item = QListWidgetItem()
    item.setData(Qt.ItemDataRole.UserRole, {"command": "Get-AzVM"})
    terminal._on_palette_item_run(item)
    terminal.process.write.assert_not_called()
    terminal._run_user_command()
    packet = terminal.process.write.call_args.args[0]
    assert "Get-AzVM" in base64.b64decode(packet).decode("utf-8")
    assert terminal._busy == "command"
    terminal._handle_marker("COMMAND_DONE")
    assert not terminal._busy


def test_retry_does_not_prompt_already_connected_services(terminal):
    terminal._service_status["azure"] = True
    sequence = "; ".join(terminal._connect_sequence())
    assert "Connect-AzAccount" not in sequence
    assert "Connect-MgGraph" in sequence


def test_disconnect_waits_for_exit_and_emits_closed_once(terminal):
    closed = Mock()
    terminal.session_closed.connect(closed)
    terminal._shell_ready = True
    terminal._disconnect()
    assert terminal._shutdown_timer.isActive()
    assert not terminal.btn_run.isEnabled()
    terminal.process.closeWriteChannel.assert_called_once()
    terminal.process.write.assert_not_called()
    closed.assert_not_called()
    terminal._emit_closed()
    terminal._emit_closed()
    closed.assert_called_once()


def test_cleanup_allows_sign_out_before_force_kill(terminal):
    process = terminal.process
    process.waitForFinished.return_value = False
    terminal.cleanup()
    assert process.waitForFinished.call_args_list[0].args == (15000,)
    process.kill.assert_called_once()
    assert "sign-out could not be verified" in terminal.output.toPlainText()
    count = process.write.call_count
    terminal.cleanup()
    assert process.write.call_count == count


def test_signout_failure_does_not_report_success(terminal):
    terminal._connected = True
    terminal._service_status["azure"] = True
    terminal._busy = "disconnect"
    terminal._handle_marker("DISCONNECT_FAILED")
    assert terminal._connected
    assert "could not sign out" in terminal.status_text.text()
    assert not terminal._busy


def test_close_guard_counts_an_open_azure_shell_before_signin(terminal):
    from app.main_window import MainWindow
    assert not terminal._connected
    assert MainWindow._tab_is_live(terminal)
    terminal.process.state.return_value = QProcess.ProcessState.NotRunning
    assert not MainWindow._tab_is_live(terminal)


def test_prompt_output_and_replies_are_not_buffered_or_echoed(terminal):
    terminal._shell_ready = True
    terminal._busy = "command"
    terminal._update_controls()
    terminal._consume_output("Continue? [Y/N]: ")
    assert "Continue? [Y/N]: " in terminal.output.toPlainText()
    assert terminal.btn_reply.isEnabled()
    terminal.command_input.setText("Y")
    terminal._run_user_command()
    terminal.process.write.assert_called_once_with(b"Y\n")
    assert terminal.command_input.text() == ""
    assert not terminal.output.toPlainText().endswith("Y\n")


def test_service_principal_never_falls_back_to_an_interactive_account():
    config = AzureShellConfig({"azure_auth_mode": "service_principal", "azure_services": ["azure", "graph", "exchange"]})
    commands = "; ".join(config.connect_commands("test"))
    assert "Connect-AzAccount" not in commands
    assert "Connect-MgGraph" not in commands
    assert "Connect-ExchangeOnline" not in commands
    assert "requires Tenant ID, Client ID, and Client Secret" in commands
    assert "Client-secret sign-in supports Azure and Graph only" in commands


def test_auth_flows_are_isolated_and_support_clouds():
    config = AzureShellConfig({
        "azure_auth_mode": "device_code", "azure_cloud": "AzureUSGovernment",
        "azure_tenant_id": "tenant-id", "azure_organization": "contoso.onmicrosoft.com",
        "azure_use_delegated_org": True,
        "azure_pnp_client_id": "app-id", "azure_services": ["azure", "graph", "exchange", "teams", "spo", "compliance"],
    })
    commands = "; ".join(config.connect_commands("test"))
    for expected in ("-Scope Process", "-ContextScope Process", "-UseDeviceAuthentication", "-UseDeviceCode", "-Device", "-DeviceLogin", "-Environment 'USGov'", "-TeamsEnvironmentName 'TeamsGCCH'", "-ExchangeEnvironmentName 'O365USGovGCCHigh'", "contoso-admin.sharepoint.us", "-ClientId 'app-id'", "-DelegatedOrganization", "office365.us/powershell-liveid"):
        assert expected in commands
    assert "-Organization 'contoso" not in commands


def test_direct_exchange_signin_does_not_require_customer_delegation():
    config = AzureShellConfig({"azure_services": ["exchange"], "azure_organization": "contoso.onmicrosoft.com", "azure_upn": "admin@contoso.com"})
    commands = "; ".join(config.connect_commands("test"))
    assert "-UserPrincipalName 'admin@contoso.com'" in commands
    assert "-DelegatedOrganization" not in commands
    assert "IsEopSession -eq $false" in commands


def test_graph_interactive_has_a_broker_failure_fallback():
    config = AzureShellConfig({"azure_services": ["graph"], "azure_client_id": "custom-graph-app"})
    commands = "; ".join(config.connect_commands("test"))
    assert "window handle|WAM|broker" in commands
    assert "-UseDeviceCode" in commands
    assert "-ClientId 'custom-graph-app'" in commands


def test_sharepoint_requires_an_app_and_does_not_guess_vanity_domains():
    config = AzureShellConfig({"azure_organization": "contoso.com", "azure_services": ["spo"]})
    assert config.sharepoint_url() == ""
    assert "Connect-PnPOnline" not in "; ".join(config.connect_commands("test"))
    assert AzureShellConfig({"azure_organization": "contoso.onmicrosoft.com"}).sharepoint_url() == "https://contoso-admin.sharepoint.com"
    with pytest.raises(ValueError):
        AzureShellConfig({"azure_sharepoint_url": "http://contoso.sharepoint.com"}).sharepoint_url()


def test_command_palette_covers_major_workloads_and_current_cmdlets():
    commands = get_azure_command_library()
    assert len(commands) >= 140
    assert len({(c["category"], c["name"]) for c in commands}) == len(commands)
    content = "\n".join(c["command"] for c in commands)
    for cmdlet in ("Get-AzResourceGroup", "Start-AzVM", "Stop-AzVM", "Restart-AzVM", "Get-AzStorageAccount", "Get-AzVirtualNetwork", "New-AzRoleAssignment", "Get-AzKeyVaultSecret", "Get-AzMetric", "Get-AzWebApp", "Get-MessageTraceV2"):
        assert cmdlet in content
    assert "Install-Module Az," in content
    assert "Get-MessageTrace -" not in content
    assert all(c["description"] for c in commands)
    cleanup = "; ".join(get_disconnect_commands())
    assert "-ListImported" in cleanup  # never load another user's cached auth to sign it out
    assert "-Scope Process" in cleanup
    for cmdlet in ("Disconnect-AzAccount", "Disconnect-ExchangeOnline", "Disconnect-MgGraph", "Disconnect-MicrosoftTeams", "Disconnect-PnPOnline", "Remove-PSSession"):
        assert cmdlet in cleanup


def test_dialog_preserves_sharepoint_settings_and_limits_secret_auth():
    dialog = ConnectionDialog(connection={
        "name": "Azure", "protocol": "azure_ps", "azure_services": ["spo"],
        "azure_sharepoint_url": "https://contoso-admin.sharepoint.com", "azure_pnp_client_id": "pnp-app",
    })
    try:
        dialog._save()
        assert dialog.get_result()["azure_sharepoint_url"] == "https://contoso-admin.sharepoint.com"
        assert dialog.get_result()["azure_pnp_client_id"] == "pnp-app"
        dialog.azure_auth_mode.setCurrentText("Service Principal")
        assert not dialog.azure_service_checks["spo"].isEnabled()
        assert not dialog.azure_service_checks["spo"].isChecked()
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_real_host_preserves_variables_and_signs_out_on_exit(shell):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} is not installed")
    script = """
function global:Disconnect-MgGraph { [CmdletBinding()] param(); throw 'test graph cleanup failure' }
function global:Disconnect-AzAccount { [CmdletBinding()] param($Scope); Write-Output ('AZ_SIGNOUT:' + $Scope) }
function global:Disconnect-PnPOnline { [CmdletBinding()] param(); Write-Host 'PNP_SIGNOUT' }
function global:Clear-AzContext { [CmdletBinding()] param($Scope, [switch]$Force); Write-Host ('AZ_CLEAR:' + $Scope) }
$taskValue = 'tenant-specific value'
"""
    packet = (
        encode_command(script) + encode_command("Write-Output $taskValue")
        + encode_command("$secret = Read-Host -Prompt 'Password' -AsSecureString; Write-Output ('SECRET_LENGTH:' + $secret.Length); $secret.Dispose()")
        + b"never-log-this\n" + encode_command("exit")
    )
    result = subprocess.run([executable, *host_arguments("test")], input=packet, capture_output=True, timeout=45)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, stderr
    assert "__RPMOTE_test|READY__" in stdout
    assert "tenant-specific value" in stdout
    assert "PNP_SIGNOUT" in stdout
    assert "test graph cleanup failure" in stdout + stderr
    assert "AZ_CLEAR:Process" in stdout
    assert "SECRET_LENGTH:14" in stdout
    assert "never-log-this" not in stdout + stderr
    assert "tenant-specific value" not in stderr


def test_generated_scripts_parse_in_powershell():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell is not installed")
    scripts = [base64.b64decode(host_arguments("test")[-1]).decode("utf-16-le")]
    for cloud in ("AzureCloud", "AzureUSGovernment", "AzureChinaCloud", "AzureGermanCloud"):
        for mode in ("interactive", "device_code", "service_principal"):
            config = AzureShellConfig({
                "azure_services": ["azure", "exchange", "graph", "teams", "spo", "compliance"],
                "azure_tenant_id": "11111111-1111-1111-1111-111111111111", "azure_organization": "contoso.onmicrosoft.com",
                "azure_subscription_id": "22222222-2222-2222-2222-222222222222", "azure_client_id": "app-id",
                "azure_client_secret": "secret'with\nquotes", "azure_pnp_client_id": "pnp-id", "azure_cloud": cloud, "azure_auth_mode": mode,
            })
            scripts.extend(config.connect_commands("test"))
            scripts.append(config.preflight_script())
    scripts.extend(re.sub(r"<[^<>]+>", "1", entry["command"]) for entry in get_azure_command_library())
    packets = []
    for script in scripts:
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        packets.append(encode_command(
            f"$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
            "$parseErrors = $null; $tokens = $null; [void][Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$parseErrors); "
            "if ($parseErrors.Count) { Write-Output ('PARSE_ERROR: ' + $parseErrors[0].Message) }"
        ))
    result = subprocess.run([executable, *host_arguments("parse")], input=b"".join(packets) + encode_command("exit"), capture_output=True, timeout=45)
    assert result.returncode == 0
    assert "PARSE_ERROR" not in result.stdout.decode("utf-8", errors="replace")
    assert not result.stderr


def _wait_until(predicate, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(20)
    raise AssertionError("Timed out waiting for the PowerShell host")


def test_qprocess_connect_partial_failure_prompt_reconnect_and_disconnect():
    if not (shutil.which("pwsh") or shutil.which("powershell")):
        pytest.skip("PowerShell is not installed")
    bootstrap = """
function global:Get-Module { param($Name,[switch]$ListAvailable); [pscustomobject]@{Name=$Name} }
function global:Import-Module { [CmdletBinding()] param($Name) }
function global:Disable-AzContextAutosave { [CmdletBinding()] param($Scope) }
function global:Update-AzConfig { [CmdletBinding()] param($Scope,$EnableLoginByWam,$LoginExperienceV2) }
function global:Clear-AzContext { [CmdletBinding()] param($Scope,[switch]$Force); $global:mockAz=$null; Write-Host 'MOCK_AZ_CLEARED' }
function global:Connect-AzAccount {
    [CmdletBinding()] param($Scope,$Environment,$Tenant,$Subscription)
    $global:mockAz = [pscustomobject]@{Account=@{Id='admin@example.test'};Tenant=@{Id='tenant'};Subscription=@{Id='subscription'}}
}
function global:Get-AzContext { [CmdletBinding()] param(); $global:mockAz }
function global:Connect-MgGraph { [CmdletBinding()] param($ContextScope,[switch]$NoWelcome,$Environment,$Scopes); Write-Error 'MOCK_GRAPH_FAILURE' }
function global:Disconnect-AzAccount { [CmdletBinding()] param($Scope); Write-Host 'MOCK_AZ_SIGNED_OUT'; $global:mockAz=$null }
function global:Disconnect-MgGraph { [CmdletBinding()] param(); Write-Host 'MOCK_GRAPH_SIGNED_OUT' }
"""
    def preflight(widget):
        widget._send_data(bootstrap)
    with patch.object(AzurePowerShellTerminal, "_send_preflight", preflight):
        widget = AzurePowerShellTerminal({"azure_services": ["azure", "graph"]})
        closed = Mock()
        widget.session_closed.connect(closed)
        try:
            _wait_until(lambda: widget._connected and not widget._busy)
            assert "Partially connected" in widget.status_text.text()
            assert "MOCK_GRAPH_FAILURE" in widget.output.toPlainText()
            widget.command_input.setText("$taskValue = 37")
            widget._run_user_command()
            _wait_until(lambda: not widget._busy)
            widget.command_input.setText("Write-Output $taskValue")
            widget._run_user_command()
            _wait_until(lambda: not widget._busy)
            assert "37" in widget.output.toPlainText()
            widget.command_input.setText("$answer = Read-Host 'MOCK_PROMPT'; Write-Output ('REPLY:' + $answer)")
            widget._run_user_command()
            try:
                _wait_until(lambda: "MOCK_PROMPT:" in widget.output.toPlainText(), seconds=5)
            except AssertionError:
                pytest.fail(widget.output.toPlainText())
            widget.command_input.setText("yes")
            widget._send_prompt_reply()
            _wait_until(lambda: not widget._busy)
            assert "REPLY:yes" in widget.output.toPlainText()
            old_process = widget.process
            widget.reconnect()
            _wait_until(lambda: widget.process is not old_process and widget._connected and not widget._busy)
            closed.assert_not_called()
            assert "MOCK_AZ_SIGNED_OUT" in widget.output.toPlainText()
            widget.command_input.setText("Read-Host 'DISCONNECT_PROMPT'")
            widget._run_user_command()
            _wait_until(lambda: "DISCONNECT_PROMPT:" in widget.output.toPlainText())
            widget._disconnect()
            _wait_until(lambda: widget.process is None)
            closed.assert_called_once()
            assert widget.output.toPlainText().count("MOCK_AZ_SIGNED_OUT") == 2
            assert "Forcing the shell" not in widget.output.toPlainText()
        finally:
            widget.cleanup()
            widget.deleteLater()
