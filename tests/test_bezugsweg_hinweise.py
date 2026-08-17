"""Erneuerungshinweise dürfen keine Wege nennen, die es nicht gibt.

Alle vier Bezugswege verwiesen im Text an den Postfachinhaber auf den
Selbstbedienungs-Upload — bei den automatischen als „Fallback, falls die
automatische Erneuerung fehlschlägt". Das beschreibt keinen gangbaren Weg: Dort
beschafft das Gateway das Zertifikat, der Postfachinhaber hat nichts in der Hand.
Der Verweis führte ihn auf ein leeres Formular, und zwar genau in dem Moment, in
dem etwas schiefgegangen ist.

⚠️ Diese Prüfung ist bewusst textnah. Das ist sonst zu vermeiden, hier aber der
Punkt: Der Fehler war ein Versprechen im Fliesstext, nicht in der Logik. Ein
Test auf Funktionsverhalten hätte ihn nie gefunden — er war formal korrekt und
inhaltlich falsch.
"""
import inspect

import pytest

from ca_backends import assisted_manual, castle_acme, digicert_direct, hub_provider

def _hub_backend():
    """HubProviderBackend braucht einen Anbietersatz — ohne ihn scheitert der
    Textaufbau. Ihn deshalb wegzulassen wäre der bequeme Weg gewesen: Der Test
    liefe grün und prüfte einen von drei automatischen Bezugswegen nicht."""
    b = hub_provider.HubProviderBackend.__new__(hub_provider.HubProviderBackend)
    b._p = {"id": "certum", "label": "Certum S/MIME"}
    return b


AUTOMATISCH = [
    ("castle_acme", castle_acme.CastleAcmeBackend, None),
    ("digicert_direct", digicert_direct.DigiCertDirectBackend, None),
    ("hub_provider", hub_provider.HubProviderBackend, _hub_backend),
]


def _text(klasse, aufbau=None, **kwargs):
    """Anweisungstext erzeugen, unabhängig von der Signatur des Bezugswegs."""
    b = aufbau() if aufbau else klasse.__new__(klasse)
    argumente = dict(email="max@example.org", days_left=14, expiry_str="01.09.2026",
                     upload_url="https://gw.example.org/selfservice/TOKEN",
                     user_config={})
    argumente.update(kwargs)
    return klasse.get_instructions_html(b, **argumente)


@pytest.mark.parametrize("name,klasse,aufbau", AUTOMATISCH)
def test_automatischer_bezug_verweist_nicht_auf_den_upload(name, klasse, aufbau):
    text = _text(klasse, aufbau)
    assert "selfservice" not in text.lower(), (
        f"{name} verweist den Postfachinhaber auf den Upload — dort hat er aber "
        f"kein Zertifikat, das er hochladen könnte")


@pytest.mark.parametrize("name,klasse,aufbau", AUTOMATISCH)
def test_automatischer_bezug_nennt_den_tatsaechlichen_weg(name, klasse, aufbau):
    """Wenn schon kein Upload, dann wenigstens die Wahrheit: Der Administrator
    greift ein, der Postfachinhaber kann nichts tun."""
    text = _text(klasse, aufbau).lower()
    assert "administrator" in text, f"{name} sagt nicht, wer im Fehlerfall handelt"


@pytest.mark.parametrize("name,klasse,aufbau", AUTOMATISCH)
def test_kein_automatischer_bezug_nennt_einen_fallback(name, klasse, aufbau):
    """Die konkrete Formulierung, die in die Irre führte."""
    assert "fallback" not in _text(klasse, aufbau).lower(), f"{name} nennt einen Fallback"


def test_manueller_bezug_verweist_weiterhin_auf_den_upload():
    """Gegenprobe: Beim manuellen Weg holt der Postfachinhaber das Zertifikat
    selbst — dort ist der Upload sein einziger Weg, es einzuspielen. Ihn dort zu
    entfernen wäre schlimmer als der ursprüngliche Fehler."""
    text = _text(assisted_manual.AssistedManualBackend)
    assert "selfservice" in text.lower(), "der manuelle Weg braucht den Upload"
