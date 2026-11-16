"""Eine Gateway-Kennung im Zitat zählt nur, wenn der Block dem Absender gehört.

Bis v1.12.47 genügte jede Kennung (`<!-- exo-sig-start -->`, `id="exo-sig-s"`,
`class="exo-gateway-sig"`). Sie ist für alle Absender dieselbe — auch für einen
Kollegen und für ein anderes Gateway. Folgen:

* Wer zum ERSTEN Mal auf eine signierte Mail antwortete, bekam nur die
  Antwort-Signatur (Gateway und Verfassen-Add-in).
* Mit `SKIP_SIG_IN_THREAD` (Vorgabe an) ließ `inject()` die Signatur bei einer
  fremden Kennung im Zitat sogar ganz weg.

Zugeordnet wird über das Persönliche einer Signatur: Name und Adresse des
Postfachs. Firma, Anschrift und Zentrale teilen Kollegen.
"""
import email

import pytest

import mail_processor as mp

KOPF = ('<p>Danke, passt.</p><div id="divRplyFwdMsg"><b>Von:</b> Erika Beispiel'
        '<br><b>An:</b> Max Mustermann; kunde@kunde.example</div><div>Hallo zusammen,</div>')

EIGEN = ("<p>Max Mustermann</p><p>Vertrieb</p><p>Firma GmbH</p>"
         "<p>Ringstraße 9, 8010 Graz</p><p>max.mustermann@firma.at</p>")
# Kollegin, gleiche Vorlage: Firma und Anschrift teilen beide — 2 von 5 Zeilen,
# unter der Schwelle des Textwegs. So prüft der Test die KENNUNG allein.
KOLLEGIN = ("<p>Erika Beispiel</p><p>Einkauf</p><p>Firma GmbH</p>"
            "<p>Ringstraße 9, 8010 Graz</p><p>erika.beispiel@firma.at</p>")
FREMD = ("<p>Erika Beispiel</p><p>Andere AG</p><p>Hauptstraße 1, 1010 Wien</p>"
         "<p>erika@andere.example</p>")

ZEILEN = [mp.text_zeilen(EIGEN)]
MERKMALE = ["Max Mustermann", "max.mustermann@firma.at"]


def _mail(html: str):
    return email.message_from_string(
        "From: max.mustermann@firma.at\nTo: erika.beispiel@firma.at\nSubject: AW: x\n"
        "MIME-Version: 1.0\nContent-Type: text/html; charset=utf-8\n\n" + html)


def _markiert(inhalt: str, art: str = "id") -> str:
    if art == "id":
        return f'<div id="x_exo-sig-s">{inhalt}</div>'
    if art == "klasse":
        return f'<div class="exo-gateway-sig">{inhalt}</div>'
    return f"<!-- exo-sig-start --><div>{inhalt}</div><!-- exo-sig-end -->"


@pytest.mark.parametrize("art", ["id", "klasse", "kommentar"])
def test_fremde_kennung_im_zitat_zaehlt_nicht(art):
    """Fall des Befunds: Signatur eines anderen Gateways im Zitat."""
    assert mp.kette_grund(_mail(KOPF + _markiert(FREMD, art)), {"max.mustermann@firma.at"},
                          ZEILEN, MERKMALE) == ""


@pytest.mark.parametrize("art", ["id", "klasse", "kommentar"])
def test_kollegin_mit_gleicher_firma_zaehlt_nicht(art):
    """A schreibt an Kunde + B in Cc; B antwortet allen zum ersten Mal."""
    assert mp.kette_grund(_mail(KOPF + _markiert(KOLLEGIN, art)), {"max.mustermann@firma.at"},
                          ZEILEN, MERKMALE) == ""


@pytest.mark.parametrize("art", ["id", "klasse", "kommentar"])
def test_eigener_block_zaehlt(art):
    assert mp.kette_grund(_mail(KOPF + _markiert(EIGEN, art)), {"max.mustermann@firma.at"},
                          ZEILEN, MERKMALE) == "kennung"


def test_eigener_block_auch_nach_vorlagenwechsel():
    """Die Kennung trägt, wo der Text nicht mehr passt: alte Vorlage, Name und
    Adresse stehen aber darin."""
    alt = "<p>Max Mustermann | max.mustermann@firma.at</p><p>Alte Abteilung</p>"
    assert mp.kette_grund(_mail(KOPF + _markiert(alt)), set(), ZEILEN, MERKMALE) == "kennung"


def test_geteiltes_postfach_ist_ein_absender():
    """Person A signierte als info@, Person B antwortet als info@ — derselbe
    Absender, also Antwort-Signatur. Geprüft wird gegen das Postfach."""
    info = "<p>Kundenservice</p><p>Firma GmbH</p><p>info@firma.at</p>"
    block_von_a = "<p>Erika Beispiel</p>" + info
    merkmale = ["Kundenservice", "info@firma.at"]
    assert mp.kette_grund(_mail(KOPF + _markiert(block_von_a)), {"info@firma.at"},
                          [mp.text_zeilen(info)], merkmale) == "kennung"


def test_vorlage_ohne_name_und_adresse_vergleicht_den_text():
    """Team-Vorlage ohne Persönliches: dann entscheidet der Text im Block."""
    team = "<p>Ihr Team Vertrieb</p><p>Firma GmbH</p><p>Ringstraße 9, 8010 Graz</p>"
    zeilen = [mp.text_zeilen(team)]
    assert mp.kette_grund(_mail(KOPF + _markiert(team)), set(), zeilen, MERKMALE) == "kennung"
    assert mp.kette_grund(_mail(KOPF + _markiert(FREMD)), set(), zeilen, MERKMALE) == ""


def test_text_ohne_kennung_unveraendert():
    """Der Textweg (fremdes Programm hat die Kennung verworfen) bleibt."""
    assert mp.kette_grund(_mail(KOPF + EIGEN), set(), ZEILEN, MERKMALE) == "text"


# ── inject(): SKIP_SIG_IN_THREAD ließ die Signatur ganz weg ─────────────────

SIG = '<div style="font-size:10pt">' + EIGEN + "</div>"


def _injiziert(msg) -> bool:
    return mp._SIG_MARKER_START in (mp.extract_html(msg) or "")


@pytest.mark.parametrize("kette_geprueft", [True, False])
def test_fremde_kennung_im_zitat_haelt_die_signatur_nicht_auf(kette_geprueft):
    msg = _mail(KOPF + _markiert(FREMD))
    out = mp.inject(msg, SIG, "Max Mustermann", use_cid_images=False,
                    kette_geprueft=kette_geprueft)
    assert _injiziert(out)


def test_eigene_markierte_signatur_im_zitat_haelt_inject_weiter_auf():
    """Ohne Handler-Prüfung (Selbsttest, kette_geprueft=False) bleibt der
    Stapelschutz für die EIGENE Signatur."""
    msg = _mail(KOPF + _markiert(EIGEN))
    out = mp.inject(msg, SIG, "Max Mustermann", use_cid_images=False)
    assert not _injiziert(out)


def test_kennung_im_verfassenbereich_haelt_inject_immer_auf():
    msg = _mail(_markiert(EIGEN) + KOPF)
    out = mp.inject(msg, SIG, "Max Mustermann", use_cid_images=False, kette_geprueft=True)
    assert mp.extract_html(out).count("exo-sig-start") == 0


def test_merkmale_aus_dem_postfach():
    import graph_client
    import signature_engine
    u = graph_client.UserData(displayName="Kundenservice", mail="info@firma.at")
    assert signature_engine.erkennungs_merkmale(u, "INFO@firma.at", "") == [
        "Kundenservice", "info@firma.at", "INFO@firma.at"]
