"""Empfängerzertifikate, die auf eine Freigabe warten.

Zertifikate von Kommunikationspartnern kommen zum Teil selbsttätig herein —
`smime_harvest` zieht sie aus eingehenden signierten Nachrichten. Lässt sich der
Aussteller nicht auf eine bekannte Wurzel zurückführen, landet das Zertifikat
hier statt im Bestand: sichtbar, nachvollziehbar, mit einer Entscheidung durch
einen Menschen.

WARUM NICHT EINFACH VERWERFEN
-----------------------------
Weil das die Entscheidung verstecken würde. Ein Partner mit einem Zertifikat aus
einer firmeneigenen Stelle ist kein Angriff, sondern ein Alltagsfall — und wer
nichts davon erfährt, wundert sich nur, warum an ihn nie verschlüsselt wird.
Umgekehrt ist ein untergeschobenes Zertifikat unter fremdem Absender genau das,
was hier auffallen soll.

WARUM NICHT EINFACH ÜBERNEHMEN
------------------------------
Das war das Verhalten bis v1.7.199 — und es hiess: Wer eine signierte Nachricht
schicken kann, bestimmt, mit welchem Schlüssel künftig an diese Adresse
verschlüsselt wird. Der Absender einer Mail ist keine geprüfte Angabe.

ZUM SPEICHER
------------
Eine Datei je wartendem Zertifikat, benannt nach dessen Fingerabdruck. Kein
Verzeichnis je Adresse wie im Bestand: Für dieselbe Adresse können mehrere
Zertifikate warten (der Partner wechselt den Aussteller), und die Adresse ist
hier gerade nicht der verlässliche Teil.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509

import config

log = logging.getLogger("wartestand")


def _verzeichnis() -> Path:
    p = Path(config.DATA_DIR) / "smime" / "wartend"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _datei(fingerabdruck: str) -> Path:
    return _verzeichnis() / f"{fingerabdruck.upper()}.json"


def merken(adresse: str, cert_pem: bytes, grund: str) -> str | None:
    """Ein Zertifikat in den Wartestand legen. Liefert den Fingerabdruck.

    Mehrfaches Eintreffen desselben Zertifikats ändert nichts als den Zeitpunkt
    des letzten Auftretens — sonst stünde nach einer regen Woche dasselbe
    Zertifikat dreissigmal in der Liste.
    """
    import trust_store
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except Exception as exc:
        log.warning("Wartendes Zertifikat für %s nicht lesbar: %s", adresse, exc)
        return None

    fp = trust_store.abdruck(cert)
    datei = _datei(fp)
    jetzt = datetime.now(timezone.utc).isoformat()
    if datei.is_file():
        try:
            vorhanden = json.loads(datei.read_text(encoding="utf-8"))
            vorhanden["zuletzt"] = jetzt
            vorhanden["anzahl"] = int(vorhanden.get("anzahl") or 1) + 1
            datei.write_text(json.dumps(vorhanden, ensure_ascii=False), encoding="utf-8")
            return fp
        except Exception:
            pass      # unlesbar → unten neu schreiben

    eintrag = {
        "fingerabdruck": fp,
        "adresse": adresse,
        "aussteller": cert.issuer.rfc4514_string(),
        "inhaber": cert.subject.rfc4514_string(),
        "gueltig_bis": _bis(cert),
        "grund": grund,
        "seit": jetzt,
        "zuletzt": jetzt,
        "anzahl": 1,
        "pem": cert_pem.decode("ascii", errors="replace"),
    }
    datei.write_text(json.dumps(eintrag, ensure_ascii=False), encoding="utf-8")
    log.info("Zertifikat für %s wartet auf Freigabe (%s)", adresse, grund)
    # Nur beim ERSTEN Mal benachrichtigen — dieser Zweig wird bei jedem
    # weiteren Eintreffen desselben Zertifikats gar nicht erreicht.
    try:
        import notification
        notification.send_cert_waiting(adresse, cert.issuer.rfc4514_string(), grund)
    except Exception as exc:
        log.warning("Hinweis auf wartendes Zertifikat nicht versandt: %s", exc)
    return fp


def _bis(cert: x509.Certificate) -> str:
    try:
        return cert.not_valid_after_utc.isoformat()
    except AttributeError:
        return cert.not_valid_after.replace(tzinfo=timezone.utc).isoformat()


def liste() -> list[dict]:
    """Alle wartenden Zertifikate, das zuletzt gesehene zuerst — ohne PEM."""
    eintraege = []
    for datei in _verzeichnis().glob("*.json"):
        try:
            e = json.loads(datei.read_text(encoding="utf-8"))
        except Exception:
            continue
        e.pop("pem", None)
        eintraege.append(e)
    return sorted(eintraege, key=lambda e: e.get("zuletzt") or "", reverse=True)


def holen(fingerabdruck: str) -> dict | None:
    datei = _datei(fingerabdruck)
    if not datei.is_file():
        return None
    try:
        return json.loads(datei.read_text(encoding="utf-8"))
    except Exception:
        return None


def verwerfen(fingerabdruck: str) -> bool:
    datei = _datei(fingerabdruck)
    if not datei.is_file():
        return False
    datei.unlink()
    return True


def freigeben(fingerabdruck: str, auch_aussteller: bool = False) -> dict:
    """Ein wartendes Zertifikat in den Bestand übernehmen.

    `auch_aussteller=True` merkt sich zusätzlich dessen Aussteller als
    freigegeben — dann warten künftige Zertifikate derselben Stelle nicht mehr.
    Das ist der übliche Fall: Wer einem Partner traut, traut meist seiner
    Zertifizierungsstelle, nicht nur diesem einen Blatt.

    Lässt sich der Aussteller nicht ermitteln (seine Adresse fehlt oder
    antwortet nicht), wird das Zertifikat selbst freigegeben. Dann gilt die
    Freigabe nur für dieses eine — ehrlicher, als eine Stelle zu bestätigen,
    die man gar nicht gesehen hat.
    """
    import smime_store
    import trust_store

    eintrag = holen(fingerabdruck)
    if not eintrag:
        return {"ok": False, "fehler": "nicht gefunden"}

    pem = (eintrag.get("pem") or "").encode()
    try:
        cert = x509.load_pem_x509_certificate(pem)
    except Exception as exc:
        return {"ok": False, "fehler": f"nicht lesbar ({exc.__class__.__name__})"}

    bezeichnung = ""
    if auch_aussteller:
        import crl_check
        ca = crl_check.ausstellerzertifikat(cert)
        if ca is not None:
            bezeichnung = trust_store._bezeichnung(ca)
            trust_store.freigeben(trust_store.abdruck(ca), f"{bezeichnung} (freigegeben)")
        else:
            trust_store.freigeben(fingerabdruck,
                                  f"Einzelfreigabe {eintrag.get('adresse') or ''}".strip())
            bezeichnung = "nur dieses Zertifikat"

    smime_store.store_recipient_cert(eintrag["adresse"], pem)
    verwerfen(fingerabdruck)
    log.info("Zertifikat für %s freigegeben (%s)", eintrag["adresse"],
             bezeichnung or "einmalig")
    return {"ok": True, "adresse": eintrag["adresse"], "aussteller": bezeichnung}
