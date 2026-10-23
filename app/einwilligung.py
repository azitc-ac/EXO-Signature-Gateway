"""Einwilligung: Darf das Gateway im Namen eines Postfachinhabers handeln?

Für den Bezug eines Zertifikats tut das Gateway zweierlei, das der
Postfachinhaber sonst selbst täte:

1. **lesen** — es liest die Mail der Zertifizierungsstelle in SEINEM Postfach
   (CASTLE: die Challenge; Hub-Anbieter: den Bestätigungslink);
2. **im Namen** — es antwortet bzw. bestätigt in seinem Namen gegenüber der
   Zertifizierungsstelle (CASTLE: Antwortmail ALS die Adresse; Hub: Klick auf
   den Bestätigungslink).

Beides braucht einen Rechtsgrund. Bis v1.10.34 gab es dafür den Haken
`auto_confirm` — er wirkte aber nur beim Hub-Bestätigungslink; der CASTLE-Weg
fragte ihn nie ab und las und antwortete immer. Der Haken nannte zudem den
Umfang nicht. Bestehende Haken gelten deshalb NICHT als Einwilligung.

Zwei Wege zur Einwilligung:

* **Administrator** — bewusst, mit genanntem Umfang (`von="admin:<kennung>"`),
  auch für Sammelläufe und die Automatik beim Aktivieren (`von="automatik"`).
* **Postfachinhaber** — per Mail: einmaliger Link → Bestätigungsseite mit zwei
  getrennten Zustimmungen und echtem Knopf (POST). Ein Mail-Scanner, der Links
  vorab öffnet, löst so nichts aus. Erst nach der Zustimmung wird bestellt —
  bei CASTLE kann nur das Gateway die Challenge beantworten (die Antwort hängt
  am ACME-Kontoschlüssel), ein Warten auf den Inhaber gibt es dort nicht.

Gespeichert in einer eigenen Datei (600), nicht in settings.json: Anfragen
entstehen auch im Hintergrund (Verlängerung), und settings_store darf nur der
Web-Prozess schreiben. Tokens liegen nur als SHA-256 darin.

Durchgesetzt wird an den Kernstellen (acme_state, hub_orders) über
`liegt_vor()` — nicht in der Oberfläche, sonst ginge ein zweiter Weg daran
vorbei.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import secure_io

log = logging.getLogger(__name__)

# Wortlaut-Fassung. Steigt sie, gelten frühere Einwilligungen nicht mehr.
FASSUNG = 1
UMFANG = ("lesen", "im_namen")
ANFRAGE_GUELTIG_TAGE = 7

_lock = threading.Lock()


def _pfad() -> Path:
    return Path(config.DATA_DIR) / "einwilligungen.json"


def _laden() -> dict:
    try:
        d = json.loads(_pfad().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("erteilt", {})
    d.setdefault("anfragen", {})
    return d


def _speichern(d: dict) -> None:
    secure_io.write_secret_json(_pfad(), d)


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


# ── Erteilte Einwilligungen ──────────────────────────────────────────────────

def liegt_vor(email: str) -> dict | None:
    """Die geltende Einwilligung (beide Teile, aktuelle Fassung) oder None."""
    e = _laden()["erteilt"].get(_norm(email))
    if not e or e.get("fassung") != FASSUNG or set(e.get("umfang") or []) != set(UMFANG):
        return None
    return e


def erteilen(email: str, von: str) -> dict:
    """Einwilligung festhalten. `von`: "nutzer", "admin:<kennung>" oder "automatik"."""
    eintrag = {"fassung": FASSUNG, "umfang": list(UMFANG),
               "am": _jetzt().isoformat(timespec="seconds"), "von": von}
    with _lock:
        d = _laden()
        d["erteilt"][_norm(email)] = eintrag
        # Offene Anfragen für diese Adresse sind damit erledigt.
        d["anfragen"] = {h: a for h, a in d["anfragen"].items() if a.get("email") != _norm(email)}
        _speichern(d)
    log.info("Einwilligung erteilt: %s (von %s, Fassung %s)", _norm(email), von, FASSUNG)
    return eintrag


def widerrufen(email: str, von: str = "") -> bool:
    with _lock:
        d = _laden()
        weg = d["erteilt"].pop(_norm(email), None)
        if weg is not None:
            _speichern(d)
    if weg is not None:
        log.info("Einwilligung widerrufen: %s (%s)", _norm(email), von or "?")
    return weg is not None


# ── Anfragen an den Postfachinhaber ──────────────────────────────────────────

def offene_anfrage(email: str) -> dict | None:
    """Noch gültige Anfrage für diese Adresse (ohne Token) oder None."""
    jetzt = _jetzt().isoformat()
    for a in _laden()["anfragen"].values():
        if a.get("email") == _norm(email) and a.get("ablauf", "") > jetzt:
            return a
    return None


def anfrage_erstellen(email: str, anbieter: str, terms_at: str = "", von: str = "") -> str:
    """Neue Anfrage; liefert das Token für den Link. Eine ältere Anfrage für
    dieselbe Adresse verfällt (nur ein Link gilt)."""
    token = secrets.token_urlsafe(32)
    jetzt = _jetzt()
    with _lock:
        d = _laden()
        d["anfragen"] = {h: a for h, a in d["anfragen"].items()
                         if a.get("email") != _norm(email) and a.get("ablauf", "") > jetzt.isoformat()}
        d["anfragen"][_hash(token)] = {
            "email": _norm(email), "anbieter": anbieter, "terms_at": terms_at,
            "von": von, "erstellt": jetzt.isoformat(timespec="seconds"),
            "ablauf": (jetzt + timedelta(days=ANFRAGE_GUELTIG_TAGE)).isoformat(timespec="seconds"),
        }
        _speichern(d)
    return token


def anfrage_lesen(token: str) -> dict | None:
    """Die Anfrage zu einem Token, wenn sie gilt — ändert NICHTS (GET-sicher)."""
    if not token:
        return None
    a = _laden()["anfragen"].get(_hash(token))
    if not a or a.get("ablauf", "") <= _jetzt().isoformat():
        return None
    return a


def anfrage_beantworten(token: str, zustimmung: bool) -> dict | None:
    """Anfrage einlösen (einmalig). Liefert die Anfrage, oder None, wenn sie
    nicht (mehr) gilt. Bei Zustimmung wird die Einwilligung festgehalten."""
    with _lock:
        d = _laden()
        a = d["anfragen"].pop(_hash(token), None) if token else None
        if not a or a.get("ablauf", "") <= _jetzt().isoformat():
            if a is not None:
                _speichern(d)
            return None
        if zustimmung:
            d["erteilt"][a["email"]] = {"fassung": FASSUNG, "umfang": list(UMFANG),
                                        "am": _jetzt().isoformat(timespec="seconds"), "von": "nutzer"}
        _speichern(d)
    log.info("Einwilligungsanfrage für %s %s", a["email"],
             "angenommen" if zustimmung else "abgelehnt")
    return a


def zustand(email: str) -> dict:
    """Für die Oberfläche: erteilt (von, am) | angefragt (erstellt, ablauf) | fehlt."""
    e = liegt_vor(email)
    if e:
        return {"zustand": "erteilt", "von": e.get("von", ""), "am": e.get("am", "")}
    a = offene_anfrage(email)
    if a:
        return {"zustand": "angefragt", "erstellt": a.get("erstellt", ""), "ablauf": a.get("ablauf", "")}
    return {"zustand": "fehlt"}


def verlangen(email: str) -> None:
    """Sperre VOR jeder Bestellung, für alle Bezugswege (CASTLE und Hub).

    Bis v1.10.39 bestellte der Hub-Weg auch ohne Einwilligung; der Inhaber
    klickte dann den Link der Zertifizierungsstelle selbst. Jetzt gilt überall:
    erst die Zustimmung, dann die Bestellung. Wirft
    `acme_state.EinwilligungFehlt` — alle Aufrufer (Einzelbestellung,
    Sammellauf, Verlängerung) fangen sie und fragen den Inhaber an.
    """
    if not liegt_vor(email):
        import acme_state
        raise acme_state.EinwilligungFehlt(
            f"Für {email} fehlt die Einwilligung, dass das Gateway das Postfach lesen "
            f"und in seinem Namen bei der Zertifizierungsstelle bestätigen darf.")


def anfrage_senden(email: str, anbieter: str, terms_at: str = "", von: str = "",
                   erneut: bool = False) -> bool:
    """Den Postfachinhaber per Mail um Zustimmung bitten.

    Läuft schon eine gültige Anfrage, wird ohne `erneut` keine zweite
    geschickt — Hintergrundwege (Verlängerung, Automatik) dürfen nicht bei
    jedem Durchlauf eine neue Mail auslösen.
    """
    if not erneut and offene_anfrage(email):
        return False
    import aussenadresse
    import ca_backends
    import notification
    token = anfrage_erstellen(email, anbieter, terms_at, von)
    link = f"{aussenadresse.basis()}/einwilligung/{token}"
    label = anbieter
    try:
        b = ca_backends.get_backend(anbieter)
        label = getattr(b, "get_label", lambda: anbieter)() or anbieter
    except Exception:                                     # noqa: BLE001
        pass
    ok = notification.send_einwilligung_anfrage(email, link, label)
    log.info("Einwilligungsanfrage an %s %s", email, "verschickt" if ok else "NICHT verschickt")
    return ok
