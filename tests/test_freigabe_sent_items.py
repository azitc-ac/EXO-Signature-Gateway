"""Freigabe aus dem Wartungsmodus räumt „Gesendete Elemente" auf.

ANLASS (07.08.2026)
Ausgehende Rechnungen standen doppelt und dreifach in den gesendeten Elementen.
Die Kopfzeilen der drei Fassungen wiesen drei Erzeuger aus: das Original des
Absenders (`X-Mailer: lxo`), eine Fassung per Graph `sendMail` und eine per
SMTP aus dem Container. Der Prüfprotokoll-Eintrag erklärte den Rest — die Mail
ging an zwei Empfänger, Exchange teilte sie in zwei Vorgänge, beide wurden im
Wartungsmodus zurückgehalten und einzeln freigegeben.

Der normale Weg plant nach jeder verarbeiteten Mail eine Aufräumung ein, die
genau ein gesendetes Element übrig lässt. Die Freigabe rief nur
`reinject.send()` und liess diesen Schritt aus.

Die Gegenrichtung ist hier genauso wichtig: Bei einer VERSCHLÜSSELTEN Mail
darf nicht aufgeräumt werden. Der Aufräumer behält die jüngste Fassung — das
wäre die Chiffre, und der Absender könnte seine eigene gesendete Mail nicht
mehr lesen.
"""
import pytest

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


def _mail(mid: str = "<abc@example.org>", verschluesselt: bool = False) -> bytes:
    if verschluesselt:
        # Verschlüsselt IN EINER HÜLLE, mit lesbarem Hinweistext davor.
        #
        # Die reine `application/pkcs7-mime`-Form taugt als Prüfung NICHT: Dort
        # findet `extract_html()` ohnehin nichts, die Aufräumung unterbliebe
        # also auch ohne den Schutz — der Test wäre aus dem falschen Grund
        # grün. Genau so ist es beim ersten Anlauf passiert: Der Gegencheck mit
        # entferntem Schutz blieb grün. Diese Form ist der Fall, den nur der
        # Schutz abfängt.
        return (
            f"From: erika@example.org\r\nTo: extern@example.com\r\n"
            f"Subject: Geheim\r\nMessage-ID: {mid}\r\n"
            'Content-Type: multipart/mixed; boundary="G"\r\n\r\n'
            "--G\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            "<html><body><p>Diese Nachricht ist verschlüsselt.</p></body></html>\r\n"
            "--G\r\n"
            'Content-Type: application/pkcs7-mime; smime-type=enveloped-data; '
            'name="smime.p7m"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
            "MIIBxyz==\r\n--G--\r\n"
        ).encode()
    return (
        f"From: erika@example.org\r\nTo: extern@example.com\r\n"
        f"Subject: Rechnung\r\nMessage-ID: {mid}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n\r\n"
        f"<html><body><p>Guten Tag</p></body></html>\r\n"
    ).encode()


@pytest.fixture
def freigabe(monkeypatch):
    """Klient + Aufzeichnung der geplanten Aufräumungen."""
    from starlette.testclient import TestClient
    import handler
    import held_mails
    import reinject
    import settings_store
    from webui import app as wa

    geplant = []

    async def merken(sender, mid, html, subject="", to_recipients=None, replace_all=False):
        geplant.append({"sender": sender, "mid": mid, "html": html,
                        "replace_all": replace_all})

    monkeypatch.setattr(handler, "_cleanup_sent_item", merken)
    # Das Fenster von _is_first_for_mid ist modulweit — sonst faerbte ein Test
    # den naechsten ein.
    handler._processed_mids.clear()
    monkeypatch.setattr(reinject, "send", lambda *a, **k: None)
    monkeypatch.setattr(held_mails, "delete", lambda mail_id: True)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: True if k == "SENT_ITEMS_UPDATE" else None)

    def stelle_bereit(roh: bytes, empfaenger=None):
        monkeypatch.setattr(held_mails, "get_raw",
                            lambda mail_id: ("erika@example.org",
                                             empfaenger or ["extern@example.com"], roh))

    wa.app.dependency_overrides[wa._require_admin] = lambda: "testadmin"
    with TestClient(wa.app) as c:
        yield c, geplant, stelle_bereit
    wa.app.dependency_overrides.clear()


def test_freigabe_plant_die_aufraeumung(freigabe):
    c, geplant, bereit = freigabe
    bereit(_mail())
    r = c.post("/api/maintenance/mails/m1/release")
    assert r.status_code == 200, r.text
    assert len(geplant) == 1, "keine Aufräumung geplant — Duplikate bleiben stehen"
    assert geplant[0]["mid"] == "<abc@example.org>"
    assert geplant[0]["replace_all"] is False
    assert "Guten Tag" in geplant[0]["html"]


def test_zweiter_vorgang_derselben_mail_raeumt_nicht_erneut(freigabe):
    """Exchange teilt eine Mail an mehrere Empfänger in getrennte Vorgänge.
    Beide werden freigegeben — aufgeräumt wird trotzdem nur einmal."""
    c, geplant, bereit = freigabe
    bereit(_mail())
    c.post("/api/maintenance/mails/m1/release")
    c.post("/api/maintenance/mails/m2/release")
    assert len(geplant) == 1, f"{len(geplant)} Aufräumungen für dieselbe Nachricht"


def test_verschluesselte_mail_wird_nicht_angetastet(freigabe):
    """Sonst bliebe die Chiffre stehen und der Klartext verschwände."""
    c, geplant, bereit = freigabe
    bereit(_mail(mid="<geheim@example.org>", verschluesselt=True))
    r = c.post("/api/maintenance/mails/m1/release")
    assert r.status_code == 200, r.text
    assert geplant == [], "verschlüsselte Mail wurde zum Aufräumen eingeplant"


def test_ohne_die_einstellung_passiert_nichts(freigabe, monkeypatch):
    """`SENT_ITEMS_UPDATE` aus heisst: Finger weg vom Postfach."""
    import settings_store
    c, geplant, bereit = freigabe
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: None)
    bereit(_mail(mid="<aus@example.org>"))
    r = c.post("/api/maintenance/mails/m1/release")
    assert r.status_code == 200, r.text
    assert geplant == []
