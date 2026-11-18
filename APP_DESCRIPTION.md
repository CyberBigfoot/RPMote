# RPMote

RPMote is a Windows desktop app for people who spend a lot of time managing remote computers.
Instead of keeping separate tools open for SSH, file transfers, RDP, server consoles, and cloud
administration, RPMote brings those daily tasks into one calm, organised workspace.

It is built for developers, IT professionals, system administrators, and anyone responsible for
more machines than they can comfortably remember. Connections can be grouped by customer,
environment, or tenant, then opened from a visual dashboard that makes a large collection of
servers easier to understand.

## What it does

RPMote gives you a full SSH terminal, SFTP and FTP file browsers, embedded or external RDP
sessions, iDRAC and NAS browser support, and an Azure and Microsoft 365 PowerShell console. The
goal is simple: connect to the right machine quickly and keep the surrounding work in the same
place.

The app also includes practical tools for real-world operations. You can check whether services
are reachable, wake machines with Wake-on-LAN, restart systems directly over SSH, move files and
folders, save useful command snippets, and monitor the health of remote hosts. For teams looking
after mixed Windows and Linux environments, RPMote can also route restart commands through a
designated server when the target machine cannot be reached directly.

## Designed for a busy estate

RPMote’s connection graph gives your infrastructure a visual home. Servers can be arranged under
their tenants, moved into a free-floating layout, and connected with relationship lines when the
map itself is useful. Search works across names, hostnames, protocols, usernames, and tenants, so
finding one machine does not require remembering exactly where it was filed.

Selecting a tenant frames its machines automatically. Selecting a connection in the sidebar
highlights its node and branch without starting a session. Closing the tenant restores the
previous view, so inspecting a customer does not disturb the arrangement you saved.

Relationship lenses can highlight machines that share a physical host, network, credential, or
restart dependency. These views are calculated from the vault when needed; they do not replace the
connections you have saved or quietly rewrite your data.

## Security and control

Connection details and snippets are stored in a locally encrypted vault protected by a master
password. RPMote does not rely on a remote account just to open your local connection library.
Recovery keys are available if you create one in advance, and sensitive values are deliberately
hidden or re-protected when the app reveals them.

The app is also explicit about potentially disruptive actions. Reachability checks are off until
you enable them, restart dialogs show the target and command before anything runs, and saved
credentials are used only for the connection workflows you choose.

## A workspace that feels like yours

RPMote includes a dark, developer-focused interface with themes ranging from the Mote house theme
to Nebula, Tokyo Night, Catppuccin, Matrix, Aurora, Synthwave, and more. Hardware acceleration
can keep the graph and animated backgrounds smooth, while a software rendering option is available
for virtual machines and remote desktop environments.

RPMote is a Windows application and can be packaged as a standalone executable, so it can be
installed on a machine without requiring a separate Python setup. It is meant to be a practical
home base for remote work: powerful when you need depth, but organised enough that everyday
connections stay quick and approachable.
