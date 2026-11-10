"""Drei Update-Kanäle: Stabil (release), Nightly, Entwicklung (main).

ANLASS (2026-11-10)
-------------------
Bis v1.10.106 gab es „main“ und „release“, und den Kanal merkte sich nur der
Browser (localStorage, Vorgabe main). Wer auf main stand, bekam jeden Push —
auch einen, dessen CI noch lief oder rot war. Jetzt:

1. Der Kanal ist die Einstellung UPDATE_CHANNEL. Neue Installationen: release;
   Bestand (Einrichtung begonnen): nightly — per Migration v6.
2. nightly = Zweig `nightly`, den .github/workflows/nightly.yml täglich auf den
   jüngsten grünen main-Stand vor 23:59 Berlin setzt (tools/nightly_stand.py).
3. Unbekannte Kanäle werden abgelehnt — auch im Watcher, der früher alles
   außer „release“ als main behandelte.
"""
import json
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

import settings_store
import update_core

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "tools"))
import nightly_stand  # noqa: E402


@pytest.fixture
def upd(data_dir):
    return update_core.Updater(repo="beispiel/repo", user_agent="Test/1",
                               data_dir=str(data_dir))


# ── update_core ───────────────────────────────────────────────────────────────

def test_nightly_liest_den_eigenen_zweig(upd, data_dir):
    (data_dir / ".remote-version").write_text("9.9.9")   # gilt nur für main
    urls = []

    def _fake(url, timeout=10, accept=None):
        urls.append(url)
        return "2.0.0"
    upd._get = _fake
    d = upd.check_update("nightly", "1.0.0")
    assert d["latest"] == "2.0.0" and d["available"] is True
    # Version aus dem Zweig nightly; die .remote-version (main) zählt hier nicht.
    assert urls[0] == "https://api.github.com/repos/beispiel/repo/contents/VERSION?ref=nightly"


def test_main_liest_weiter_main(upd):
    urls = []
    upd._get = lambda url, timeout=10, accept=None: urls.append(url) or "2.0.0"
    upd.check_update("main", "1.0.0")
    assert urls[0].endswith("?ref=main")


def test_nightly_noch_nicht_angelegt(upd):
    def _404(url, timeout=10, accept=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    upd._get = _404
    d = upd.check_update("nightly", "1.0.0")
    assert d["available"] is False and "Nightly" in d["note"]


def test_unbekannter_kanal_wird_abgelehnt(upd):
    upd._get = lambda *a, **kw: pytest.fail("kein Abruf für einen unbekannten Kanal")
    assert upd.check_update("beta", "1.0.0")["available"] is False
    assert upd.request_update("admin", "1.0.0", channel="beta")["ok"] is False
    assert not upd.trigger.exists()


def test_update_anfrage_traegt_den_kanal(upd):
    assert upd.request_update("admin", "1.0.0", channel="nightly")["ok"] is True
    assert json.loads(upd.trigger.read_text())["channel"] == "nightly"


# ── Einstellung und Migration ────────────────────────────────────────────────

def test_neue_installation_steht_auf_stabil():
    assert settings_store.DEFAULTS["UPDATE_CHANNEL"] == "release"
    daten, _ = settings_store._run_migrations(dict(settings_store.DEFAULTS))
    assert daten["UPDATE_CHANNEL"] == "release"


@pytest.mark.parametrize("merkmal", [{"SETUP_COMPLETE": True}, {"TENANT_ID": "t"},
                                     {"CLIENT_ID": "c"}])
def test_bestand_kommt_auf_nightly(merkmal):
    daten = {**settings_store.DEFAULTS, "_SCHEMA_VERSION": 5, **merkmal}
    daten, geaendert = settings_store._run_migrations(daten)
    assert geaendert and daten["UPDATE_CHANNEL"] == "nightly"


def test_gewaehlter_kanal_bleibt_nach_der_migration():
    daten = {**settings_store.DEFAULTS, "_SCHEMA_VERSION": 6, "SETUP_COMPLETE": True,
             "UPDATE_CHANNEL": "main"}
    daten, _ = settings_store._run_migrations(daten)
    assert daten["UPDATE_CHANNEL"] == "main"


def test_kanal_ist_kein_browserwert_mehr():
    seite = (WURZEL / "app/webui/templates/backup.html").read_text(encoding="utf-8")
    assert "exo_upd_channel" not in seite and "localStorage" not in seite.split("_updSaveChannel")[1][:400]
    assert "savePartial({UPDATE_CHANNEL:" in seite
    for wert in update_core.KANAELE:
        assert f'<option value="{wert}"' in seite


# ── Watcher ──────────────────────────────────────────────────────────────────

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _watcher_lauf(repo: Path, kanal: str) -> subprocess.CompletedProcess:
    funktion = subprocess.run(
        ["sed", "-n", "/^do_git_update()/,/^}/p", str(WURZEL / "update-watcher.sh")],
        capture_output=True, text=True, check=True).stdout
    skript = f'REPO="{repo}"\nCHANNEL="{kanal}"\n{funktion}\ndo_git_update'
    return subprocess.run(["bash", "-c", skript], capture_output=True, text=True)


@pytest.fixture
def klon(tmp_path):
    """Ursprung mit main (v1) und nightly (v2), dazu ein Klon auf main."""
    quelle = tmp_path / "quelle"
    quelle.mkdir()
    _git("init", "-q", "-b", "main", cwd=quelle)
    _git("config", "user.email", "t@t", cwd=quelle)
    _git("config", "user.name", "t", cwd=quelle)
    (quelle / "VERSION").write_text("1\n")
    _git("add", "VERSION", cwd=quelle)
    _git("commit", "-qm", "v1", cwd=quelle)
    _git("branch", "nightly", cwd=quelle)
    _git("checkout", "-q", "nightly", cwd=quelle)
    (quelle / "VERSION").write_text("2\n")
    _git("commit", "-qam", "v2", cwd=quelle)
    _git("checkout", "-q", "main", cwd=quelle)
    ziel = tmp_path / "klon"
    _git("clone", "-q", str(quelle), str(ziel), cwd=tmp_path)
    return ziel


def test_watcher_holt_nightly(klon):
    r = _watcher_lauf(klon, "nightly")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (klon / "VERSION").read_text().strip() == "2"


def test_watcher_lehnt_unbekannten_kanal_ab(klon):
    r = _watcher_lauf(klon, "beta")
    assert r.returncode != 0 and "Unbekannter Kanal" in r.stdout
    assert (klon / "VERSION").read_text().strip() == "1"


# ── Auswahl des Nightly-Stands ───────────────────────────────────────────────

def _lauf(sha, zeit, ergebnis="success", zweig="main", ereignis="push"):
    return {"head_sha": sha, "conclusion": ergebnis, "head_branch": zweig, "event": ereignis,
            "created_at": zeit, "head_commit": {"timestamp": zeit}}


LAEUFE = [_lauf("a", "2026-11-09T20:00:00Z"), _lauf("b", "2026-11-09T22:30:00Z"),
          _lauf("spaet", "2026-11-09T23:30:00Z"), _lauf("rot", "2026-11-09T22:40:00Z", "failure"),
          _lauf("pr", "2026-11-09T22:50:00Z", ereignis="pull_request")]


def test_juengster_gruener_stand_vor_2359_berlin():
    # Winter: 23:59 Berlin = 22:59 UTC. Lauf um 23:15 UTC (00:15 Berlin).
    assert nightly_stand.waehle(LAEUFE, datetime(2026, 11, 9, 23, 15, tzinfo=timezone.utc)) == "b"


def test_vor_der_stichzeit_gilt_der_vortag():
    # 22:15 UTC = 23:15 Berlin: Stichzeit ist der 08.11. — da gab es nichts.
    assert nightly_stand.waehle(LAEUFE, datetime(2026, 11, 9, 22, 15, tzinfo=timezone.utc)) is None


def test_sommerzeit():
    jetzt = datetime(2026, 7, 1, 22, 15, tzinfo=timezone.utc)      # 00:15 Berlin, 2. Juli
    grenze = nightly_stand.stichzeitpunkt(jetzt)
    assert (grenze.day, grenze.hour, grenze.minute) == (1, 23, 59)
    assert grenze.utcoffset().total_seconds() == 7200


def test_workflow_ruft_die_auswahl_und_pusht_ohne_force():
    wf = (WURZEL / ".github/workflows/nightly.yml").read_text(encoding="utf-8")
    assert "python3 tools/nightly_stand.py" in wf
    assert "refs/heads/nightly" in wf and "--force" not in wf.split("run:", 1)[1].replace(
        "ohne --force", "")
    assert 'cron: "15 22,23 * * *"' in wf
