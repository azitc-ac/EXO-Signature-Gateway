"""update_core — Container-Seite des Selbst-Updates.

Kein Netzzugriff: `_get` wird durchgehend ersetzt. Ein Test, der GitHub
befragt, wäre in der CI unzuverlässig und würde bei Ausfall fälschlich
Alarm schlagen.
"""
import json
import urllib.error

import pytest

import update_core


@pytest.fixture
def upd(data_dir):
    return update_core.Updater(repo="beispiel/repo", user_agent="Test/1",
                               data_dir=str(data_dir))


def stub(upd, antwort=None, fehler=None):
    """`_get` durch eine feste Antwort oder einen Fehler ersetzen."""
    def _fake(url, timeout=10, accept=None):
        # Signatur MUSS der echten `_get` folgen — sonst schlaegt der Stub mit
        # TypeError fehl, sobald dort ein Parameter dazukommt, und der Test
        # meldet einen Folgefehler statt der Ursache.
        if fehler:
            raise fehler
        return antwort
    upd._get = _fake


# ── Der Drei-Zustands-Vertrag von `available` ────────────────────────────────
# Die Oberfläche wertet drei Fälle aus (backup.html: === true / === null / sonst):
#   True  → Update anbieten
#   None  → nicht ermittelbar: Hinweis zeigen, Schaltfläche SICHTBAR lassen
#   False → aktuell / nichts zu installieren: Schaltfläche VERBERGEN
# None auf False zu vereinheitlichen sah beim Zusammenlegen wie eine Aufräumung
# aus und hätte auf privaten Repos das Update-Starten unmöglich gemacht.

def test_available_true_wenn_neuere_version(upd):
    stub(upd, "2.0.0")
    d = upd.check_update("main", "1.0.0")
    assert d["available"] is True
    assert d["latest"] == "2.0.0"


def test_available_false_wenn_aktuell(upd):
    stub(upd, "1.0.0")
    assert upd.check_update("main", "1.0.0")["available"] is False


def test_available_false_wenn_fernversion_aelter(upd):
    stub(upd, "0.9.0")
    assert upd.check_update("main", "1.0.0")["available"] is False


def test_available_none_bei_privatem_repo_ohne_remote_version(upd):
    """HTTP 404 heisst bei privaten Repos: nicht ermittelbar — NICHT 'aktuell'.
    Bei False verschwände die Installations-Schaltfläche."""
    stub(upd, fehler=urllib.error.HTTPError("u", 404, "Not Found", {}, None))
    d = upd.check_update("main", "1.0.0")
    assert d["available"] is None, "muss None sein, damit die Schaltfläche sichtbar bleibt"
    assert d["ok"] is True
    assert "privates Repository" in d["note"]


def test_available_false_wenn_kein_release_veroeffentlicht(upd):
    """Anders als beim 404-Fall: hier GIBT es nichts zu installieren, die
    Schaltfläche soll also verborgen bleiben."""
    stub(upd, json.dumps({"message": "Not Found"}))
    d = upd.check_update("release", "1.0.0")
    assert d["available"] is False
    assert "kein Release" in d["note"]


# ── Private Repositorys: .remote-version als Primärquelle ────────────────────

def test_remote_version_datei_hat_vorrang_vor_der_api(upd, data_dir):
    (data_dir / ".remote-version").write_text("3.1.4\n")
    stub(upd, fehler=AssertionError("die API darf gar nicht befragt werden"))
    d = upd.check_update("main", "1.0.0")
    assert d["latest"] == "3.1.4"
    assert d["available"] is True


def test_leere_remote_version_faellt_auf_die_api_zurueck(upd, data_dir):
    (data_dir / ".remote-version").write_text("   \n")
    stub(upd, "2.0.0")
    assert upd.check_update("main", "1.0.0")["latest"] == "2.0.0"


def test_remote_version_wird_im_release_kanal_ignoriert(upd, data_dir):
    (data_dir / ".remote-version").write_text("9.9.9")
    stub(upd, json.dumps({"tag_name": "v2.0.0", "html_url": "http://x"}))
    d = upd.check_update("release", "1.0.0")
    assert d["latest"] == "2.0.0"


# ── Fehlerfälle ──────────────────────────────────────────────────────────────

def test_netzwerkfehler_meldet_nicht_ok(upd):
    stub(upd, fehler=urllib.error.URLError("keine Route"))
    d = upd.check_update("main", "1.0.0")
    assert d["ok"] is False
    assert "nicht erreichbar" in d["error"]


def test_http_500_meldet_nicht_ok(upd):
    stub(upd, fehler=urllib.error.HTTPError("u", 500, "Serverfehler", {}, None))
    d = upd.check_update("main", "1.0.0")
    assert d["ok"] is False
    assert "500" in d["error"]


# ── Auslöser-Dateien ─────────────────────────────────────────────────────────

def test_trigger_wird_mit_644_geschrieben(upd, data_dir):
    """644 ist hier RICHTIG: der Host-Watcher läuft als anderer Benutzer und
    muss die Datei lesen. Sie enthält keine Geheimnisse."""
    from conftest import mode_of
    assert upd.request_update("tester", "1.0.0")["ok"] is True
    t = data_dir / ".update-trigger"
    assert mode_of(t) == "644"
    payload = json.loads(t.read_text())
    assert payload["requested_by"] == "tester"
    assert payload["current_version"] == "1.0.0"
    assert payload["channel"] == "main"


def test_zielversion_landet_im_trigger(upd, data_dir):
    upd.request_update("t", "1.0.0", "release", target_version="1.2.3")
    assert json.loads((data_dir / ".update-trigger").read_text())["target_version"] == "1.2.3"


def test_kein_update_waehrend_eines_laufenden(upd, data_dir):
    (data_dir / ".update-status").write_text('{"state":"running"}')
    assert upd.request_update("t", "1.0.0") == {"ok": False, "error": "Update läuft bereits"}
    assert not (data_dir / ".update-trigger").exists()


def test_kein_neustart_waehrend_eines_updates(upd, data_dir):
    (data_dir / ".update-status").write_text('{"state":"running"}')
    r = upd.request_container_restart("t")
    assert r["ok"] is False


def test_status_ohne_datei_ist_idle(upd):
    assert upd.get_status() == {"state": "idle"}


def test_clear_status_ohne_datei_wirft_nicht(upd):
    upd.clear_status()


# ── Watcher-Heartbeat ────────────────────────────────────────────────────────

def test_watcher_ok_false_ohne_heartbeat(upd):
    assert upd.watcher_ok() is False


def test_watcher_ok_true_mit_frischem_heartbeat(upd, data_dir):
    (data_dir / ".update-heartbeat").write_text("x")
    assert upd.watcher_ok() is True


def test_watcher_ok_false_bei_altem_heartbeat(upd, data_dir):
    import os, time
    hb = data_dir / ".update-heartbeat"
    hb.write_text("x")
    alt = time.time() - update_core.HEARTBEAT_MAX_AGE_S - 60
    os.utime(hb, (alt, alt))
    assert upd.watcher_ok() is False


# ── Versionsvergleich ────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b,groesser", [
    ("1.0.1", "1.0.0", True), ("1.10.0", "1.9.0", True),
    ("v2.0.0", "1.9.9", True), ("1.0.0", "1.0.0", False),
    ("kaputt", "1.0.0", False),
])
def test_versionsvergleich(a, b, groesser):
    assert (update_core._version_tuple(a) > update_core._version_tuple(b)) is groesser


def test_changelog_wird_nach_versionsabstand_begrenzt(upd):
    stub(upd, "\n".join(f"## v1.0.{i}\nText {i}" for i in range(30, 0, -1)))
    entries = upd.fetch_changelog_entries("1.0.1", "1.0.4")
    assert len(entries) == 3            # Abstand 3
    assert entries[0]["header"] == "## v1.0.30"


def test_changelog_deckelt_bei_25(upd):
    stub(upd, "\n".join(f"## v1.0.{i}\nText" for i in range(40, 0, -1)))
    assert len(upd.fetch_changelog_entries("1.0.0", "9.0.0")) == update_core.MAX_CHANGELOG_ENTRIES


def test_changelog_bei_netzwerkfehler_leer_statt_absturz(upd):
    stub(upd, fehler=urllib.error.URLError("weg"))
    assert upd.fetch_changelog_entries("1.0.0", "2.0.0") == []


def test_repo_datei_nutzt_die_api_statt_raw():
    """Die Fernversion darf NICHT ueber raw.githubusercontent geholt werden.

    Der Dienst liefert ueber ein Auslieferungsnetz mit max-age=300 aus und
    meldet direkt nach einer Veroeffentlichung bis zu fuenf Minuten lang die
    vorige Fassung. Am 2026-07-31 gemessen: raw lieferte 1.7.113
    (source-age 271), waehrend im Repository 1.7.114 stand.

    Drei naheliegende Auswege wurden am selben Fall geprueft und blieben
    wirkungslos: ein wechselnder Abfrageparameter, `Cache-Control: no-cache`
    und `Pragma: no-cache`. Nur die API antwortete frisch. Wer hier auf raw
    zurueckbaut, bekommt das Verhalten zurueck — deshalb dieser Test.
    """
    import update_core

    u = update_core.Updater.__new__(update_core.Updater)
    u.repo, u.user_agent = "beispiel/repo", "test"
    gesehen = {}

    def _fake_get(url, timeout=10, accept=None):
        gesehen.update(url=url, accept=accept)
        return "1.2.3\n"

    u._get = _fake_get
    assert u._repo_datei("VERSION") == "1.2.3\n"
    assert "raw.githubusercontent.com" not in gesehen["url"]
    assert gesehen["url"].startswith(
        "https://api.github.com/repos/beispiel/repo/contents/VERSION")
    assert gesehen["accept"] == "application/vnd.github.raw"
