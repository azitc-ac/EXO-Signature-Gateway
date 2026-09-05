# Changelog — EXO SMTP Relay

## 0.1.0 — 2026-09-04

Erste Fassung: das SMTP-Relay des EXO Signature Gateway als eigenständiger,
schlanker Dienst.

- **Mailpfad**: Geräteliste, Lernmodus, Absender- und Zielgrenze sind mit dem
  Gateway inhaltsgleich (`smtp_relay.py`, `relay_hosts.py`; geprüft von
  `tools/driftcheck.py`). Der Rückweg geht über den Smarthost auf Port 25 oder
  wahlweise über Port 587 mit Dienstkonto. Gezählt wird zugestellte Post;
  scheitert der Smarthost, antwortet der Dienst mit 451.
- **Adressquelle**: Postfachliste per ExchangeOnlineManagement-Modul, mit
  Plattencache über Neustarts hinweg, ergänzt um Adressen von Hand.
- **Weboberfläche**: Übersicht, Geräte, Einstellungen, Protokolle. Nur örtliche
  Anmeldung, gedrosselt; Herkunftsprüfung und Sicherheits-Header.
- **Zertifikate**: TLS-Zertifikat selbstsigniert oder aus PFX; Auth-Zertifikat
  für die App-Registrierung ohne `openssl`-Binary.
- **Exchange Online**: Anmeldetest und Inbound-Connector (Zertifikat- oder
  Adressvariante) über `scripts/setup_relay_connector.ps1`, PowerShell 5.1 und 7.
- **Verpackung**: Docker (amd64/arm64), systemd-Unit, Windows-Dienst mit
  `windows/install.ps1`.
