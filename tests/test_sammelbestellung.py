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
        # ⚠️ Katalog-Kennung OHNE hub:-Präfix — so führt der Hub seine Anbieter.
        # Der Bezugsweg heisst "hub:certum", der Anbieter "certum". Solange der
        # Mock beides gleich behandelte, prüfte kein Test die Übersetzung.
        # `available` gehört dazu: die Vorschau prüft seit v1.7.221, ob der
        # Bezugsweg überhaupt einsatzbereit ist. Ein Katalogeintrag ohne dieses
        # Feld gilt als nicht verfügbar — real wie im Test.
        "provider": {"id": "certum", "label": "Certum", "price_cents": 500,
                     "available": True},
    }

    import settings_store, smime_store, hub_catalog, hub_client
    monkeypatch.setattr(settings_store, "get",
                        lambda k: stand["mailboxes"] if k == "MAILBOX_CONFIG" else None)
    monkeypatch.setattr(smime_store, "list_user_certs",
                        lambda e: stand["certs"].get(e, []))
    monkeypatch.setattr(hub_catalog, "get",
                        lambda pid: stand["provider"] if pid == stand["provider"]["id"] else None)

    async def _rechte():
        return stand["rechte"]
    monkeypatch.setattr(hub_client, "cert_eligibility", _rechte)
    monkeypatch.setattr(hub_client, "cert_is_registered", lambda: True)
    return stand


def _vorschau(adressen, provider="hub:certum"):
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
    # Der Wortlaut kann sich ändern (seit die Bereitschaft des Bezugswegs zuerst
    # geprüft wird, meldet der Hub-Stub „nicht verfügbar"). Die Sache bleibt:
    # kein Start, kein erfundener Preis, und ein Grund steht da.
    assert v["hindernisse"], "kein Grund genannt"


def test_katalog_wird_vor_der_preisfrage_aufgefrischt(welt, monkeypatch):
    """Ein frisch gestarteter Prozess hat den Katalog noch nicht."""
    gerufen = []
    import hub_catalog

    async def _refresh():
        gerufen.append(True)
    monkeypatch.setattr(hub_catalog, "refresh", _refresh)
    _vorschau(["a@x.de"])
    assert gerufen, "Katalog wurde nicht aufgefrischt"


# ── Der Lauf ─────────────────────────────────────────────────────────────────
"""
Ein Sammellauf über hundert Postfächer dauert Minuten. Drei Dinge entscheiden,
ob er brauchbar ist:

* ⚠️ Ein Fehler bei Postfach 37 darf die übrigen 63 nicht mitnehmen.
* ⚠️ Guthabenmangel betrifft ALLE folgenden gleichermassen — er gehört nicht als
  63 Einzelfehler protokolliert, sondern hält den Lauf an.
* Fortsetzen darf nichts wiederholen, sonst bestellt und bezahlt man doppelt.
"""


@pytest.fixture
def lauf(welt, monkeypatch):
    """Bestellungen ersetzen — je Adresse ein vorgegebenes Ergebnis.

    ⚠️ Baut auf `welt` auf, seit `lauf_starten()` die Deckung vorab prüft: Ohne
    die Hub-Antworten scheitert schon der Start, und die Tests prüften dann
    etwas ganz anderes als das Verhalten des Laufs.
    """
    import ca_backends, settings_store
    # c@x.de steht in `welt` ohne S/MIME — für die Lauf-Tests wird es gebraucht.
    welt["mailboxes"]["g3"]["smime"] = True
    plan = {}

    class _Backend:
        def get_name(self):
            return "hub:certum"

        def is_ready(self):
            return True

        def not_ready_reason(self):
            return ""

        async def initiate_renewal(self, email, cfg, extra=None):
            was = plan.get(email, "ok")
            if was == "ok":
                return True
            raise RuntimeError(was)

    monkeypatch.setattr(ca_backends, "get_backend", lambda pid: _Backend())
    # MAILBOX_CONFIG muss aus `welt` kommen — CA_USER_CONFIG ist hier leer.
    monkeypatch.setattr(settings_store, "get",
                        lambda k: welt["mailboxes"] if k == "MAILBOX_CONFIG" else {})
    sb._lauf = None
    yield plan
    sb._lauf = None


async def _durchlaufen(adressen, plan_start=True):
    import asyncio
    if plan_start:
        await sb.lauf_starten("hub:certum", adressen)
    for _ in range(40):
        await asyncio.sleep(0)
        z = sb.lauf_zustand()
        if z and z["status"] != sb.LAEUFT:
            return z
    return sb.lauf_zustand()


def test_ein_fehler_reisst_den_lauf_nicht_ab(lauf):
    import asyncio
    lauf["b@x.de"] = "CA lehnt ab"
    z = asyncio.run(_durchlaufen(["a@x.de", "b@x.de", "c@x.de"]))
    assert z["status"] == sb.FERTIG
    assert [e["email"] for e in z["erledigt"]] == ["a@x.de", "b@x.de", "c@x.de"]
    assert [e["ok"] for e in z["erledigt"]] == [True, False, True]


def test_guthabenmangel_haelt_an_statt_63_mal_zu_scheitern(lauf):
    import asyncio
    lauf["b@x.de"] = "Guthaben zu niedrig — es fehlen 9,90 €"
    z = asyncio.run(_durchlaufen(["a@x.de", "b@x.de", "c@x.de"]))
    assert z["status"] == sb.PAUSIERT
    assert len(z["erledigt"]) == 1, "hat trotz Guthabenmangel weitergemacht"
    assert z["offen"][0] == "b@x.de", "gescheiterte Adresse ist aus der Liste gefallen"


def test_fortsetzen_wiederholt_nichts(lauf):
    import asyncio

    async def ablauf():
        lauf["b@x.de"] = "Guthaben zu niedrig"
        await _durchlaufen(["a@x.de", "b@x.de", "c@x.de"])
        lauf.pop("b@x.de")                       # „aufgeladen"
        await sb.lauf_fortsetzen()
        return await _durchlaufen([], plan_start=False)

    z = asyncio.run(ablauf())
    assert z["status"] == sb.FERTIG
    emails = [e["email"] for e in z["erledigt"]]
    assert emails == ["a@x.de", "b@x.de", "c@x.de"]
    assert len(emails) == len(set(emails)), "eine Adresse wurde doppelt bestellt"


def test_zweiter_lauf_wird_abgelehnt(lauf):
    """⚠️ Zwei Läufe verplanen dasselbe Guthaben und bestellen dieselben
    Postfächer doppelt — beides fällt erst auf, wenn das Geld weg ist."""
    import asyncio

    async def ablauf():
        await sb.lauf_starten("hub:certum", ["a@x.de"])
        sb._lauf["status"] = sb.LAEUFT            # so tun, als liefe er noch
        return await sb.lauf_starten("hub:certum", ["b@x.de"])

    assert asyncio.run(ablauf())["ok"] is False


def test_abbruch_wirkt_nach_der_laufenden_bestellung(lauf):
    """Hart abbrechen ginge nicht: Eine Bestellung, die bei der
    Zertifizierungsstelle liegt, muss verbucht werden — sonst entsteht ein
    unbezahlter Vorgang."""
    import asyncio

    async def ablauf():
        await sb.lauf_starten("hub:certum", ["a@x.de", "b@x.de", "c@x.de"])
        sb.lauf_abbrechen()
        return await _durchlaufen([], plan_start=False)

    z = asyncio.run(ablauf())
    assert z["status"] == sb.ABGEBROCHEN
    assert len(z["erledigt"]) < 3


def test_unerwarteter_fehler_reisst_den_lauf_nicht_ab(lauf, monkeypatch):
    """⚠️ Diese Lücke fiel erst durch eine Mutation auf: Der Test oben provoziert
    Fehler INNERHALB der Bestellung, und die fängt `_eine_bestellung` selbst ab.
    Die äussere Absicherung — für alles, was davor schiefgeht (Konfiguration
    nicht lesbar, Bezugsweg wirft beim Erzeugen) — war damit ungeprüft, und ein
    entfernter `except` blieb unbemerkt.

    Bei hundert Postfächern ist genau das der teure Fall: Ein Ausrutscher bei
    Nummer 37 nimmt die restlichen 63 mit, und niemand weiss, wie weit der Lauf
    gekommen ist.
    """
    import asyncio

    echte = sb._eine_bestellung
    async def _stolpert(adresse, provider_id, ca_terms_accepted_at=""):
        if adresse == "b@x.de":
            raise RuntimeError("Konfiguration nicht lesbar")
        return await echte(adresse, provider_id)
    monkeypatch.setattr(sb, "_eine_bestellung", _stolpert)

    z = asyncio.run(_durchlaufen(["a@x.de", "b@x.de", "c@x.de"]))

    assert z["status"] == sb.FERTIG, "Lauf wurde durch einen einzelnen Fehler beendet"
    assert [e["email"] for e in z["erledigt"]] == ["a@x.de", "b@x.de", "c@x.de"]
    assert [e["ok"] for e in z["erledigt"]] == [True, False, True]
    assert "Konfiguration" in z["erledigt"][1]["grund"]


# ── Deckung VOR dem Start ────────────────────────────────────────────────────
"""
Vom Nutzer angestossen: „Wenn die schief geht, ist eine Menge Geld weg."

Ein Lauf, der erst bei der ersten Bestellung merkt, dass das Guthaben nicht
reicht, hat dann schon bestellt — bei hundert Postfächern womöglich fünf, die
niemand geplant hat. Und bei aktiver Automatik wird abgebucht, ohne dass vorher
jemand eine Zahl gesehen hat.
"""


@pytest.fixture
def startprobe(welt, monkeypatch):
    """Bestellungen ersetzen, damit nur das Startverhalten geprüft wird."""
    import ca_backends, settings_store

    class _Backend:
        def get_name(self):
            return "hub:certum"

        def is_ready(self):
            return True

        def not_ready_reason(self):
            return ""

        async def initiate_renewal(self, email, cfg, extra=None):
            return True
    monkeypatch.setattr(ca_backends, "get_backend", lambda pid: _Backend())
    sb._lauf = None
    yield welt
    sb._lauf = None


def _starten(adressen):
    import asyncio
    return asyncio.run(sb.lauf_starten("hub:certum", adressen))


def test_ohne_deckung_und_ohne_automatik_startet_nichts(startprobe):
    """⚠️ Sonst bestellt der Lauf, bis das Guthaben leer ist, und hinterlässt
    einen halben Stand."""
    startprobe["rechte"].update({"balance_cents": 100, "auto_topup_aktiv": False})
    r = _starten(["a@x.de", "b@x.de"])
    assert r["ok"] is False
    assert "aufladen" in r["error"].lower()
    assert r.get("fehlbetrag_cents", 0) > 0, "Fehlbetrag nicht beziffert"
    assert sb.lauf_zustand() is None, "Lauf wurde trotzdem angelegt"


def test_mit_automatik_startet_er_und_nennt_den_betrag(startprobe):
    """Die Automatik schliesst die Lücke — der Betrag ist eine Ankündigung, die
    bestätigt werden will, kein Hindernis."""
    import asyncio
    startprobe["rechte"].update({"balance_cents": 100, "auto_topup_aktiv": True,
                                 "auto_topup_schritt_cents": 2500,
                                 "min_topup_cents": 2500, "max_topup_cents": 100000})
    v = asyncio.run(sb.vorschau("hub:certum", ["a@x.de", "b@x.de"]))
    assert v["startbereit"] is True
    assert v["nachladung_cents"] == 2500, "Nachladebetrag falsch oder fehlt"
    assert any("2500" in h.replace(",", "").replace(".", "") or "25,00" in h
               for h in v["hindernisse"]), "Betrag wird nicht angekündigt"
    assert _starten(["a@x.de", "b@x.de"])["ok"] is True


def test_nachladebetrag_entspricht_der_rechnung_des_hubs(startprobe):
    """Ein angekündigter Betrag, der dann abweicht, ist schlimmer als keiner:
    aufgerundet auf Vielfache des Schritts, mindestens der Mindestbetrag."""
    r = {"auto_topup_schritt_cents": 500, "min_topup_cents": 2500,
         "max_topup_cents": 100000}
    assert sb._nachladebetrag(100, r) == 2500        # Mindestbetrag schlägt Schritt
    r["min_topup_cents"] = 100
    assert sb._nachladebetrag(100, r) == 500         # ein Schritt
    assert sb._nachladebetrag(1200, r) == 1500       # drei Schritte, aufgerundet
    r["max_topup_cents"] = 1000
    assert sb._nachladebetrag(1200, r) == 1000       # Deckel


def test_fehlende_voraussetzung_verhindert_den_start(startprobe):
    startprobe["rechte"].update({"ok": False, "reason": "Bedingungen nicht akzeptiert."})
    r = _starten(["a@x.de"])
    assert r["ok"] is False
    assert "Bedingungen" in r["error"]


def test_ohne_bestellbare_postfaecher_startet_nichts(startprobe):
    startprobe["certs"]["a@x.de"] = [{"expiry": "01.01.2030"}]
    r = _starten(["a@x.de"])
    assert r["ok"] is False


def test_ohne_bestellbare_nennt_die_vorschau_den_grund(startprobe):
    """⚠️ „Nicht startbereit" ohne Grund ist eine Sackgasse — der Betreiber sieht
    eine Ablehnung und weiss nicht, was zu tun wäre."""
    import asyncio
    startprobe["certs"]["a@x.de"] = [{"expiry": "01.01.2030"}]
    v = asyncio.run(sb.vorschau("hub:certum", ["a@x.de"]))
    assert v["startbereit"] is False
    assert v["hindernisse"], "kein Grund genannt"
    assert "gültiges Zertifikat" in v["hindernisse"][0]

    leer = asyncio.run(sb.vorschau("hub:certum", []))
    assert leer["hindernisse"] == ["Keine Postfächer ausgewählt."]


def test_grund_verdraengt_nicht_den_guthabenhinweis(startprobe):
    """Beide Gründe können zugleich gelten; der Guthabenhinweis darf nicht
    verschwinden, nur weil zusätzlich nichts bestellbar ist."""
    import asyncio
    startprobe["rechte"].update({"balance_cents": 0, "auto_topup_aktiv": False})
    v = asyncio.run(sb.vorschau("hub:certum", ["a@x.de", "b@x.de"]))
    assert any("fehlen" in h for h in v["hindernisse"]), v["hindernisse"]


def test_falscher_bezugsweg_bestellt_nicht_ueber_einen_anderen(startprobe, monkeypatch):
    """⚠️ ca_backends.get_backend() faellt bei unbekanntem Namen still auf
    `assisted_manual` zurueck. Ohne Gegenprobe haette ein Sammellauf lautlos
    ueber den falschen Bezugsweg bestellt — bei hundert Postfaechern hundertmal.
    """
    import asyncio, ca_backends

    class _Falsch:
        def get_name(self):
            return "assisted_manual"          # NICHT das, wonach gefragt wurde

        async def initiate_renewal(self, email, cfg, extra=None):
            raise AssertionError("darf gar nicht erst bestellen")

    monkeypatch.setattr(ca_backends, "get_backend", lambda pid: _Falsch())
    r = asyncio.run(sb._eine_bestellung("a@x.de", "hub:certum"))
    assert r["ok"] is False
    assert "unbekannt" in r["grund"]


def test_handbetrieb_ist_nicht_sammelfaehig(startprobe):
    """`assisted_manual` verlangt je Postfach einen Schritt von Hand — ein
    Sammellauf darüber erzeugt nur hundert offene Vorgänge."""
    import asyncio
    v = asyncio.run(sb.vorschau("assisted_manual", ["a@x.de"]))
    assert v["ok"] is False
    assert "von Hand" in v["hindernisse"][0]
    assert v["abrechnung"] is None


# ── Bezugswege ohne Guthaben (CASTLE, DigiCert direkt) ───────────────────────
"""
CASTLE ist kostenlos und erneuert vollautomatisch — es gab nie einen Grund, es
vom Sammellauf auszuschliessen. Das erste Kriterium („kommt aus dem Katalog")
war schlicht falsch gewählt; richtig ist „kann ohne Zutun des Postfachinhabers
bestellen".

⚠️ Die drei Abrechnungsarten sind NICHT austauschbar. Ein Betrag von 0 heisst
bei CASTLE „kostenlos" und bei DigiCert direkt „hier nicht bekannt" — wer das
gleich behandelt, zeigt dem Betreiber eine Null, wo eine Rechnung von der
Zertifizierungsstelle kommt.
"""


@pytest.fixture
def bereit(welt, monkeypatch):
    """Alle statischen Bezugswege als einsatzbereit ausweisen."""
    import ca_backends

    echt = ca_backends.get_backend

    def _get(name):
        b = echt(name)
        monkeypatch.setattr(type(b), "is_ready", lambda self: True, raising=False)
        return b
    monkeypatch.setattr(ca_backends, "get_backend", _get)
    return welt


def test_castle_ist_sammelfaehig_und_kostenlos(bereit):
    import asyncio
    v = asyncio.run(sb.vorschau("castle_acme", ["a@x.de", "b@x.de"]))
    assert v["ok"] is True
    assert v["abrechnung"] == sb.KOSTENLOS
    assert v["bestellbar"] == 2
    assert v["kosten_cents"] == 0
    assert v["kosten_bekannt"] is True
    assert v["startbereit"] is True, "kostenlos, also nichts im Weg"


def test_castle_startet_auch_ohne_guthaben(bereit):
    """⚠️ Der eigentliche Punkt: Ein leeres Guthaben darf einen kostenlosen
    Bezugsweg nicht blockieren."""
    import asyncio
    bereit["rechte"].update({"balance_cents": 0, "ok": False,
                             "reason": "Kein Guthaben."})
    v = asyncio.run(sb.vorschau("castle_acme_staging", ["a@x.de"]))
    assert v["ok"] is True
    assert v["startbereit"] is True
    assert v["hindernisse"] == []
    assert v["fehlbetrag_cents"] == 0


def test_digicert_direkt_behauptet_keinen_preis(bereit):
    """⚠️ „0,00 €" wäre hier keine fehlende Angabe, sondern eine falsche: Die
    Rechnung kommt über den eigenen CertCentral-Vertrag."""
    import asyncio
    v = asyncio.run(sb.vorschau("digicert_direct", ["a@x.de"]))
    assert v["ok"] is True
    assert v["abrechnung"] == sb.FREMDVERTRAG
    assert v["kosten_bekannt"] is False


def test_nicht_eingerichteter_bezugsweg_startet_nicht(welt, monkeypatch):
    """Ohne API-Schlüssel liefe ein DigiCert-Lauf los und scheiterte bei jedem
    einzelnen Postfach."""
    import asyncio, ca_backends
    from ca_backends.digicert_direct import DigiCertDirectBackend
    monkeypatch.setattr(DigiCertDirectBackend, "is_ready", lambda self: False)
    v = asyncio.run(sb.vorschau("digicert_direct", ["a@x.de"]))
    assert v["ok"] is False
    assert "API-Key" in v["hindernisse"][0]


# ── Zustimmung zu den Bedingungen der Zertifizierungsstelle ──────────────────
"""
⚠️ Am 20.08.2026 im Livelauf aufgefallen: Ein Sammellauf über vier Postfächer
startete, schickte vier Bestellungen los und bekam viermal dieselbe Absage —
„Zustimmung zu den Bedingungen der Zertifizierungsstelle erforderlich". Der
Beleg wurde nirgends mitgegeben; die Einzelbestellung holt ihn über einen
Dialog, der Sammellauf kannte ihn nicht.

Zwei Lehren, beide hier geprüft:
  * Was für ALLE Bestellungen gleichermassen gilt, gehört vor den Lauf. Vier
    Einzelfehler für eine gemeinsame Ursache sind vier Gelegenheiten, die
    Ursache zu übersehen.
  * Der Beleg darf nicht im Server entstehen. Ein Zeitstempel, den sich die
    Anwendung selbst ausstellt, belegt keine Zustimmung.
"""


@pytest.fixture
def mit_bedingungen(welt):
    welt["provider"]["terms_url"] = "https://example.org/bedingungen.pdf"
    return welt


def test_ohne_zustimmung_startet_kein_lauf(mit_bedingungen, startprobe):
    import asyncio
    r = asyncio.run(sb.lauf_starten("hub:certum", ["a@x.de", "b@x.de"]))
    assert r["ok"] is False
    assert "zugestimmt" in r["error"], r["error"]
    assert sb.lauf_zustand() is None, "Lauf wurde trotzdem angelegt"


def test_mit_zustimmung_laeuft_er(mit_bedingungen, startprobe):
    import asyncio
    r = asyncio.run(sb.lauf_starten("hub:certum", ["a@x.de", "b@x.de"],
                                    ca_terms_accepted_at="2026-08-20T10:00:00Z"))
    assert r["ok"] is True


def test_der_beleg_erreicht_die_bestellung(startprobe, monkeypatch):
    """⚠️ Der eigentliche Punkt: Er muss bis zur Zertifizierungsstelle kommen.
    Ein Beleg, der auf halbem Weg verlorengeht, sieht bis zum Livelauf wie eine
    funktionierende Zustimmung aus."""
    import asyncio, ca_backends
    gesehen = {}

    class _Backend:
        def get_name(self):
            return "hub:certum"

        def is_ready(self):
            return True

        def not_ready_reason(self):
            return ""

        async def initiate_renewal(self, email, cfg, extra=None):
            gesehen[email] = (extra or {}).get("ca_terms_accepted_at")
            return True

    monkeypatch.setattr(ca_backends, "get_backend", lambda pid: _Backend())
    asyncio.run(sb._eine_bestellung("a@x.de", "hub:certum", "2026-08-20T10:00:00Z"))
    assert gesehen["a@x.de"] == "2026-08-20T10:00:00Z"


def test_anbieter_ohne_bedingungen_brauchen_keinen_beleg(bereit):
    """CASTLE und die Direktanbindungen führen keine Bedingungen — dort wäre
    eine Zustimmungsabfrage eine Hürde ohne Gegenstand."""
    import asyncio
    v = asyncio.run(sb.vorschau("castle_acme", ["a@x.de"]))
    assert v["ok"] is True
    assert sb._zustimmung_fehlt("castle_acme", "") == ""


def test_vorschau_kennt_postfaecher_die_gerade_eingeschaltet_werden(welt):
    """⚠️ Die Rückfrage vor dem Speichern prüfte gegen die GESPEICHERTE
    Konfiguration — in der die Postfächer noch nicht stehen, weil sie gerade
    erst eingeschaltet werden. Ergebnis: null bestellbare, keine Rückfrage, und
    der Lauf startete danach trotzdem. Genau die stille Bestellung, die die
    Rückfrage verhindern sollte."""
    import asyncio
    v = asyncio.run(sb.vorschau("hub:certum", ["ganz.neu@x.de"], nur_zertifikate=True))
    assert v["bestellbar"] == 1, v
    ohne = asyncio.run(sb.vorschau("hub:certum", ["ganz.neu@x.de"]))
    assert ohne["bestellbar"] == 0, "ohne das Kennzeichen bleibt es beim alten Verhalten"


# ── Was übergeben wird, ist ein Wunsch — kein Auftrag ────────────────────────
"""
⚠️ Am 20.08.2026 im Livelauf aufgefallen und von KEINEM Test bemerkt: Die
Vorschau wies „3 bestellbar, 1 übersprungen" aus, der Lauf legte vier
Bestellungen an. Er arbeitete die Liste ab, die ihm gereicht wurde.

Warum die vorhandenen Tests blind waren: Sie geben nur bestellbare Postfächer
in die Auswahl. Damit war „Auswahl == bestellbare Menge" nie eine Annahme, die
schiefgehen konnte. Der teure Fall — jemand markiert alle Postfächer, ein paar
davon sind längst versorgt — kam darin nicht vor.
"""


def test_versorgte_postfaecher_werden_nicht_mitbestellt(lauf, monkeypatch):
    """Die Auswahl enthält ein Postfach, das bereits versorgt ist."""
    import asyncio, smime_store
    versorgt = {"b@x.de": [{"expiry": "01.01.2030"}]}
    monkeypatch.setattr(smime_store, "list_user_certs", lambda e: versorgt.get(e, []))

    z = asyncio.run(_durchlaufen(["a@x.de", "b@x.de", "c@x.de"]))
    bestellt = [e["email"] for e in z["erledigt"] if e.get("ok")]
    assert "b@x.de" not in bestellt, "für ein versorgtes Postfach wurde bestellt"
    assert z["gesamt"] == 2, f"Lauf umfasst {z['gesamt']} statt 2 Postfächer"


def test_zwischenzeitlich_versorgtes_postfach_wird_uebersprungen(lauf, monkeypatch):
    """⚠️ Ein Lauf über hundert Postfächer dauert Minuten. Trifft in dieser Zeit
    ein Zertifikat ein — aus einer Einzelbestellung, einer automatischen
    Erneuerung, einem Hochladen von Hand —, ist die Prüfung vom Start veraltet.
    Bezahlt würde trotzdem."""
    import asyncio
    import smime_store

    stand = {"a@x.de": [], "b@x.de": [], "c@x.de": []}
    monkeypatch.setattr(smime_store, "list_user_certs", lambda e: stand.get(e, []))

    echte = sb._eine_bestellung

    async def _bestellen(adresse, provider_id, beleg=""):
        # Nach der ersten Bestellung trifft für b@x.de ein Zertifikat ein
        stand["b@x.de"] = [{"expiry": "01.01.2030"}]
        return await echte(adresse, provider_id, beleg)

    monkeypatch.setattr(sb, "_eine_bestellung", _bestellen)
    z = asyncio.run(_durchlaufen(["a@x.de", "b@x.de", "c@x.de"]))
    b = next(e for e in z["erledigt"] if e["email"] == "b@x.de")
    assert b["ok"] is False and "inzwischen" in b["grund"], b


def test_sammellauf_kann_die_bestaetigung_uebernehmen(lauf, monkeypatch):
    """⚠️ Ohne sie bleibt am Ende jedes Sammellaufs für jedes Postfach eine
    Mail liegen, die ein Mensch anklicken muss — bei hundert Postfächern genau
    der Engpass, den der Lauf abschaffen soll."""
    import asyncio, settings_store
    geschrieben = {}
    # ⚠️ monkeypatch, nicht direkt zuweisen: Eine ohne ihn ersetzte Funktion
    # bleibt nach dem Test stehen. Genau das hat hier 13 fremde Tests
    # umgeworfen, die danach in ein Wörterbuch statt in die Einstellungen
    # schrieben.
    monkeypatch.setattr(settings_store, "update", lambda d: geschrieben.update(d))

    asyncio.run(sb.lauf_starten("hub:certum", ["a@x.de", "b@x.de"], auto_confirm=True))
    cfg = geschrieben.get("CA_USER_CONFIG", {})
    assert cfg.get("a@x.de", {}).get("auto_confirm") is True, cfg
    assert cfg.get("a@x.de", {}).get("backend") == "hub:certum", (
        "ohne Bezugsweg fiele der Eintrag bei der nächsten Erneuerung auf "
        "assisted_manual zurück — dort gibt es nichts zu bestätigen")


def test_ohne_haken_wird_nichts_gesetzt(lauf, monkeypatch):
    """Die Bestätigung ist eine Befugnis: Das Gateway liest dafür Mail im
    Postfach des Nutzers. Sie darf nicht stillschweigend entstehen."""
    import asyncio, settings_store
    geschrieben = {}
    monkeypatch.setattr(settings_store, "update", lambda d: geschrieben.update(d))
    asyncio.run(sb.lauf_starten("hub:certum", ["a@x.de"]))
    assert "CA_USER_CONFIG" not in geschrieben
