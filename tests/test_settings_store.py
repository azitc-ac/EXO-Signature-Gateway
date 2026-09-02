"""settings_store — Klassifizierung, Typsicherheit, Bereinigung.

`SETTINGS_FILE` ist im Modul fest verdrahtet (`/app/data/settings.json`), im
Hub dagegen über `SP_DATA_DIR` einstellbar — eine kleine, bewusst stehen
gelassene Abweichung. Für die Tests wird der Pfad umgebogen; ein Test, der
gegen die echte Datei liefe, würde CLIENT_SECRET und MAILBOX_CONFIG
überschreiben.
"""
import json

import pytest

import settings_store as ss
from conftest import mode_of


@pytest.fixture
def store(data_dir, monkeypatch):
    """Frischer Speicher auf einer temporären Datei."""
    monkeypatch.setattr(ss, "SETTINGS_FILE", data_dir / "settings.json")
    monkeypatch.setattr(ss, "_data", {})
    ss.init()
    return ss


# ── Typerzwingung ────────────────────────────────────────────────────────────
# str(False) ist "False" und damit truthy. Käme eine Boolean-Einstellung je als
# Zeichenkette herein, wäre sie dauerhaft und still eingeschaltet — genau der
# Fehler, den der Hub bei SECTIGO_RES_TEST hatte.

@pytest.mark.parametrize("eingabe,erwartet", [
    ("false", False), ("False", False), ("0", False), ("nein", False), ("", False),
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    (0, False), (1, True), (True, True), (False, False),
])
def test_bool_einstellung_wird_erzwungen(store, eingabe, erwartet):
    schluessel = next(k for k, v in ss.DEFAULTS.items() if isinstance(v, bool))
    assert ss._coerce(schluessel, eingabe) is erwartet


def test_der_gefaehrliche_fall_string_false(store):
    schluessel = next(k for k, v in ss.DEFAULTS.items() if isinstance(v, bool))
    assert bool("false") is True, "Ausgangslage: die naive Auswertung ist falsch"
    assert ss._coerce(schluessel, "false") is False


def test_int_einstellung_aus_ziffernkette(store):
    k = next(k for k, v in ss.DEFAULTS.items()
             if isinstance(v, int) and not isinstance(v, bool))
    assert ss._coerce(k, "45") == 45
    assert ss._coerce(k, "-3") == -3


def test_unsinnige_zahl_bleibt_unveraendert(store):
    """Bewusst nicht abgefangen: ein Typfehler bei int fällt lautstark auf,
    im Gegensatz zum stillen bool-Fehler."""
    k = next(k for k, v in ss.DEFAULTS.items()
             if isinstance(v, int) and not isinstance(v, bool))
    assert ss._coerce(k, "abc") == "abc"


@pytest.mark.parametrize("wert", ["text", ["a"], {"a": 1}, None])
def test_andere_typen_werden_nicht_angefasst(store, wert):
    k = next(k for k, v in ss.DEFAULTS.items() if isinstance(v, str))
    assert ss._coerce(k, wert) == wert


def test_update_erzwingt_den_typ_beim_speichern(store):
    k = next(k for k, v in ss.DEFAULTS.items() if isinstance(v, bool))
    ss.update({k: "false"})
    assert ss.get(k) is False
    assert json.loads((ss.SETTINGS_FILE).read_text())[k] is False


# ── Whitelist ────────────────────────────────────────────────────────────────

def test_update_nimmt_nur_deklarierte_schluessel(store):
    ss.update({"VOELLIG_UNBEKANNT": "x"})
    assert ss.get("VOELLIG_UNBEKANNT") is None
    assert "VOELLIG_UNBEKANNT" not in json.loads(ss.SETTINGS_FILE.read_text())


# ── Maskierung ───────────────────────────────────────────────────────────────

def test_public_view_maskiert_gesetzte_geheimnisse(store):
    ss.update({"CLIENT_SECRET": "streng-geheim"})
    pv = ss.public_view()
    assert pv["CLIENT_SECRET"] == ss.MASK
    assert "streng-geheim" not in json.dumps(pv)


def test_public_view_laesst_leere_geheimnisse_leer(store):
    ss.update({"CLIENT_SECRET": ""})
    assert ss.public_view()["CLIENT_SECRET"] == ""


def test_public_view_erhaelt_den_wahrheitswert(store):
    """Vorlagen prüfen `{% if s.X %}` — die Maskierung darf das nicht kippen."""
    ss.update({"CLIENT_SECRET": "x", "SMIME_KEY_PASSWORD": ""})
    pv = ss.public_view()
    for k in ss.SECRET_KEYS:
        if k in pv:
            assert bool(pv[k]) == bool(ss.get(k)), f"{k} kippt den Wahrheitswert"


def test_public_view_laesst_nicht_geheimnisse_unveraendert(store):
    ss.update({"CLIENT_ID": "abc-123"})
    assert ss.public_view()["CLIENT_ID"] == "abc-123"


def test_public_view_maskiert_den_app_pool_listenweise(store):
    ss.update({"APP_POOL": [{"client_id": "a", "client_secret": "geheim"}]})
    pv = ss.public_view()
    assert pv["APP_POOL"] == [ss.MASK]
    assert "geheim" not in json.dumps(pv)


def test_kein_geheimnis_taucht_in_der_vorlagensicht_auf(store):
    """Gesamtschutz: alle deklarierten Geheimnisse setzen und prüfen, dass
    keiner der Werte in der serialisierten Sicht vorkommt."""
    werte = {k: f"WERT-{k}" for k in ss.SECRET_KEYS
             if isinstance(ss.DEFAULTS.get(k), str)}
    ss.update(werte)
    serialisiert = json.dumps(ss.public_view())
    for k, v in werte.items():
        assert v not in serialisiert, f"{k} steht im Klartext in der Vorlagensicht"


# ── Verwaiste Schlüssel ──────────────────────────────────────────────────────

def test_purge_entfernt_nur_gelistete_schluessel(store):
    veraltet = next(iter(ss.OBSOLETE_KEYS))
    ss.force_update({veraltet: "alt", "MAILBOX_HEALTH": {"a": 1}, "_DAILY_LAST_RUN": "x"})
    entfernt = ss.purge_obsolete()
    assert veraltet in entfernt
    assert ss.get("MAILBOX_HEALTH") == {"a": 1}, "Laufzeitzustand darf nicht wegfallen"
    assert ss.get("_DAILY_LAST_RUN") == "x"


def test_purge_ist_idempotent(store):
    ss.force_update({next(iter(ss.OBSOLETE_KEYS)): "x"})
    ss.purge_obsolete()
    assert ss.purge_obsolete() == []


def test_purge_entfernt_die_alten_ca_zugangsdaten(store):
    """Der konkrete Anlass: nach dem Ausbau der CA-Direktanbindung (v1.5.125)
    blieben Zugangsdaten in settings.json stehen, ohne dass Code sie las oder
    eine Oberfläche sie löschen konnte."""
    assert "SECTIGO_PASSWORD" in ss.OBSOLETE_KEYS
    ss.force_update({"SECTIGO_PASSWORD": "altes-passwort"})
    ss.purge_obsolete()
    assert "SECTIGO_PASSWORD" not in json.loads(ss.SETTINGS_FILE.read_text())


def test_unknown_keys_meldet_undeklarierte(store):
    ss.force_update({"IRGENDWAS_NEUES": 1})
    assert "IRGENDWAS_NEUES" in ss.unknown_keys()


def test_unknown_keys_schweigt_bei_internem_zustand(store):
    ss.force_update({k: "x" for k in ss.INTERNAL_KEYS})
    assert ss.unknown_keys() == []


def test_obsolete_keys_haben_alle_eine_begruendung(store):
    for k, grund in ss.OBSOLETE_KEYS.items():
        assert grund and len(grund) > 10, f"{k} ohne brauchbare Begründung"


def test_klassifizierungen_ueberschneiden_sich_nicht(store):
    assert not (set(ss.OBSOLETE_KEYS) & set(ss.DEFAULTS)), \
        "ein Schlüssel kann nicht gleichzeitig gültig und veraltet sein"
    assert not (ss.INTERNAL_KEYS & set(ss.OBSOLETE_KEYS))


def test_alle_secret_keys_sind_deklariert(store):
    unbekannt = ss.SECRET_KEYS - set(ss.DEFAULTS)
    assert not unbekannt, f"SECRET_KEYS nennt undeklarierte Schlüssel: {unbekannt}"


# ── Dateirechte ──────────────────────────────────────────────────────────────

def test_settings_json_wird_mit_600_geschrieben(store):
    ss.update({"CLIENT_ID": "x"})
    assert mode_of(ss.SETTINGS_FILE) == "600"


def test_rechte_bleiben_nach_erneutem_speichern(store):
    ss.update({"CLIENT_ID": "x"})
    ss.SETTINGS_FILE.chmod(0o644)
    ss.update({"CLIENT_ID": "y"})
    assert mode_of(ss.SETTINGS_FILE) == "600", \
        "rename() erbt die Rechte der Temp-Datei — chmod muss dort passieren"


def test_backup_datei_bekommt_ebenfalls_600(store):
    ss.update({"CLIENT_ID": "x"})
    ss.update({"CLIENT_ID": "y"})          # erzeugt settings.bak
    bak = ss.SETTINGS_FILE.with_suffix(".bak")
    if bak.exists():
        assert mode_of(bak) == "600", "die Sicherung enthält dieselben Geheimnisse"


# ── Selbstinitialisierung ────────────────────────────────────────────────────

def test_lesen_ohne_init_liefert_die_datei_nicht_die_vorgaben(data_dir, monkeypatch):
    """Vorher lieferten get()/get_all() ohne init() STILL die Vorgabewerte —
    in Subprozessen also verlässlich das Falsche."""
    f = data_dir / "settings.json"
    f.write_text(json.dumps({"CLIENT_ID": "aus-der-datei"}))
    monkeypatch.setattr(ss, "SETTINGS_FILE", f)
    monkeypatch.setattr(ss, "_data", {})
    assert ss.get("CLIENT_ID") == "aus-der-datei"
    assert len(ss.get_all()) > 1


# ── Migration v2 → v3: STRIP_CLIENT_SIGS aufgeteilt ───────────────────────────

def test_migration_strip_client_sigs_an_wird_desktop(store, monkeypatch):
    """Ein gespeichertes STRIP_CLIENT_SIGS=True war die bewusste Wahl für die
    Heuristik → landet in _DESKTOP; _MOBILE kommt auf True; alter Schlüssel weg."""
    daten = {"STRIP_CLIENT_SIGS": True}
    aus = ss._migrate_v2_to_v3(daten)
    assert aus["STRIP_CLIENT_SIGS_DESKTOP"] is True
    assert aus["STRIP_CLIENT_SIGS_MOBILE"] is True
    assert "STRIP_CLIENT_SIGS" not in aus


def test_migration_strip_client_sigs_aus_bleibt_desktop_aus(store):
    aus = ss._migrate_v2_to_v3({"STRIP_CLIENT_SIGS": False})
    assert aus["STRIP_CLIENT_SIGS_DESKTOP"] is False
    assert aus["STRIP_CLIENT_SIGS_MOBILE"] is True
    assert "STRIP_CLIENT_SIGS" not in aus


def test_migration_ohne_alten_schluessel_aendert_nichts(store):
    """Wer den Schlüssel nie gespeichert hatte, bekommt nur die neuen Vorgaben
    (Mobile an, Desktop aus) — die Migration fasst nichts an."""
    aus = ss._migrate_v2_to_v3({"CLIENT_ID": "x"})
    assert "STRIP_CLIENT_SIGS_DESKTOP" not in aus
    assert "STRIP_CLIENT_SIGS_MOBILE" not in aus


def test_migrationskette_setzt_desktop_und_entfernt_alt(store):
    """Über die volle Kette (_run_migrations): alte Datei mit STRIP_CLIENT_SIGS
    kommt mit _DESKTOP heraus, ohne den alten Schlüssel, auf aktueller Version."""
    daten, geaendert = ss._run_migrations({"STRIP_CLIENT_SIGS": True})
    assert geaendert
    assert daten["STRIP_CLIENT_SIGS_DESKTOP"] is True
    assert "STRIP_CLIENT_SIGS" not in daten
    assert daten["_SCHEMA_VERSION"] == ss.SETTINGS_SCHEMA_VERSION
