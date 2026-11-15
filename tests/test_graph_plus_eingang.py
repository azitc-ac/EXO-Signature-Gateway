"""Graph++: Eingang per Graph statt IMAP APPEND (GRAPH_PLUS_EINGANG).

Gemessen 15.11.2026 (DEV-Tenant): Die Graph-Anlage mit PR_MESSAGE_FLAGS beim
Anlegen trägt HTML mit Inline-Bild, große Anhänge, Nur-Text und — mit den
Herkunftsfeldern aus v1.12.32 — das Threading. Eine noch signierte oder
verschlüsselte Hülle kann sie NICHT abbilden; solche Post bleibt bei IMAP.
Die Vorgabe „imap" lässt alles beim Alten.
"""
import pytest

import graph_reinject
import notification
import reinject
import settings_store
import smtp_submit
import stats

TEXT = (b"From: Erika Extern <erika@extern.example>\r\nTo: max@firma.at\r\n"
        b"Subject: Angebot\r\nMessage-ID: <a1@extern.example>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nHallo\r\n")
VERSCHLUESSELT = (b"From: erika@extern.example\r\nTo: max@firma.at\r\nSubject: x\r\n"
                  b"Content-Type: application/pkcs7-mime; smime-type=enveloped-data\r\n"
                  b"Content-Transfer-Encoding: base64\r\n\r\nMIAGCSqGSIb3DQEHA6CA\r\n")
SIGNIERT = (b"From: erika@extern.example\r\nTo: max@firma.at\r\nSubject: x\r\n"
            b'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
            b'boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nHallo\r\n--b--\r\n')


@pytest.fixture
def umgebung(monkeypatch):
    werte = {"REINJECT_MODE": "graph_plus", "GRAPH_PLUS_EINGANG": "graph"}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"max@firma.at"})
    rufe = {"graph": [], "imap": [], "sendmail": [], "fail": []}
    ergebnis = {"graph": True, "imap": True}

    def _graph(mail_from, rcpts, inhalt):
        rufe["graph"].append((mail_from, list(rcpts)))
        return ergebnis["graph"]

    def _imap(rcpt, inhalt, mail_from=""):
        rufe["imap"].append((mail_from, rcpt))
        return ergebnis["imap"]
    monkeypatch.setattr(graph_reinject, "deliver_to_mailbox", _graph)
    monkeypatch.setattr(smtp_submit, "deliver_inbound_imap", _imap)
    monkeypatch.setattr(graph_reinject, "send_via_graph",
                        lambda *a, **kw: rufe["sendmail"].append(a) or False)
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime",
                        lambda *a, **kw: rufe["sendmail"].append(a) or False)
    monkeypatch.setattr(reinject, "_fail_delivery",
                        lambda mail_from, rcpts, *a, **kw: rufe["fail"].append(list(rcpts)))
    zaehler = []
    monkeypatch.setattr(stats, "increment", zaehler.append)
    return {"werte": werte, "rufe": rufe, "ergebnis": ergebnis, "zaehler": zaehler}


def test_graph_legt_per_graph_ab_und_nicht_per_imap(umgebung):
    assert reinject._eingang_ablegen("max@firma.at", TEXT, "erika@extern.example")
    assert umgebung["rufe"]["graph"] == [("erika@extern.example", ["max@firma.at"])], \
        "Absender muss mit — sonst fehlt die Abwesenheitsantwort (oof_ablage)"
    assert umgebung["rufe"]["imap"] == []
    assert "eingang_graph" in umgebung["zaehler"]


def test_vorgabe_imap_bleibt_beim_alten(umgebung):
    umgebung["werte"]["GRAPH_PLUS_EINGANG"] = None
    assert reinject._eingang_ablegen("max@firma.at", TEXT, "erika@extern.example")
    assert umgebung["rufe"]["graph"] == []
    assert umgebung["rufe"]["imap"] == [("erika@extern.example", "max@firma.at")]
    assert "eingang_imap" in umgebung["zaehler"]


@pytest.mark.parametrize("wert", ["Graph ", "GRAPH", "graph"])
def test_schreibweise_graph_wird_erkannt(umgebung, wert):
    umgebung["werte"]["GRAPH_PLUS_EINGANG"] = wert
    assert settings_store.graph_plus_eingang() == "graph"


@pytest.mark.parametrize("wert", ["imap", "", None, "grap", "json", 1])
def test_unbekannter_wert_gilt_als_imap(umgebung, wert):
    umgebung["werte"]["GRAPH_PLUS_EINGANG"] = wert if wert != 1 else "1"
    assert settings_store.graph_plus_eingang() == "imap"


@pytest.mark.parametrize("inhalt", [VERSCHLUESSELT, SIGNIERT], ids=["verschluesselt", "signiert"])
def test_smime_huelle_geht_immer_per_imap(umgebung, inhalt):
    """Ohne Schlüssel reicht das Gateway verschlüsselt durch — per JSON würde
    daraus eine Anlage smime.p7m, die Outlook nicht mehr entschlüsselt."""
    assert reinject._eingang_ablegen("max@firma.at", inhalt, "erika@extern.example")
    assert umgebung["rufe"]["graph"] == []
    assert umgebung["rufe"]["imap"] == [("erika@extern.example", "max@firma.at")]


def test_graph_fehler_faellt_auf_imap_zurueck(umgebung):
    umgebung["ergebnis"]["graph"] = False
    assert reinject._eingang_ablegen("max@firma.at", TEXT, "erika@extern.example")
    assert umgebung["rufe"]["graph"] and umgebung["rufe"]["imap"]
    assert "eingang_imap" in umgebung["zaehler"] and "eingang_graph" not in umgebung["zaehler"]


def test_empfaenger_ausserhalb_des_tenants_wird_nicht_abgelegt(umgebung):
    assert not reinject._eingang_ablegen("fremd@woanders.example", TEXT, "erika@extern.example")
    assert umgebung["rufe"]["graph"] == []


def test_send_graph_plus_extern_nutzt_den_graph_weg(umgebung):
    reinject.send("erika@extern.example", ["max@firma.at"], TEXT)
    assert umgebung["rufe"]["graph"] and not umgebung["rufe"]["imap"]
    assert umgebung["rufe"]["sendmail"] == [] and umgebung["rufe"]["fail"] == []


def test_beide_wege_gescheitert_nichts_geht_still_verloren(umgebung):
    umgebung["ergebnis"].update(graph=False, imap=False)
    reinject.send("erika@extern.example", ["max@firma.at"], TEXT)
    assert umgebung["rufe"]["fail"] == [["max@firma.at"]], \
        "nach Graph-Anlage, IMAP und sendMail muss die Mail in die Warteschlange"


# ── Tagesbericht ──────────────────────────────────────────────────────────────

def _dval(werte):
    return lambda k: werte.get(k, 0)


def test_tagesbericht_zeigt_eingang_auch_mit_null(umgebung):
    zeile = notification._eingang_rows(_dval({}))
    assert "Eingang abgelegt (Graph / IMAP)" in zeile and "0 / 0" in zeile


def test_tagesbericht_warnt_wenn_alles_auf_imap_faellt(umgebung):
    zeile = notification._eingang_rows(_dval({"eingang_imap": 5}))
    assert "0 / 5" in zeile and "#e67e22" in zeile


def test_tagesbericht_schweigt_im_imap_weg(umgebung):
    umgebung["werte"]["GRAPH_PLUS_EINGANG"] = "imap"
    assert notification._eingang_rows(_dval({"eingang_imap": 5})) == ""


def test_tagesbericht_nennt_unbestaetigte_signaturen(umgebung):
    umgebung["werte"]["REINJECT_MODE"] = "smtp"
    assert "Signatur nicht bestätigt" in notification._eingang_rows(_dval({"smime_unbestaetigt": 2}))
    assert notification._eingang_rows(_dval({})) == ""


# ── Betreff-Vermerk ───────────────────────────────────────────────────────────

def test_vermerk_unbestaetigt_behauptet_keine_pruefung(umgebung):
    import handler
    tag = handler._build_subject_tag(enc=True, signer_name="Erika", unbestaetigt=True)
    assert "Signatur nicht bestätigt" in tag and "signiert von" not in tag


def test_vermerk_unbestaetigt_auch_wenn_signiert_vermerk_aus(umgebung):
    import handler
    umgebung["werte"]["SMIME_TAG_SIGNED_ENABLED"] = False
    assert "Signatur nicht bestätigt" in handler._build_subject_tag(False, None, unbestaetigt=True)


def test_vermerk_unbestaetigt_wird_wieder_entfernt(umgebung):
    import handler
    assert handler._strip_subject_tags("[Signatur nicht bestätigt] AW: Angebot") == "AW: Angebot"
