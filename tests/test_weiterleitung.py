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


def test_kauf_uebergibt_die_zahlungsweise(monkeypatch, client):
    """Der erste der drei Fehler: die Oberfläche schickte sie, der
    Gateway-Endpunkt warf sie weg, der Hub nahm seine Vorgabe."""
    import hub_client
    gesehen = {}

    async def _purchase(tenant_id, mailboxes, zahlungsweise=""):
        gesehen["tenant_id"] = tenant_id
        gesehen["mailboxes"] = mailboxes
        gesehen["zahlungsweise"] = zahlungsweise
        return {"ok": False, "error": "Attrappe — hier endet der Test"}

    monkeypatch.setattr(hub_client, "purchase_license", _purchase)
    # Das Zustimmungs-Gate (Lizenzbedingungen-Ergänzung) ist hier nicht der
    # Prüfgegenstand und in der Testumgebung leer — sonst antwortet der
    # Endpunkt mit 403, bevor er hub_client überhaupt erreicht.
    import legal_consent
    monkeypatch.setattr(legal_consent, "context_consented", lambda ctx: True)
    import settings_store
    settings_store.update({"TENANT_ID": "11111111-2222-3333-4444-555555555555"})

    client.post("/api/license/purchase",
                json={"mailboxes": 110, "zahlungsweise": "jaehrlich"})
    assert gesehen.get("zahlungsweise") == "jaehrlich", (
        "Die Zahlungsweise erreicht hub_client nicht — der Kunde bekäme eine "
        "andere Laufzeit als die angezeigte.")
