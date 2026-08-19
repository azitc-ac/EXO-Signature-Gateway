"""Wirkt die Bearbeiter-Rolle auch wirklich? — echte Aufrufe, echte Sitzung.

Abgrenzung zu `test_wachen.py`: Dort steht die **Liste** — welche Adresse ohne
Verwaltungsrolle auskommt (`EDITOR_DARF`), und sie ist abschliessend. Das ist
eine Prüfung der Routentabelle: Sie liest ab, welche Wache an einer Route hängt.

Genau das ist ihre Grenze. Wäre `_require_admin` selbst wirkungslos — etwa weil
die Rolle nicht mehr aus dem Sitzungskeks gelesen wird oder ein Umbau die
Prüfung entschärft — bliebe dort alles grün, während jeder Bearbeiter alles
darf. Eine Wache, die nur behauptet zu wachen, ist schlimmer als keine: Man
verlässt sich auf sie.

Deshalb hier der Gegenbeweis mit einem echten Sitzungskeks und echten Aufrufen.

⚠️ Die Liste selbst gehört NICHT hierher. Zwei Listen für dieselbe Sache laufen
auseinander; `EDITOR_DARF` in `test_wachen.py` ist die eine Quelle.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def bearbeiter_sitzung(monkeypatch):
    """Angemeldeter Bearbeiter — echtes Sitzungskeks, keine übergangene Prüfung.

    Bewusst KEIN `dependency_overrides`: Damit würde genau die Prüfung ersetzt,
    die hier bewiesen werden soll.
    """
    from starlette.testclient import TestClient
    import sso
    from webui import app as wa

    # Sitzungsgeheimnis festlegen statt erzeugen lassen: `_get_secret()` würde
    # sonst über settings_store.update() in die echte settings.json schreiben.
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-rollenpruefung")
    keks = sso.create_session_cookie("bearbeiter@example.org", role=sso.ROLE_EDITOR)
    with TestClient(wa.app) as c:
        c.cookies.set(sso.SESSION_COOKIE, keks)
        yield c


@pytest.fixture
def verwaltungs_sitzung(monkeypatch):
    """Dasselbe mit Verwaltungsrolle — für die Gegenprobe."""
    from starlette.testclient import TestClient
    import sso
    from webui import app as wa

    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-rollenpruefung")
    keks = sso.create_session_cookie("chefin@example.org", role=sso.ROLE_ADMIN)
    with TestClient(wa.app) as c:
        c.cookies.set(sso.SESSION_COOKIE, keks)
        yield c


# Je eine Adresse aus den Bereichen, die dem Bearbeiter am 19.08.2026 entzogen
# wurden — plus zwei, die er nie durfte.
VERBOTEN = [
    ("/api/mailboxes", "Betriebssicht auf alle Postfächer"),
    ("/api/settings/template-policies", "Zuweisung Vorlage → Postfach"),
    ("/api/smime/sammel/lauf", "Zertifikatsbestellung"),
    ("/api/admin-users", "Benutzerverwaltung"),
    ("/api/health/audit-log", "Protokollauszug"),
]


@pytest.mark.parametrize("adresse,zweck", VERBOTEN)
def test_bearbeiter_wird_abgewiesen(bearbeiter_sitzung, adresse, zweck):
    """⚠️ 403, nicht 401: Er IST angemeldet, ihm fehlt die Rolle. Ein 401 würde
    ihn auf die Anmeldeseite schicken, wo er sich im Kreis drehte."""
    antwort = bearbeiter_sitzung.get(adresse)
    assert antwort.status_code == 403, f"{adresse} ({zweck}) → {antwort.status_code}"


def test_bearbeiter_kommt_an_seine_vorlagen(bearbeiter_sitzung):
    """Die Gegenprobe. Ohne sie bewiese der Test oben nur, dass irgendetwas
    blockiert — womöglich die Anmeldung selbst."""
    antwort = bearbeiter_sitzung.get("/api/templates")
    assert antwort.status_code == 200, antwort.text[:200]
    assert "templates" in antwort.json()


def test_bearbeiter_kann_keine_testmail_verschicken(bearbeiter_sitzung):
    """Die Route verschickt echte Mail mit frei wählbarem Absender und Empfänger."""
    antwort = bearbeiter_sitzung.post("/api/test-mail",
                                      json={"from_email": "a@x.de", "to_email": "b@x.de"})
    assert antwort.status_code == 403


@pytest.mark.parametrize("adresse,zweck", VERBOTEN)
def test_die_verwaltung_kommt_ueberall_hin(verwaltungs_sitzung, adresse, zweck):
    """⚠️ Ohne diese Gegenprobe wäre nicht gezeigt, dass das 403 an der ROLLE
    liegt. Eine kaputte Anmeldung träfe beide gleich — und sähe genauso aus."""
    antwort = verwaltungs_sitzung.get(adresse)
    assert antwort.status_code != 403, f"{adresse} ({zweck}) verweigert auch der Verwaltung"


def test_schnittstellenbeschreibung_ist_abgeschaltet():
    """⚠️ FastAPI liefert /docs, /redoc und /openapi.json ohne Anmeldung aus.
    Am 19.08.2026 lagen dort 229 Endpunkte samt Parametern offen — die
    Voreinstellung, nie entschieden. Wer Rechte einschränkt, legt nicht
    gleichzeitig die Landkarte der eigenen Angriffsfläche aus.
    """
    from webui.app import app
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    pfade = {getattr(r, "path", "") for r in app.routes}
    assert not ({"/openapi.json", "/docs", "/redoc"} & pfade)
