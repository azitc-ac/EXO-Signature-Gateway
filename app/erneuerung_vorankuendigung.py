"""Vorankündigung der automatischen S/MIME-Verlängerung — mit Link zum Abwählen.

ANLASS (2026-11-20)
-------------------
Seit v1.12.115 verlängert der tägliche Lauf auch über den Hub (kostenpflichtig)
und nach Sammelbestellungen. Eine Verlängerung ohne Vorwarnung wäre eine
Bestellung, von der niemand weiß. Deshalb: Bei der ersten Schwelle
(CERT_RENEWAL_THRESHOLDS, Vorgabe 30 Tage) wird angekündigt statt bestellt;
bestellt wird bei der nächsten (Vorgabe 14 Tage). Dazwischen lässt sich die
Verlängerung über einen Link abwählen.

Empfänger (Entscheidung Alex 20.11.):
* Hat der Postfachinhaber eingewilligt, dass das Gateway für ihn bestellt
  (`einwilligung.liegt_vor`), geht die Ankündigung an die VERWALTUNG — sie ist
  dann die, die bestellt. Abschaltbar über NOTIFY_RENEWAL_NOTICE.
* Sonst an den POSTFACHINHABER (Nutzer-Mail `erneuerung_vorankuendigung`,
  anpassbar, folgt NOTIFY_USER_CERT).

⚠️ Der Aufruf des Links (GET) ändert nichts — Mail-Scanner öffnen Links vorab.
Abgewählt wird nur per POST. Der Link gilt bis zur Bestellung bzw. bis zum
Ablauf des Zertifikats; danach ist er wirkungslos.

Zustand im internen Schlüssel `_ERNEUERUNG_ANGEKUENDIGT`:
``{postfach: {"ablauf": "TT.MM.JJJJ", "am": "JJJJ-MM-TT", "an": "verwaltung|inhaber|niemand",
              "token": sha256, "bestellung_ab": "TT.MM.JJJJ", "bestellen_bei": Tage}}``
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta

import settings_store

log = logging.getLogger(__name__)

SCHLUESSEL = "_ERNEUERUNG_ANGEKUENDIGT"


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _alle() -> dict:
    return dict(settings_store.get(SCHLUESSEL) or {})


def _schreiben(alle: dict) -> None:
    settings_store.force_update({SCHLUESSEL: alle})


def eintrag(email: str, ablauf: str) -> dict | None:
    """Die Ankündigung für DIESES Zertifikat (Ablaufdatum), sonst None. Eine
    Ankündigung für ein früheres Zertifikat zählt nicht."""
    e = _alle().get(_norm(email))
    return e if e and e.get("ablauf") == ablauf else None


def _bestellung_ab(ablauf: str, tage_vorher: int) -> str:
    try:
        return (datetime.strptime(ablauf, "%d.%m.%Y") - timedelta(days=tage_vorher)).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def ankuendigen(email: str, cert_info: dict, anbieter: str, tage_vorher: int) -> str:
    """Ankündigung festhalten und verschicken. `tage_vorher`: bei welcher Schwelle
    bestellt wird. Liefert, wer sie bekam: "verwaltung", "inhaber" oder
    "niemand" (abgeschaltet/keine Adresse) — festgehalten wird sie in jedem
    Fall, damit die Bestellung bei der nächsten Schwelle folgt."""
    import aussenadresse
    import einwilligung
    import notification
    ablauf = cert_info.get("expiry", "")
    token = secrets.token_urlsafe(32)
    link = f"{aussenadresse.basis()}/erneuerung/abwahl/{token}"
    ab = _bestellung_ab(ablauf, tage_vorher)
    if einwilligung.liegt_vor(email):
        an = "verwaltung" if notification.send_renewal_notice_admin(email, ablauf, anbieter, ab, link) else "niemand"
    else:
        an = "inhaber" if notification.send_renewal_notice_user(email, ablauf, anbieter, ab, link) else "niemand"
    alle = _alle()
    alle[_norm(email)] = {"ablauf": ablauf, "am": datetime.now().strftime("%Y-%m-%d"),
                          "an": an, "token": _hash(token), "bestellung_ab": ab, "anbieter": anbieter,
                          "bestellen_bei": int(tage_vorher)}
    _schreiben(alle)
    log.info("Verlängerung für %s angekündigt (Ablauf %s, Bestellung ab %s, an %s)", email, ablauf, ab, an)
    return an


def erledigt(email: str) -> None:
    """Nach gestarteter Bestellung: Ankündigung und Link verfallen."""
    alle = _alle()
    if alle.pop(_norm(email), None) is not None:
        _schreiben(alle)


def _zum_token(token: str) -> tuple[str, dict] | None:
    if not token:
        return None
    h = _hash(token)
    for email, e in _alle().items():
        if e.get("token") == h:
            return email, e
    return None


def abwahl_lesen(token: str) -> dict | None:
    """Die Ankündigung zu einem Link, solange er gilt — ändert NICHTS (GET-sicher)."""
    t = _zum_token(token)
    if not t:
        return None
    email, e = t
    return {**e, "email": email}


def abwahl_ausfuehren(token: str) -> dict | None:
    """„Automatisch erneuern“ für das Postfach abschalten. Einmalig: Danach
    verfällt der Link. None, wenn er nicht (mehr) gilt."""
    t = _zum_token(token)
    if not t:
        return None
    email, e = t
    cfg = dict(settings_store.get("CA_USER_CONFIG") or {})
    eintrag_cfg = dict(cfg.get(email) or {})
    eintrag_cfg["auto_renew"] = False
    cfg[email] = eintrag_cfg
    settings_store.update({"CA_USER_CONFIG": cfg})
    erledigt(email)
    log.info("Automatische Verlängerung für %s über den Link der Ankündigung abgewählt", email)
    return {**e, "email": email}
