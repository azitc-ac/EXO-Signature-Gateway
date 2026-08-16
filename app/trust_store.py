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
veröffentlicht es dort. Benutzt wird der Bericht, der die Wurzeln **mitsamt
Zertifikat** liefert und sich beim Abruf auf einen Verwendungszweck einschränken
lässt: `?MicrosoftEKUs=Secure Email`. Gemessen am 16.08.2026: 204 Wurzeln,
324 KB — gegenüber 549 Einträgen in der ungefilterten Liste.

Der Zweck ist entscheidend: Ein Wurzelzertifikat, das nur für Webserver oder
Codesignatur zugelassen ist, soll keine E-Mail-Zertifikate beglaubigen.

⚠️ Es gibt auch einen Bericht, der nur Fingerabdrücke enthält. Der erste Anlauf
nahm ihn und lieh sich die fehlenden Zertifikate aus dem Zertifikatsspeicher des
Systems — von 217 Wurzeln waren dort 104 zu finden. Das war ein Umweg mit zwei
Beständen und zwei Vertrauensregeln. Zum Schliessen einer Kette braucht es den
öffentlichen Schlüssel der Wurzel; wer den Bericht mit Zertifikaten nimmt, hat
alles aus einer Hand.

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

import logging
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes

import config
import settings_store

log = logging.getLogger("trust")

# Der Verwendungszweck, auf den es ankommt — er steckt in der Adresse.
ZWECK = "Secure Email"

# ⚠️ Dieser Bericht liefert die Wurzeln MITSAMT ZERTIFIKAT, nicht nur deren
# Fingerabdrücke. Das ist der Unterschied, auf den es ankommt: Zum Schliessen
# einer Kette braucht es den öffentlichen Schlüssel der Wurzel, sonst lässt sich
# nicht prüfen, ob sie die Zwischenstelle wirklich ausgestellt hat.
#
# Der erste Anlauf nahm den Bericht ohne Zertifikate und lieh sich die
# fehlenden aus dem Zertifikatsspeicher des Systems — 104 von 217 waren dort zu
# finden. Das war ein Umweg: Microsoft veröffentlicht beides, man muss nur den
# richtigen Bericht nehmen. Gemessen am 16.08.2026: 204 Wurzeln, 324 KB.
QUELLE = ("https://ccadb.my.salesforce-sites.com/microsoft/"
          "IncludedRootsPEMCSVForMSFT?MicrosoftEKUs=Secure%20Email")

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
    return Path(config.DATA_DIR) / "trusted_roots.pem"


def _abrufen() -> str | None:
    """Nur der Transport — getrennt vom Zwischenspeicher, damit sich beides
    einzeln prüfen lässt (dieselbe Trennung wie in `crl_check`)."""
    import httpx
    try:
        r = httpx.get(QUELLE, timeout=ABRUF_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as exc:
        log.warning("Wurzelspeicher nicht abrufbar: %s: %s", exc.__class__.__name__, exc)
        return None


def _bezeichnung(cert: x509.Certificate) -> str:
    """Ein Name, den ein Betreiber wiedererkennt — Organisation, sonst CN."""
    from cryptography.x509.oid import NameOID
    for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        werte = cert.subject.get_attributes_for_oid(oid)
        if werte:
            return str(werte[0].value)
    return cert.subject.rfc4514_string()[:60]


def _auswerten(inhalt: str) -> dict[str, x509.Certificate]:
    """Fingerabdruck → Wurzelzertifikat.

    Der Bericht ist eine CSV mit genau einer Spalte, in der das PEM steht. Statt
    sie zu zerlegen, werden die Zertifikatsblöcke direkt herausgeschnitten — das
    ist unempfindlich gegen Anführungszeichen und Zeilenumbrüche in der Spalte.
    """
    treffer: dict[str, x509.Certificate] = {}
    for block in inhalt.split("-----END CERTIFICATE-----"):
        anfang = block.find("-----BEGIN CERTIFICATE-----")
        if anfang == -1:
            continue
        roh = block[anfang:].replace('"', "").strip() + "\n-----END CERTIFICATE-----\n"
        try:
            cert = x509.load_pem_x509_certificate(roh.encode())
        except Exception:
            continue
        treffer[abdruck(cert)] = cert
    return treffer


def aktualisieren() -> dict:
    """Wurzeln frisch holen und als PEM-Bündel ablegen. Für den Tageslauf."""
    inhalt = _abrufen()
    if not inhalt:
        return {"ok": False, "anzahl": 0}
    gefunden = _auswerten(inhalt)
    if not gefunden:
        # Lieber die alte Fassung behalten als eine leere schreiben: Eine
        # Formatänderung an der Quelle würde sonst schlagartig alles sperren.
        log.warning("Wurzelspeicher lieferte 0 verwertbare Einträge — alte Fassung bleibt")
        return {"ok": False, "anzahl": 0}
    try:
        from cryptography.hazmat.primitives.serialization import Encoding
        teile = [f"# Stand: {datetime.now(timezone.utc).isoformat()}\n".encode()]
        for cert in gefunden.values():
            teile.append(cert.public_bytes(Encoding.PEM))
        _cache_datei().write_bytes(b"".join(teile))
    except Exception as exc:
        log.warning("Wurzelspeicher nicht speicherbar: %s", exc)
    log.info("Wurzelspeicher aktualisiert: %d Wurzeln mit Zweck %r", len(gefunden), ZWECK)
    _speicher_leeren()
    return {"ok": True, "anzahl": len(gefunden)}


_geladen: dict[str, x509.Certificate] | None = None
_geladen_stand: datetime | None = None


def _speicher_leeren() -> None:
    global _geladen, _geladen_stand, _system_wurzeln
    _geladen = None
    _geladen_stand = None
    _system_wurzeln = None


def _gespeichert() -> tuple[dict[str, x509.Certificate], datetime | None]:
    """Die abgelegten Wurzeln, einmal je Prozesslauf geparst."""
    global _geladen, _geladen_stand
    if _geladen is not None:
        return _geladen, _geladen_stand
    p = _cache_datei()
    if not p.is_file():
        return {}, None
    inhalt = p.read_text(encoding="utf-8", errors="replace")
    stand = None
    erste = inhalt.split("\n", 1)[0]
    if erste.startswith("# Stand:"):
        try:
            stand = datetime.fromisoformat(erste.split(":", 1)[1].strip())
        except Exception:
            stand = None
    _geladen = _auswerten(inhalt)
    _geladen_stand = stand
    return _geladen, stand


def wurzeln(jetzt: datetime | None = None) -> dict[str, str]:
    """Die geltende Liste. Holt sie nach, wenn sie fehlt oder zu alt ist.

    Ist der Bezug abgeschaltet, bleibt sie leer — dann zählen ausschliesslich
    die örtlichen Freigaben. Das ist für Umgebungen gedacht, die keine
    ausgehenden Verbindungen zulassen oder ihre Aussteller selbst führen wollen.
    """
    if settings_store.get("TRUST_MS_ROOTS") is False:
        return {}
    jetzt = jetzt or datetime.now(timezone.utc)
    gespeichert, stand = _gespeichert()
    if gespeichert and stand is not None:
        alter = (jetzt - stand).total_seconds() / 3600
        if alter <= HOECHSTALTER_STUNDEN:
            return {fp: _bezeichnung(c) for fp, c in gespeichert.items()}
    if aktualisieren()["ok"]:
        return {fp: _bezeichnung(c) for fp, c in _gespeichert()[0].items()}
    # Nachladen misslungen: lieber die alte Fassung als gar keine. Ein
    # Wurzelprogramm veraltet in Tagen nicht.
    return {fp: _bezeichnung(c) for fp, c in gespeichert.items()}


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

def system_wurzeln() -> dict[str, list[x509.Certificate]]:
    """Die bezogenen Wurzeln, nach Inhaber-Name gebündelt — zum Kettenschluss.

    Der Name ist historisch: Zuerst kamen diese Zertifikate aus dem
    Zertifikatsspeicher des Systems, weil der damals benutzte Bericht nur
    Fingerabdrücke lieferte. Sie kommen jetzt aus derselben Quelle wie die
    Vertrauensentscheidung — eine Liste, kein Abgleich zweier Bestände.
    """
    global _system_wurzeln
    if _system_wurzeln is not None:
        return _system_wurzeln
    gefunden: dict[str, list[x509.Certificate]] = {}
    for cert in _gespeichert()[0].values():
        gefunden.setdefault(cert.subject.rfc4514_string(), []).append(cert)
    _system_wurzeln = gefunden
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


# ── Was passiert mit dem Ergebnis? ───────────────────────────────────────────

ANNEHMEN = "annehmen"
WARTEN = "warten"


def entscheiden(kette: list[x509.Certificate]) -> tuple[str, str]:
    """`(ANNEHMEN | WARTEN, Begründung)` — die Bewertung plus die Einstellungen.

    `bewerten()` beantwortet die Sachfrage („kenne ich den Aussteller?"),
    hier kommt die Betreiber-Entscheidung dazu. Drei Schalter, deren Vorgaben
    den Normalfall ohne Zutun tragen:

    * `TRUST_MS_ROOTS` — Microsofts Liste beziehen (Vorgabe an). Aus heisst:
      nur örtliche Freigaben zählen.
    * `TRUST_AUTO_KNOWN` — von bekannten Wurzeln ausgestellte Zertifikate ohne
      Rückfrage annehmen (Vorgabe an). Aus ist für Häuser gedacht, die JEDEN
      Kommunikationspartner einzeln bestätigen wollen.
    * `TRUST_UNKNOWN_MODE` — alles Übrige: `"manuell"` wartet auf eine
      Freigabe (Vorgabe), `"auto"` nimmt es an.

    ⚠️ `"auto"` stellt das Verhalten von vor v1.7.199 wieder her: Dann kommt
    jeder Aussteller ungefragt in den Bestand, auch ein selbst betriebener. Das
    ist eine bewusste Wahl und keine Vorgabe.
    """
    bekannt, grund = bewerten(kette)
    if bekannt:
        if settings_store.get("TRUST_AUTO_KNOWN") is False:
            return WARTEN, f"{grund} — Freigabe ist trotzdem verlangt"
        return ANNEHMEN, grund
    if (settings_store.get("TRUST_UNKNOWN_MODE") or "manuell") == "auto":
        return ANNEHMEN, f"{grund} — ohne Prüfung angenommen (so eingestellt)"
    return WARTEN, grund
