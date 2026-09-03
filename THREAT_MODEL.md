# Threat Model — EXO Signature Gateway

Dieses Dokument beschreibt die **Vertrauensgrenzen** des Gateways, die
durchgesetzten Schutzmaßnahmen und die verbleibenden Annahmen/Restrisiken. Es
ist eine Momentaufnahme des Stands und wird mit dem Produkt fortgeschrieben. Für
den Meldeweg von Sicherheitsproblemen siehe [SECURITY.md](SECURITY.md).

## Geltungsbereich

Das Gateway hängt **serverseitig** im ausgehenden (und teils eingehenden)
Mailfluss von Exchange Online: Exchange liefert per Transport-Connector auf
Port 25 ein, das Gateway fügt Signatur/S-MIME hinzu und reicht die Post zurück.
Dazu kommt eine Web-Oberfläche (443) und ein ACME-/First-Run-Server (80).

**Außerhalb des Geltungsbereichs:** Spam-/Malware-Filterung (bewusst nicht Aufgabe
des Gateways), die Plattformsicherheit von Microsoft 365 / Azure, die physische
Sicherheit des Hosts sowie DoS auf Bandbreitenebene.

## Schützenswerte Güter

- **Integrität der Post** (keine fremde Einschleusung/Weiterleitung unter fremder Identität)
- **Reputation des eigenen Tenants** (kein offenes Relay)
- **Private S/MIME-Schlüssel** und **App-Zugangsdaten** (Client-Secret, Auth-Zertifikat)
- **Verwaltungszugang** zur Oberfläche (Rollentrennung)
- **Gespeicherte Daten** (Audit-Log mit Betreffzeilen, verschlüsselte Portalinhalte)

## Vertrauensgrenzen & durchgesetzte Maßnahmen

| Grenze | Wer/Was | Durchgesetzt |
|---|---|---|
| **Port 25** (Exchange → Gateway) | Exchange-Online-Adressbereich; freigegebene Relay-Geräte | Quell-IP-Prüfung (`smtp_acl`); **Tenant-Gate**: Post aus dem EXO-Raum muss `X-MS-Exchange-CrossTenant-Id == TENANT_ID` tragen, sonst `554` (`RELAY_TENANT_CHECK`); STARTTLS-Pflicht (Lockerung **nur** für eingetragene Relay-Geräte); Loop-Header auf allen Rückwegen |
| **Port 80** (öffentlich) | Let's-Encrypt-Server, First-Run-Browser | ACME-Challenges werden **nur** aus dem webroot ausgeliefert (Pfad-Containment, keine `..`-Traversal); First-Run-Wizard ist nach Anmeldung gated; sonst Umleitung auf HTTPS |
| **Port 443** (Web-UI/API) | Verwaltung, Bearbeiter, Add-in | Rolle & Kennung aus **derselben** signierten Sitzungsquelle (Cookie ODER `X-Addin-Session`); Login-Drosselung (Backoff je IP/Benutzer); Security-Header (nosniff, Referrer, X-Frame-Options, HSTS); Herkunftsprüfung (Origin/Referer) auf mutierende Anfragen; OpenAPI/`/docs` abgeschaltet; Vorlagen in Jinja-Sandbox |
| **Container ↔ Host** (Update-Kette) | root-Watcher, Trigger-Dateien | Trigger-Werte werden gequotet übergeben (Whitelist-Prüfung offen); Bind-Mount-Rechte 600/700 (`secure_io`) |
| **Gateway ↔ Exchange/Graph/Azure** | App-Registrierungen | EXO per **Zertifikat** (kein Secret); Sitzungs-Cookies signiert, `httponly`/`samesite=lax`/`secure`; Geheimnisse in `settings.json` mit 600 |
| **Ruhende Daten** (`data/`) | Dateisystem des Hosts | Schlüssel 0600, Ordner 0700 (`secure_io.harden_tree`); Portalinhalte AES-256-GCM-verschlüsselt (Schlüssel im Link-Fragment, nicht in der DB) |

## Kernannahmen & Restrisiken

- **EXO-Adressbereich ≠ eigener Tenant.** Microsofts Bereiche teilen sich alle
  M365-Tenants. Das Tenant-Gate erzwingt die Herkunft; fehlt jedoch die
  `X-MS-Exchange-CrossTenant-Id` oder die eigene `TENANT_ID`, wird angenommen und
  sichtbar protokolliert (kleines Restrisiko).
- **`smtp_acl` ist bei NIE geladener Liste fail-open** („Mailfluss vor Strenge"
  beim Erststart). Abgefedert durch das Tenant-Gate.
- **`SMIME_KEY_PASSWORD` liegt in `settings.json`** neben den verschlüsselten
  Schlüsseln. Das schützt gegen Teilkopien, **nicht** gegen Vollzugriff auf
  `data/` — wer das Verzeichnis liest, hat beides. Alternative: Azure Key Vault.
- **Lieferkette.** Der Update-Watcher läuft als **root** und macht
  `git reset --hard` ohne Signatur-/Tag-Prüfung; der pwsh-Tarball wird ohne
  Prüfsumme geladen. Das Base-Image ist per Digest gepinnt, `requirements.lock`
  aus dem Produktivcontainer. (Härtung offen: `git verify-tag`, pwsh-SHA256.)
- **CSP ist vorerst nur berichtend** (`Content-Security-Policy-Report-Only`), weil
  die Oberfläche viele Inline-Styles/-Skripte nutzt.
- **Single Point of Failure.** Ein Container im Mailfluss; fällt er aus, staut
  Exchange die Post. Milderung: Bypass-Wächter (in Arbeit) stellt bei Ausfall
  unsigniert zu.
- **Graph-Berechtigungen sind tenantweit** (`Mail.Send`, `Mail.ReadWrite.All`,
  `User.Read.All`). Feineres Scoping (ApplicationAccessPolicy / RBAC for
  Applications) ist geplant.
- **Zwei Gateways im selben Tenant** kollidieren, wenn dieselbe Adresse in
  beiden Verteilerlisten steht bzw. beide „Route via"-Regeln aktiv sind
  (Doppelsignatur/Fremdsignatur). Ein Name → genau ein Gateway.

## Verifikation

Die Rollen-, Tenant-, Pfad- und Härtungsgrenzen sind durch Tests abgesichert
(u.a. `test_rollen`, `test_tenant_gate`, `test_acme_pfad`, `test_web_haerten`,
`test_login_drossel`, `test_relay_tls`). Ein grüner Lauf beweist die Wirkung der
jeweiligen Wache; er ersetzt nicht die Betrachtung neuer Angriffsflächen.
