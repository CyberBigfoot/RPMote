# ✦ RPMote

*Every remote. One mote.*

**RPMote** is a premium, high-performance remote connection manager designed for developers and system administrators. It provides a secure, unified interface for SSH, Telnet, serial consoles, SFTP, FTP, RDP, VNC, web consoles (iDRAC, NAS, firewalls, switches…) and Azure PowerShell, with a focus on aesthetics and productivity.

![App Icon](app/resources/icons/app_icon.ico)

---

## 🚀 Key Features

### 🔐 Secure Credential Vault
*   **Encrypted vault**: connections, passwords and snippets are stored encrypted on this PC with Fernet (AES-128-CBC plus an HMAC-SHA256 integrity check), under a key derived from your master password.
*   **Master Password Protection**: Secure access with a master password using PBKDF2-HMAC-SHA256 key derivation.
*   **Saved Passwords Viewer**: Review every stored credential from **File → Saved Passwords** (`Ctrl+Shift+P`), or right-click a connection → **Show Password**. Revealing or copying re-prompts for the master password, secrets re-hide after 30 seconds, and the clipboard clears itself.
*   **Encrypted Export/Import**: Safely move your connections between machines.

### 🧰 Everyday MSP Workflow
*   **Go to anything (`Ctrl+P` or `Ctrl+Shift+Space`)**: type part of a connection, tenant, host, username or note — or a menu command (start with `>` for commands only). Words can come from different fields, so `contoso dc` finds DC01 in Contoso. **Enter** opens, **Ctrl+Enter** opens another session, **Shift+Enter** edits.
*   **Overview card**: what needs attention, your favourites and recent connections, over the dashboard graph.
*   **Favourites, duplicates, multi-select**: star connections, duplicate one as a template, and open, move, wake or delete several at once from the sidebar.
*   **Session history and time per tenant**: Session › Session History & Time shows who you worked on and for how long, per tenant, with a CSV export for billing.
*   **Shared credentials**: File › Shared Credentials holds a login that many connections use — rotate the password once and every connection follows. Saved Passwords flags passwords reused across connections or tenants and ones older than a year.
*   **VPN first, per tenant**: right-click a tenant › **Connect VPN first…** to upload an OpenVPN `.ovpn` or WireGuard `.conf` tunnel config, enter **PPTP** or **L2TP/IPsec** connection details, select an existing Windows VPN profile, or use your own command. Manual connections accept a server address, username, password, optional domain, authentication/encryption settings and split tunneling; L2TP also accepts an IPsec pre-shared key. Login details are saved in the encrypted vault and passed to Windows in memory. **Connect VPN now** tests the setup (and offers **Disconnect VPN** once it is up); a machine opened while it is up shares that VPN rather than dialling it again, and keeps it up after the window closes. A VPN RPMote connected is disconnected as soon as the last session opened over it closes (in its tenant, or any other tenant set to the same VPN), if you cancel the wait, and when RPMote quits; a VPN that was already up is left alone, and **Leave the VPN connected after the sessions that needed it close** keeps it up. OpenVPN GUI and WireGuard need their installed clients; OpenVPN GUI imports the selected file when connecting (keep referenced certificates/keys alongside it), and WireGuard uses the selected file in place. The first WireGuard connection asks once for administrator approval to install the tunnel; after that RPMote connects and disconnects it without asking. A tunnel of the same name already switched on in the WireGuard app is used as it is, and a config uploaded again from a new folder replaces the old tunnel (one more approval). A WireGuard tunnel only counts as connected once its server answers: if it never does, RPMote stops the tunnel again and says why (a private server address in the config, or the server not knowing this PC's public key, which it shows). For a Remote Desktop connection through an RD Gateway, or SSH through jump hosts, RPMote waits for the gateway or the outermost jump host, since that is what this PC dials. While waiting for a machine, RPMote also says why it is not answering: not routed through the VPN, nothing coming back, the machine refusing the port, answering ping but not the port, not answering at all (with the address the VPN gave this PC), or two different devices answering at the machine's address: an IP address conflict, told apart by the TTL of their ping replies. Windows VPN errors whose own wording does not say what to check (PPTP 807, L2TP/IPsec 789 and 809, authentication and encryption mismatches) come with a hint. Each connect, wait and disconnect is written to the app log (**Help › Troubleshooting › Open Log Folder**), without logins or keys. Keep selected config files at their saved locations.
*   **Import from other tools**: File › Import reads `.rdp` files, CSV exports (Devolutions RDM, Royal TS, spreadsheets), mRemoteNG `confCons.xml` (including its encrypted passwords), PuTTY saved sessions and `~/.ssh/config`, with a preview first.
*   **Recently Deleted and backups**: deleted connections stay restorable for 30 days; the vault is backed up daily and before any import, restore or bulk delete (File › Backups).
*   **Failures that say what to do**: a connection that cannot connect says why in plain words — the name does not resolve, nothing answers, the port is closed, the login was rejected — with **Retry**, **Wake & retry**, **Check reachability** and **Edit connection** right there.
*   **Settings in one place (`Ctrl+,`)**: appearance, terminal, connection defaults, auto-lock, host-key policy, backups and troubleshooting tools.
*   **Choose how the app locks**: use the **Lock** control in the status bar, **File → Screen Lock Options**, or **Settings → Security**. Pick **Never**, **1, 5, 10, 15, 30 or 60 minutes**, or enter a custom idle timeout up to 24 hours. Separately choose to lock when the app is minimized, when Windows is locked, or when the computer sleeps/resumes. Changes are saved in this app's vault and apply immediately. Existing timeouts are kept; the new event-based options start off until enabled. **Never** disables only the idle timeout, so enabled event rules and **Lock screen now** still work. Screen locking keeps sessions connected and requires the master password to return; Windows unlocking does not unlock the app. Open app dialogs are covered and their unsaved drafts are kept. Windows notifications use the [Windows session](https://learn.microsoft.com/en-us/windows/win32/termserv/wm-wtssession-change) and [power](https://learn.microsoft.com/en-us/windows/win32/power/wm-powerbroadcast-messages) APIs.

### 💻 Advanced SSH Terminal
*   **Full ANSI Support**: Flawless rendering of terminal colors and complex TUI applications.
*   **Meslo Nerd Fonts**: Pre-integrated Nerd Fonts for perfect icon rendering in the terminal.
*   **Smart Font Scaling**: Easy zoom in/out with high-quality developer-focused typography.
*   **Latency Monitoring**: Real-time ping display for your active sessions.

### 🧭 Shell Integration
*   **Semantic Prompt Marks**: OSC 133 hooks are installed quietly on connect so jump-to-prompt, copy-last-output and the exit-status badge read what the shell reports instead of guessing. Your prompt, aliases and PATH are left untouched.
*   **Transition Aware**: Hooks are re-installed automatically after `sudo -i` / `su`.

### 📂 Integrated SFTP/FTP Browser
*   **Dual-Pane Interface**: Side-by-side local and remote filesystem views.
*   **Background Transfers**: Perform file operations without blocking your workflow.
*   **Drag & Drop Spirit**: Context-menu-driven uploads, downloads, and file management.
*   **Drop In From Your Desktop**: Drag files *and* folders straight onto the Library panel or onto either pane of an SFTP/FTP tab. Folders transfer with everything inside them, recursively. Drop onto a folder row to land inside it, or anywhere else to land in the directory you're browsing. Dropping on a **remote** pane uploads; dropping on a **local** pane copies into that folder.
*   **Upload Files and Folders Together**: **Upload** opens one picker that accepts any mix of files and folders in a single selection — no separate "upload folder" trip. Available in the Library and on both SFTP and FTP tabs.
*   **Remote text editor**: double-click a file in **Library → Files**, an SFTP tab, or an FTP/FTPS tab, or right-click → **Edit remote file…**. Edit it in RPMote, choose **Review changes** to see added/removed lines, then **Save with backup**. `Ctrl+S` opens the changes preview first; press it again there to save.
*   **Responsive Library file operations**: connecting, browsing, creating files/folders, renaming, permissions and bulk deletion run in the background. Creation and deletion use their own SFTP channel, show progress/errors, and block duplicate file operations while running. Switching SSH sessions stops remaining delete requests and keeps results from the previous host out of the new tree. Folder deletion removes children before parents and does not follow directory symlinks.
*   **YAML editing and formatting**: `.yml` and `.yaml` files use spaces for **Tab**, **Shift+Tab** to outdent, and contextual indentation on **Enter**. New files default to two spaces; existing files use their detected indentation style. Pasted fragments align with the destination while keeping relative indentation and literal scalar contents. **Format YAML** normalizes indentation as an undoable draft edit and checks that values, tags, aliases and comments survive. **Review changes** validates YAML in the background, repairs structural indentation tabs and reports errors with a line/column before saving. Saves keep the original encoding and line endings, and formatting changes are included in the changes preview.
*   **Backups and conflict checks**: each save verifies that the remote contents, target and (for SFTP) Unix permissions/ownership still match the opened version. A private SFTP backup and a staged upload are verified before replacement. The editor preserves UTF-8/UTF-16 BOMs, line endings and SFTP Unix permissions/ownership. Backups stay beside the original as hidden `.filename.rpmote-….bak` files, with the exact path shown after saving. FTP backup access follows the FTP server's policy. SFTP requires atomic rename support; if replacement fails, the editor keeps your draft and reports the backup path.
*   **Folder comparison**: **Compare folders…** on an SFTP/FTP tab compares the folders currently shown in its two panes. In Library Files, right-click → **Compare with local folder…** and choose a local folder. Optionally include subfolders; the result distinguishes identical, different, missing, conflicting and unverified files. Select an existing remote file and **Review local version…** to load the local contents as an editable draft before saving.
*   **Bounded previews**: the text editor accepts UTF-8 and BOM-marked UTF-16 files up to **2 MiB**. Directory comparisons check contents for small files, skip links/special files, and explicitly mark large or unverified files. Comparisons stop at 2,000 entries and cap content comparisons at 32 MiB; folder comparison does not perform automatic synchronization.

### 🖼️ Seamless RDP Integration
*   **Embedded ActiveX Mode**: Run RDP sessions directly inside application tabs.
*   **External Mode**: Traditional RDP experience for maximum compatibility.
*   **Stable embedded window movement**: moving the app, including between monitors with different DPI scaling, keeps the RDP session connected. Hosted-window placement is repaired separately from session resolution; resize reconnects are debounced and only occur after the panel size changes, with no relaunch while minimized, hidden or showing a credentials prompt.
*   **Screens menu**: the embedded RDP header lists the current session screen and indicates when no additional screens are available. The embedded client currently negotiates one RDP display. The remote PC's physical monitors belong to its console desktop and are not shared by a normal RDP connection; see [Microsoft's explanation of console and remote displays](https://learn.microsoft.com/en-us/windows/win32/termserv/consoles-vs-terminals).
*   **Auto-Login**: Securely passes credentials to the RDP session.
*   **RD Gateway ("Connect from anywhere")**: give a connection its gateway address in the connection dialog — the same three settings mstsc offers: server address, *bypass for local addresses*, and *use my gateway credentials for the remote computer*. A pasted `https://gw.example.com/...` URL is reduced to the address for you.
*   **No second password prompt**: before mstsc starts, RPMote stores the desktop login *and* the gateway login in Windows Credential Manager exactly the way mstsc's own "Remember me" does (the gateway one as a *Windows credential* under the bare gateway address — a generic entry there is ignored by mstsc). They are written through the Credential Manager API, never through a `cmdkey` command line.
*   **It tells you what mstsc is doing**: the tab's status line follows the session — *connecting through RD Gateway gw…*, *Windows is asking for credentials*, or the text of mstsc's own error box (an untrusted gateway certificate, a CAP/RAP refusal, a gateway that did not answer) turned into what to check. Every step is written to the crash log under `rpmote.rdp`.
*   **Tip**: gateways authenticate over NTLM, so save the account as `DOMAIN\user` or `user@domain`; the dialog reminds you when a login has no domain.

### 🛠️ Productivity Tools
*   **Command Snippets**: Save frequently used bash commands and execute them with one click.
*   **Cron jobs in the Library**: **Library → Commands → Cron jobs** contains **28** common schedules and editable examples for backups, database dumps, website checks, cache cleanup, log rotation, certificate renewal, Python scripts, Laravel/WordPress tasks, startup jobs, overlap prevention and execution time limits. Double-click a template to review and copy its crontab entry; **Open user/root crontab** opens the appropriate terminal editor. Schedule templates are never executed as shell commands. **Cron management** adds **12** commands for listing, editing, backing up, logs, service status, timezone and existing timer checks. Examples need their paths/accounts adjusted before installation; syntax follows the [crontab manual](https://man7.org/linux/man-pages/man5/crontab.5.html).
*   **Remote Health Monitoring**: Real-time background tracking of remote CPU, RAM, and Disk usage.
*   **Modern Themes**: Start with **Mote**, the RPMote house theme (deep navy and cyan), or pick **Nebula**, **Tokyo Night**, **Catppuccin** or **Classic Dark** — or an animated dashboard: **Matrix** rain, **Dot Wave**, **Aurora**, **Synthwave**, **Circuit**, **Plexus**, **Warp** and **Ember** (View › Theme, or Ctrl+T to cycle). An animated scene sits **Calm** behind the graph by default — dimmed, slowed, and shaded under every tenant so lines and names stay readable; switch to **Balanced**, **Vivid** (full strength) or **Plain** (no scene) under Graph › Appearance › Background.

### 🔑 Account Recovery
*   **Recovery key**: a one-time 25-character code that can open your vault if you forget the master password. Create it from **File → Recovery Key** (you are also offered one right after first-time setup).
*   **Forgot your password?**: on the login screen, redeem the recovery key to get straight back in with everything intact, then set a new master password.
*   **No back door**: the vault is encrypted with your password, and only a recovery key created *beforehand* can open it. If both are lost, the login screen offers a deliberate wipe-and-start-over — nobody, including RPMote, can decrypt it for you.
*   Changing your master password does **not** invalidate your recovery key.

### 🗺️ Customisable Connection Graph
*   **Two layouts**: keep the classic **Hub** view with the RPMote orb at the centre, or switch to **Free Floating** — no centre, every tenant spread across an open canvas.
*   **Connection branches**: each saved connection hangs off its tenant as a colour-coded node, so you can see at a glance what belongs where. Ring layout keeps even a 40-connection tenant tidy.
*   **Pan and zoom**: hold **right-click** (or middle-click, or drag empty canvas) and move to pan; the wheel zooms toward the cursor. **Fit to View** frames the whole estate in one keystroke.
*   **Focus a tenant**: select a tenant in the sidebar or click its graph tile to frame the tenant and its machines, including branches you placed far away. Selecting a sidebar connection highlights that machine and its branch without opening a session. Close the tenant or use **Expand All** or **Collapse All** to restore the previous view; saved node positions and the saved zoom and pan stay intact.
*   **Visual links between anything**: **Alt-drag** from one node to another, or use Link Mode, to draw a relationship line. Either end can be a **tenant or an individual connection**, so you can show that one customer's management server talks to another customer's box. Set colour, style (solid / dashed / dotted) and label. These are **purely cosmetic** — they never touch a connection.
*   **Compact tenant tiles**: with branches on, tiles collapse to a single row — tenant dot, name, connection count — instead of repeating the icon strip. Set to **Auto**, **Full** or **Compact**.
*   **Built for a big estate**: past 40 connections the free-floating layout holds each tenant's machines back until you open it — click a tile to expand it, click again to close, and the tile carries an **open ▸** badge so a collapsed tenant never reads as an empty one. What you had open is restored next time you sign in. Force it on or off under **Graph Settings → Hide until opened**.
*   **Tidy Up Layout**: snaps hand-dragged tenant tiles back onto their grid *without* throwing away your arrangement — the customer you put top-left stays top-left; what goes away is the overlap nobody arranged on purpose.
*   **Hover to explore**: a tenant or connection lifts, glows and sends out a ripple when the cursor lands on it; its lines light up with a pulse running out to each machine while the rest of the graph fades back, and a card beside it says what it is, where it lives and how it is doing. Zoomed right out, hovering a tenant fans its machines' names out around it.
*   **Tune everything**: the **Graph** menu and **Graph Settings…** panel control background strength, line style, branch labels, spacing, tile size, particles and animation. Everything is saved to your vault.

### 🔗 Relationship Lenses
The graph normally draws what you told it — tenants, their machines, and links you drew yourself. A lens draws what the vault *implies*, computed on the spot and never stored. Pick one from **Graph → Relationship Lens** or the canvas right-click menu; everything unrelated dims, the machines involved stay lit, and **Esc** clears it.

*   **Same machine**: the RDP entry, the SSH entry and the iDRAC entry that all point at one physical box — matched on hostname *or* MAC, so a dual-homed server filed under two addresses is still recognised as one machine.
*   **Same network**: everything sharing a `/24`, worked out from literal IP addresses. Hostnames are never resolved — three hundred DNS lookups would freeze the window, and a lens has to be instant.
*   **Shared credentials**: machines opened by the same username and key, or the same password. This is the blast radius of one key, drawn — and the password case is matched by digest, so a group can be found without the secret ever being shown.
*   **Restart dependencies**: the arrows already implied by your restart setup — which server reboots which machine, with per-connection overrides shown as the route that would actually run.
*   **Anything crossing tenants is drawn in pink**, because one host or one credential shared between two customers is either deliberate shared infrastructure or a mistake, and both are worth looking at.
*   **A group is one hub, not every pair**: fifty machines on a subnet are drawn as fifty spokes to one labelled hub rather than 1,225 lines. Hovering any machine also lists its relationships in the tooltip, without switching lens.

### 🔦 Spotlight Search
*   **Ctrl+F, then type**: find any tenant or machine across the whole estate without hunting for it. Also on the **Graph** menu and the canvas right-click menu.
*   **Fuzzy, across every field**: matches on connection name, hostname, tenant, protocol and username — so `dc01`, `10.10.0.5`, `rdp` and `fabrikam` all narrow the graph. Typing `nwex` finds *Northwind-EX01*; you do not have to remember how you spelled it.
*   **Ranked the way you'd expect**: an exact hit beats a prefix, a prefix beats a substring, and a match on a word boundary beats one buried mid-word. A tenant outranks its own machines, so a customer name lands on the customer.
*   **The estate dims, the matches light up**: everything that did not match falls back behind a scrim while every match stays lit, and the one you are on gets a breathing ring. One machine among forty is genuinely findable.
*   **Arrows walk the results**: **↑↓** steps through every match with the camera following, **Enter** goes to it, **Ctrl+Enter** opens the connection outright, **Esc** closes. Clicking a row in the list does the same as Enter on it.
*   **Finds what is hidden**: a match inside a collapsed Hub-mode tenant reveals that tenant on the way to it. If probing is on, each result carries its reachability dot, so the list doubles as a status list.

### 🟢 Reachability Checks
*   **Three states, not two**: green means the port you actually connect on answered. **Amber** means the host replies to ping but the service is down — a stopped service, not a missing server. Red means nothing answered at all.
*   **Off by default**: probing reaches out to customer networks, so you switch it on deliberately — **Graph → Ping Connections**, or the canvas right-click menu.
*   **On a schedule or on demand**: sweeps run on an interval you set (default 30s), and **Check All Now** (`Ctrl + Shift + H`) or a single connection's **Check Reachability Now** forces one immediately.
*   **Checked the moment you log in**: unlocking the vault fires one sweep of the whole estate, whether or not continuous probing is on, so the graph is telling the truth by the time you have finished looking at it. Turn it off in **Graph Settings → Reachability** if you would rather reach out to nothing until you say so.
*   **Tenant roll-up**: a tenant tile shows the worst state among its machines, so one dead server never hides behind four healthy ones.
*   **No new dependencies**: plain TCP connects plus the system `ping`, so it works unchanged inside the packaged .exe.

### ⏻ Wake-on-LAN
*   **Wake a machine that is simply switched off**: save a connection's **MAC address** and RPMote can send it a magic packet — right-click the connection → **Wake (Wake-on-LAN)**, in the graph or the sidebar.
*   **Wake a whole tenant**: right-click a tenant tile → **Wake All in "…"**, or **Graph → Wake All** for everything with a MAC saved. Machines without one are left alone and told about, not silently skipped.
*   **Aims everywhere the packet could get through**: the subnet broadcast worked out from the host, the host's own address (in case the ARP entry survived), and the limited broadcast — sent from *every* local NIC, which is what makes it work with a VPN up or Hyper-V switches installed. Override the subnet with `wol_broadcast` or the port with `wol_port` on the connection if your network needs it.
*   **Tells you whether it actually woke**: waking one machine waits for it to answer and reports when it does; waking a batch re-checks reachability a minute later so the graph catches up on its own.
*   **Test it while you are still standing at the box**: the **⏻ Wake** button in the connection dialog fires a packet immediately, before you have even saved.

### 🔁 Remote Restart
*   **Direct restart, no setup at all**: for any SSH or SFTP connection, right-click → **Restart Directly over SSH…**. RPMote signs in with the credentials you already saved and reboots the machine. Nothing to configure first.
*   **Asks for sudo properly**: the reboot runs on a real PTY with the password fed on stdin, so `sudo` can actually authenticate instead of failing with *"a terminal is required"*. Offer the connection's saved password in one click, or type a different one; it is held in memory for the session only, never written to the vault, and masked out of any output shown back to you.
*   **Picks a method that works**: the default tries `systemctl reboot` and falls back to `shutdown -r now`, so the same command reboots a Linux box and a Mac. Override it per restart from the Method dropdown, or edit the command outright.
*   **Knows a reboot from a failure**: when the machine drops the connection without reporting an exit status, that is reported as a successful reboot, not an error — but a rejected sudo password is still reported as a rejected password, and a user missing from sudoers gets told that rather than being sent off to retype a correct password.
*   **Through a server you nominate**: for anything RPMote cannot shell into — an RDP-only Windows box, say — each tenant designates one of its own connections as its **restart server**, the box that already has the rights and the network path, and RPMote hands it the command.
*   **Two transports**: **SSH** to the restart server (uses the bundled paramiko), or **local PowerShell** reaching it over WinRM. Chosen per server, so a mixed Windows/Linux estate works.
*   **Your command, your placeholders**: edit the template directly, with `{host}`, `{server}`, `{tenant}` and more. Presets cover `shutdown.exe`, `Restart-Computer`, `Invoke-Command` and `systemctl reboot`. A live preview shows exactly what will run.
*   **Confirmation that tells you something**: the dialog names the target, the server the command travels through, and the exact command — and Cancel is the default button.
*   **Per-connection overrides**: one machine that needs a different route can have its own restart server without changing the tenant's.
*   **Refuses to be tricked**: a hostname containing shell control characters is rejected rather than substituted into a command.

### ☁️ Azure & Microsoft 365 Admin
*   **Multi-service console**: One local PowerShell tab that signs in to **Azure (Az)**, **Exchange Online**, **Microsoft Graph**, **Microsoft Teams**, **SharePoint/OneDrive (PnP)**, and **Security & Compliance**.
*   **Cross-tenant admin**: Point a connection at a customer tenant (Tenant ID + Organization domain) to administer their mailboxes, users, groups, and licensing as a systems admin.
*   **Auth modes**: Supports **Interactive**, **Device Code**, and **Service Principal** (client ID + secret, covering Azure and Graph) login flows.
*   **Verified connection status**: Reports authenticated, failed, and partially connected services instead of treating a running shell as a successful sign-in. Azure and Graph contexts stay scoped to the shell.
*   **Clean disconnect**: Disconnect, reconnect, tab close, and shell exit sign out of all loaded Microsoft services, clear the Azure process context, and remove remoting sessions. A stuck shell gets a 15-second cleanup deadline and a visible warning before forced closure.
*   **Admin command palette**: 145 ready-to-edit PowerShell snippets for Azure resources, VMs, storage, networking, roles, Key Vault, monitoring, web apps, mailboxes, users, groups, licensing, Teams, SharePoint, and compliance. Commands are loaded for review; replace placeholders and press Run. Use **Send input** to answer a running command's prompt.

### ⚡ Hardware Acceleration
*   **GPU rendering, on by default**: the dashboard graph paints through OpenGL at **60 FPS** (30 FPS on the software path), so a large estate stays smooth while the animated background runs. The embedded NAS/iDRAC browsers composite in software either way: handing their GPU frames to the OpenGL window leaks memory in Qt WebEngine 6.11.
*   **Built for big estates**: nodes outside the viewport are skipped, tenant tiles and connection pills are drawn once and reused until something about them changes, and animation speed is tied to the clock rather than the frame rate.
*   **One switch**: **View → Use Hardware Acceleration (GPU)**. Turn it off if you see rendering glitches, run inside a VM without a GPU, or use RPMote over RDP. Takes effect after a restart (RPMote offers one).
*   **Support override**: `set RPMOTE_HW_ACCEL=0` (or `=1`) forces the mode for a single launch without touching the saved setting. **Help → About** shows which renderer is active.

### 🚀 Fast Start
*   **The login screen comes up first**: only what the vault door needs is loaded before it appears. The rest of the app, the terminal fonts and the GPU driver are warmed up while you type your password.
*   **Unlocking is quick too**: the key and the password check are derived side by side, and the door animation is short.
*   **Timed in the log**: every `startup.*` line in `%LOCALAPPDATA%\RPMote\logs\crash-*.log` carries `t_ms` (milliseconds since launch) and `startup.warmup` lists what was warmed and how long each part took — the place to look if a launch ever feels slow.

---

## 🛠️ Getting Started

### Prerequisites
*   **Python 3.10+**
*   **Windows OS** (Required for RDP ActiveX integration)

### Installation

1.  **Clone the repository**:
    ```bash
    git clone <repository-url>
    cd RPMote
    ```

2.  **Set up a Virtual Environment** (Recommended):
    ```bash
    python -m venv .venv
    .venv\Scripts\activate
    ```

3.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

### Running the App
Launch the main application script:
```bash
python main.py
```

### Export a Windows EXE from PyCharm

The project includes a saved PyCharm run configuration named **RPMote - Clean EXE Export**.
Select it from the run-configuration dropdown and run it with the project `.venv` interpreter.
It removes the previous PyInstaller output, rebuilds the app, and verifies the Python,
Visual C++ runtime, and Qt WebEngine payload. The standalone executable is written to:

```text
dist\\RPMote\\RPMote.exe
```

For a full installer export, run `build.py --clean --installer` after installing Inno Setup 6.

The bundle includes the Visual C++ runtime beside `_internal\python310.dll`, so installed
RPMote does not require a separate Python or Visual C++ installation. A build with those
runtime DLLs only in `PyQt6\Qt6\bin` can report **Failed to load Python DLL** on a new
system; both the build verifier and installer scripts reject that incomplete layout.

---

## 📖 How to Use

### 1. Initial Setup
On your first launch, you will be prompted to set a **Master Password**. This password is used to encrypt your local vault. If you forget it, only a **recovery key** created beforehand can open the vault (see *Account Recovery* above) — so create one when you are offered it.

### 2. Adding Connections
*   Click the **"New Connection"** button in the sidebar or toolbar.
*   Choose an existing **Tenant** or type a new one, then enter the **Connection Name**. Existing tenants keep their saved color; use the color button to change it deliberately.
*   Select your protocol (**SSH, SFTP, FTP, RDP, iDRAC, NAS, or Azure PowerShell**).
*   Enter the host, port, and authentication details (Password or Private Key).
*   Optionally fill in the **MAC Address** to enable Wake-on-LAN for that machine.
*   Use the **"Test Connection"** button to verify connectivity before saving.

For a temporary connection, click **Quick Connect** at the top right or press **Ctrl + Shift + N** to expand the connection form. It starts collapsed and remembers your last choice; collapsing it keeps the values you entered. **Esc** closes the form while you are working in it. The theme, Connections sidebar, Library panel, and screenshot buttons stay visible beside the menus, even when Quick Connect is collapsed.

For NAS and iDRAC web consoles, an untrusted HTTPS certificate requires approval. Verify its SHA-256 fingerprint with the server administrator, then choose **Trust for This Session** or **Trust and Remember**. Remembered approvals are stored in your encrypted vault and apply to the exact server, port, certificate and approved certificate error. A changed certificate requires a new decision. Manage saved approvals under **File → Trusted Web Certificates**; restart RPMote after removing an approval to clear the browser's current session exceptions.

#### Azure PowerShell connection notes
When protocol is **Azure PowerShell**, configure:

*   **Sign-in**:
    *   `Interactive` (recommended for MFA/admin interactive use)
    *   `Device Code` (for restricted browser/sign-in environments)
    *   `Service Principal` (automation/non-interactive — covers Azure and Graph via client ID + secret)
*   **Services** — tick the Microsoft services this connection should sign in to:
    Azure (Az), Exchange Online (mailboxes), Microsoft Graph (users/licensing),
    Microsoft Teams, SharePoint/OneDrive (PnP), Security & Compliance.
*   **Tenant ID** — the customer tenant ID or domain to sign in to.
*   **Organization** — the customer's primary domain (e.g. `contoso.onmicrosoft.com`),
    used to derive the SharePoint admin URL and as the tenant fallback for Azure/Graph.
*   **Use Organization for delegated customer access** — enable only when signing into a
    customer's Exchange/Compliance tenant through delegated admin permissions (GDAP).
    Leave it off for direct tenant sign-in and optionally set Admin UPN.
*   **SharePoint URL** — optional explicit HTTPS admin/site URL, required when Organization
    is a vanity domain or when the SharePoint tenant name differs from the original domain.
*   **PnP Client ID** — your registered Entra app client ID for SharePoint/PnP sign-in.
    Current PnP.PowerShell requires PowerShell 7.4 or newer.
*   **Admin UPN** (optional) — the delegated/admin account to sign in as.
*   **Subscription ID** (optional default Azure context)
*   **Cloud** (AzureCloud, AzureUSGovernment, AzureChinaCloud, AzureGermanCloud)
*   For **Service Principal**, provide **Client ID** and **Client Secret**.
    This mode supports Azure and Graph; other services are disabled rather than silently
    signing in as an interactive user. App-only Graph access needs application permissions
    granted to the app; delegated scopes do not grant application permissions.
    Client ID can also specify a custom Graph app for delegated sign-in.

On connect, RPMote launches a profile-free local `pwsh` (or `powershell.exe` fallback), checks that the
required modules for the selected services are installed, then runs the matching
`Connect-*` cmdlet for each service (with your tenant/organization/subscription applied).
Each service uses terminating error handling so failures cannot report success, and one
failure does not abort the rest. The console shows Azure/Graph account and tenant context.
Use **Connect** to retry failed services, **Reconnect** to sign out and start a fresh shell,
and **Disconnect** to sign out and close the tab. Sign-in failures stay visible for review.
Device codes are supported for Azure, Graph, Exchange (PowerShell 7), Teams, and PnP;
Security & Compliance uses browser sign-in because its cmdlet does not support device codes.
If Graph's browser/broker login needs a native window handle, RPMote reports it and
retries with device-code sign-in.
The cloud selector also applies the matching Graph, Exchange, Teams, PnP, and compliance
endpoints (Azure US Government maps to GCC High for Microsoft 365 services).
Text `Read-Host` prompts appear in the console and accept replies through **Send input**;
secure prompts mask the reply. Native console-only prompts fail visibly in this embedded
host. For a reviewed cmdlet that needs native confirmation, supply its `-Confirm:$false`
or `-Force` option; the provided VM stop command already includes `-Force`.

> Prerequisite: install the modules for the services you use, e.g.:
>
> `Install-Module Az, ExchangeOnlineManagement, Microsoft.Graph, MicrosoftTeams, PnP.PowerShell -Scope CurrentUser`
>
> The command palette's **Connect** category includes this installer. The full **Az**
> module set is needed for Azure tasks; **Az.Accounts** alone only provides account/context cmdlets.

### 3. Using the Terminal
Once connected via SSH, use the terminal just like any standard shell. Use the toolbar icons to:
*   Adjust font sizes.
*   Open the **SFTP Browser** for the current session.

### 4. File Management
Double-click folders to navigate. Right-click files or folders to access upload, download, and delete options. Transfer progress is visible at the bottom of the browser panel.

---

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| `Ctrl + P` or `Ctrl + Shift + Space` | Go to a connection, tenant or command |
| `Ctrl + N` | New Connection |
| `Ctrl + ,` | Settings |
| `Ctrl + Shift + N` | Show / Hide Quick Connect |
| `Ctrl + T` | Next Theme |
| `Ctrl + Tab` / `Ctrl + Shift + Tab` | Next / previous tab (also inside a terminal) |
| `Ctrl + Shift + W` | Close Current Tab (also inside a terminal) |
| `Ctrl + W` | Close Current Tab (outside a terminal) |
| `Ctrl + K` | Run a command or snippet in the current session |
| `Ctrl + Shift + B` | Broadcast session |
| `Ctrl + B` | Toggle Sidebar |
| `Ctrl + Shift + S` | Toggle Library Panel |
| `Ctrl + Shift + L` | Lock Screen (keep sessions running) |
| `Ctrl + L` | Lock Vault |
| `Ctrl + +` | Increase Font Size |
| `Ctrl + -` | Decrease Font Size |

### Vault

| Shortcut | Action |
| :--- | :--- |
| `Ctrl + Shift + P` | Saved Passwords |
| `File → Recovery Key` | Create, replace or remove your recovery key |

### Dashboard Graph

| Shortcut | Action |
| :--- | :--- |
| `Right-drag` | Pan the canvas (middle-drag and empty-canvas left-drag also work) |
| `Right-click` | Context menu for the tenant, connection, link or canvas under the cursor |
| `Alt + drag` | Draw a visual link between any two nodes (tenants or connections) |
| `Double-click` | Open the connection under the cursor |
| `Wheel` | Zoom toward the cursor |
| `Arrow keys` | Pan |
| `Delete` | Remove the selected link |
| `Ctrl + Shift + F` | Fit graph to view |
| `Ctrl + Shift + R` | Reset view |
| `Ctrl + Shift + G` | Toggle link mode |
| `Ctrl + Shift + H` | Check every connection's reachability now |
| `Click a tile` | Open or collapse that tenant (free-floating layout, large estates) |
| `Esc` | Clear the relationship lens (or cancel link mode / close search) |

---

## 🧰 Built With
*   **PyQt6**: Modern Python bindings for Qt.
*   **Paramiko**: SSHv2 protocol library.
*   **Cryptography**: Fernet vault encryption.
*   **PyWin32**: Windows API integration for RDP.

---

*Developed by Grant*
