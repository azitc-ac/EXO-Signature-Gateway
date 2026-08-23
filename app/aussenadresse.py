"""Unter welcher Adresse ist dieses Gateway von aussen erreichbar?

ANLASS (2026-08-23)
-------------------
Diese Frage wurde an drei Stellen unabhaengig beantwortet, mit vier
verschiedenen Quellen und drei verschiedenen Rangfolgen:

    hilfen._build_redirect_uri   ADDIN_BASE_URL → PUBLIC_HOSTNAME → localhost
    scheduler._get_gateway_url   GATEWAY_EXTERNAL_URL → LE_DOMAIN → localhost
    hilfen._addin_base_url       ADDIN_BASE_URL → X-Forwarded-Host → request

Praktische Folge auf einem Gateway, das PUBLIC_HOSTNAME gesetzt hatte, aber
weder GATEWAY_EXTERNAL_URL noch LE_DOMAIN: Der Zeitplaner baute
`https://localhost:8080/smime/renew/<token>` — und genau diese Adresse ging per
Mail an Postfachinhaber, die ihr Zertifikat erneuern sollten. Fuer den
Empfaenger ein toter Link. Die Anmeldung fand denselben Rechner ueber dieselbe
Frage korrekt, weil sie eine andere Rangfolge benutzte.

DIE EINE RANGFOLGE
------------------
1. `ADDIN_BASE_URL`   — die vom Betreiber gesetzte kanonische Aussenadresse,
                        vollstaendig mit Schema und ohne Port. Hat ueberall
                        Vorrang, auch bei der Anmeldung.
2. `PUBLIC_HOSTNAME`  — der im Assistenten hinterlegte oeffentliche Name.
3. `LE_DOMAIN`        — die Domain des Zertifikats; wer eines ausstellen liess,
                        ist unter diesem Namen erreichbar.
4. `https://localhost:<Port>` — nur noch Notbehelf.

⚠️ Nie einen Port anhaengen: 8080 ist der Port INNERHALB des Containers,
aussen wird 443 ausgeliefert (docker-compose bildet 443 auf 8080 ab). Ein
`:8080` in einer Rueckadresse fuehrt bei der Anmeldung zu AADSTS50011. Wer
aussen einen abweichenden Port braucht, setzt ihn in `ADDIN_BASE_URL` mit.

`GATEWAY_EXTERNAL_URL` ist entfallen; ein vorhandener Wert wird beim Start nach
`ADDIN_BASE_URL` uebernommen, sofern die leer ist (siehe settings_store).
"""
from __future__ import annotations

import settings_store


def _mit_schema(name: str) -> str:
    name = name.strip().rstrip("/")
    if name.startswith(("http://", "https://")):
        return name
    return f"https://{name}"


def konfiguriert() -> str | None:
    """Die Adresse, die der Betreiber ausdruecklich als Aussenadresse gesetzt hat.

    Nur `ADDIN_BASE_URL` und `PUBLIC_HOSTNAME` — beides Angaben, die genau diesen
    Zweck haben. `None`, wenn keine davon gesetzt ist.

    ⚠️ Fuer Rueckadressen der Anmeldung ist das die einzige zulaessige Quelle:
    Sie muss Zeichen fuer Zeichen mit dem uebereinstimmen, was in der
    Anwendungsregistrierung hinterlegt ist, sonst AADSTS50011. Ist nichts
    gesetzt, gehoert dort der oertliche Einrichtungsweg hin (HTTP auf
    localhost) — nicht etwa `LE_DOMAIN`, die niemand dort eingetragen hat.
    """
    for schluessel in ("ADDIN_BASE_URL", "PUBLIC_HOSTNAME"):
        wert = (settings_store.get(schluessel) or "").strip()
        if wert:
            return _mit_schema(wert)
    return None


def basis() -> str:
    """Aussenadresse fuer Verweise, die per Mail hinausgehen — immer eine Adresse.

    Wie `konfiguriert()`, zusaetzlich `LE_DOMAIN`: Wer ein Zertifikat auf eine
    Domain ausstellen liess, ist unter diesem Namen erreichbar. Fuer einen Link
    in einer Mail ist das ein brauchbarer Anhaltspunkt, fuer eine Rueckadresse
    der Anmeldung nicht.
    """
    gesetzt = konfiguriert()
    if gesetzt:
        return gesetzt

    domain = (settings_store.get("LE_DOMAIN") or "").strip()
    if domain:
        return _mit_schema(domain)

    import config
    return f"https://localhost:{config.WEBUI_PORT}"
