# Hinweise zu Drittanbieter-Code

Der überwiegende Teil des EXO Signature Gateway steht unter der Lizenz in
`LICENSE.md`. Die folgenden Teile stammen aus anderen Projekten und stehen
unter ihrer eigenen Lizenz; diese geht für die jeweiligen Dateien vor.

## DNS-Provider-Katalog (`app/dns_provider/`)

- **Herkunft:** EXO SMTP Relay — <https://github.com/azitc-ac/exo-smtp-relay>,
  Ordner `app/dns_provider/`, Katalogfassung laut `app/dns_provider/KATALOG_VERSION`
  (übernommen aus dem Relay-Stand v0.2.57-beta).
- **Lizenz:** Apache License, Version 2.0 — vollständiger Text in
  `LICENSES/Apache-2.0.txt`. Jede Datei trägt den Kopf
  `SPDX-License-Identifier: Apache-2.0`.
- **Änderungen:** keine. Der Katalog wird unverändert übernommen; die
  Prüfsummen in `KATALOG_VERSION` und `tests/test_dns_katalog_gleichlauf.py`
  halten ihn auf dem Stand des Relays. Die Anbindung an das Gateway
  (Einstellungen, Oberfläche, Zertifikat) liegt ausserhalb des Ordners, in
  `app/tls_acme_dns.py`.

NOTICE des Ursprungsprojekts (Auszug, soweit er diesen Teil betrifft):

```
EXO SMTP Relay
Copyright 2026 Alexander Zarenko / Alexander Zarenko IT Consulting (AZITC)

This product is licensed under the Apache License, Version 2.0 (see LICENSE.md).

The Apache License does not grant permission to use the trade names,
trademarks, service marks or product names of the licensor (section 6).
```
