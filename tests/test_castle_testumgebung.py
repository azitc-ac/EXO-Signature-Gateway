"""CASTLE-Testumgebung ist ein eigener Bezugsweg, kein Ankreuzfeld.

Bis 18.08.2026 stand „Staging-Server verwenden" als Ankreuzfeld NEBEN der
Auswahl des Bezugswegs. Zwei Nachteile, die erst auffallen, wenn es zu spät ist:

* Die Wahl „echte oder unechte Zertifikate" ist die Wahl des Wegs — sie gehört
  in dieselbe Liste wie alle anderen, nicht daneben.
* Das Feld blieb gesetzt, wenn jemand den Bezugsweg wechselte. Dort unsichtbar,
  aber vorhanden: Wer später zu CASTLE zurückkehrte, bekam ohne weiteres Zutun
  wieder Testzertifikate — also unbrauchbare, ohne es zu merken.

Invarianten:

1. Beide Wege stehen in der Auswahl, und der Testweg ist als solcher erkennbar.
2. Der Produktionsweg bestellt bei der Produktion, der Testweg beim Testserver.
3. ⚠️ Die Umstellung eines Altbestands ändert NUR Einträge mit gesetztem Flag —
   und lässt das Flag stehen. Wer es entfernte, würde einen Bestand, der die
   Umstellung nicht durchläuft, still auf echte Zertifikate umstellen.
4. Sie ist idempotent: ein zweiter Lauf ändert nichts mehr.
"""
import pytest

import settings_store
from ca_backends import registry
from ca_backends.castle_acme import CastleAcmeBackend, CastleAcmeStagingBackend


def test_beide_wege_stehen_zur_auswahl():
    namen = {b["name"] for b in registry.list_backends()}
    assert {"castle_acme", "castle_acme_staging"} <= namen


def test_der_testweg_ist_als_solcher_beschriftet():
    """Wer ihn versehentlich wählt, muss es an der Beschriftung merken —
    ausgestellte Zertifikate sind wertlos."""
    label = CastleAcmeStagingBackend().get_label().lower()
    assert "test" in label
    assert "keine gültigen" in label or "keine gueltigen" in label


def test_produktionsweg_bestellt_bei_der_produktion():
    assert CastleAcmeBackend().ist_testumgebung({}) is False


def test_testweg_bestellt_immer_beim_testserver():
    """Auch ohne gesetztes Flag — der Weg selbst ist die Aussage."""
    assert CastleAcmeStagingBackend().ist_testumgebung({}) is True
    assert CastleAcmeStagingBackend().ist_testumgebung({"staging": False}) is True


def test_altbestand_mit_flag_wird_ueberfuehrt(monkeypatch):
    gespeichert = {}
    cfg = {
        "a@x.de": {"backend": "castle_acme", "staging": True},
        "b@x.de": {"backend": "castle_acme", "staging": False},
        "c@x.de": {"backend": "assisted_manual", "staging": True},
        "d@x.de": {"backend": "hub:certum"},
    }
    monkeypatch.setattr(settings_store, "get", lambda k: cfg if k == "CA_USER_CONFIG" else None)
    monkeypatch.setattr(settings_store, "update", lambda d: gespeichert.update(d))

    anzahl = registry.migriere_staging_flag()

    neu = gespeichert["CA_USER_CONFIG"]
    assert anzahl == 1
    assert neu["a@x.de"]["backend"] == "castle_acme_staging"
    assert neu["a@x.de"]["staging"] is True, "Flag entfernt — ein Bestand ohne " \
        "Umstellung bestellte damit still echte Zertifikate"
    assert neu["b@x.de"]["backend"] == "castle_acme", "ohne Flag umgestellt"
    assert neu["c@x.de"]["backend"] == "assisted_manual", "fremder Bezugsweg angefasst"
    assert neu["d@x.de"]["backend"] == "hub:certum"


def test_umstellung_ist_idempotent(monkeypatch):
    cfg = {"a@x.de": {"backend": "castle_acme_staging", "staging": True}}
    geschrieben = []
    monkeypatch.setattr(settings_store, "get", lambda k: cfg if k == "CA_USER_CONFIG" else None)
    monkeypatch.setattr(settings_store, "update", lambda d: geschrieben.append(d))

    assert registry.migriere_staging_flag() == 0
    assert geschrieben == [], "schreibt, obwohl nichts zu ändern war"


def test_kaputte_eintraege_brechen_die_umstellung_nicht(monkeypatch):
    """Beim Start darf ein einzelner unbrauchbarer Eintrag nicht den ganzen
    Lauf verhindern — sonst bliebe auch alles andere unumgestellt."""
    cfg = {"a@x.de": "kein dict", "b@x.de": {"backend": "castle_acme", "staging": True}}
    gespeichert = {}
    monkeypatch.setattr(settings_store, "get", lambda k: cfg if k == "CA_USER_CONFIG" else None)
    monkeypatch.setattr(settings_store, "update", lambda d: gespeichert.update(d))

    assert registry.migriere_staging_flag() == 1
    assert gespeichert["CA_USER_CONFIG"]["b@x.de"]["backend"] == "castle_acme_staging"
