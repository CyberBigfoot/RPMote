"""PowerShell host and authentication scripts for the Azure admin console."""

from __future__ import annotations

import base64
import re
from urllib.parse import urlsplit

from app.azure_commands_library import AZURE_SERVICES, DEFAULT_AZURE_SERVICES, get_disconnect_commands


GRAPH_SCOPES = (
    "User.ReadWrite.All", "Group.ReadWrite.All", "Directory.ReadWrite.All",
    "Organization.Read.All", "LicenseAssignment.ReadWrite.All",
    "UserAuthenticationMethod.Read.All", "User.RevokeSessions.All",
)

_CLOUDS = {
    "AzureCloud": ("Global", "O365Default", "", "Production", "sharepoint.com"),
    "AzureUSGovernment": ("USGov", "O365USGovGCCHigh", "TeamsGCCH", "USGovernmentHigh", "sharepoint.us"),
    "AzureChinaCloud": ("China", "O365China", "TeamsChina", "China", "sharepoint.cn"),
}


def ps_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def encode_command(script: str) -> bytes:
    """Transport commands through stdin without shell prompts echoing secrets."""
    return base64.b64encode(script.encode("utf-8")) + b"\n"


def marker(token: str, event: str) -> str:
    return f"[Console]::Out.WriteLine({ps_quote(f'__RPMOTE_{token}|{event}__')})"


def host_arguments(token: str) -> list[str]:
    # A profile-free host keeps each tenant independent. Dot sourcing preserves
    # module connections and variables between submitted commands. Finally also
    # runs for a user-entered `exit` or stdin EOF.
    cleanup = "; ".join(get_disconnect_commands())
    read_host = (
        "function global:Read-Host { [CmdletBinding(DefaultParameterSetName='Plain')] "
        "param([Parameter(Position=0)][object]$Prompt, "
        "[Parameter(ParameterSetName='Secure')][switch]$AsSecureString, "
        "[Parameter(ParameterSetName='Mask')][switch]$MaskInput); "
        "if ($AsSecureString -or $MaskInput) { " + marker(token, "PROMPT_SECURE") + " }; "
        "if ($null -ne $Prompt) { [Console]::Out.Write(([string]$Prompt) + ': ') }; "
        "[Console]::Out.Flush(); $response = [Console]::ReadLine(); "
        "if ($null -eq $response) { throw [OperationCanceledException]::new('PowerShell input was closed.') }; "
        "if ($AsSecureString) { $secure = [Security.SecureString]::new(); "
        "foreach ($character in ([string]$response).ToCharArray()) { $secure.AppendChar($character) }; "
        "$secure.MakeReadOnly(); $response = $null; return $secure }; return $response }; "
    )
    script = (
        "[Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [Console]::OutputEncoding; "
        "$ProgressPreference = 'SilentlyContinue'; $ErrorActionPreference = 'Continue'; "
        "if (Get-Variable PSStyle -ErrorAction SilentlyContinue) { $PSStyle.OutputRendering = 'PlainText' }; "
        "$global:rpmoteServices = @{}; " + read_host +
        f"{marker(token, 'READY')}; "
        "try { while ($null -ne ($rpmoteLine = [Console]::ReadLine())) { "
        "try { $rpmoteScript = [ScriptBlock]::Create([Text.Encoding]::UTF8.GetString("
        "[Convert]::FromBase64String($rpmoteLine))); . $rpmoteScript | Out-Default } "
        "catch { Write-Error -Message $_.Exception.Message -ErrorAction Continue } "
        "finally { $rpmoteLine = $null; $rpmoteScript = $null } } } "
        f"finally {{ {cleanup} }}"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # Text output avoids CLIXML on stderr from -EncodedCommand.
    # Native ConsoleHost prompts bypass redirected output. Read-Host is bridged
    # above; other console-only prompts must fail visibly instead of hanging.
    # Browser/device-code authentication remains available.
    return ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-OutputFormat", "Text", "-EncodedCommand", encoded]


class AzureShellConfig:
    def __init__(self, info: dict):
        self.info = dict(info or {})
        raw = self.info.get("azure_services")
        if isinstance(raw, str):
            raw = raw.split(",")
        if not isinstance(raw, (list, tuple, set)) or not raw:
            raw = DEFAULT_AZURE_SERVICES
        selected = {str(s).strip().lower() for s in raw}
        self.services = [s for s in AZURE_SERVICES if s in selected]

    def value(self, key: str, default: str = "") -> str:
        return str(self.info.get(key) or default).strip()

    def sharepoint_url(self) -> str:
        explicit = self.value("azure_sharepoint_url")
        if explicit:
            parsed = urlsplit(explicit)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("SharePoint URL must be an HTTPS site/admin URL without credentials, query, or fragment.")
            return explicit.rstrip("/")
        organization = self.value("azure_organization").lower()
        match = re.fullmatch(r"([a-z0-9][a-z0-9-]*)\.onmicrosoft\.(com|us|cn)", organization)
        cloud = self.value("azure_cloud", "AzureCloud")
        if match and cloud in _CLOUDS:
            return f"https://{match[1]}-admin.{_CLOUDS[cloud][4]}"
        return ""

    def preflight_script(self) -> str:
        modules = sorted({AZURE_SERVICES[s]["module"] for s in self.services})
        commands = ["Write-Host ('PowerShell ' + $PSVersionTable.PSVersion)"]
        for module in modules:
            commands.append(
                f"if (Get-Module -ListAvailable -Name {ps_quote(module)}) {{ "
                f"Write-Host '[ok] {module}' }} else {{ Write-Warning "
                f"'Missing {module}. Install-Module {module} -Scope CurrentUser' }}"
            )
        if "azure" in self.services:
            commands.append(
                "if (-not (Get-Module -ListAvailable -Name Az)) { Write-Warning "
                "'Azure task cmdlets require the Az modules: Install-Module Az -Scope CurrentUser' }"
            )
        if "graph" in self.services:
            commands.append(
                "foreach ($module in @('Microsoft.Graph.Users','Microsoft.Graph.Users.Actions',"
                "'Microsoft.Graph.Groups','Microsoft.Graph.Identity.DirectoryManagement',"
                "'Microsoft.Graph.Identity.SignIns')) { "
                "if (-not (Get-Module -ListAvailable -Name $module)) { "
                "Write-Warning ('Missing task module ' + $module + '. Install-Module Microsoft.Graph -Scope CurrentUser') } }"
            )
        return "; ".join(commands)

    def _service_script(self, service: str) -> str:
        mode = self.value("azure_auth_mode", "interactive").lower()
        tenant = self.value("azure_tenant_id") or self.value("azure_organization")
        organization = self.value("azure_organization")
        sub = self.value("azure_subscription_id")
        cloud = self.value("azure_cloud", "AzureCloud")
        upn = self.value("azure_upn")
        client = self.value("azure_client_id")
        secret = str(self.info.get("azure_client_secret") or "")
        if mode not in ("interactive", "device_code", "service_principal"):
            raise ValueError(f"Unknown authentication mode: {mode}")
        if cloud not in _CLOUDS and not (service == "azure" and cloud == "AzureGermanCloud"):
            raise ValueError(f"Unsupported cloud for {AZURE_SERVICES[service]['label']}: {cloud}")
        if mode == "service_principal":
            if service not in ("azure", "graph"):
                raise ValueError("Client-secret sign-in supports Azure and Graph only. Use a separate interactive/device-code connection for this service; certificate sign-in is required for app-only access.")
            if not all((tenant, client, secret)):
                raise ValueError("Service Principal requires Tenant ID, Client ID, and Client Secret.")
        module = AZURE_SERVICES[service]["module"]
        parts = [
            f"if (-not (Get-Module -ListAvailable -Name {ps_quote(module)})) {{ throw 'Missing {module}. Install-Module {module} -Scope CurrentUser' }}",
            f"Import-Module {ps_quote(module)} -ErrorAction Stop",
        ]
        if mode == "service_principal":
            parts.append(
                f"$rpmoteSpSecret = ConvertTo-SecureString {ps_quote(secret)} -AsPlainText -Force; "
                f"$rpmoteSpCred = [Management.Automation.PSCredential]::new({ps_quote(client)}, $rpmoteSpSecret)"
            )

        if service == "azure":
            parts.extend([
                "Disable-AzContextAutosave -Scope Process -ErrorAction Stop | Out-Null",
                "Clear-AzContext -Scope Process -Force -ErrorAction Stop | Out-Null",
                "$configCmd = Get-Command Update-AzConfig -ErrorAction SilentlyContinue; "
                "if ($configCmd) { $configArgs = @{Scope='Process'; ErrorAction='Stop'}; "
                "if ($configCmd.Parameters.ContainsKey('EnableLoginByWam')) { $configArgs.EnableLoginByWam = $false }; "
                "if ($configCmd.Parameters.ContainsKey('LoginExperienceV2')) { $configArgs.LoginExperienceV2 = 'Off' }; "
                "Update-AzConfig @configArgs | Out-Null }",
            ])
            cmd = f"Connect-AzAccount -Scope Process -Environment {ps_quote(cloud)} -ErrorAction Stop"
            if tenant:
                cmd += f" -Tenant {ps_quote(tenant)}"
            if sub:
                cmd += f" -Subscription {ps_quote(sub)}"
            if mode == "service_principal":
                cmd += " -ServicePrincipal -Credential $rpmoteSpCred"
            elif mode == "device_code":
                cmd += " -UseDeviceAuthentication"
            elif upn:
                cmd += f" -AccountId {ps_quote(upn)}"
            parts.extend([
                cmd + " | Out-Null",
                "$ctx = Get-AzContext -ErrorAction Stop; if (-not $ctx.Account -or -not $ctx.Tenant) { throw 'Azure returned no authenticated context.' }",
            ])
            if sub:
                parts.append(f"if ($ctx.Subscription.Id -ne {ps_quote(sub)}) {{ throw 'Azure connected to a different subscription.' }}")
            if re.fullmatch(r"[0-9a-fA-F-]{36}", tenant):
                parts.append(f"if ($ctx.Tenant.Id -ne {ps_quote(tenant)}) {{ throw 'Azure connected to a different tenant.' }}")
            parts.append("Write-Host ('Azure account: ' + $ctx.Account.Id + ' | Tenant: ' + $ctx.Tenant.Id + ' | Subscription: ' + $ctx.Subscription.Id)")

        elif service == "graph":
            cmd = f"Connect-MgGraph -ContextScope Process -NoWelcome -Environment {ps_quote(_CLOUDS[cloud][0])} -ErrorAction Stop"
            if tenant:
                cmd += f" -TenantId {ps_quote(tenant)}"
            if mode == "service_principal":
                cmd += " -ClientSecretCredential $rpmoteSpCred"
            else:
                if client:
                    cmd += f" -ClientId {ps_quote(client)}"
                cmd += " -Scopes " + ",".join(ps_quote(s) for s in GRAPH_SCOPES)
                if mode == "device_code":
                    cmd += " -UseDeviceCode"
            if mode == "interactive":
                # Current Graph SDKs may require a native WAM window handle.
                # Device codes preserve delegated sign-in in an embedded host.
                parts.append(
                    f"try {{ {cmd} | Out-Null }} catch {{ "
                    "if ($_.Exception.Message -match '(?i)window handle|WAM|broker') { "
                    "Write-Warning 'Graph browser/broker sign-in is unavailable in this shell. Retrying with a device code.'; "
                    f"{cmd} -UseDeviceCode | Out-Null }} else {{ throw }} }}"
                )
            else:
                parts.append(cmd + " | Out-Null")
            parts.extend([
                "$ctx = Get-MgContext -ErrorAction Stop; if (-not $ctx.TenantId) { throw 'Graph returned no authenticated context.' }",
            ])
            if re.fullmatch(r"[0-9a-fA-F-]{36}", tenant):
                parts.append(f"if ($ctx.TenantId -ne {ps_quote(tenant)}) {{ throw 'Graph connected to a different tenant.' }}")
            parts.append("Write-Host ('Graph account: ' + $ctx.Account + ' | Tenant: ' + $ctx.TenantId + ' | Auth: ' + $ctx.AuthType)")

        elif service in ("exchange", "compliance"):
            cmd = "Connect-ExchangeOnline -ShowBanner:$false" if service == "exchange" else "Connect-IPPSSession -ShowBanner:$false"
            # Organization is for app-only authentication. DelegatedOrganization
            # explicitly selects a customer tenant for delegated administration.
            if self.info.get("azure_use_delegated_org"):
                if not organization:
                    raise ValueError("Delegated customer access requires an Organization domain.")
                cmd += f" -DelegatedOrganization {ps_quote(organization)}"
            if upn:
                cmd += f" -UserPrincipalName {ps_quote(upn)}"
            if service == "exchange":
                cmd += f" -ExchangeEnvironmentName {ps_quote(_CLOUDS[cloud][1])}"
                if mode == "device_code":
                    parts.append("if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Exchange device-code sign-in requires PowerShell 7.' }")
                    cmd += " -Device"
            else:
                if mode == "device_code":
                    parts.append("Write-Warning 'Security & Compliance uses browser sign-in; its cmdlet does not support device codes.'")
                if cloud != "AzureCloud":
                    uri, authority = (
                        ("https://ps.compliance.protection.office365.us/powershell-liveid/", "https://login.microsoftonline.us/organizations")
                        if cloud == "AzureUSGovernment" else
                        ("https://ps.compliance.protection.partner.outlook.cn/powershell-liveid", "https://login.chinacloudapi.cn/organizations")
                    )
                    cmd += f" -ConnectionUri {ps_quote(uri)} -AzureADAuthorizationEndpointUri {ps_quote(authority)}"
            parts.append(
                f"$connectCmd = Get-Command {cmd.split()[0]} -ErrorAction Stop; "
                "if ($connectCmd.Parameters.ContainsKey('DisableWAM')) { $wamArgs = @{DisableWAM=$true} } else { $wamArgs = @{} }"
            )
            parts.append(cmd + " @wamArgs -ErrorAction Stop | Out-Null")
            parts.extend([
                "$connections = @(Get-ConnectionInformation -ErrorAction Stop | "
                f"Where-Object {{ $_.State -eq 'Connected' -and $_.IsEopSession -eq ${'true' if service == 'compliance' else 'false'} }}); "
                "if (-not $connections.Count) { throw 'Exchange/Compliance returned no connected session.' }",
                "$connections | Select-Object UserPrincipalName, TenantID, State, ConnectionUri | Format-Table",
            ])
            if re.fullmatch(r"[0-9a-fA-F-]{36}", tenant):
                parts.append(f"if (-not ($connections | Where-Object TenantID -eq {ps_quote(tenant)})) {{ throw 'Exchange/Compliance connected to a different tenant.' }}")

        elif service == "teams":
            cmd = "Connect-MicrosoftTeams -ErrorAction Stop"
            if tenant:
                cmd += f" -TenantId {ps_quote(tenant)}"
            if _CLOUDS[cloud][2]:
                cmd += f" -TeamsEnvironmentName {ps_quote(_CLOUDS[cloud][2])}"
            if mode == "device_code":
                cmd += " -UseDeviceAuthentication"
            parts.append(cmd + " | Out-Null")

        elif service == "spo":
            url = self.sharepoint_url()
            pnp_client = self.value("azure_pnp_client_id") or client
            if not url:
                raise ValueError("SharePoint requires a SharePoint URL or the tenant's original onmicrosoft domain in Organization.")
            if not pnp_client:
                raise ValueError("SharePoint requires your registered PnP app Client ID.")
            parts.append("if ($PSVersionTable.PSVersion -lt [version]'7.4.0') { throw 'Current PnP.PowerShell requires PowerShell 7.4 or newer.' }")
            cmd = f"Connect-PnPOnline -Url {ps_quote(url)} -ClientId {ps_quote(pnp_client)} -AzureEnvironment {ps_quote(_CLOUDS[cloud][3])} -ErrorAction Stop"
            cmd += " -DeviceLogin" if mode == "device_code" else " -Interactive"
            parts.extend([
                cmd,
                "$pnp = Get-PnPConnection -ErrorAction Stop; if (-not $pnp) { throw 'PnP returned no connection.' }",
                "Write-Host ('SharePoint URL: ' + $pnp.Url)",
            ])
        return "; ".join(parts)

    def connect_commands(self, token: str, services: list[str] | None = None) -> list[str]:
        commands = []
        for service in self.services if services is None else services:
            label = AZURE_SERVICES[service]["label"]
            try:
                expression = self._service_script(service)
            except ValueError as exc:
                expression = "throw " + ps_quote(str(exc))
            cleanup = ""
            if service in ("azure", "graph", "spo", "teams"):
                disconnect = AZURE_SERVICES[service]["disconnect"]
                cleanup = (
                    f"if (Get-Command {disconnect.split()[0]} -ListImported -ErrorAction SilentlyContinue) {{ "
                    f"try {{ {disconnect} | Out-Null }} catch {{ }} }}; "
                )
                if service == "azure":
                    cleanup += "if (Get-Command Clear-AzContext -ListImported -ErrorAction SilentlyContinue) { Clear-AzContext -Scope Process -Force -ErrorAction SilentlyContinue | Out-Null }; "
            commands.append(
                f"Write-Host {ps_quote('Connecting to ' + label + '...')}; "
                f"try {{ {expression}; $global:rpmoteServices[{ps_quote(service)}] = $true; "
                f"Write-Host {ps_quote('Connected: ' + label)}; {marker(token, f'SERVICE|{service}|OK')} }} "
                f"catch {{ $global:rpmoteServices[{ps_quote(service)}] = $false; "
                f"Write-Warning ({ps_quote('Failed: ' + label + ': ')} + $_.Exception.Message); "
                f"{cleanup}"
                f"{marker(token, f'SERVICE|{service}|FAILED')} }} "
                "finally { if ($rpmoteSpSecret) { $rpmoteSpSecret.Dispose() }; "
                "$rpmoteSpSecret = $null; $rpmoteSpCred = $null }"
            )
        return commands
