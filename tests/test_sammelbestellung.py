"""Vorschau eines Sammellaufs — was würde passieren, bevor Geld fliesst.

Die Vorschau ist nicht Beiwerk, sondern der Kern des Vorhabens. Ohne sie würde
ein Sammellauf für hundert Postfächer hundert Einzelprüfungen auslösen, jede
Lücke eine eigene Nachladung mit eigener Grundgebühr, und ein Monatskontingent
könnte mitten im Lauf greifen und einen halb erledigten Stand hinterlassen.

Invarianten:

1. Bestellt würde nur, wer es nötig hat — wer schon ein gültiges Zertifikat hat
   oder für wen S/MIME gar nicht eingeschaltet ist, fällt heraus.
2. ⚠️ Abgelaufene Zertifikate zählen NICHT als Versorgung. Sonst überspringt der
   Lauf genau die Postfächer, die ihn am dringendsten brauchen.
3. Die Kosten stehen VOR dem Lauf fest, brutto, und werden gegen das Guthaben
   gehalten.
4. Ein Monatskontingent kappt die Menge sichtbar, statt mitten im Lauf zu greifen.
5. Fehlende Voraussetzungen werden EINMAL genannt, nicht je Postfach.
"""
import pytest

import sammelbestellung as sb


@pytest.fixture
def welt(monkeypatch):
    """Postfächer, Zertifikate und Hub-Auskunft ersetzen."""
    stand = {
        "mailboxes": {
            "g1": {"primary": "a@x.de", "display_name": "A", "smime": True},
            "g2": {"primary": "b@x.de", "display_name": "B", "smime": True},
            "g3": {"primary": "c@x.de", "display_name": "C", "smime": False},
        },
        "certs": {},                      # email -> [{expiry, error}]
        "rechte": {"ok": True, "reason": "", "balance_cents": 10000,
                   "cert_price_cents": 1000, "vat_percent": 19,
                   "monthly_limit": 0, "used_this_month": 0,
                   "billing_mode": "prepaid"},
        "provider": {"id": "certum", "label": "Certum", "price_cents": 500},
    }

    import settings_store, smime_store, hub_catalog, hub_client
    monkeypatch.setattr(settings_store, "get",
                        lambda k: stand["mailboxes"] if k == "MAILBOX_CONFIG" else None)
    monkeypatch.setattr(smime_store, "list_user_certs",
                        lambda e: stand["certs"].get(e, []))
    monkeypatch.setattr(hub_catalog, "get", lambda pid: stand["provider"])

    async def _rechte():
        return stand["rechte"]
    monkeypatch.setattr(hub_client, "cert_eligibility", _rechte)
    return stand


def _vorschau(adressen, provider="certum"):
    import asyncio
    return asyncio.run(sb.vorschau(provider, adressen))


def test_nur_wer_es_noetig_hat_wird_bestellt(welt):
    welt["certs"]["a@x.de"] = [{"expiry": "01.01.2030"}]
    v = _vorschau(["a@x.de", "b@x.de", "c@x.de"])
    zustaende = {z["email"]: z["zustand"] for z in v["postfaecher"]}
    assert zustaende == {"a@x.de": sb.HAT_ZERTIFIKAT,
                         "b@x.de": sb.BEREIT,
                         "c@x.de": sb.KEIN_SMIME}
    assert v["bestellbar"] == 1


def test_abgelaufenes_zertifikat_zaehlt_nicht_als_versorgt(welt):
    """⚠️ Sonst überspringt der Lauf genau die Postfächer, die ihn brauchen."""
    welt["certs"]["a@x.de"] = [{"expiry": "01.01.2020"}]
    v = _vorschau(["a@x.de"])
    assert v["postfaecher"][0]["zustand"] == sb.BEREIT


def test_defektes_zertifikat_zaehlt_nicht(welt):
    welt["certs"]["a@x.de"] = [{"expiry": "01.01.2030", "error": "nicht lesbar"}]
    assert _vorschau(["a@x.de"])["bestellbar"] == 1


def test_kosten_stehen_vorher_fest_und_zwar_brutto(welt):
    v = _vorschau(["a@x.de", "b@x.de"])
    assert v["anbieter"]["netto_cents"] == 500
    assert v["anbieter"]["brutto_cents"] == 595          # 5,00 € + 19 %
    assert v["kosten_cents"] == 1190                      # zwei Postfächer
    assert v["fehlbetrag_cents"] == 0                     # 100 € Guthaben reichen


def test_fehlbetrag_wird_beziffert(welt):
    welt["rechte"]["balance_cents"] = 500
    v = _vorschau(["a@x.de", "b@x.de"])
    assert v["fehlbetrag_cents"] == 690
    assert any("fehlen" in h for h in v["hindernisse"])


def test_kostenloser_anbieter_kostet_nichts(welt):
    """Preis 0 ist kostenlos, nicht „nicht gesetzt" — derselbe Fehler hat am
    18.08.2026 echtes Geld gekostet."""
    welt["provider"]["price_cents"] = 0
    v = _vorschau(["a@x.de", "b@x.de"])
    assert v["kosten_cents"] == 0
    assert v["hindernisse"] == []


def test_kontingent_kappt_sichtbar(welt):
    """⚠️ Ein Limit, das mitten im Lauf greift, hinterlässt einen halben Stand."""
    welt["rechte"].update({"monthly_limit": 5, "used_this_month": 4})
    v = _vorschau(["a@x.de", "b@x.de"])
    assert v["bestellbar"] == 1
    assert v["kontingent_frei"] == 1
    assert any("Monatskontingent" in h for h in v["hindernisse"])


def test_fehlende_voraussetzung_wird_einmal_genannt(welt):
    welt["rechte"].update({"ok": False, "reason": "Bedingungen nicht akzeptiert."})
    v = _vorschau(["a@x.de", "b@x.de"])
    assert v["hindernisse"].count("Bedingungen nicht akzeptiert.") == 1


def test_unbekanntes_postfach_wird_benannt_statt_ignoriert(welt):
    v = _vorschau(["fremd@x.de"])
    assert v["postfaecher"][0]["zustand"] == "unbekannt"
    assert v["bestellbar"] == 0


def test_rechnungskunde_hat_keinen_fehlbetrag(welt):
    """Er zahlt über die Rechnung — Guthaben ist dort kein Kriterium."""
    welt["rechte"].update({"billing_mode": "invoice", "balance_cents": 0})
    v = _vorschau(["a@x.de", "b@x.de"])
    assert v["fehlbetrag_cents"] == 0
    assert not any("Guthaben" in h for h in v["hindernisse"])


def test_unbekannter_anbieter_nennt_keine_erfundenen_kosten(welt, monkeypatch):
    """⚠️ Zuerst falsch gebaut und live aufgefallen: Ein nicht gefundener
    Anbieter fiel still auf den Vorgabepreis zurück — die Vorschau zeigte für
    einen kostenlosen Anbieter 11,90 € je Postfach. Eine Kostenangabe, die im
    Zweifel rät, ist schlimmer als keine: Sie wird geglaubt."""
    import hub_catalog
    monkeypatch.setattr(hub_catalog, "get", lambda pid: None)
    v = _vorschau(["a@x.de"])
    assert v["ok"] is False
    assert v["kosten_cents"] == 0
    assert any("Katalog" in h for h in v["hindernisse"])


def test_katalog_wird_vor_der_preisfrage_aufgefrischt(welt, monkeypatch):
    """Ein frisch gestarteter Prozess hat den Katalog noch nicht."""
    gerufen = []
    import hub_catalog

    async def _refresh():
        gerufen.append(True)
    monkeypatch.setattr(hub_catalog, "refresh", _refresh)
    _vorschau(["a@x.de"])
    assert gerufen, "Katalog wurde nicht aufgefrischt"
