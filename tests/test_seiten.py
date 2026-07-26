"""Aufruf-Tests der Weboberfläche — zweites Netz für den app.py-Umbau.

WOFÜR
-----
`tests/test_routes.py` beweist, dass die Routen*tabelle* nach einem Umbau
dieselbe ist. Das genügt nicht: wird beim Verschieben eine Abhängigkeit falsch
importiert, bleibt die Route bestehen und scheitert erst beim Aufruf. Diese
Datei ruft die Seiten deshalb tatsächlich auf.

Zwei Ebenen:
  1. Benannte Seiten mit einem Inhaltsmerkmal — nicht nur „antwortet", sondern
     „liefert die richtige Seite".
  2. Ein Rundumlauf über ALLE parameterlosen GET-Routen: keine darf mit einem
     Serverfehler antworten. Das fängt genau den Fall „Import nach dem
     Verschieben kaputt".

Die Anmeldung wird über `app.dependency_overrides` umgangen — der übliche Weg
bei FastAPI. Die Prüfung, DASS Anmeldung nötig ist, steht unten separat und
läuft ohne diese Umgehung.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")
from starlette.testclient import TestClient   # noqa: E402


# Module mit fest verdrahteten /app-Pfaden. Ohne Umbiegen scheitern die
# betroffenen Endpunkte ausserhalb des Containers mit PermissionError auf '/app'
# — das waere ein Umgebungsproblem, kein Befund.
_PFADE = [
    ("hub_orders", "_DIR", "hub_orders"),
    ("legal_consent", "_DB_PATH", "legal_consent.db"),
    ("mail_audit", "DB_PATH", "mail_audit.db"),
    ("portal_store", "_DB_PATH", "portal.db"),
    ("portal_store", "_BLOB_DIR", "portal"),
    ("smime_store", "SMIME_DIR", "smime"),
    ("smime_store", "RECIPIENT_DIR", "smime/recipients"),
    ("held_mails", "_HELD_DIR", "held_mails"),
]


@pytest.fixture(scope="module")
def client():
    """TestClient mit umgangener Anmeldung und temporären Datenpfaden."""
    tmp = Path(tempfile.mkdtemp(prefix="webui-tests-"))

    import settings_store
    settings_store.SETTINGS_FILE = tmp / "settings.json"
    settings_store._data = {}
    settings_store.init()
    # Ohne abgeschlossenes Setup liefert "/" die Einrichtungsseite — der Test
    # prüfte dann die falsche Seite, ohne dass es auffiel.
    #
    # BEWUSST NUR dieses eine Feld: setzt man zusätzlich TENANT_ID/CLIENT_ID,
    # halten vier Endpunkte die erfundenen Werte für echt und versuchen
    # Azure-Aufrufe — die dann mit 500 scheitern. Eine Testvorbereitung, die
    # Netzwerkverkehr auslöst, ist keine.
    settings_store.update({"SETUP_COMPLETE": True})

    for modul, attribut, unterpfad in _PFADE:
        m = __import__(modul)
        ziel = tmp / unterpfad
        # Verzeichnisse ANLEGEN, nicht nur ihren Elternteil: ein Endpunkt, der
        # `iterdir()` aufruft, scheitert sonst mit FileNotFoundError und sieht
        # aus wie ein Codefehler.
        if ziel.suffix:
            ziel.parent.mkdir(parents=True, exist_ok=True)
        else:
            ziel.mkdir(parents=True, exist_ok=True)
        setattr(m, attribut, ziel)

    from webui.app import app, _check_auth, _require_admin
    app.dependency_overrides[_check_auth] = lambda: "testadmin"
    app.dependency_overrides[_require_admin] = lambda: "testadmin"
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── Ebene 1: die wichtigen Seiten liefern die richtige Seite ─────────────────

# Merkmal je Seite: eine Element-ID, die NUR in dieser Vorlage vorkommt.
#
# Erst standen hier sprechende Wörter ("S/MIME", "Backup"). Die Gegenprobe zeigte,
# dass das zu schwach ist: liess man `/settings/smime` versehentlich `backup.html`
# rendern, bestand der Test weiterhin — das Wort "S/MIME" steht auch dort. Eine
# eindeutige ID beantwortet dagegen wirklich die Frage "ist es DIESE Seite".
SEITEN = [
    ("/",                    "auditContent"),
    ("/mailboxes",           "bk-fetch-btn-modal"),
    ("/settings",            "account-pw-mismatch"),
    ("/settings/smime",      "adv-cb-smime"),
    ("/settings/signature",  "adv-cb-sig"),
    ("/settings/connect",    "adv-cb-digicert"),
    ("/advanced",            "acl-count"),
    ("/debug",               "acme-proxy-input"),
    ("/backup",              "backup-dl-btn"),
    ("/auth/login",          "Anmeld"),
]


@pytest.mark.parametrize("pfad,merkmal", SEITEN)
def test_seite_rendert(client, pfad, merkmal):
    r = client.get(pfad)
    assert r.status_code == 200, f"{pfad} antwortet {r.status_code}"
    assert r.headers["content-type"].startswith("text/html"), f"{pfad} liefert kein HTML"
    assert merkmal.lower() in r.text.lower(), (
        f"{pfad} rendert, aber ohne das seiteneigene Merkmal {merkmal!r} — "
        f"vermutlich die falsche Vorlage")


@pytest.mark.parametrize("pfad,merkmal", SEITEN)
def test_seite_laedt_die_gemeinsamen_helfer(client, pfad, merkmal):
    """common.js muss über base.html auf jeder Seite hängen — sonst sind esc()
    und Geschwister zur Laufzeit undefiniert."""
    if pfad == "/auth/login":
        return                                   # eigenständige Anmeldeseite
    assert "/static/common.js" in client.get(pfad).text, \
        f"{pfad} lädt common.js nicht"


def test_keine_seite_gibt_ein_geheimnis_aus(client):
    """Gesamtschutz auf Seitenebene: alle deklarierten Geheimnisse setzen und
    prüfen, dass keiner der Werte im ausgelieferten HTML auftaucht."""
    import settings_store as ss
    werte = {k: f"GEHEIMNIS-{k}" for k in ss.SECRET_KEYS
             if isinstance(ss.DEFAULTS.get(k), str)}
    ss.update(werte)
    for pfad, _ in SEITEN:
        html = client.get(pfad).text
        for k, v in werte.items():
            assert v not in html, f"{pfad} gibt {k} im Klartext aus"


# ── Ebene 2: Rundumlauf über alle parameterlosen GET-Routen ─────────────────

# Endpunkte, die fest auf das Datenverzeichnis des Containers zugreifen
# (`shutil.disk_usage("/app/data")`) und sich nicht durch Umbiegen einer
# Modulkonstanten lösen lassen. Sie sind damit ausserhalb des Containers nicht
# aufrufbar — eine Grenze dieses Netzes, kein Freibrief.
#
# Hintergrund: im Gateway stehen 35 Literale "/app/data…" verteilt im Code,
# im Hub nur 3 (dort gibt es `config.DATA_DIR`). Ein zentraler Pfad wäre der
# richtige Weg und gehört in den app.py-Umbau — eine halb eingeführte Konstante
# wäre genau das Stückwerk, das vermieden werden soll.
UMGEBUNGSGEBUNDEN = {
    "/api/system/info",
    "/api/support/download",
}


def _parameterlose_get_routen() -> list[str]:
    schnappschuss = Path(__file__).parent / "routes_snapshot.json"
    return [r["pfad"] for r in json.loads(schnappschuss.read_text())
            if "GET" in r["methoden"]
            and "{" not in r["pfad"]
            and not r["pfad"].startswith("/static")]


def test_keine_route_antwortet_mit_serverfehler(client):
    """500 heisst: der Code ist beim Aufruf gescheitert.

    Andere Statuscodes sind hier ausdrücklich in Ordnung — 422 (Pflichtparameter
    fehlt), 503 (Dienst nicht konfiguriert), 404 (Objekt gibt es nicht) sind
    legitime Antworten und hängen von der Testumgebung ab. Nur der Serverfehler
    ist immer ein Befund.
    """
    fehler = []
    for pfad in _parameterlose_get_routen():
        if pfad in UMGEBUNGSGEBUNDEN:
            continue
        r = client.get(pfad, follow_redirects=False)
        # 503 ist KEIN Serverfehler, sondern "Dienst nicht konfiguriert" — in
        # der Testumgebung der Normalfall.
        if r.status_code >= 500 and r.status_code != 503:
            fehler.append(f"{pfad} → {r.status_code}")
    assert not fehler, ("Routen mit Serverfehler:\n  " + "\n  ".join(fehler)
                        + "\n\nNach einem Umbau meist ein falscher oder fehlender Import.")


def test_alle_html_seiten_erben_von_base(client):
    """Eine Seite, die base.html nicht erweitert, verliert Navigation, Dark Mode
    und common.js — beim Verschieben von Endpunkten schon vorgekommen."""
    ohne = []
    for pfad, _ in SEITEN:
        if pfad == "/auth/login":
            continue
        html = client.get(pfad).text
        if 'data-theme' not in html and 'exo-theme' not in html:
            ohne.append(pfad)
    assert not ohne, f"Seiten ohne base.html-Gerüst: {ohne}"


# ── Anmeldung ist tatsächlich nötig ─────────────────────────────────────────

def test_geschuetzte_seite_ohne_anmeldung_nicht_erreichbar():
    """Ohne dependency_overrides: die Oberfläche darf nicht offen sein.
    Eigener TestClient, damit die Umgehung aus der Fixture nicht greift."""
    from webui.app import app
    gesichert = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/settings", follow_redirects=False)
            assert r.status_code in (302, 303, 307, 401), \
                f"/settings ist ohne Anmeldung mit {r.status_code} erreichbar"
            r = c.get("/api/mailboxes", follow_redirects=False)
            assert r.status_code == 401, \
                f"/api/mailboxes antwortet ohne Anmeldung mit {r.status_code}"
    finally:
        app.dependency_overrides.update(gesichert)
