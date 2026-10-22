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


def test_setup_zeigt_angeforderte_berechtigungen(client):
    """Schritt 5 zeigt den Stand der App-Rollen (Baustein _berechtigungen.html),
    und die Status-API liefert sie aus DERSELBEN Quelle wie der Graph-Antrag
    (setup_wizard._*_PERMISSION_SPECS) — die Anzeige kann nicht abdriften.

    Anmeldung per Basic-Auth: /setup umgeht die override-Dependencies und prüft
    die Session/Basic selbst; admin/admin greift auf dem frischen Test-Gateway.
    """
    import settings_store as ss
    import config as _cfg
    # Unabhängig von der Testreihenfolge: ohne Hash prüft _check_password gegen
    # config.WEBUI_PASSWORD (ein vorheriger Test setzt ADMIN_PASSWORD_HASH auf einen
    # Platzhalter). AZURE_APP_CREATED, damit der Schritt-5-Block überhaupt rendert.
    ss.update({"AZURE_APP_CREATED": True, "ADMIN_PASSWORD_HASH": ""})
    r = client.get("/setup", auth=("admin", _cfg.WEBUI_PASSWORD or "admin"),
                   follow_redirects=False)
    assert r.status_code == 200, f"{r.status_code} (Auth/Redirect?)"
    assert 'id="ber-app"' in r.text, "Berechtigungs-Status fehlt in Schritt 5"
    assert "Admin-Consent: erteilt" not in r.text, "fester Text statt echtem Stand"
    namen = [b["name"] for b in client.get("/api/setup/berechtigungen").json()["berechtigungen"]]
    for name in ("MailboxSettings.ReadWrite", "Mail.Send", "IMAP.AccessAsApp"):
        assert name in namen, f"Berechtigung {name} fehlt im Status"


def test_setup_nennt_die_fassung_des_teams_pakets(client):
    """Neben dem Download steht die Fassung, die das Paket gerade trägt — das
    Admin Center zeigt keine Versionsnummer, nur „Last modified“."""
    import settings_store as ss
    import config as _cfg
    ss.update({"ADMIN_PASSWORD_HASH": ""})
    r = client.get("/setup", auth=("admin", _cfg.WEBUI_PASSWORD or "admin"), follow_redirects=False)
    assert r.status_code == 200
    stand = ss.get("_TEAMS_APP_STAND")
    assert stand and stand.get("version"), "Seite hat den Stand nicht ermittelt"
    assert f"Fassung {stand['version']} · seit {stand['seit']}" in r.text


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


# ── Funktionsschalter „Zentrale Abwesenheiten": Menüpunkt und Seite ──────────────────────────────────────────────

def test_menue_und_seite_folgen_dem_schalter(client):
    import settings_store
    settings_store.update({"ABWESENHEIT_AKTIV": False})
    r = client.get("/mailboxes")
    assert 'href="/abwesenheit"' not in r.text
    r = client.get("/abwesenheit", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/setup")

    settings_store.update({"ABWESENHEIT_AKTIV": True})
    r = client.get("/mailboxes")
    assert 'href="/abwesenheit"' in r.text
    assert client.get("/abwesenheit", follow_redirects=False).status_code == 200
    settings_store.update({"ABWESENHEIT_AKTIV": False})


def test_verfassen_leiste_fuehrt_zum_self_service_und_zurueck(client):
    """Der Add-in-Knopf heisst beim Verfassen wie beim Lesen — die Verfassen-Leiste
    muss also zur Selbstverwaltung führen, und /self wieder zurück."""
    import settings_store
    settings_store.update({"ABWESENHEIT_AKTIV": True})
    r = client.get("/addin/compose")
    assert 'href="/self?von=verfassen"' in r.text and "Meine Abwesenheit &amp; Signatur" in r.text
    r = client.get("/self?von=verfassen")
    assert 'href="/addin/compose"' in r.text
    assert 'href="/addin/compose"' not in client.get("/self").text

    settings_store.update({"ABWESENHEIT_AKTIV": False})
    assert "Meine Signatur verwalten" in client.get("/addin/compose").text
    assert "<h1>Meine Signatur</h1>" in client.get("/self").text


def test_abwesenheit_aus_hinweis_zeigt_auf_den_schalter_dieser_seite(client):
    """Der Schalter zog in v1.9.112 von „Postfächer“ auf diese Seite. Der Hinweis
    verwies danach weiter auf /mailboxes, wo es ihn nicht mehr gab."""
    import re
    import settings_store
    settings_store.update({"ABWESENHEIT_AKTIV": True})
    html = client.get("/abwesenheit").text
    hinweis = re.search(r'<div id="ooo-aus-hinweis".*?</div>', html, re.S).group(0)
    ziel = re.search(r'href="#([^"]+)"', hinweis).group(1)
    assert 'id="ooo-enabled"' in html[html.index('id="' + ziel + '"'):], "Anker führt nicht zum Schalter"
    assert "/mailboxes" not in hinweis
    settings_store.update({"ABWESENHEIT_AKTIV": False})


def test_kalender_hauptschalter_sperrt_die_abhaengigen_felder(client):
    """Aus: Automatik, Termindauer und Neulesen gesperrt, Widerrufs-Hinweis sichtbar.
    An: alles bedienbar, Hinweis verborgen."""
    import re
    import settings_store
    settings_store.update({"ABWESENHEIT_AKTIV": True, "OOO_CALENDAR_READ": False})
    html = client.get("/abwesenheit").text
    block = re.search(r'<fieldset id="ooo-kal-block"[^>]*>(.*?)</fieldset>', html, re.S)
    assert block and "disabled" in block.group(0).split(">")[0]
    for feld in ("ooo-cal-auto", "ooo-cal-hours", "ooo-cal-refresh"):
        assert f'id="{feld}"' in block.group(1), f"{feld} liegt nicht im gesperrten Block"
    assert re.search(r'id="ooo-cal-read"[^>]*>', html).group(0).find("checked") < 0
    assert "hidden" not in re.search(r'<p[^>]*id="ooo-kal-aus-hinweis"[^>]*>', html).group(0)
    assert "var KALENDER_LESEN = false;" in html

    settings_store.update({"OOO_CALENDAR_READ": True})
    html = client.get("/abwesenheit").text
    assert "disabled" not in re.search(r'<fieldset id="ooo-kal-block"[^>]*>', html).group(0)
    assert "hidden" in re.search(r'<p[^>]*id="ooo-kal-aus-hinweis"[^>]*>', html).group(0)
    settings_store.update({"ABWESENHEIT_AKTIV": False})


@pytest.mark.parametrize("katalog,erwartet", [
    ({"zustand": "ok", "version": "1.9.156"}, "⚠ Im Teams-Katalog: Fassung 1.9.156, das Paket hat"),
    ({"zustand": "recht_fehlt", "version": None}, "<code>AppCatalog.Read.All</code> fehlt"),
    ({"zustand": "nicht_gefunden", "version": None}, "Nicht im Teams-Katalog"),
])
def test_setup_vergleicht_teams_katalog_mit_dem_paket(client, monkeypatch, katalog, erwartet):
    import settings_store as ss
    import config as _cfg
    import graph_client
    aufrufe = []

    async def fake(eid):
        aufrufe.append(eid)
        return dict(katalog)
    monkeypatch.setattr(graph_client, "teams_katalog", fake)
    ss.update({"ADMIN_PASSWORD_HASH": "", "ADDIN_ENABLED": True, "TEAMS_APP_AKTIV": True})
    html = client.get("/setup", auth=("admin", _cfg.WEBUI_PASSWORD or "admin")).text
    assert erwartet in html and 'id="ber-teams"' in html

    paket = ss.get("_TEAMS_APP_STAND")["version"]                    # Paket = Katalog → aktuell
    monkeypatch.setattr(graph_client, "teams_katalog",
                        lambda eid: _gleich(paket))
    html = client.get("/setup", auth=("admin", _cfg.WEBUI_PASSWORD or "admin")).text
    assert f"✓ Im Teams-Katalog: Fassung {paket}, aktuell" in html

    ss.update({"TEAMS_APP_AKTIV": False})                            # aus → keine Abfrage
    n = len(aufrufe)
    html = client.get("/setup", auth=("admin", _cfg.WEBUI_PASSWORD or "admin")).text
    assert len(aufrufe) == n and 'id="teams-katalog-stand"' not in html and 'id="ber-teams"' not in html
    ss.update({"ADDIN_ENABLED": False})


def test_setup_bietet_das_verfassen_addin_an(client):
    """Bis v1.10.35 bot die Einrichtung nur den Knopf an; das Add-in, das beim
    Verfassen selbst einfügt, war gemountet, aber nirgends verlinkt — und der
    Hinweis am Knopf las sich wie das automatische Einfügen."""
    import re
    import settings_store as ss
    import config as _cfg
    ss.update({"ADMIN_PASSWORD_HASH": "", "ADDIN_ENABLED": True})
    try:
        html = client.get("/setup", auth=("admin", _cfg.WEBUI_PASSWORD or "admin")).text
        assert 'id="addin-verfassen"' in html
        assert "Zeigt die Gateway-Signatur beim Verfassen direkt" not in html
        urls = set(re.findall(r'<code[^>]*>(https?://[^<]*/addin/verfassen/manifest\.xml)</code>', html))
        assert len(urls) == 1, urls
        # Die angebotene Adresse liefert wirklich das Add-in mit Start-Ereignis
        pfad = urls.pop().split("://", 1)[1].split("/", 1)[1]
        xml = client.get("/" + pfad).text
        assert 'LaunchEvent Type="OnNewMessageCompose"' in xml
        # … und deren Fassung steht danach neben der Adresse
        version = ss.get("_ADDIN_VERFASSEN_STAND")["version"]
        html = client.get("/setup", auth=("admin", _cfg.WEBUI_PASSWORD or "admin")).text
        assert f"Fassung {version}" in html.split('id="addin-verfassen-fassung"', 1)[1][:200]
    finally:
        ss.update({"ADDIN_ENABLED": False})


async def _gleich(version):
    return {"zustand": "ok", "version": version}


def test_berechtigungen_erteilen_verlangt_nur_zwei_scopes(client, monkeypatch):
    """Nachtragen einer App-Rolle braucht nicht die Schreibrechte des vollen
    Einrichtungslaufs (Directory.ReadWrite.All, RoleManagement.ReadWrite.Directory).
    Bis v1.10.3 verlangte das Zustimmungsfenster sie trotzdem — und der Code-Tausch
    im Callback verlangte unabhängig vom /authorize die Vorgabe."""
    import urllib.parse
    import pkce
    import setup_wizard
    url = client.get("/api/setup/berechtigungen/erteilen").json()["auth_url"]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["scope"][0].split() == pkce.BERECHTIGUNGEN_SCOPES
    for zuviel in ("Directory.ReadWrite.All", "RoleManagement.ReadWrite.Directory", "offline_access"):
        assert zuviel not in q["scope"][0]

    getauscht = {}

    async def tausch(code, verifier, redirect_uri, scopes=None):
        getauscht["scopes"] = scopes
        return {"access_token": "tok"}

    async def nachziehen(tok):
        return {"ok": True, "fehler": []}
    monkeypatch.setattr(pkce, "exchange_code", tausch)
    monkeypatch.setattr(setup_wizard, "berechtigungen_nachziehen", nachziehen)
    r = client.get(f"/auth/callback?code=c&state={q['state'][0]}")
    assert r.status_code == 200 and "berechtigungen-done" in r.text
    assert getauscht["scopes"] == pkce.BERECHTIGUNGEN_SCOPES


def _eingefuegt(client, monkeypatch, start_url, gerufen):
    """Anmeldung starten und die localhost-Adresse über /api/setup/auth-paste
    zurückgeben — wie der Betreiber, wenn die HTTPS-Rücksprungadresse fehlt."""
    import urllib.parse
    import pkce
    import setup_wizard
    d = client.get(start_url).json()
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(d["auth_url"]).query)["state"][0]

    async def tausch(code, verifier, redirect_uri, scopes=None):
        gerufen["scopes"] = scopes
        return {"access_token": "tok"}

    async def nachziehen(tok):
        gerufen["nachziehen"] = tok
        return {"ok": True, "fehler": []}

    async def einrichtung(tok):
        gerufen["einrichtung"] = tok
        return {}

    async def rueckadresse(tok, host):
        gerufen["rueckadresse"] = tok

    async def grants(tok, oid):
        gerufen["grants"] = oid
        return {"app_role": True, "global_reader": True, "app_id": "APP"}
    monkeypatch.setattr(pkce, "exchange_code", tausch)
    monkeypatch.setattr(setup_wizard, "berechtigungen_nachziehen", nachziehen)
    monkeypatch.setattr(setup_wizard, "run_post_auth_setup", einrichtung)
    monkeypatch.setattr(setup_wizard, "patch_bootstrap_redirect_uri", rueckadresse)
    monkeypatch.setattr(setup_wizard, "grant_watchdog_graph_roles", grants)
    return d, client.post("/api/setup/auth-paste",
                          json={"url": f"http://localhost:8080/auth/callback?code=c&state={state}"})


def test_eingefuegte_berechtigungs_anmeldung_ist_keine_einrichtung(client, monkeypatch):
    """Endet „Fehlende Berechtigungen erteilen“ auf localhost, fügt der Betreiber
    die Adresse ein. Bis v1.10.26 lief dafür die komplette Ersteinrichtung
    (run_post_auth_setup) — mit Token-Tausch über die Bootstrap-Vorgabe."""
    import pkce
    gerufen = {}
    d, r = _eingefuegt(client, monkeypatch, "/api/setup/berechtigungen/erteilen", gerufen)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["flow"] == "berechtigungen"
    assert "einrichtung" not in gerufen, "Ersteinrichtung für einen Berechtigungs-Code"
    assert gerufen["nachziehen"] == "tok"
    assert gerufen["scopes"] == pkce.BERECHTIGUNGEN_SCOPES
    # heilt die Ursache gleich mit: Rücksprungadresse an der Bootstrap-App ergänzen
    assert gerufen["rueckadresse"] == "tok"


def test_eingefuegte_waechter_anmeldung_erteilt_zuweisungen(client, monkeypatch):
    gerufen = {}
    oid = "11111111-2222-3333-4444-555555555555"
    d, r = _eingefuegt(client, monkeypatch, f"/api/watchdog/start-grants?mi_object_id={oid}", gerufen)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["app_id"] == "APP"
    assert gerufen["grants"] == oid and "einrichtung" not in gerufen


def test_eingefuegte_einrichtung_bleibt_einrichtung(client, monkeypatch):
    gerufen = {}
    d, r = _eingefuegt(client, monkeypatch, "/auth/start?localhost=1", gerufen)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert gerufen["einrichtung"] == "tok" and "nachziehen" not in gerufen


def test_sso_anmeldung_laesst_sich_nicht_einfuegen(client, monkeypatch):
    import pkce
    _state, url = pkce.create_session("http://localhost:8080/auth/callback", flow="sso")
    gerufen = {}

    async def tausch(*a, **kw):
        gerufen["tausch"] = True
        return {"access_token": "tok"}
    monkeypatch.setattr(pkce, "exchange_code", tausch)
    r = client.post("/api/setup/auth-paste",
                    json={"url": f"http://localhost:8080/auth/callback?code=c&state={_state}"})
    assert r.status_code == 400 and "tausch" not in gerufen


def test_erteilen_meldet_localhost_rueckkehr(client, monkeypatch):
    """Die Box zeigt das Einfügefeld nur, wenn die Anmeldung auf localhost endet."""
    from webui.routen import anmeldung
    monkeypatch.setattr(anmeldung, "_setup_redirect_uri", lambda: "http://localhost:8080/auth/callback")
    assert client.get("/api/setup/berechtigungen/erteilen").json()["einfuegen"] is True
    monkeypatch.setattr(anmeldung, "_setup_redirect_uri", lambda: "https://sig.example/auth/callback")
    assert client.get("/api/setup/berechtigungen/erteilen").json()["einfuegen"] is False


def test_abgelaufene_anmeldung_meldet_sich_im_popup(client, monkeypatch):
    """Kehrt das Zustimmungs-Popup nach Ablauf der PKCE-Sitzung zurück, landete es
    auf der Gateway-Anmeldung, nichts war erteilt, und das Hauptfenster erfuhr es
    nie. Jetzt: Seite meldet „anmeldung-abgelaufen" ans öffnende Fenster; ohne
    öffnendes Fenster (SSO als ganze Seite) weiter zur Anmeldung."""
    import time
    import urllib.parse
    import pkce
    url = client.get("/api/setup/berechtigungen/erteilen").json()["auth_url"]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    # 20 Minuten Bedenkzeit (MFA, Rechte prüfen) — muss noch reichen
    pkce._sessions[state]["created_at"] = time.monotonic() - 20 * 60

    async def tausch(code, verifier, redirect_uri, scopes=None):
        return {"access_token": "tok"}

    async def nachziehen(tok):
        return {"ok": True, "fehler": []}
    import setup_wizard
    monkeypatch.setattr(pkce, "exchange_code", tausch)
    monkeypatch.setattr(setup_wizard, "berechtigungen_nachziehen", nachziehen)
    r = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
    assert r.status_code == 200 and "berechtigungen-done" in r.text

    r = client.get("/auth/callback?code=c&state=unbekannt", follow_redirects=False)
    assert r.status_code == 200
    assert '"type": "anmeldung-abgelaufen"' in r.text
    assert "location.replace('/auth/login?error=session_expired')" in r.text



# ── S/MIME: Zertifikat bestellen mit Standardbezugsweg ──────────────────────

import re  # noqa: E402

_WEGE = [{"name": "castle_acme", "label": "CASTLE", "auto": True, "ready": True},
         {"name": "assisted_manual", "label": "Assistiert manuell", "auto": False, "ready": True}]


def _smime_neu(client, monkeypatch, vorgabe):
    import ca_backends
    import settings_store
    monkeypatch.setitem(settings_store._data, "MAILBOX_CONFIG",
                        {"g1": {"primary": "neu@x.de", "smime": True}})
    monkeypatch.setitem(settings_store._data, "CA_USER_CONFIG", {})
    monkeypatch.setitem(settings_store._data, "SMIME_AUTO_ENROLL_CA", vorgabe)

    async def wege():
        return _WEGE
    monkeypatch.setattr(ca_backends, "list_backends_aktuell", wege)
    r = client.get("/smime")
    assert r.status_code == 200
    zeile = r.text[r.text.index("<strong style=\"font-size:14px\">neu@x.de</strong>"):]
    zeile = zeile[:zeile.index("lifecycle-section")]
    knopf = re.search(r'<button id="ae-btn-neu_x_de"[^>]*>', zeile, re.S).group(0)
    return r.text, zeile, knopf


def test_postfach_ohne_zertifikat_hat_knopf_zertifikat_bestellen(client, monkeypatch):
    html, zeile, knopf = _smime_neu(client, monkeypatch, "castle_acme")
    assert "Zertifikat bestellen" in zeile
    assert "startAutoEnroll('neu@x.de'" in knopf and "disabled" not in knopf
    # Lifecycle-Auswahl zeigt den Weg aus der Vorgabe
    auswahl = html[html.index('class="lc-backend" data-email="neu@x.de"'):]
    auswahl = auswahl[:auswahl.index("</select>")]
    assert re.search(r'value="castle_acme"\s+selected', auswahl), auswahl


def test_ohne_vorgabe_oeffnet_der_knopf_die_einstellungen(client, monkeypatch):
    html, zeile, knopf = _smime_neu(client, monkeypatch, "")
    assert "_zeileMehr(event, this)" in knopf and "startAutoEnroll" not in knopf
    assert "disabled" not in knopf, "ausgegraut war die Sackgasse"


def test_bestellung_ueber_vorgabe_haelt_den_weg_am_postfach_fest(client, monkeypatch):
    import ca_backends
    import settings_store
    monkeypatch.setitem(settings_store._data, "MAILBOX_CONFIG",
                        {"g1": {"primary": "neu@x.de", "smime": True}})
    monkeypatch.setitem(settings_store._data, "CA_USER_CONFIG", {})
    monkeypatch.setitem(settings_store._data, "SMIME_AUTO_ENROLL_CA", "castle_acme")
    gerufen = {}

    class _Weg:
        def get_name(self): return "castle_acme"
        def can_auto_renew(self): return True
        async def initiate_renewal(self, email, cfg, extra=None):
            gerufen["email"] = email

    monkeypatch.setattr(ca_backends, "get_backend", lambda name: _Weg())
    r = client.post("/api/smime/renewal/initiate/neu@x.de", json={})
    assert r.status_code == 200, r.text
    assert gerufen["email"] == "neu@x.de"
    assert settings_store.get("CA_USER_CONFIG")["neu@x.de"]["backend"] == "castle_acme"


# ── Einwilligung: Bestätigungsseite des Postfachinhabers ────────────────────

@pytest.fixture
def ew_ablage(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    return tmp_path


def test_einwilligung_get_aendert_nichts(client, ew_ablage):
    """Mail-Scanner öffnen Links vorab — der Aufruf allein darf nicht zustimmen."""
    import einwilligung
    token = einwilligung.anfrage_erstellen("inhaber@x.de", "castle_acme")
    r = client.get(f"/einwilligung/{token}")
    assert r.status_code == 200 and 'name="lesen"' in r.text and 'name="im_namen"' in r.text
    assert einwilligung.liegt_vor("inhaber@x.de") is None
    assert einwilligung.anfrage_lesen(token), "Link durch blosses Öffnen verbraucht"


def test_einwilligung_nur_mit_beiden_teilen(client, ew_ablage):
    import einwilligung
    token = einwilligung.anfrage_erstellen("inhaber@x.de", "castle_acme")
    r = client.post(f"/einwilligung/{token}", data={"antwort": "ja", "lesen": "1"})
    assert r.status_code == 200 and "beiden Punkten" in r.text
    assert einwilligung.liegt_vor("inhaber@x.de") is None
    assert einwilligung.anfrage_lesen(token), "Link bei unvollständiger Antwort verbraucht"


def test_einwilligung_zustimmung_startet_bestellung(client, ew_ablage, monkeypatch):
    import ca_backends
    import einwilligung
    gestartet = {}

    class _Weg:
        def get_label(self): return "CASTLE"
        async def initiate_renewal(self, email, cfg, extra=None):
            gestartet.update(email=email, extra=extra, backend=cfg.get("backend"))
    monkeypatch.setattr(ca_backends, "get_backend", lambda name: _Weg())
    token = einwilligung.anfrage_erstellen("inhaber@x.de", "castle_acme", "2026-10-22T10:00:00Z")
    r = client.post(f"/einwilligung/{token}", data={"antwort": "ja", "lesen": "1", "im_namen": "1"})
    assert r.status_code == 200 and "Danke" in r.text
    assert einwilligung.liegt_vor("inhaber@x.de")["von"] == "nutzer"
    assert gestartet == {"email": "inhaber@x.de", "backend": "castle_acme",
                         "extra": {"ca_terms_accepted_at": "2026-10-22T10:00:00Z"}}
    assert "nicht mehr gültig" in client.get(f"/einwilligung/{token}").text, "einmalig"


def test_einwilligung_ablehnen(client, ew_ablage):
    import einwilligung
    token = einwilligung.anfrage_erstellen("inhaber@x.de", "castle_acme")
    r = client.post(f"/einwilligung/{token}", data={"antwort": "nein"})
    assert "Abgelehnt" in r.text and einwilligung.liegt_vor("inhaber@x.de") is None


def test_unbekannter_link(client, ew_ablage):
    assert "nicht mehr gültig" in client.get("/einwilligung/gibtsnicht").text
    assert "nicht mehr gültig" in client.post("/einwilligung/gibtsnicht",
                                              data={"antwort": "ja", "lesen": "1", "im_namen": "1"}).text


def test_bestellen_ohne_einwilligung_meldet_409(client, ew_ablage, monkeypatch):
    import acme_state
    import ca_backends
    import settings_store
    monkeypatch.setitem(settings_store._data, "MAILBOX_CONFIG",
                        {"g1": {"primary": "neu@x.de", "smime": True}})
    monkeypatch.setitem(settings_store._data, "CA_USER_CONFIG", {"neu@x.de": {"backend": "castle_acme"}})

    class _Weg:
        def get_name(self): return "castle_acme"
        def can_auto_renew(self): return True
        async def initiate_renewal(self, email, cfg, extra=None):
            raise acme_state.EinwilligungFehlt("fehlt")
    monkeypatch.setattr(ca_backends, "get_backend", lambda name: _Weg())
    r = client.post("/api/smime/renewal/initiate/neu@x.de", json={})
    assert r.status_code == 409 and r.json()["einwilligung_fehlt"] is True


def test_admin_einwilligung_haelt_den_namen_fest(client, ew_ablage):
    import einwilligung
    r = client.post("/api/smime/einwilligung/neu@x.de", json={"aktion": "admin"})
    assert r.status_code == 200 and r.json()["zustand"] == "erteilt"
    assert einwilligung.liegt_vor("neu@x.de")["von"] == "admin:testadmin"
    r = client.post("/api/smime/einwilligung/neu@x.de", json={"aktion": "widerrufen"})
    assert r.json()["zustand"] == "fehlt"
