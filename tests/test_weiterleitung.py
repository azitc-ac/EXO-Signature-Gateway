"""Weiterleitungen an den Hub reichen durch — sie zählen nicht auf.

WARUM DAS EINEN TEST BRAUCHT
----------------------------
Ein Wert wandert durch vier Schichten: Oberfläche → Gateway-Endpunkt →
`hub_client` → Hub-Endpunkt. Zählte jede Schicht die Felder einzeln auf, muss
man bei jedem neuen Feld an vier Stellen denken.

Am 27.07.2026 ist genau das dreimal an einem Tag schiefgegangen — mit derselben
Zahlungsweise: erst kam sie beim Kauf nicht am Hub an (der Kunde sah den
Jahrespreis und bekam einen Monat berechnet), dann fehlte sie in der
Verlängerungsansicht, dann ihr Umschaltwunsch. Jedes Mal war die Ursache eine
Schicht, die Felder von Hand abschrieb.

Deshalb ist „durchreichen" hier eine zugesicherte Eigenschaft und kein Stil:
Diese Tests schlagen fehl, sobald eine Schicht wieder aufzählt.
"""
import pytest

from test_seiten import client   # noqa: F401  (Fixture wiederverwenden)


@pytest.fixture
def hub_antwort(monkeypatch):
    """hub_client durch eine Attrappe ersetzen, die ein unbekanntes Feld
    mitliefert. Kommt es unten an, wird durchgereicht."""
    import hub_client

    antwort = {
        "ok": True, "license": "EXOSIG1.geheim", "mailboxes": 110,
        "expires": "2027-01-31", "auto_renew": True, "cancelled_at": "",
        "renew_error": "", "zahlungsweise": "monatlich",
        "renew_zahlungsweise": "jaehrlich", "refund_preview_cents": 1190,
        "ein_spaeter_ergaenztes_feld": "muss ankommen",
    }

    async def _get_license():
        return antwort

    monkeypatch.setattr(hub_client, "get_license", _get_license)
    monkeypatch.setattr(hub_client, "is_registered", lambda: True)
    return antwort


def test_verlaengerungsansicht_reicht_unbekannte_felder_durch(client, hub_antwort):
    r = client.get("/api/license/renewal")
    assert r.status_code == 200
    d = r.json()
    assert d["ein_spaeter_ergaenztes_feld"] == "muss ankommen", (
        "Der Endpunkt zählt Felder auf, statt durchzureichen — genau so gingen "
        "zahlungsweise und renew_zahlungsweise verloren.")


@pytest.mark.parametrize("feld", [
    "auto_renew", "expires", "cancelled_at", "renew_error",
    "zahlungsweise", "renew_zahlungsweise", "refund_preview_cents",
])
def test_von_der_oberflaeche_gelesene_felder_kommen_an(client, hub_antwort, feld):
    """Die Liste entspricht dem, was licRenewLoad() in settings_connect.html
    tatsächlich ausliest."""
    d = client.get("/api/license/renewal").json()
    assert feld in d, f"{feld} fehlt in der Antwort"


def test_lizenzschluessel_geht_NICHT_an_die_oberflaeche(client, hub_antwort):
    """Durchreichen heißt nicht alles durchreichen: der Schlüssel wird für die
    Anzeige nicht gebraucht und hat im Browserfenster nichts zu suchen."""
    d = client.get("/api/license/renewal").json()
    assert "license" not in d


def test_abo_aktion_reicht_jedes_feld_durch(monkeypatch, client):
    """Der erste der drei Fehler: die Oberfläche schickte die Zahlungsweise, der
    Gateway-Endpunkt warf sie weg, der Hub nahm seine Vorgabe.

    Seit dem Umbau auf Abos gibt es nur noch EINEN Durchreicher für alle
    Aktionen. Der Test prüft deshalb nicht mehr ein bestimmtes Feld, sondern
    die Eigenschaft: was die Oberfläche schickt, kommt an — auch etwas, das
    es beim Schreiben dieses Tests noch nicht gab.
    """
    import hub_client
    gesehen = {}

    async def _weiter(pfad, nutzlast):
        gesehen["pfad"] = pfad
        gesehen.update(nutzlast)
        return {"ok": False, "error": "Attrappe — hier endet der Test"}

    monkeypatch.setattr(hub_client, "_license_json", _weiter)
    # Das Zustimmungs-Gate ist hier nicht der Prüfgegenstand — es hat einen
    # eigenen Test weiter unten. Ohne diese Zeile antwortet der Endpunkt mit
    # 403, bevor er hub_client überhaupt erreicht.
    import legal_consent
    monkeypatch.setattr(legal_consent, "context_consented", lambda ctx: True)
    import settings_store
    settings_store.update({"TENANT_ID": "11111111-2222-3333-4444-555555555555"})

    client.post("/api/license/hub/checkout",
                json={"mailboxes": 110, "zahlungsweise": "jaehrlich",
                      "ein_spaeter_ergaenztes_feld": "muss ankommen"})
    assert gesehen.get("pfad") == "checkout"
    assert gesehen.get("zahlungsweise") == "jaehrlich", (
        "Die Zahlungsweise erreicht hub_client nicht — der Kunde bekäme eine "
        "andere Laufzeit als die angezeigte.")
    assert gesehen.get("mailboxes") == 110
    assert gesehen.get("ein_spaeter_ergaenztes_feld") == "muss ankommen", (
        "Der Durchreicher zählt Felder auf, statt sie weiterzugeben.")


def test_tenant_id_kommt_vom_gateway_nicht_vom_browser(monkeypatch, client):
    """Der Kopierschutz-Anker darf nicht aus dem Browser stammen.

    Käme er von dort, könnte man eine Lizenz für einen fremden Mandanten
    bestellen. Der Endpunkt setzt ihn deshalb selbst — und überschreibt einen
    mitgeschickten Wert.
    """
    import hub_client
    gesehen = {}

    async def _weiter(pfad, nutzlast):
        gesehen.update(nutzlast)
        return {"ok": False, "error": "Attrappe"}

    monkeypatch.setattr(hub_client, "_license_json", _weiter)
    # Das Zustimmungs-Gate ist hier nicht der Prüfgegenstand — es hat einen
    # eigenen Test weiter unten. Ohne diese Zeile antwortet der Endpunkt mit
    # 403, bevor er hub_client überhaupt erreicht.
    import legal_consent
    monkeypatch.setattr(legal_consent, "context_consented", lambda ctx: True)
    import settings_store
    settings_store.update({"TENANT_ID": "11111111-2222-3333-4444-555555555555"})

    client.post("/api/license/hub/checkout",
                json={"mailboxes": 110, "tenant_id": "99999999-9999-9999-9999-999999999999"})
    assert gesehen.get("tenant_id") == "11111111-2222-3333-4444-555555555555"


def test_unbekannte_abo_aktion_wird_abgelehnt(client):
    """Ohne Weissliste wäre der Durchreicher ein offener Weiterleiter."""
    assert client.post("/api/license/hub/beliebig", json={}).status_code == 404


# ── Zustimmungs-Gate ─────────────────────────────────────────────────────────
# Am 28.07.2026 lief ein Kauf durch, obwohl den geänderten Bedingungen nicht
# zugestimmt war. Beim Zusammenlegen der fünf Weiterleitungs-Endpunkte auf einen
# blieb die Prüfung des alten Kaufendpunkts zurück.
#
# ⚠️ Diese Tests rufen den ENDPUNKT auf, nicht `context_consented()`. Genau
# dieser Unterschied war der Grund, warum der Fehler unbemerkt blieb: die
# Hilfsfunktion lieferte isoliert das richtige Ergebnis — sie wurde nur
# nirgends mehr gefragt.

@pytest.fixture
def ohne_zustimmung(monkeypatch):
    import legal_consent
    monkeypatch.setattr(legal_consent, "context_consented", lambda ctx: False)


@pytest.fixture
def hub_attrappe(monkeypatch):
    """Zählt, ob der Hub überhaupt erreicht wurde."""
    import hub_client
    erreicht = []

    async def _weiter(pfad, nutzlast):
        erreicht.append(pfad)
        return {"ok": True}

    monkeypatch.setattr(hub_client, "_license_json", _weiter)
    return erreicht


@pytest.mark.parametrize("aktion", ["checkout", "quantity", "zahlungsweise"])
def test_ohne_zustimmung_kein_kauf(client, ohne_zustimmung, hub_attrappe, aktion):
    r = client.post(f"/api/license/hub/{aktion}", json={"mailboxes": 110})
    assert r.status_code == 403, f"{aktion} lief ohne Zustimmung durch"
    assert not hub_attrappe, "der Hub wurde trotz fehlender Zustimmung angefragt"


@pytest.mark.parametrize("aktion", ["cancel", "portal"])
def test_beenden_bleibt_ohne_zustimmung_moeglich(client, ohne_zustimmung,
                                                 hub_attrappe, aktion):
    """Wer den neuen Bedingungen NICHT zustimmt, muss beenden können.

    Eine Kündigung an die Zustimmung zu den Bedingungen zu binden, die man
    gerade ablehnt, wäre eine Falle — der Kunde käme nicht mehr heraus.
    """
    r = client.post(f"/api/license/hub/{aktion}", json={})
    assert r.status_code == 200, f"{aktion} wurde blockiert"
    assert hub_attrappe == [aktion]


def test_geltende_fassungen_gehen_an_den_hub(client, hub_attrappe, monkeypatch):
    """Der Hub hat die Dokumente nicht und kann die Aktualität eines Belegs
    sonst nicht beurteilen — er liess einen Beleg über eine ÜBERHOLTE Fassung
    durchgehen."""
    import legal_consent, hub_client
    monkeypatch.setattr(legal_consent, "context_consented", lambda ctx: True)
    gesehen = {}

    async def _weiter(pfad, nutzlast):
        gesehen.update(nutzlast)
        return {"ok": True}

    monkeypatch.setattr(hub_client, "_license_json", _weiter)
    client.post("/api/license/hub/checkout", json={"mailboxes": 110})
    versionen = gesehen.get("doc_versions") or {}
    assert versionen.get("license-supplement") == \
        legal_consent.CURRENT_DOCUMENTS["license-supplement"]["version"]
