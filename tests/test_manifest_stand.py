"""Jede Änderung an einem Add-in- oder Teams-Manifest kostet JEDE Installation
einen Handgriff bei Microsoft (Admin Center → Integrierte Apps → Aktualisieren,
Teams-Katalog) und bis zu 72 Stunden Verteilung. Deshalb darf ein Manifest sich
nicht nebenbei ändern.

Dieser Test hält den Inhalt aller fünf Manifeste fest (Hash ohne Adresse,
Kennung und Version — dieselbe Grundlage wie die Fassung, siehe
webui/routen/addin.mit_fassung). Ändert eine Codeänderung ein Manifest, schlägt
er fehl. Ist die Änderung gewollt:

    .venv/bin/python tests/test_manifest_stand.py --schreiben <neue Gateway-Version>

und im Changelog dieser Version ein „Zu tun“, das das Aktualisieren im Admin
Center nennt (test_neue_fassung_hat_ein_zu_tun prüft das).

Anlass (25.11.2026): Betreiber mussten „alle Nase lang“ bei Microsoft
aktualisieren — einmal wegen eines neuen Namens, davor wegen Codeänderungen,
denen niemand ansah, dass sie das Manifest berühren.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

STAND_DATEI = Path(__file__).resolve().parent / "daten" / "manifest_stand.json"
BASE = "https://gw.firma.at"
HOST = "gw.firma.at"
EINSTELLUNGEN = {"GATEWAY_NAME": "EXO Signature Gateway", "ABWESENHEIT_AKTIV": True,
                 "ADDIN_ENABLED": True}


def _hashes(monkeypatch=None) -> dict[str, str]:
    import settings_store
    from webui.routen import addin, addin_verfassen
    alt_get = settings_store.get
    settings_store.get = lambda k, d=None: EINSTELLUNGEN.get(k, d)
    try:
        t = addin._manifest_texte({"name": "EXO Signature Gateway", "abwesenheit": True})
        roh = {
            "knopf": addin._knopf_bauen(BASE, HOST, addin.knopf_addin_id(HOST), False, t)[1],
            "knopf_lokal": addin._knopf_bauen(BASE, HOST, addin.knopf_addin_id(HOST), True, t)[1],
            "verfassen": addin_verfassen._verfassen_bauen(BASE, HOST, addin_verfassen.addin_id(HOST), t)[1],
            "teams": addin._teams_bauen(BASE, HOST, t)[1],
            "teams_notfall": addin._notfall_bauen(BASE, HOST, t)[1],
        }
    finally:
        settings_store.get = alt_get
    return {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in roh.items()}


def _stand() -> dict:
    return json.loads(STAND_DATEI.read_text(encoding="utf-8"))


def test_manifeste_unveraendert():
    erwartet = _stand()["hashes"]
    ist = _hashes()
    geaendert = sorted(k for k in set(erwartet) | set(ist) if erwartet.get(k) != ist.get(k))
    assert not geaendert, (
        f"Manifest geändert: {geaendert}. Das zwingt JEDE Installation, bei Microsoft zu "
        "aktualisieren (Admin Center bzw. Teams-Katalog, bis 72 h Verteilung). Wenn das nicht "
        "gewollt ist: die Änderung aus dem Manifest heraushalten (Aufgabenbereich/Skript werden "
        "vom Gateway ausgeliefert und brauchen kein neues Manifest). Wenn gewollt: "
        "`.venv/bin/python tests/test_manifest_stand.py --schreiben <Version>` und im Changelog "
        "dieser Version ein „Zu tun“ mit dem Aktualisieren im Admin Center.")


def test_neue_fassung_hat_ein_zu_tun():
    stand = _stand()
    version = stand.get("letzte_aenderung")
    if version is None:
        # Nur der Ausgangsstand bei Einführung der Prüfung darf ohne Änderung sein.
        assert stand.get("eingefuehrt") == "1.12.161", stand
        return
    text = (WURZEL / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## v" + re.escape(version) + r" — .*?(?=^## v|\Z)", text, re.M | re.S)
    assert m, f"Kein Changelog-Eintrag für v{version}"
    zu_tun = re.search(r"\*\*Zu tun:\*\*(.*)", m.group(0), re.S)
    assert zu_tun and "Admin Center" in zu_tun.group(1), (
        f"v{version} ändert ein Manifest — das „Zu tun“ muss das Aktualisieren im Admin Center nennen.")


def test_gegenprobe_erkennt_geaenderten_text():
    """Der Hash muss auf eine Änderung im Manifest ansprechen — sonst wäre die
    Prüfung oben wirkungslos."""
    import settings_store
    from webui.routen import addin
    alt_get = settings_store.get
    settings_store.get = lambda k, d=None: EINSTELLUNGEN.get(k, d)
    try:
        t = addin._manifest_texte({"name": "Anderer Name", "abwesenheit": True})
        roh = addin._knopf_bauen(BASE, HOST, addin.knopf_addin_id(HOST), False, t)[1]
    finally:
        settings_store.get = alt_get
    assert hashlib.sha256(roh.encode()).hexdigest() != _stand()["hashes"]["knopf"]


if __name__ == "__main__" and "--schreiben" in sys.argv:
    i = sys.argv.index("--schreiben")
    version = sys.argv[i + 1] if len(sys.argv) > i + 1 else None
    alt = json.loads(STAND_DATEI.read_text(encoding="utf-8")) if STAND_DATEI.exists() else {}
    neu = {"eingefuehrt": alt.get("eingefuehrt", version), "letzte_aenderung": version,
           "hashes": _hashes()}
    if not alt:
        neu["letzte_aenderung"] = None
    STAND_DATEI.write_text(json.dumps(neu, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(neu, indent=2))
