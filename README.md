**🇬🇧 English** | [🇩🇪 Deutsch](README.de.md)

# EXO Signature Service

> **License:** [PolyForm Internal Use License 1.0.0](LICENSE.md) (Community
> Edition) — free for your own/internal use (including within a company)
> for **up to 100 mailboxes**. Beyond that, a paid Commercial License is
> required — contact alexander@zarenko.net. Not permitted regardless of size:
> redistribution, operation as a service for third parties, or
> selling/offering as a service by anyone other than the licensor.

A Docker-based SMTP proxy that automatically embeds personalized email signatures in Exchange Online (EXO) – and **can run entirely on Azure without outbound port 25**.

Exchange Online offers server-side transport rules for disclaimers – but these are limited to simple, static text blocks with a restricted set of AD attributes. This service goes considerably further:

- **Dynamic signatures via Graph API** – job title, phone, department, website, booking URLs and custom fields (`extensionAttributes`) are fetched live per sender
- **Full HTML freedom** via Jinja2 templates – no comparison to the limited options of EXO transport rules
- **Intelligent reply detection** – the signature is inserted before the quoted text, not appended at the end; no stacked signatures in mail threads; an existing signature already present in the thread is detected and not inserted a second time
- **Client signatures are detected and removed** (Outlook Mobile and others), so no duplicate signatures arise
- **S/MIME signing and encryption** – integrated, configurable per sender, with Azure Key Vault support
- **Automatic S/MIME certificate lifecycle** via CASTLE ACME (RFC 8823) – fully automatic renewal without user interaction (differs from Let's Encrypt ACME, which only issues TLS certificates)
- **Azure-compatible** – no outbound port 25 required; all outbound connections run over HTTPS (Graph API) and IMAPS (port 993)

---

## How it works

Outlook and other mail clients always communicate directly with Exchange Online (via MAPI or REST) – never through this service. The service hooks into the outbound path of Exchange Online as a **server-side transport gateway**:

```
Outlook / mail client
       │ MAPI / EAS / REST
       ▼
Exchange Online (EXO)
       │ Outbound Transport Connector → SMTP port 25 (inbound to the gateway)
       ▼
EXO Signature Service  ◄── MS Graph API (sender data)
       │
       ├─ graph mode (default/Azure): Graph API sendMail HTTPS
       │
       ├─ imap mode (Azure, S/MIME inbound): Graph API sendMail HTTPS (outbound)
       │                                      IMAP APPEND port 993 (S/MIME decryption)
       │
       └─ smtp mode (classic): SMTP port 25 → EXO smarthost
       ▼
Exchange Online (EXO) → delivery to recipient
```

1. Outlook sends the mail to Exchange Online as usual (MAPI/REST).
2. An **EXO transport rule** routes outbound mail from the configured senders through a **send connector** to this service (SMTP port 25, inbound to the gateway container).
3. The service looks up the sender via the Microsoft Graph API and injects the Jinja2 signature (HTML + plaintext) – also for plain-text and TNEF mails (Outlook RTF).
4. The signed mail is handed back to Exchange depending on `REINJECT_MODE` (details below).

---

## Network requirements

### Inbound (firewall / port forwarding on the host)

| Port | Protocol | From whom | Purpose | When needed |
|------|----------|-----------|---------|-------------|
| **25** | SMTP + STARTTLS | Exchange Online (transport connector) | Incoming mail for processing | **always** |
| **80** | HTTP | Let's Encrypt server / browser | HTTP-01 ACME challenge; first-run setup wizard | **always** |
| **443** | HTTPS | admins / browser | Web UI & REST API | **always** |

> Internally the container listens on port 8080; Docker maps `443:8080`. On the router / in the VM firewall
> open only port 443 (not 8080).

### Outbound (container → internet)

| Port | Protocol | Target | Purpose | Re-inject mode |
|------|----------|--------|---------|----------------|
| **443** | HTTPS | `graph.microsoft.com` | Graph API: user data, sendMail, mailbox polling | all |
| **443** | HTTPS | `login.microsoftonline.com` | Azure AD token endpoint | all |
| **443** | HTTPS | `acme.castle.cloud` | CASTLE ACME (S/MIME certificates) | on CASTLE enrollment |
| **443** | HTTPS | `acme-v02.api.letsencrypt.org` | Let's Encrypt TLS certificate | on Let's Encrypt |
| **993** | IMAPS | `outlook.office365.com` | IMAP APPEND (inbox inject without draft flag) | `imap` |
| **25** | SMTP | `<tenant>.mail.protection.outlook.com` | Re-inject via SMTP | `smtp` (not Azure-compatible) |

Azure VMs block outbound port 25. With `REINJECT_MODE=graph` or `imap` the service runs entirely without outbound port 25.

---

## Running on Azure (no outbound port 25)

With `REINJECT_MODE=graph` (default) or `imap` the service runs without outbound port 25. Both modes need only outbound port 443 (HTTPS). The `imap` mode adds IMAPS port 993 for a specific use case (see below).

### Create an Azure VM (PowerShell script)

The included `azure-vm-setup.ps1` script provisions a Standard_B1ms VM (Ubuntu 24.04,
32 GB disk) with a static IP and pre-configured firewall, and installs Docker + the gateway:

```powershell
.\azure-vm-setup.ps1 `
    -ResourceGroup "exo-gateway-rg" `
    -Location "germanywestcentral" `
    -SshPublicKeyFile "~/.ssh/id_rsa.pub" `
    -RepoUrl "https://github.com/azitc-ac/EXO-signature-service.git"
```

When finished, the script prints the public IP and the next steps (set DNS,
open the setup wizard).

**Why B1ms?** 2 GB RAM — building PowerShell + the ExchangeOnlineManagement module
briefly needs ~1.5 GB; in steady-state operation ~70–150 MB is enough. B1ms (~€14/month)
is the sweet spot for up to a few hundred mailboxes.

**When is IMAP APPEND needed?**  
Only in one specific case: an external sender sends an S/MIME-encrypted mail to an internal recipient. The gateway decrypts it and must place the result directly into the recipient's mailbox. The Graph API (`POST /mailFolders/inbox/messages`) sets the `MSGFLAG_UNSENT` flag in doing so — Outlook shows the mail as a draft with a Send button. IMAP APPEND does not set this flag; the message arrives as a genuine received mail. **Outbound mail** (signature injection) still goes through Graph API sendMail in `imap` mode.

### Prerequisites for imap mode

In addition to the standard permissions:

1. **Entra portal**: App → API permissions → Office 365 Exchange Online → `IMAP.AccessAsApp` → admin consent
2. **Setup wizard** → step "Set up IMAP access" (registers the service principal in EXO + grants `FullAccess` on all mailboxes)

---

## Prerequisites

- Docker + Docker Compose
- Microsoft 365 / Exchange Online tenant
- An Azure app registration with these **application permissions**:

| Permission | Required | Purpose |
|---|---|---|
| `User.Read.All` | ✓ | Read user data (signature fields) via Graph |
| `Mail.Send` | ✓ | Send mail via Graph API (re-inject + ACME) |
| `Mail.ReadWrite.All` | ✓* | Patch sent items; includes `Mail.Read.All` |
| `Exchange.ManageAsApp` | ✓ | EXO PowerShell (connector/DG setup via wizard) |
| `IMAP.AccessAsApp` | IMAP mode | IMAP APPEND into recipient mailboxes (no draft) |

*\* Includes `Mail.Read.All` (needed for CASTLE ACME mailbox polling).*

---

## Re-inject modes

> **Terminology:** The *mode* (`REINJECT_MODE`) determines how the gateway hands fully processed mail back to Exchange. IMAP APPEND is not a mode of its own, but a mechanism within `imap` mode for a specific case (S/MIME decryption).

The mode is configured in the Web UI under **Settings → Re-inject mode** or via `REINJECT_MODE`.

### `graph` — default / Azure

- **All** re-injects via Graph API `sendMail` (HTTPS)
- No port 25 required; works on Azure
- S/MIME-decrypted inbound mails are injected via `/mailFolders/inbox/messages` – can produce a draft status
- No `IMAP.AccessAsApp` required

### `imap` — Azure with S/MIME inbound without draft flag

- **Outbound mail** (signature injection): identical to `graph` — Graph API `sendMail` (HTTPS)
- **S/MIME-decrypted inbound mail**: IMAP APPEND (port 993) directly into the recipient's mailbox — no draft flag, no "Send" button in Outlook
- **No outbound port 25** required
- Requires `IMAP.AccessAsApp` + EXO service principal (can be set up via the setup wizard)

### `smtp` — classic / on-premises

- Re-inject via SMTP + STARTTLS to the EXO smarthost (`<tenant>.mail.protection.outlook.com:25`)
- Requires outbound port 25 – **not Azure-compatible**
- No additional app permission required

---

## Quick start

### Supported architectures — x86-64 (amd64) and ARM64

The image builds **natively on both `linux/amd64` (x86-64) and `linux/arm64`**
— so a self-hosted deployment works equally on an ordinary x64 server/VM and on
a Raspberry Pi. There is nothing to configure: `docker compose up -d --build`
detects the host architecture and builds the matching image (base image,
PowerShell, and all Python dependencies are multi-arch).

> **Build on the target architecture natively.** Do **not** cross-build the
> image via QEMU emulation (e.g. `docker buildx --platform linux/amd64` on an
> ARM host): the build installs the `ExchangeOnlineManagement` PowerShell
> module, which runs .NET — and .NET aborts under QEMU user-mode emulation
> (SIGABRT / exit 134). This is an emulation limitation, **not** an
> architecture problem. Build on a real x64 machine for x64, on ARM for ARM.

### 1. Clone the repository

```bash
git clone https://github.com/azitc-ac/EXO-signature-service.git
cd EXO-signature-service
```

### 2. Start

```bash
docker compose up -d
```

### 3. First run — TLS certificate via port 80

On the very first start there is no TLS certificate yet. The service starts a
minimal setup wizard on **port 80 (HTTP)**:

```
http://<public-ip>
```

> DNS does not need to be set yet — IP access is enough for this step.

Enter the hostname (`sig.example.com`) and Let's Encrypt email there → **Request certificate**.
Prerequisite: the hostname's DNS already points to this IP (Let's Encrypt validates via DNS).

After success:

```bash
docker compose restart
```

### 4. HTTPS setup wizard

From now on the Web UI is reachable over HTTPS:

```
https://sig.example.com
```

> **Default login:** username `admin`, password `admin` — or `changeme` on Azure VMs
> provisioned by `azure-vm-setup.ps1` (it writes the placeholder into `.env`). The setup
> wizard's first step forces you to set a new password before anything else.

The setup wizard guides you through the initial configuration:

- Azure app registration (automatically via PKCE flow incl. admin consent)
- EXO connector, transport rule, distribution group
- Optional: IMAP access for Azure operation (`REINJECT_MODE=imap`)

### 5. Optional `.env` for secrets

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `TENANT_ID` | Azure AD tenant ID |
| `CLIENT_ID` | App registration client ID |
| `CLIENT_SECRET` | App registration secret |
| `EXO_SMARTHOST` | Exchange Online smarthost (SMTP mode only) |
| `WEBUI_PASSWORD` | Password for the Web UI |
| `WEBUI_SECRET_KEY` | Random string for session signing |

---

## Configuration

### Settings (Web UI / `data/settings.json`)

| Setting | Default | Description |
|---|---|---|
| `REINJECT_MODE` | `graph` | `imap` (Azure, no port 25), `graph` (default), `smtp` (port 25) |
| `FALLBACK_ON_ERROR` | `true` | Forward mail without signature on error |
| `LOG_LEVEL` | `INFO` | Log level (takes effect after restart) |
| `WEBUI_USERNAME` | `admin` | Username for the Web UI |
| `LOOP_HEADER` | `X-Sig-Applied` | Header to prevent double injection |
| `SENT_ITEMS_UPDATE` | `false` | Write the signature into Sent Items |
| `EXO_PORT` | `25` | Port to the EXO smarthost (SMTP mode only) |
| `USER_WEBSITES` | `{}` | Website URL per email address (override) |
| `USER_BOOKINGS` | `{}` | Booking URL per email address (override) |

---

## Web UI

Reachable at `https://<host>:8080`.

| Page | Function |
|---|---|
| **Dashboard** | Mail statistics (today / month / year), S/MIME counters, errors, certificate expiry, system monitoring (RAM, disk, logs, in-flight, avg. processing time) |
| **Mailboxes** | Manage enabled mailboxes, sync the EXO distribution group |
| **Templates** | Edit signature HTML and plaintext |
| **Preview** | Render the signature for any email address |
| **Settings** | All configuration options, test mail, Let's Encrypt, re-inject mode, Entra app |
| **S/MIME** | Certificate upload, Azure Key Vault migration, CASTLE ACME enrollment |
| **Log** | Live view of the service logs |
| **Add-in** | Office add-in for Outlook (signature preview and selection in the compose window) |
| **Setup** | Initial configuration wizard (app registration, connector, IMAP access, SMTP hostname) |
| **Debug** | ACME send method, account key reset, Exchange Header Observatory |

---

## S/MIME signing & encryption

Mail can be digitally signed and/or encrypted per sender.

1. Upload the PFX certificate via the Web UI under **S/MIME → Import**
2. The certificate is automatically assigned to the sender; multiple certificates per address are possible
3. Works in all modes (Graph, IMAP, SMTP)

**Azure Key Vault integration:**  
Private keys can optionally be stored in Azure Key Vault. The service then signs via the Key Vault Sign API – the private key never leaves the HSM. Fallback mode (`KV_KEY_MODE=fallback`) allows a local backup copy as a safety net.

For encryption, recipient certificates are managed separately (section "Recipient certificates").

---

## S/MIME certificate lifecycle & auto-enrollment (CASTLE ACME)

The service monitors certificate expiry dates and can trigger renewals. A CA backend is configurable per user.

### Available backends

| Backend | Type | Description |
|---|---|---|
| **Assisted Manual** | manual | The user receives an email with a link to the CA portal and a self-service upload link |
| **CASTLE Platform (RFC 8823)** | fully automatic | The ACME order is placed automatically, the certificate is imported without user interaction |

### CASTLE ACME – flow

```
Gateway          CASTLE ACME              Exchange Online (mailbox)
   │                  │                           │
   │── new-order ────►│                           │
   │◄── authz ────────│                           │
   │                  │── challenge email ────────►│
   │                  │   Subject: "ACME: <token>" │
   │◄── Graph API inbox poll (every 30 s) ────────│
   │── Graph sendMail (Re: ACME: …) ──────────────►│
   │                  │   (Exchange → connector → gateway → rebuild → CASTLE MX)
   │                  │◄── RFC 8823-compliant reply
   │── trigger_challenge ──►│                      │
   │── finalize (CSR) ─────►│                      │
   │◄── certificate (PEM) ──│                      │
   │── imports certificate, admin notification
```

**Why Graph API polling instead of SMTP intercept:**  
The MX record of the customer domain points to Exchange Online — the challenge email goes directly into the mailbox without passing through the gateway. The service therefore actively polls the mailbox via the Graph API.

**No port 25 / no ACS needed:**  
The challenge reply is sent via `Graph sendMail`. Exchange routes the reply mail back through the gateway over the outbound connector — the handler recognizes `Subject: Re: ACME:` and rebuilds an RFC 8823-compliant MIME with CRLF. This way the reply reaches CASTLE – without direct port-25 access.

---

## Signature templates

Templates live under `./templates/` and are mounted into the container via a volume – changes take effect immediately without a rebuild.

### Available Jinja2 variables (`{{ user.* }}`)

| Variable | Source |
|---|---|
| `user.displayName` | Graph `displayName` |
| `user.jobTitle` | Graph `jobTitle` |
| `user.department` | Graph `department` |
| `user.companyName` | Graph `companyName` |
| `user.mail` | Graph `mail` |
| `user.phone` | Graph `businessPhones[0]` |
| `user.mobilePhone` | Graph `mobilePhone` |
| `user.officeLocation` | Graph `officeLocation` |
| `user.website` | `USER_WEBSITES` override → `businessHomePage` → `extensionAttribute1` |
| `user.bookingsUrl` | `USER_BOOKINGS` override → `extensionAttribute2` |

---

## Mail types

The service correctly processes all common MIME formats:

- **HTML mails** – the signature is inserted before `</body>`
- **Plain-text mails** – converted to `multipart/alternative` (text + HTML)
- **TNEF/Winmail.dat** (Outlook RTF) – unpacked, HTML body extracted or reconstructed from plaintext
- **Multipart mails** – HTML and text parts are each supplemented separately

---

## Let's Encrypt

Port 80 is already open in the included `docker-compose.yml` (HTTP-01 challenge).

1. Make sure port 80 and port 25 are reachable externally (firewall / NAT rule).
2. In the Web UI under **Settings → TLS / Let's Encrypt** enter domain and email.
3. Click the **Renew certificate** button.
4. After success: **Restart the service** (button in Settings or `docker compose restart`).

**Separate SMTP hostname:** If the web hostname is behind a reverse proxy / Azure Application Proxy and cannot receive SMTP, a separate `SMTP_HOSTNAME` with its own certificate can be configured (setup wizard step 2).

---

## Sent Items

When `SENT_ITEMS_UPDATE` is enabled, after sending the service patches the mail in the sender's Sent Items with the signed version.

**Prerequisite:** `Mail.ReadWrite.All` application permission with admin consent.

---

## Volumes

| Path (host) | Container | Content |
|---|---|---|
| `./templates/` | `/app/templates/` | Signature templates (take effect immediately) |
| `./certs/` | `/app/certs/` | TLS certificate (`cert.pem`, `key.pem`) |
| `./data/` | `/app/data/` | Settings, S/MIME certificates, ACME data |

---

## Architecture

```
app/
├── main.py              Entry point (SMTP + Web UI + ACME HTTP server)
├── config.py            Secrets from environment variables
├── settings_store.py    Persistent settings (data/settings.json)
├── handler.py           SMTP handler (loop check → Graph → signature → reinject)
├── graph_client.py      MS Graph API (user data, sent-items update, inbox polling)
├── graph_reinject.py    Graph API sendMail + inbox inject
├── smtp_submit.py       IMAP APPEND (inbox inject without draft flag, Azure mode)
├── reinject.py          Re-inject routing (imap / graph / smtp)
├── mail_processor.py    Signature injection (HTML, plaintext, TNEF)
├── mail_audit.py        SQLite audit log of all processed mails (data/mail_audit.db)
├── signature_engine.py  Jinja2 rendering
├── loop_detector.py     Prevent double injection (configurable header)
├── health_check.py      /health endpoint for the Docker healthcheck
├── log_manager.py       Rotating log files (data/logs/)
├── smime_signer.py      S/MIME signing (local or via Azure Key Vault)
├── smime_encrypt.py     S/MIME encryption (recipient certificates)
├── smime_decrypt.py     S/MIME decryption of incoming mail
├── cms_sign.py          PKCS#7 / CMS SignedData builder (Key Vault Sign API)
├── keyvault.py          Azure Key Vault integration (import, sign, RBAC)
├── smime_store.py       Certificate management (multi-slot, KV migration, backup)
├── smime_harvest.py     Harvest incoming S/MIME certificates
├── mime_observatory.py  Exchange header analysis (Debug tab)
├── acme_client.py       ACME v2 client (RFC 8555)
├── acme_state.py        ACME order state, Graph API mailbox polling
├── ca_backends/         CA backend abstractions (Assisted Manual, CASTLE ACME)
├── selfservice.py       Token-based self-service upload
├── scheduler.py         Daily lifecycle check (expiry dates, notifications)
├── notification.py      Email notifications (admin + user)
├── setup_wizard.py      Initial configuration wizard
├── pkce.py              Azure PKCE OAuth flow
├── sso.py               Entra SSO (OIDC/PKCE), session management, RBAC
├── stats.py             Mail statistics (in-memory + daily JSON time series)
└── webui/
    ├── app.py           FastAPI Web UI (routes, API endpoints)
    └── templates/       Jinja2 HTML templates of the Web UI
```

---

## License

[PolyForm Internal Use License 1.0.0](LICENSE.md) (Community Edition) — free
for up to 100 mailboxes; a paid Commercial License is required beyond that.
See the full text in `LICENSE.md`.
