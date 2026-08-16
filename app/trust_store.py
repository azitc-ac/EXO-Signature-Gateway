"""Welche Zertifizierungsstellen dürfen Empfängerzertifikate ausstellen?

WOZU
----
Empfängerzertifikate kommen zum Teil selbsttätig herein: `smime_harvest` zieht
sie aus eingehenden signierten Nachrichten. Bis dahin fragte niemand, WER sie
ausgestellt hat — ein selbst betriebener Aussteller kam genauso in den Bestand
wie ein Trustcenter. Verschlüsselt wurde anschliessend an beides.

Diese Datei beantwortet die Frage in drei Stufen:

1. **Microsofts Wurzelspeicher** — dieselbe Liste, gegen die Windows und Outlook
   prüfen. Automatisch gepflegt, täglich abgeholt, kein eigener Pflegeaufwand.
2. **Örtliche Freigaben** des Gateway-Betreibers, für alles, was dort fehlt.
3. **Alles Übrige wartet** auf eine Entscheidung, statt stillschweigend benutzt
   zu werden.

WARUM DIESE QUELLE
------------------
Microsoft speist sein Wurzelprogramm in die **CCADB** (Common CA Database) und
veröffentlicht es dort als CSV. Gemessen am 16.08.2026: 550 Einträge, davon 331
mit Status „Included" — und **217 mit dem Verwendungszweck „Secure Email"**.
Genau dieser Zweck zählt hier: Ein Wurzelzertifikat, das nur für Webserver
zugelassen ist, soll keine E-Mail-Zertifikate beglaubigen.

Die Wahl liegt nahe, weil das Gateway ohnehin in einer Microsoft-Welt steht:
Was Outlook als vertrauenswürdig anzeigt, ist dieselbe Liste. Ein Zertifikat,
das hier durchfällt, hätte beim Empfänger ohnehin eine Warnung ausgelöst.

⚠️ CASTLE STEHT NICHT DARIN
---------------------------
Nachgemessen: Sectigo, DigiCert, SwissSign, Certum, HARICA und SSL.com sind in
Microsofts Liste — CASTLE nicht. Da das Gateway seine eigenen Zertifikate über
CASTLE bezieht, wäre der Bestand ohne Vorbelegung ab der ersten Nachricht
blockiert. Der Aussteller der Produktivumgebung ist deshalb ab Werk freigegeben.

⚠️ FEHLT DIE LISTE, WIRD NICHTS AUTOMATISCH FREIGEGEBEN
-------------------------------------------------------
Ist die Quelle nicht erreichbar und liegt keine zwischengespeicherte Fassung
vor, gilt kein Aussteller als geprüft — neue Zertifikate warten dann auf eine
Entscheidung. Der umgekehrte Weg (im Zweifel durchlassen) machte die Prüfung
wertlos, und anders als beim Verschlüsseln kostet Vorsicht hier nichts: Der
Bestand bleibt, wie er ist, und nur Neuzugänge warten.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes

import config
import settings_store

log = logging.getLogger("trust")

QUELLE = ("https://ccadb.my.salesforce-sites.com/microsoft/"
          "IncludedCACertificateReportForMSFTCSV")

# Nur diese Zustände zählen. „Disabled" heisst: Microsoft hat das Vertrauen
# entzogen — dann soll es hier erst recht nicht gelten.
BRAUCHBARE_ZUSTAENDE = {"Included", "NotBefore"}

# Der Verwendungszweck, auf den es ankommt.
ZWECK = "Secure Email"

# Einmal am Tag genügt: Ein Wurzelprogramm ändert sich in Wochen, nicht Stunden.
HOECHSTALTER_STUNDEN = 36

ABRUF_TIMEOUT = 30.0

# Ab Werk freigegeben: der Aussteller, über den dieses Gateway seine eigenen
# Zertifikate bezieht. Ohne ihn wäre der eigene Bezugsweg blockiert.
# Ermittelt am 16.08.2026 über die im Zertifikat genannte Ausstelleradresse
# (`http://ca.castle.cloud/certs/CASTLE_IRE1.crt`).
AB_WERK: dict[str, str] = {
    "92966A8D8FBC35CAFA320FCF32F805DC7BE483E95615DF258B8D38EACE0CFBB9":
        "CASTLE Platform IRE1 (Bezugsweg dieses Gateways)",
}


def _cache_datei() -> Path:
    return Path(config.DATA_DIR) / "trusted_roots.json"


def _abrufen() -> list[dict] | None:
    """Nur der Transport — getrennt vom Zwischenspeicher, damit sich beides
    einzeln prüfen lässt (dieselbe Trennung wie in `crl_check`)."""
    import csv
    import io
    import httpx
    try:
        r = httpx.get(QUELLE, timeout=ABRUF_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    except Exception as exc:
        log.warning("Wurzelspeicher nicht abrufbar: %s: %s", exc.__class__.__name__, exc)
        return None


def _auswerten(zeilen: list[dict]) -> dict[str, str]:
    """Fingerabdruck → Name, für alle Wurzeln mit dem Zweck „Secure Email"."""
    treffer: dict[str, str] = {}
    for z in zeilen:
        if (z.get("Microsoft Status") or "").strip() not in BRAUCHBARE_ZUSTAENDE:
            continue
        if ZWECK not in (z.get("Microsoft EKUs") or ""):
            continue
        abdruck = (z.get("SHA-256 Fingerprint") or "").strip().upper().replace(":", "")
        if len(abdruck) == 64:
            treffer[abdruck] = (z.get("CA Owner") or "").strip() or "unbekannt"
    return treffer


def aktualisieren() -> dict:
    """Liste frisch holen und ablegen. Für den Tageslauf."""
    zeilen = _abrufen()
    if not zeilen:
        return {"ok": False, "anzahl": 0}
    wurzeln = _auswerten(zeilen)
    if not wurzeln:
        # Lieber die alte Fassung behalten als eine leere schreiben: Eine
        # Formatänderung an der Quelle würde sonst schlagartig alles sperren.
        log.warning("Wurzelspeicher lieferte 0 verwertbare Einträge — alte Fassung bleibt")
        return {"ok": False, "anzahl": 0}
    try:
        _cache_datei().write_text(json.dumps(
            {"stand": datetime.now(timezone.utc).isoformat(), "wurzeln": wurzeln},
            ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.warning("Wurzelspeicher nicht speicherbar: %s", exc)
    log.info("Wurzelspeicher aktualisiert: %d Wurzeln mit Zweck %r", len(wurzeln), ZWECK)
    return {"ok": True, "anzahl": len(wurzeln)}


def _gespeichert() -> tuple[dict[str, str], datetime | None]:
    p = _cache_datei()
    if not p.is_file():
        return {}, None
    try:
        daten = json.loads(p.read_text(encoding="utf-8"))
        stand = datetime.fromisoformat(daten["stand"])
        return daten.get("wurzeln") or {}, stand
    except Exception as exc:
        log.warning("Wurzelspeicher nicht lesbar (%s) — gilt als leer", exc)
        return {}, None


def wurzeln(jetzt: datetime | None = None) -> dict[str, str]:
    """Die geltende Liste. Holt sie nach, wenn sie fehlt oder zu alt ist."""
    jetzt = jetzt or datetime.now(timezone.utc)
    gespeichert, stand = _gespeichert()
    if gespeichert and stand is not None:
        alter = (jetzt - stand).total_seconds() / 3600
        if alter <= HOECHSTALTER_STUNDEN:
            return gespeichert
    if aktualisieren()["ok"]:
        return _gespeichert()[0]
    # Nachladen misslungen: lieber die alte Fassung als gar keine. Ein
    # Wurzelprogramm veraltet in Tagen nicht.
    return gespeichert


def freigaben() -> dict[str, str]:
    """Örtliche Freigaben des Betreibers, samt der ab Werk gesetzten."""
    eigene = settings_store.get("TRUSTED_ISSUERS") or {}
    return {**AB_WERK, **{k.upper(): v for k, v in eigene.items()}}


def freigeben(abdruck: str, bezeichnung: str) -> None:
    eigene = dict(settings_store.get("TRUSTED_ISSUERS") or {})
    eigene[abdruck.upper()] = bezeichnung
    settings_store.update({"TRUSTED_ISSUERS": eigene})
    log.info("Aussteller freigegeben: %s (%s)", bezeichnung, abdruck[:16])


def freigabe_zuruecknehmen(abdruck: str) -> None:
    eigene = dict(settings_store.get("TRUSTED_ISSUERS") or {})
    if eigene.pop(abdruck.upper(), None) is not None:
        settings_store.update({"TRUSTED_ISSUERS": eigene})
        log.info("Freigabe zurückgenommen: %s", abdruck[:16])


def abdruck(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex().upper()


MAX_KETTENLAENGE = 6

# Wurzelzertifikate des Systems, nach Inhaber-Name gebündelt.
#
# ⚠️ Ohne sie bleibt fast jede Kette unvollständig, und das ist kein Zufall:
# Wurzelzertifikate werden praktisch **nie** über die Ausstelleradresse
# verlinkt — sie sollen ja im Vertrauensspeicher liegen, nicht nachgeladen
# werden. Gemessen an den Ausstellern des produktiven Bestands (16.08.2026):
# Bei DigiCert und SwissSign endete die Kette am vorletzten Glied, weil das
# Zwischenzertifikat keine Ausstelleradresse mehr nennt.
#
# Der Systemspeicher liefert die fehlenden Zertifikate, Microsofts Liste
# entscheidet über das Vertrauen. Beides zusammen: 104 verwendbare Wurzeln.
_system_wurzeln: dict[str, list[x509.Certificate]] | None = None

SYSTEM_SPEICHER = ("/etc/ssl/certs/ca-certificates.crt",
                   "/etc/pki/tls/certs/ca-bundle.crt")


def system_wurzeln() -> dict[str, list[x509.Certificate]]:
    global _system_wurzeln
    if _system_wurzeln is not None:
        return _system_wurzeln
    gefunden: dict[str, list[x509.Certificate]] = {}
    pfade = list(SYSTEM_SPEICHER)
    try:
        import certifi
        pfade.append(certifi.where())
    except Exception:
        pass
    for pfad in pfade:
        p = Path(pfad)
        if not p.is_file():
            continue
        for block in p.read_bytes().split(b"-----END CERTIFICATE-----"):
            b = block.strip()
            if not b:
                continue
            try:
                c = x509.load_pem_x509_certificate(b + b"\n-----END CERTIFICATE-----\n")
            except Exception:
                continue
            gefunden.setdefault(c.subject.rfc4514_string(), []).append(c)
    _system_wurzeln = gefunden
    log.info("Systemspeicher: %d Wurzelzertifikate gelesen", sum(len(v) for v in gefunden.values()))
    return gefunden


def _aussteller_aus_system(cert: x509.Certificate) -> x509.Certificate | None:
    """Wurzel, die *cert* ausgestellt hat — aus dem Systemspeicher.

    Auch hier wird gebunden, nicht geglaubt: Der Name allein genügt nicht, die
    Signatur muss passen.
    """
    for kandidat in system_wurzeln().get(cert.issuer.rfc4514_string(), []):
        try:
            cert.verify_directly_issued_by(kandidat)
            return kandidat
        except Exception:
            continue
    return None


def kette_bauen(cert: x509.Certificate) -> list[x509.Certificate]:
    """Vom Zertifikat aufwärts, so weit die Aussteller sich ermitteln lassen.

    Jede Stufe wird über die im Zertifikat genannte Ausstelleradresse geladen und
    dabei **an die vorige gebunden** — `crl_check.ausstellerzertifikat()` nimmt
    nur ein Zertifikat an, das das jeweilige tatsächlich ausgestellt hat. Eine
    über HTTP geladene Kette wäre sonst wertlos.

    Die Kette endet, wenn kein Aussteller mehr zu ermitteln ist oder das
    Zertifikat sich selbst ausgestellt hat (Wurzel). `MAX_KETTENLAENGE` ist die
    Notbremse gegen eine Gegenstelle, die im Kreis verweist.
    """
    import crl_check
    kette = [cert]
    aktuell = cert
    for _ in range(MAX_KETTENLAENGE):
        if aktuell.subject == aktuell.issuer:
            break            # selbstsigniert — weiter geht es nicht
        # Erst der Systemspeicher — dort liegen die Wurzeln, und er kostet
        # keinen Netzabruf. Dann die im Zertifikat genannte Adresse.
        naechster = _aussteller_aus_system(aktuell) or crl_check.ausstellerzertifikat(aktuell)
        if naechster is None:
            break
        if any(abdruck(naechster) == abdruck(k) for k in kette):
            break            # Verweisschleife
        kette.append(naechster)
        aktuell = naechster
    return kette


def bewerten(kette: list[x509.Certificate]) -> tuple[bool, str]:
    """Darf ein Zertifikat mit dieser Kette in den Bestand?

    *kette* ist das Zertifikat selbst, gefolgt von so vielen Ausstellern, wie
    sich ermitteln liessen. Geprüft wird **jede Stufe**, nicht nur die Wurzel:

    * Die Kette lässt sich nicht immer bis zur Wurzel aufbauen — CASTLEs
      Testumgebung etwa liefert ihr Ausstellerzertifikat gar nicht aus (404,
      gemessen am 16.08.2026). Bestünde die Prüfung auf einer Wurzel, wäre alles
      dahinter unentscheidbar.
    * Eine Freigabe auf einer Zwischenstufe ist zudem das, was ein Betreiber
      tatsächlich aussprechen will: „Zertifikate von DIESER Stelle sind in
      Ordnung", nicht „alles unter dieser Wurzel".

    Liefert `(True, Begründung)` oder `(False, Grund)`.
    """
    if not kette:
        return False, "keine Kette"
    bekannte = wurzeln()
    erlaubte = freigaben()
    # Auch das Blatt (Stufe 0) wird geprüft: So lässt sich ein einzelnes
    # Zertifikat freigeben, dessen Aussteller sich gar nicht ermitteln lässt —
    # etwa CASTLEs Testumgebung, die ihr Ausstellerzertifikat nicht ausliefert.
    for cert in kette:
        fp = abdruck(cert)
        if fp in erlaubte:
            return True, f"freigegeben: {erlaubte[fp]}"
        if fp in bekannte:
            return True, f"in Microsofts Wurzelspeicher: {bekannte[fp]}"
    if not bekannte:
        return False, "Wurzelspeicher nicht verfügbar — Entscheidung nötig"
    return False, "Aussteller unbekannt"
