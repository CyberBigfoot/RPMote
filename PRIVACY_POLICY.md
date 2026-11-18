# Privacy Policy for RPMote

**Effective Date:** September 18, 2026
**Last Updated:** September 18, 2026
**Version:** 1.0.0

## Overview

RPMote is a desktop remote connection manager that prioritizes user privacy. This policy explains what data RPMote handles, how it is stored, and what leaves your machine.

## Data Collection

**RPMote does not collect, transmit, or share any personal data, usage analytics, telemetry, crash reports, or behavioral data.** There are no third-party analytics services, tracking pixels, or data brokers integrated into this application.

## Data Stored Locally

RPMote stores the following data **exclusively on your local machine** in the `~/.rpmote/` directory:

### Credential Vault (`vault.enc`)
- Connection details (hostnames, ports, usernames, passwords, SSH key paths)
- Azure service principal credentials (client ID, client secret, tenant ID)
- Command snippets
- Application settings (theme preferences, dashboard layout)

**All vault data is encrypted at rest using AES-256 via Fernet symmetric encryption.** The encryption key is derived from your master password using PBKDF2-HMAC-SHA256 with 600,000 iterations and a 32-byte random salt. There is no password recovery mechanism; if you lose your master password, the vault data cannot be decrypted.

### Vault Metadata
- `vault.salt` — Random salt for key derivation (not sensitive)
- `vault.hash` — PBKDF2 hash of the master password for verification

## Network Activity

RPMote has no update check and makes **no unsolicited network connections**.

### User-Initiated Connections
RPMote connects to remote hosts **only when you explicitly initiate a connection** (SSH, SFTP, FTP, RDP, iDRAC, NAS, Azure PowerShell). These connections go directly to the hosts you configure — RPMote does not proxy, intercept, or log this traffic.

## Data That Never Leaves Your Machine

- Passwords and credentials (encrypted in the local vault)
- Session terminal output (held in memory only; optionally saved to a file you choose)
- Connection history and configuration
- Screenshots (saved to your Desktop, never uploaded)
- Command snippets and bash library usage

## Session Logging

RPMote keeps an **in-memory** session log of terminal output for the current SSH session. This log:
- Exists only in RAM while the session is active
- Is **never** written to disk automatically
- Can be saved to a file **only** by explicit user action ("Save Session Log" button)
- Is discarded when the session closes

## Debug Output

RPMote outputs minimal debug messages to `stdout` (the console/terminal) for troubleshooting purposes. These messages:
- Contain only error descriptions and status indicators (e.g., "Monitoring error: timeout")
- Do **not** contain credentials, hostnames, IP addresses, or personal data
- Are **not** written to any log file
- Are only visible if the application is launched from a terminal/console

## Third-Party Services

RPMote does **not** integrate with:
- Analytics platforms (Google Analytics, Mixpanel, etc.)
- Crash reporting services (Sentry, Bugsnag, etc.)
- Advertising networks
- Data brokers or aggregators
- Cloud storage (beyond user-configured connections)

## Embedded Web Browser

The embedded Chromium browser (used for iDRAC and NAS consoles) operates with an isolated profile per protocol. Browser data (cookies, sessions) is managed locally by Qt WebEngine and is not shared with or accessible to RPMote application code.

## RDP Credential Handling

For RDP connections, RPMote may temporarily store credentials in the **Windows Credential Manager** (via `cmdkey`) to enable auto-login. These credentials are managed by Windows and follow the Windows credential lifecycle.

## Data Retention

- Vault data persists until you delete it or perform a Factory Reset
- In-memory session data is cleared when the application closes
- No data is retained on remote servers by RPMote itself

## Children's Privacy

RPMote does not knowingly collect any information from children under 13 years of age.

## Changes to This Policy

Updates to this privacy policy will be reflected in this document with an updated "Last Updated" date. Significant changes will be noted in the application's release notes.

## Contact

For privacy concerns or questions, please open an issue on the [RPMote GitHub repository](https://github.com/CyberBigfoot/RPMote).

## Open Source

RPMote is open source under the MIT License. You can audit the complete source code to verify these privacy claims.
