"""
Curated Microsoft 365 / Azure PowerShell command library.

These are ready-to-edit administration snippets for the Azure PowerShell tab.
Most commands contain <angle-bracket placeholders> that you fill in before
running (mailbox address, tenant domain, license SKU, etc.). Clicking a command
inserts it into the input box — it is NOT auto-executed — so you can review and
adjust it first. That matters here: many of these cmdlets change or remove
tenant objects.

Modules referenced:
  * Az                          -> Accounts, resources, compute, storage, networking
  * ExchangeOnlineManagement    -> Connect-ExchangeOnline / Connect-IPPSSession
  * Microsoft.Graph             -> Connect-MgGraph (users, groups, licensing)
  * MicrosoftTeams              -> Connect-MicrosoftTeams
  * PnP.PowerShell              -> Connect-PnPOnline (SharePoint / OneDrive)
"""

from __future__ import annotations


def _c(category: str, name: str, command: str, description: str = "") -> dict:
    return {
        "category": category,
        "name": name,
        "command": command,
        "description": description,
    }


def get_azure_command_library() -> list[dict]:
    commands: list[dict] = [
        # ── Connect (sign in to a customer / tenant) ──────────────────────
        _c("Connect", "Install all admin modules (CurrentUser)",
           "Install-Module Az, ExchangeOnlineManagement, Microsoft.Graph, MicrosoftTeams, PnP.PowerShell -Scope CurrentUser -Force -AllowClobber",
           "One-time install of every module used by this console."),
        _c("Connect", "Azure — interactive sign-in",
           "Connect-AzAccount -Tenant '<tenant-id-or-domain>'",
           "Browser sign-in to a specific tenant (Azure resources)."),
        _c("Connect", "Azure — device code sign-in",
           "Connect-AzAccount -UseDeviceAuthentication -Tenant '<tenant-id-or-domain>'",
           "Use when no local browser is available."),
        _c("Connect", "Exchange Online — sign in to customer",
           "Connect-ExchangeOnline -DelegatedOrganization '<customer>.onmicrosoft.com' -DisableWAM",
           "Mailbox administration for the specified tenant."),
        _c("Connect", "Exchange Online — sign in as admin UPN",
           "Connect-ExchangeOnline -UserPrincipalName '<admin>@<customer>.onmicrosoft.com' -DelegatedOrganization '<customer>.onmicrosoft.com' -DisableWAM",
           "Prompts for the delegated/admin account."),
        _c("Connect", "Microsoft Graph — tenant admin scopes",
           "Connect-MgGraph -TenantId '<tenant-id-or-domain>' -ContextScope Process -Scopes 'User.ReadWrite.All','Group.ReadWrite.All','Directory.ReadWrite.All','Organization.Read.All','LicenseAssignment.ReadWrite.All','UserAuthenticationMethod.Read.All','User.RevokeSessions.All'",
           "Users, groups, licensing and directory admin."),
        _c("Connect", "Microsoft Teams — sign in",
           "Connect-MicrosoftTeams -TenantId '<tenant-id-or-domain>'",
           "Teams / Skype for Business Online administration."),
        _c("Connect", "SharePoint / OneDrive (PnP) — sign in",
           "Connect-PnPOnline -Url 'https://<customer>-admin.sharepoint.com' -ClientId '<app-client-id>' -Interactive",
           "SharePoint Online and OneDrive administration."),
        _c("Connect", "Security & Compliance — sign in",
           "Connect-IPPSSession -DelegatedOrganization '<customer>.onmicrosoft.com' -DisableWAM",
           "Purview / compliance center (eDiscovery, retention, DLP)."),

        # ── Session / who am I ────────────────────────────────────────────
        _c("Session", "Show Azure context", "Get-AzContext | Format-List",
           "Current Azure account, tenant and subscription."),
        _c("Session", "Show Exchange connection", "Get-ConnectionInformation | Format-List",
           "Active Exchange Online sessions and the tenant they point at."),
        _c("Session", "Show Graph context", "Get-MgContext | Format-List",
           "Current Microsoft Graph account, tenant and scopes."),
        _c("Session", "Show tenant org profile", "Get-MgOrganization | Format-List DisplayName, Id, VerifiedDomains",
           "Confirm which tenant you are actually connected to."),
        _c("Session", "List all subscriptions", "Get-AzSubscription | Format-Table Name, Id, State",
           "Azure subscriptions visible to the signed-in account."),

        # ── Exchange: mailboxes ───────────────────────────────────────────
        _c("Exchange · Mailboxes", "List all mailboxes",
           "Get-Mailbox -ResultSize Unlimited | Format-Table DisplayName, PrimarySmtpAddress, RecipientTypeDetails",
           "Every mailbox in the tenant."),
        _c("Exchange · Mailboxes", "Get one mailbox (detail)",
           "Get-Mailbox -Identity '<user@customer.com>' | Format-List",
           "Full properties for a single mailbox."),
        _c("Exchange · Mailboxes", "Mailbox size / item count",
           "Get-MailboxStatistics -Identity '<user@customer.com>' | Format-List DisplayName, TotalItemSize, ItemCount, LastLogonTime",
           "Usage stats for a mailbox."),
        _c("Exchange · Mailboxes", "Set mailbox display name",
           "Set-Mailbox -Identity '<user@customer.com>' -DisplayName '<New Name>'",
           "Rename the mailbox display name."),
        _c("Exchange · Mailboxes", "Set primary email address",
           "Set-Mailbox -Identity '<user@customer.com>' -WindowsEmailAddress '<new@customer.com>'",
           "Change the primary SMTP address."),
        _c("Exchange · Mailboxes", "Add alias (proxy address)",
           "Set-Mailbox -Identity '<user@customer.com>' -EmailAddresses @{add='<alias@customer.com>'}",
           "Add a secondary email alias."),
        _c("Exchange · Mailboxes", "Enable online archive",
           "Enable-Mailbox -Identity '<user@customer.com>' -Archive",
           "Turn on the In-Place archive mailbox."),
        _c("Exchange · Mailboxes", "Enable litigation hold",
           "Set-Mailbox -Identity '<user@customer.com>' -LitigationHoldEnabled $true -LitigationHoldDuration 2555",
           "Preserve mailbox content (days)."),
        _c("Exchange · Mailboxes", "Convert to shared mailbox",
           "Set-Mailbox -Identity '<user@customer.com>' -Type Shared",
           "Convert a user mailbox to shared (frees the license)."),
        _c("Exchange · Mailboxes", "Hide from address list",
           "Set-Mailbox -Identity '<user@customer.com>' -HiddenFromAddressListsEnabled $true",
           "Hide the mailbox from the GAL."),
        _c("Exchange · Mailboxes", "Set language / timezone",
           "Set-MailboxRegionalConfiguration -Identity '<user@customer.com>' -Language en-US -TimeZone 'Eastern Standard Time' -LocalizeDefaultFolderName",
           "Regional configuration for a mailbox."),

        # ── Exchange: permissions & delegation ────────────────────────────
        _c("Exchange · Permissions", "Grant Full Access",
           "Add-MailboxPermission -Identity '<mailbox@customer.com>' -User '<delegate@customer.com>' -AccessRights FullAccess -AutoMapping $true",
           "Give a delegate full access to a mailbox."),
        _c("Exchange · Permissions", "Remove Full Access",
           "Remove-MailboxPermission -Identity '<mailbox@customer.com>' -User '<delegate@customer.com>' -AccessRights FullAccess -Confirm:$false",
           "Revoke full access."),
        _c("Exchange · Permissions", "Grant Send As",
           "Add-RecipientPermission -Identity '<mailbox@customer.com>' -Trustee '<delegate@customer.com>' -AccessRights SendAs -Confirm:$false",
           "Allow a delegate to send as the mailbox."),
        _c("Exchange · Permissions", "Grant Send on Behalf",
           "Set-Mailbox -Identity '<mailbox@customer.com>' -GrantSendOnBehalfTo @{add='<delegate@customer.com>'}",
           "Allow a delegate to send on behalf of the mailbox."),
        _c("Exchange · Permissions", "List mailbox permissions",
           "Get-MailboxPermission -Identity '<mailbox@customer.com>' | Where-Object {$_.User -notlike 'NT AUTHORITY*'} | Format-Table User, AccessRights",
           "Who has access to a mailbox."),
        _c("Exchange · Permissions", "Share calendar (reviewer)",
           "Set-MailboxFolderPermission -Identity '<user@customer.com>:\\Calendar' -User '<delegate@customer.com>' -AccessRights Reviewer",
           "Grant calendar visibility."),

        # ── Exchange: mail flow, rules, auto-reply ────────────────────────
        _c("Exchange · Mail Flow", "Set forwarding address",
           "Set-Mailbox -Identity '<user@customer.com>' -ForwardingSmtpAddress '<dest@external.com>' -DeliverToMailboxAndForward $true",
           "Forward a copy of incoming mail externally."),
        _c("Exchange · Mail Flow", "Remove forwarding",
           "Set-Mailbox -Identity '<user@customer.com>' -ForwardingSmtpAddress $null -ForwardingAddress $null",
           "Stop forwarding."),
        _c("Exchange · Mail Flow", "Set out-of-office reply",
           "Set-MailboxAutoReplyConfiguration -Identity '<user@customer.com>' -AutoReplyState Enabled -InternalMessage 'Out of office.' -ExternalMessage 'Out of office.'",
           "Enable an automatic reply."),
        _c("Exchange · Mail Flow", "Disable out-of-office reply",
           "Set-MailboxAutoReplyConfiguration -Identity '<user@customer.com>' -AutoReplyState Disabled",
           "Turn off automatic replies."),
        _c("Exchange · Mail Flow", "List transport (mail flow) rules",
           "Get-TransportRule | Format-Table Name, State, Priority",
           "Tenant mail-flow rules."),
        _c("Exchange · Mail Flow", "Message trace (last 24h)",
           "Get-MessageTraceV2 -RecipientAddress '<user@customer.com>' -StartDate (Get-Date).AddDays(-1) -EndDate (Get-Date) | Format-Table Received, SenderAddress, Subject, Status",
           "Trace recent messages for a recipient."),

        # ── Exchange: distribution & shared ───────────────────────────────
        _c("Exchange · Groups", "List distribution groups",
           "Get-DistributionGroup -ResultSize Unlimited | Format-Table DisplayName, PrimarySmtpAddress",
           "All distribution lists."),
        _c("Exchange · Groups", "Add member to distribution group",
           "Add-DistributionGroupMember -Identity '<group@customer.com>' -Member '<user@customer.com>'",
           "Add a user to a distribution list."),
        _c("Exchange · Groups", "List distribution group members",
           "Get-DistributionGroupMember -Identity '<group@customer.com>' | Format-Table DisplayName, PrimarySmtpAddress",
           "Members of a distribution list."),

        # ── Microsoft Graph: users ────────────────────────────────────────
        _c("Graph · Users", "List all users",
           "Get-MgUser -All -Property DisplayName,UserPrincipalName,AccountEnabled | Format-Table DisplayName, UserPrincipalName, AccountEnabled",
           "Every user in the tenant."),
        _c("Graph · Users", "Get one user (detail)",
           "Get-MgUser -UserId '<user@customer.com>' -Property * | Format-List",
           "Full properties for a user."),
        _c("Graph · Users", "Create a new user",
           "$pw = @{ Password='<TempPassw0rd!>'; ForceChangePasswordNextSignIn=$true }; New-MgUser -DisplayName '<Full Name>' -UserPrincipalName '<user@customer.com>' -MailNickname '<user>' -AccountEnabled -PasswordProfile $pw -UsageLocation 'US'",
           "Provision a new cloud user."),
        _c("Graph · Users", "Reset a user's password",
           "$pw = @{ Password='<NewPassw0rd!>'; ForceChangePasswordNextSignIn=$true }; Update-MgUser -UserId '<user@customer.com>' -PasswordProfile $pw",
           "Force a new password at next sign-in."),
        _c("Graph · Users", "Block sign-in",
           "Update-MgUser -UserId '<user@customer.com>' -AccountEnabled:$false",
           "Disable a user's ability to sign in."),
        _c("Graph · Users", "Enable sign-in",
           "Update-MgUser -UserId '<user@customer.com>' -AccountEnabled:$true",
           "Re-enable a user."),
        _c("Graph · Users", "Revoke all sign-in sessions",
           "Revoke-MgUserSignInSession -UserId '<user@customer.com>'",
           "Force sign-out everywhere (offboarding / compromise)."),
        _c("Graph · Users", "Show user's manager",
           "Get-MgUserManager -UserId '<user@customer.com>' | Select-Object -ExpandProperty AdditionalProperties",
           "The user's assigned manager."),
        _c("Graph · Users", "Show auth methods (MFA)",
           "Get-MgUserAuthenticationMethod -UserId '<user@customer.com>'",
           "Registered MFA / passwordless methods."),

        # ── Microsoft Graph: groups ───────────────────────────────────────
        _c("Graph · Groups", "List all groups",
           "Get-MgGroup -All | Format-Table DisplayName, MailNickname, GroupTypes, Id",
           "Every group (security + M365)."),
        _c("Graph · Groups", "Create a security group",
           "New-MgGroup -DisplayName '<Group Name>' -MailEnabled:$false -MailNickname '<groupnick>' -SecurityEnabled:$true",
           "Create a new security group."),
        _c("Graph · Groups", "Add member to group",
           "New-MgGroupMember -GroupId '<group-object-id>' -DirectoryObjectId (Get-MgUser -UserId '<user@customer.com>').Id",
           "Add a user to a group by object id."),
        _c("Graph · Groups", "List group members",
           "Get-MgGroupMember -GroupId '<group-object-id>' -All | Select-Object -ExpandProperty AdditionalProperties",
           "Members of a group."),

        # ── Microsoft Graph: licensing ────────────────────────────────────
        _c("Graph · Licensing", "List available licenses (SKUs)",
           "Get-MgSubscribedSku | Format-Table SkuPartNumber, SkuId, @{N='Consumed';E={$_.ConsumedUnits}}, @{N='Total';E={$_.PrepaidUnits.Enabled}}",
           "License inventory and seats used."),
        _c("Graph · Licensing", "Set usage location (required to license)",
           "Update-MgUser -UserId '<user@customer.com>' -UsageLocation 'US'",
           "A usage location is required before assigning a license."),
        _c("Graph · Licensing", "Assign a license",
           "Set-MgUserLicense -UserId '<user@customer.com>' -AddLicenses @{ SkuId = (Get-MgSubscribedSku | Where-Object SkuPartNumber -eq '<SPE_E3>').SkuId } -RemoveLicenses @()",
           "Grant a license SKU to a user."),
        _c("Graph · Licensing", "Remove a license",
           "Set-MgUserLicense -UserId '<user@customer.com>' -RemoveLicenses @((Get-MgSubscribedSku | Where-Object SkuPartNumber -eq '<SPE_E3>').SkuId) -AddLicenses @()",
           "Revoke a license SKU from a user."),
        _c("Graph · Licensing", "Show a user's licenses",
           "Get-MgUserLicenseDetail -UserId '<user@customer.com>' | Format-Table SkuPartNumber, SkuId",
           "Licenses currently assigned to a user."),

        # ── Teams / SharePoint ────────────────────────────────────────────
        _c("Teams · SharePoint", "List all Teams",
           "Get-Team | Format-Table DisplayName, GroupId, Visibility",
           "Every Microsoft Team in the tenant."),
        _c("Teams · SharePoint", "List Teams users",
           "Get-CsOnlineUser | Format-Table DisplayName, UserPrincipalName, EnterpriseVoiceEnabled",
           "Teams-enabled users."),
        _c("Teams · SharePoint", "List SharePoint sites (PnP)",
           "Get-PnPTenantSite | Format-Table Url, Title, StorageUsageCurrent",
           "SharePoint site collections (requires PnP admin connection)."),

        # ── Disconnect ────────────────────────────────────────────────────
        _c("Disconnect", "Disconnect Azure",
           "Disconnect-AzAccount -Scope Process -ErrorAction Stop; Clear-AzContext -Scope Process -Force -ErrorAction Stop",
           "Sign out of Azure (Az)."),
        _c("Disconnect", "Disconnect Exchange Online",
           "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction Stop",
           "Close Exchange Online sessions."),
        _c("Disconnect", "Disconnect Microsoft Graph",
           "Disconnect-MgGraph -ErrorAction Stop",
           "Sign out of Microsoft Graph."),
        _c("Disconnect", "Disconnect Teams",
           "Disconnect-MicrosoftTeams -ErrorAction Stop",
           "Sign out of Microsoft Teams."),
        _c("Disconnect", "Disconnect SharePoint (PnP)",
           "Disconnect-PnPOnline -ErrorAction Stop",
           "Sign out of SharePoint Online."),
        _c("Disconnect", "Disconnect ALL services",
           "; ".join(get_disconnect_commands()),
           "Cleanly sign out of every Microsoft service in this session."),
    ]
    commands.extend(_azure_task_commands())
    for entry in commands:
        if entry["category"] == "Connect" and entry["command"].startswith("Connect-AzAccount"):
            entry["command"] += " -Scope Process"
    return commands


# Service metadata used by the Azure terminal to build connect / disconnect
# sequences and module preflight checks. Keys match the values stored in a
# connection's ``azure_services`` list.
AZURE_SERVICES: dict[str, dict] = {
    "azure": {
        "label": "Azure (Az)",
        "module": "Az.Accounts",
        "disconnect": "Disconnect-AzAccount -Scope Process -ErrorAction Stop",
    },
    "exchange": {
        "label": "Exchange Online",
        "module": "ExchangeOnlineManagement",
        "disconnect": "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction Stop",
    },
    "graph": {
        "label": "Microsoft Graph",
        "module": "Microsoft.Graph.Authentication",
        "disconnect": "Disconnect-MgGraph -ErrorAction Stop",
    },
    "teams": {
        "label": "Microsoft Teams",
        "module": "MicrosoftTeams",
        "disconnect": "Disconnect-MicrosoftTeams -ErrorAction Stop",
    },
    "spo": {
        "label": "SharePoint (PnP)",
        "module": "PnP.PowerShell",
        "disconnect": "Disconnect-PnPOnline -ErrorAction Stop",
    },
    "compliance": {
        "label": "Security & Compliance",
        "module": "ExchangeOnlineManagement",
        "disconnect": "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction Stop",
    },
}

# Default service selection when a connection predates the multi-service field.
DEFAULT_AZURE_SERVICES = ["azure"]


def get_disconnect_commands() -> list[str]:
    """Disconnect every available service, including services signed in manually."""
    expressions = list(dict.fromkeys(
        meta["disconnect"] for meta in reversed(list(AZURE_SERVICES.values()))
    ))
    expressions.extend([
        "Clear-AzContext -Scope Process -Force -ErrorAction Stop",
        "Get-PSSession | Remove-PSSession -ErrorAction Stop",
    ])
    return ["$global:rpmoteDisconnectFailed = $false"] + [
        f"if (Get-Command {expr.split()[0]} -ListImported -ErrorAction SilentlyContinue) {{ "
        f"try {{ {expr} | Out-Null }} catch {{ "
        f"$global:rpmoteDisconnectFailed = $true; Write-Warning ('{expr.split()[0]}: ' + $_.Exception.Message) -WarningAction Continue }} }}"
        for expr in expressions
    ] + ["Remove-Variable rpmoteSpSecret,rpmoteSpCred -Scope Global -ErrorAction SilentlyContinue"]


def _azure_task_commands() -> list[dict]:
    """Major Azure operations with the module required for each task."""
    groups = {
        "Azure · Resources": ("Az.Resources", [
            ("List resource groups", "Get-AzResourceGroup | Format-Table ResourceGroupName, Location, ProvisioningState"),
            ("Create a resource group", "New-AzResourceGroup -Name '<resource-group>' -Location '<eastus>'"),
            ("List resources in a group", "Get-AzResource -ResourceGroupName '<resource-group>' | Format-Table Name, ResourceType, Location"),
            ("Find resources by tag", "Get-AzResource -TagName '<Environment>' -TagValue '<Production>' | Format-Table Name, ResourceGroupName, ResourceType"),
            ("Merge resource tags", "Update-AzTag -ResourceId '<resource-id>' -Tag @{ '<Environment>' = '<Production>' } -Operation Merge"),
            ("Preview resource group removal", "Remove-AzResourceGroup -Name '<resource-group>' -WhatIf"),
            ("List Azure regions", "Get-AzLocation | Format-Table Location, DisplayName"),
            ("Validate a deployment", "Test-AzResourceGroupDeployment -ResourceGroupName '<resource-group>' -TemplateFile '<template.json>'"),
            ("Preview deployment changes", "New-AzResourceGroupDeployment -ResourceGroupName '<resource-group>' -TemplateFile '<template.json>' -WhatIf"),
            ("Deploy an ARM template", "New-AzResourceGroupDeployment -Name '<deployment-name>' -ResourceGroupName '<resource-group>' -TemplateFile '<template.json>'"),
            ("List deployments", "Get-AzResourceGroupDeployment -ResourceGroupName '<resource-group>' | Format-Table DeploymentName, ProvisioningState, Timestamp"),
            ("List resource locks", "Get-AzResourceLock -ResourceGroupName '<resource-group>' | Format-Table Name, LockId, Properties"),
        ]),
        "Azure · Compute": ("Az.Compute", [
            ("List virtual machines and power states", "Get-AzVM -Status | Format-Table Name, ResourceGroupName, Location, PowerState"),
            ("Inspect a virtual machine", "Get-AzVM -ResourceGroupName '<resource-group>' -Name '<vm-name>' -Status | Format-List"),
            ("Start a virtual machine", "Start-AzVM -ResourceGroupName '<resource-group>' -Name '<vm-name>'"),
            ("Stop and deallocate a virtual machine", "Stop-AzVM -ResourceGroupName '<resource-group>' -Name '<vm-name>' -Force"),
            ("Restart a virtual machine", "Restart-AzVM -ResourceGroupName '<resource-group>' -Name '<vm-name>'"),
            ("List VM sizes in a region", "Get-AzVMSize -Location '<eastus>' | Format-Table Name, NumberOfCores, MemoryInMB"),
            ("List managed disks", "Get-AzDisk -ResourceGroupName '<resource-group>' | Format-Table Name, DiskSizeGB, DiskState"),
            ("List disk snapshots", "Get-AzSnapshot -ResourceGroupName '<resource-group>' | Format-Table Name, TimeCreated, DiskSizeGB"),
            ("List VM extensions", "Get-AzVMExtension -ResourceGroupName '<resource-group>' -VMName '<vm-name>' | Format-Table Name, Publisher, ProvisioningState"),
        ]),
        "Azure · Storage": ("Az.Storage", [
            ("List storage accounts", "Get-AzStorageAccount | Format-Table StorageAccountName, ResourceGroupName, Location, Kind"),
            ("Create a storage account", "New-AzStorageAccount -ResourceGroupName '<resource-group>' -Name '<globallyuniquename>' -Location '<eastus>' -SkuName Standard_LRS -Kind StorageV2 -EnableHttpsTrafficOnly $true -AllowBlobPublicAccess $false"),
            ("Create storage context with current login", "$storageContext = New-AzStorageContext -StorageAccountName '<storage-account>' -UseConnectedAccount"),
            ("List blob containers", "Get-AzStorageContainer -Context $storageContext | Format-Table Name, PublicAccess"),
            ("Create a private blob container", "New-AzStorageContainer -Name '<container>' -Context $storageContext -Permission Off"),
            ("List blobs", "Get-AzStorageBlob -Container '<container>' -Context $storageContext | Format-Table Name, Length, LastModified"),
            ("Upload a blob", "Set-AzStorageBlobContent -File '<local-file-path>' -Container '<container>' -Blob '<blob-name>' -Context $storageContext"),
            ("Download a blob", "Get-AzStorageBlobContent -Container '<container>' -Blob '<blob-name>' -Destination '<local-file-path>' -Context $storageContext"),
        ]),
        "Azure · Networking": ("Az.Network", [
            ("List virtual networks", "Get-AzVirtualNetwork | Format-Table Name, ResourceGroupName, Location"),
            ("List subnets", "(Get-AzVirtualNetwork -ResourceGroupName '<resource-group>' -Name '<vnet-name>').Subnets | Format-Table Name, AddressPrefix"),
            ("List public IP addresses", "Get-AzPublicIpAddress | Format-Table Name, ResourceGroupName, IpAddress, PublicIpAllocationMethod"),
            ("List network interfaces", "Get-AzNetworkInterface | Format-Table Name, ResourceGroupName, Location"),
            ("List network security groups", "Get-AzNetworkSecurityGroup | Format-Table Name, ResourceGroupName, Location"),
            ("Inspect NSG rules", "(Get-AzNetworkSecurityGroup -ResourceGroupName '<resource-group>' -Name '<nsg-name>').SecurityRules | Format-Table Name, Priority, Direction, Access, DestinationPortRange"),
            ("List load balancers", "Get-AzLoadBalancer | Format-Table Name, ResourceGroupName, Location"),
            ("List private endpoints", "Get-AzPrivateEndpoint | Format-Table Name, ResourceGroupName, ProvisioningState"),
        ]),
        "Azure · Access": ("Az.Resources", [
            ("List role assignments", "Get-AzRoleAssignment -Scope '<resource-or-subscription-id>' | Format-Table DisplayName, RoleDefinitionName, Scope"),
            ("List Azure roles", "Get-AzRoleDefinition | Format-Table Name, IsCustom, Id"),
            ("Grant an Azure role", "New-AzRoleAssignment -ObjectId '<principal-object-id>' -RoleDefinitionName '<Reader>' -Scope '<resource-or-subscription-id>'"),
            ("Preview role removal", "Remove-AzRoleAssignment -ObjectId '<principal-object-id>' -RoleDefinitionName '<Reader>' -Scope '<resource-or-subscription-id>' -WhatIf"),
            ("List policy assignments", "Get-AzPolicyAssignment | Format-Table Name, Scope, PolicyDefinitionId"),
        ]),
        "Azure · Key Vault": ("Az.KeyVault", [
            ("List key vaults", "Get-AzKeyVault | Format-Table VaultName, ResourceGroupName, Location"),
            ("List secret metadata", "Get-AzKeyVaultSecret -VaultName '<vault-name>' | Format-Table Name, Enabled, Expires"),
            ("List vault certificates", "Get-AzKeyVaultCertificate -VaultName '<vault-name>' | Format-Table Name, Thumbprint, Expires"),
            ("List vault keys", "Get-AzKeyVaultKey -VaultName '<vault-name>' | Format-Table Name, Enabled, Expires"),
        ]),
        "Azure · Monitoring": ("Az.Monitor", [
            ("Activity log for the last day", "Get-AzActivityLog -StartTime (Get-Date).AddDays(-1) | Select-Object EventTimestamp, ResourceGroupName, OperationName, Status"),
            ("List resource metric definitions", "Get-AzMetricDefinition -ResourceId '<resource-id>' | Select-Object Name, Unit"),
            ("Read resource metrics", "Get-AzMetric -ResourceId '<resource-id>' -MetricName '<Percentage CPU>' -StartTime (Get-Date).AddHours(-1) -EndTime (Get-Date)"),
            ("List diagnostic settings", "Get-AzDiagnosticSetting -ResourceId '<resource-id>'"),
        ]),
        "Azure · Log Analytics": ("Az.OperationalInsights", [
            ("List Log Analytics workspaces", "Get-AzOperationalInsightsWorkspace | Format-Table Name, ResourceGroupName, CustomerId"),
            ("Query Log Analytics", "Invoke-AzOperationalInsightsQuery -WorkspaceId '<workspace-customer-id>' -Query 'Heartbeat | take 20'"),
        ]),
        "Azure · Web Apps": ("Az.Websites", [
            ("List web apps", "Get-AzWebApp | Format-Table Name, ResourceGroup, State, DefaultHostName"),
            ("Start a web app", "Start-AzWebApp -ResourceGroupName '<resource-group>' -Name '<app-name>'"),
            ("Stop a web app", "Stop-AzWebApp -ResourceGroupName '<resource-group>' -Name '<app-name>'"),
            ("Restart a web app", "Restart-AzWebApp -ResourceGroupName '<resource-group>' -Name '<app-name>'"),
            ("List App Service plans", "Get-AzAppServicePlan | Format-Table Name, ResourceGroup, Location, Sku"),
        ]),
        "Teams · SharePoint": ("MicrosoftTeams / PnP.PowerShell", [
            ("List Team members", "Get-TeamUser -GroupId '<team-group-id>' | Format-Table User, Name, Role"),
            ("Add a Team member", "Add-TeamUser -GroupId '<team-group-id>' -User '<user@customer.com>' -Role Member"),
            ("List Team channels", "Get-TeamChannel -GroupId '<team-group-id>' | Format-Table DisplayName, MembershipType"),
            ("List OneDrive sites", "Get-PnPTenantSite -IncludeOneDriveSites | Where-Object Url -Like '*-my.sharepoint.*' | Format-Table Url, Owner, StorageUsageCurrent"),
            ("Inspect a SharePoint site", "Get-PnPTenantSite -Url 'https://<customer>.sharepoint.com/sites/<site>' -Detailed | Format-List"),
            ("Set SharePoint site storage quota", "Set-PnPTenantSite -Url 'https://<customer>.sharepoint.com/sites/<site>' -StorageQuota <quota-in-mb>"),
        ]),
        "Compliance": ("ExchangeOnlineManagement", [
            ("List retention policies", "Get-RetentionCompliancePolicy | Format-Table Name, Enabled, Mode"),
            ("List DLP policies", "Get-DlpCompliancePolicy | Format-Table Name, Mode"),
            ("Search unified audit log", "Search-UnifiedAuditLog -StartDate (Get-Date).AddDays(-1) -EndDate (Get-Date) -UserIds '<user@customer.com>' -ResultSize 1000"),
        ]),
        "PowerShell": ("PowerShell", [
            ("Show PowerShell version", "$PSVersionTable | Format-Table"),
            ("List installed admin modules", "Get-Module -ListAvailable Az*,ExchangeOnlineManagement,Microsoft.Graph*,MicrosoftTeams,PnP.PowerShell | Sort-Object Name,Version -Descending | Format-Table Name, Version"),
            ("Find Azure commands", "Get-Command -Name '*-Az*' | Sort-Object Name | Format-Table Name, Source"),
            ("Get help for a command", "Get-Help '<cmdlet-name>' -Full"),
            ("Export Azure resource inventory", "Get-AzResource | Select-Object Name, ResourceGroupName, ResourceType, Location | Export-Csv -Path '<output.csv>' -NoTypeInformation -Encoding UTF8"),
        ]),
        "Session": ("Az.Accounts", [
            ("Switch Azure subscription", "Set-AzContext -Subscription '<subscription-id>' -Scope Process"),
            ("List Azure tenants", "Get-AzTenant | Format-Table Id, Name, Domains"),
        ]),
    }
    commands = []
    for category, (module, rows) in groups.items():
        for name, command in rows:
            description = f"{name}. Requires {module}."
            if "$storageContext" in command and "New-AzStorageContext" not in command:
                description += " Run the storage context command first; storage data roles are required."
            commands.append(_c(category, name, command, description))
    commands.extend([
        _c("Graph · Groups", "Remove group member", "Remove-MgGroupMemberByRef -GroupId '<group-object-id>' -DirectoryObjectId '<user-object-id>'", "Remove membership without deleting the user."),
        _c("Graph · Users", "List deleted users", "Get-MgDirectoryDeletedItemAsUser -All | Format-Table Id, DisplayName, UserPrincipalName", "Requires Microsoft.Graph.Identity.DirectoryManagement."),
        _c("Graph · Users", "Restore a deleted user", "Restore-MgDirectoryDeletedItem -DirectoryObjectId '<deleted-user-object-id>'", "Requires Microsoft.Graph.Identity.DirectoryManagement."),
        _c("Exchange · Mail Flow", "Message trace event details", "Get-MessageTraceDetailV2 -MessageTraceId '<trace-id>' -RecipientAddress '<user@customer.com>'", "Inspect events from a Get-MessageTraceV2 result."),
        _c("Exchange · Groups", "Remove distribution group member", "Remove-DistributionGroupMember -Identity '<group@customer.com>' -Member '<user@customer.com>' -Confirm:$false", "Remove a reviewed group member."),
    ])
    return commands
