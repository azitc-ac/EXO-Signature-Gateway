"""Entfernen von Signaturen vor dem Einfügen der eigenen.

`_strip_client_sig_divs()` erledigt drei Dinge, die verschieden riskant sind:

1. Outlook **Mobile** — an bekannten Div-IDs, eindeutig.
2. Eine **früher vom Gateway selbst** gesetzte Signatur — an unseren Markern.
3. Outlook **Desktop** — Heuristik „der letzte namenlose Block ist wohl die
   Signatur", kann danebengreifen.

Seit v1.8.56 zwei getrennte Schalter: `STRIP_CLIENT_SIGS_MOBILE` (Vorgabe an,
sicher) gatet 1, `STRIP_CLIENT_SIGS_DESKTOP` (Vorgabe aus, „experimentell")
gatet 3. Keiner von beiden gatet 2: Die eigene Signatur erkennen wir an einem
Merkmal, das wir selbst gesetzt haben — da ist nichts zu raten.
"""
import re

import pytest

import mail_processor as mp

EIGENE = (mp._SIG_MARKER_START
          + f'<div class="{mp._SIG_CLASS}">EIGENE-GATEWAY-SIGNATUR</div>'
          + mp._SIG_MARKER_END)
FREMD = '<div id="ms-outlook-mobile-signature">FREMDE-CLIENT-SIGNATUR</div>'
MAIL = ('<html><body><div>Antworttext</div>'
        + FREMD + EIGENE
        + '<blockquote type="cite">Zitierter Text</blockquote></body></html>')


@pytest.fixture
def schalter(monkeypatch):
    """Setzt STRIP_CLIENT_SIGS_MOBILE (FREMD ist eine Outlook-Mobile-Signatur →
    Weg 1); alle anderen Schlüssel liefern None (Desktop damit aus)."""
    def stellen(wert):
        monkeypatch.setattr(mp.settings_store, "get",
                            lambda k, *a, **kw: wert if k == "STRIP_CLIENT_SIGS_MOBILE" else None)
    return stellen


def test_schalter_aus_entfernt_trotzdem_die_eigene_signatur(schalter):
    """Sonst stünden bei einem zweiten Durchlauf zwei Gateway-Signaturen da.

    Bis v1.7.190 lag die Schalterprüfung ganz oben in der Funktion und nahm
    diesen Schritt stillschweigend mit — obwohl die Beschriftung nur von
    Client-Signaturen spricht.
    """
    schalter(False)
    aus = mp._strip_client_sig_divs(MAIL, "EIGENE-GATEWAY-SIGNATUR")
    assert "EIGENE-GATEWAY-SIGNATUR" not in aus, \
        "Die eigene Signatur muss unabhängig vom Schalter verschwinden"


def test_schalter_aus_laesst_fremde_signaturen_stehen(schalter):
    """Das ist der Zweck des Schalters — genau das und nichts anderes."""
    schalter(False)
    aus = mp._strip_client_sig_divs(MAIL, "EIGENE-GATEWAY-SIGNATUR")
    assert "FREMDE-CLIENT-SIGNATUR" in aus


def test_schalter_an_entfernt_beide(schalter):
    schalter(True)
    aus = mp._strip_client_sig_divs(MAIL, "EIGENE-GATEWAY-SIGNATUR")
    assert "EIGENE-GATEWAY-SIGNATUR" not in aus
    assert "FREMDE-CLIENT-SIGNATUR" not in aus


@pytest.mark.parametrize("wert", [True, False])
def test_der_zitierte_text_bleibt_immer(schalter, wert):
    schalter(wert)
    assert "Zitierter Text" in mp._strip_client_sig_divs(MAIL, "EIGENE-GATEWAY-SIGNATUR")


# ── Desktop-Heuristik (3): Vorgabe AUS ────────────────────────────────────────
# WordSection1 mit einem Signaturblock, dessen Wörter zum übergebenen Fingerprint
# passen — die Heuristik WÜRDE ihn entfernen, wenn sie liefe.
WORDSECTION = ('<html><body><div class="WordSection1"><p>Antworttext</p>'
               '<div><p>Viele Grüße<br>DESKTOP-SIGNATUR-XYZ</p></div>'
               '</div></body></html>')
WS_FINGERPRINT = "Viele Grüße DESKTOP-SIGNATUR-XYZ"


def test_desktop_heuristik_bei_frischen_settings_aus(monkeypatch):
    """Neuer Vorgabewert: ohne ausdrückliches STRIP_CLIENT_SIGS_DESKTOP=True
    greift die experimentelle Desktop-Heuristik nicht — die Signatur bleibt."""
    monkeypatch.setattr(mp.settings_store, "get", lambda k, *a, **kw: None)  # alles Vorgabe
    aus = mp._strip_client_sig_divs(WORDSECTION, WS_FINGERPRINT)
    assert "DESKTOP-SIGNATUR-XYZ" in aus, \
        "Desktop-Heuristik darf bei Vorgabe-Settings nicht greifen"


def test_desktop_heuristik_eingeschaltet_greift(monkeypatch):
    """Gegenprobe: mit STRIP_CLIENT_SIGS_DESKTOP=True fällt die Desktop-Signatur."""
    werte = {"STRIP_CLIENT_SIGS_DESKTOP": True, "SIG_STRIP_MIN_MATCH_PCT": 50}
    monkeypatch.setattr(mp.settings_store, "get", lambda k, *a, **kw: werte.get(k))
    aus = mp._strip_client_sig_divs(WORDSECTION, WS_FINGERPRINT)
    assert "DESKTOP-SIGNATUR-XYZ" not in aus, \
        "Mit eingeschalteter Heuristik muss die Desktop-Signatur fallen"


# ── Der eingepackte Antworttrenner ───────────────────────────────────────────

# Echte Form aus dem Verkehr (14.08.2026): Outlook legt den Trenner in ein
# ATTRIBUTLOSES Div. Wer nur das äussere Tag ansieht, hält den ganzen Block für
# einen Signaturkandidaten — und `_strip_wordsection_sig()` entfernt den
# letzten solchen Block, also den zitierten Text des Gegenübers.
VERSCHACHTELT = ('<div><div style="border:none; border-top:solid #E1E1E1 1.0pt; '
                 'padding:3.0pt 0cm 0cm 0cm"><p><b>Von:</b> Vorname Nachname</p></div></div>')
DIREKT = ('<div style="border:none; border-top:solid #E1E1E1 1.0pt; '
          'padding:3.0pt 0cm 0cm 0cm"><p><b>Von:</b> Vorname Nachname</p></div>')
UMSCHLAG_MIT_TEXT = '<div>Ein Satz<div style="border:none; border-top:solid #E1E1E1 1.0pt">x</div></div>'


def test_trenner_wird_auch_eingepackt_erkannt():
    assert mp._zitatblock_erkannt(VERSCHACHTELT, 0), \
        "Der eingepackte Outlook-Trenner wurde nicht erkannt"


def test_trenner_ohne_umschlag_wird_weiter_erkannt():
    assert mp._zitatblock_erkannt(DIREKT, 0)


def test_umschlag_mit_eigenem_text_ist_kein_zitatblock():
    """Nur ein blosser Umschlag zählt. Steht im äusseren Div selbst Text, ist es
    kein Zitatblock — sonst würde jede Signatur mit Trennlinie verschont und es
    stünden zwei Signaturen in der Mail."""
    assert mp._zitatblock_erkannt(UMSCHLAG_MIT_TEXT, 0) is None


def test_gewoehnlicher_block_ist_kein_zitatblock():
    assert mp._zitatblock_erkannt('<div><p>Viele Grüsse</p></div>', 0) is None


def test_zitat_wrapper_id_auch_mit_exchange_praefix():
    """Exchange stellt fremden IDs beim Zitieren ein `x_` voran."""
    assert mp._zitatblock_erkannt('<div id="x_divRplyFwdMsg">…</div>', 0)


def test_der_zitierte_text_ueberlebt_die_kandidatensuche():
    """Der eigentliche Schaden: `_strip_wordsection_sig()` darf den Zitatblock
    nicht als Signatur entfernen — auch nicht ohne Fingerprint, wo der Code
    sonst „der Struktur vertraut".

    ⚠️ Signatur und Zitat tragen absichtlich UNTERSCHIEDLICHE Wörter. Mit
    denselben (etwa „Vorname Nachname" in beiden) wäre der Test grün, ohne
    irgendetwas zu zeigen: Der gesuchte Text stünde ja noch im jeweils anderen
    Block.
    """
    html = ('<html><body><div class="WordSection1"><p>Antworttext</p>'
            '<div><p>Viele Grüsse<br>KENNWORT-DER-SIGNATUR</p></div>'
            + VERSCHACHTELT +
            '</div></body></html>')
    aus = mp._strip_wordsection_sig(html, frozenset())
    assert "KENNWORT-DER-SIGNATUR" not in aus, \
        "Der Signaturblock hätte fallen müssen — sonst prüft der Test nichts"
    assert "Von:" in aus and "Vorname Nachname" in aus, \
        "Der zitierte Text wurde als Signatur entfernt"
