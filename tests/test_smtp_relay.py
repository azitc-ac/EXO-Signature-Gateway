"""Das Relay hat drei Grenzen — und keine davon darf still ausfallen.

ANLASS (2026-08-25)
-------------------
Wiederkehrende Kundenanforderung: Drucker und Anwendungen liefern anonym per
SMTP ab, wie bei einem Exchange vor Ort.

⚠️ Der Ausgangspunkt war nicht „das kann das Gateway noch nicht", sondern das
Gegenteil: `handler.py` reicht jede Nachricht weiter, deren Absender nicht in
`MAILBOX_CONFIG` steht. Wer ein Netz in die Quell-IP-Liste einträgt, hat damit
ein Relay — ohne Absenderprüfung, ohne Zielbeschränkung, ohne dass es irgendwo
stünde. Dieses Modul macht daraus eine Entscheidung mit Grenzen.

Die Tests prüfen deshalb vor allem, dass die Grenzen HALTEN — nicht, dass das
Relay funktioniert. Ein Relay, das zu viel durchlässt, macht dem Kunden Ärger,
den er nicht uns zuschreibt, sondern seinem Ruf.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import smtp_relay  # noqa: E402


@pytest.fixture
def anlage(monkeypatch):
    """Ein Gateway mit Relay für 10.1.5.0/24 und zwei bekannten Postfächern."""
    werte = {
        "SMTP_RELAY_ENABLED": True,
        "SMTP_RELAY_NETWORKS": ["10.1.5.0/24"],
        "SMTP_RELAY_EXTERNAL": False,
        "TENANT_DOMAIN": "firma.onmicrosoft.com",
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))

    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses",
                        lambda: {"chefin@firma.de", "lager@firma.de"})
    return werte


def test_geraet_im_freigegebenen_netz_darf_intern_zustellen(anlage):
    assert smtp_relay.ist_relay_quelle("10.1.5.30")
    erlaubt, grund, _ = smtp_relay.pruefe("drucker@firma.de", ["chefin@firma.de"], "10.1.5.30")
    assert erlaubt, grund


def test_fremdes_netz_ist_keine_relay_quelle(anlage):
    assert not smtp_relay.ist_relay_quelle("192.168.0.5")
    assert not smtp_relay.ist_relay_quelle("")
    assert not smtp_relay.ist_relay_quelle("keine-ip")


def test_abgeschaltet_ist_abgeschaltet(anlage):
    anlage["SMTP_RELAY_ENABLED"] = False
    assert not smtp_relay.ist_relay_quelle("10.1.5.30"), (
        "Bei abgeschaltetem Relay darf kein Netz als Quelle gelten — sonst "
        "wäre der Schalter wirkungslos.")


def test_fremde_absenderdomaene_wird_abgewiesen(anlage):
    """Ein übernommenes Gerät soll nicht als fremde Firma versenden können."""
    erlaubt, grund, antwort = smtp_relay.pruefe(
        "rechnung@paypal.com", ["chefin@firma.de"], "10.1.5.30")
    assert not erlaubt
    assert "paypal.com" in grund
    assert antwort.startswith("550"), "dauerhafte Ablehnung, kein Wiederversuch"


def test_leerer_absender_wird_abgewiesen(anlage):
    """Ein leerer Absender ist bei Zustellberichten üblich — über ein Relay
    hat er nichts zu suchen, denn er lässt sich keiner Domäne zuordnen."""
    erlaubt, _, _ = smtp_relay.pruefe("", ["chefin@firma.de"], "10.1.5.30")
    assert not erlaubt


def test_externes_ziel_nur_nach_freigabe(anlage):
    erlaubt, grund, antwort = smtp_relay.pruefe(
        "drucker@firma.de", ["kunde@extern.de"], "10.1.5.30")
    assert not erlaubt
    assert "kunde@extern.de" in grund
    assert antwort.startswith("550")

    anlage["SMTP_RELAY_EXTERNAL"] = True
    erlaubt, grund, _ = smtp_relay.pruefe(
        "drucker@firma.de", ["kunde@extern.de"], "10.1.5.30")
    assert erlaubt, grund


def test_unbekannte_adresse_eigener_domaene_zaehlt_nicht_als_intern(anlage):
    """⚠️ Feinheit mit Aussenwirkung: `gibtsnicht@firma.de` hat die richtige
    Domäne, aber kein Postfach. Exchange erzeugte daraus einen
    Unzustellbarkeitsbericht — der nach aussen geht. Damit wäre die
    Zielbeschränkung umgangen.
    """
    erlaubt, _, _ = smtp_relay.pruefe(
        "drucker@firma.de", ["gibtsnicht@firma.de"], "10.1.5.30")
    assert not erlaubt


def test_ohne_bekannte_adressen_wird_verweigert(anlage, monkeypatch):
    """⚠️ Die Ausfallrichtung — der wichtigste Test hier.

    `smtp_acl` lässt bei leerer Liste alles durch, damit der Mailfluss nicht
    stoppt. Für ein Relay wäre das falsch herum: Ohne die eigenen Adressen
    lässt sich weder Absender noch Ziel beurteilen. Dann gilt: nichts.
    """
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses", set)
    erlaubt, grund, antwort = smtp_relay.pruefe(
        "drucker@firma.de", ["chefin@firma.de"], "10.1.5.30")
    assert not erlaubt
    assert "nicht bekannt" in grund
    assert antwort.startswith("451"), (
        "vorübergehend, nicht dauerhaft — der Zustand behebt sich, sobald die "
        "Postfachliste geladen ist, und das Gerät soll es dann erneut versuchen")


def test_kaputte_netzangabe_oeffnet_nichts(anlage):
    anlage["SMTP_RELAY_NETWORKS"] = ["nonsens", "10.1.5.0/24"]
    assert smtp_relay.ist_relay_quelle("10.1.5.30"), "gültige Einträge gelten weiter"
    assert not smtp_relay.ist_relay_quelle("8.8.8.8"), (
        "Ein unlesbarer Eintrag darf nicht dazu führen, dass alles erlaubt ist.")


def test_nur_im_smtp_modus(anlage):
    """Der Rückweg entscheidet — Graph und IMAP können fremde Absender nicht.

    ⚠️ Diese Grenze steht bewusst NICHT nur in der Oberfläche: Der Modus lässt
    sich nachträglich umstellen, das Relay bliebe eingeschaltet und nähme Post
    an, die anschliessend niemand zustellen kann. Angenommen und dann verworfen
    ist der schlechteste aller Ausgänge — das Gerät meldet Erfolg.
    """
    gut = ("drucker@firma.de", ["chefin@firma.de"], "10.1.5.30")
    assert smtp_relay.pruefe(*gut)[0], "im Modus smtp muss es gehen"

    for modus in ("graph", "imap", "smtp587"):
        anlage["REINJECT_MODE"] = modus
        erlaubt, grund, antwort = smtp_relay.pruefe(*gut)
        assert not erlaubt, f"Modus {modus} hätte abgelehnt werden müssen"
        assert modus in grund, "der Grund muss den Modus nennen"
        # 4xx, nicht 5xx: Das Gerät soll es nach einer Umstellung erneut
        # versuchen — die Ursache ist Konfiguration, kein dauerhafter Fehler.
        assert antwort.startswith("451"), antwort
