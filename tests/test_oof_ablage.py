"""Abwesenheitsantwort für Post, die per Ablage (IMAP APPEND / Graph-Inject) ins
Postfach kommt — dort schickt Exchange keine.

ANLASS (2026-10-11, Outlook-Testlauf): Seit v1.9.139 nimmt Post aus dem Tenant
im Modus Graph++ den Exchange-Zustellweg, Post von außen aber weiter IMAP
APPEND. Eingehende S/MIME-Post von außen bekam damit nie eine
Abwesenheitsantwort, obwohl der Empfänger sie eingeschaltet hatte.
"""
import ast
import email
import email.policy
import json
from pathlib import Path

import pytest

import exo_mailboxes
import graph_client
import graph_reinject
import oof_ablage
import settings_store

ROOT = Path(__file__).resolve().parent.parent

ORIG = (b"From: Kunde <kunde@extern.de>\r\nTo: b@t.de\r\n"
        b"Subject: =?utf-8?q?Angebot_f=C3=BCr_Sie?=\r\nMessage-ID: <orig-1@extern.de>\r\n"
        b"Content-Type: text/plain\r\n\r\nhallo\r\n")


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data or {}

    def json(self):
        return self._d


@pytest.fixture
def graph(monkeypatch, tmp_path):
    """Graph-Attrappe: mailboxSettings + Kontakte; sendMail wird mitgeschrieben."""
    zustand = {
        "setting": {"status": "alwaysEnabled", "externalAudience": "all",
                    "internalReplyMessage": "<p>intern weg</p>",
                    "externalReplyMessage": "<p>extern weg</p>"},
        "locale": "de-DE", "kontakte": [], "kontakte_status": 200,
        "gesendet": [],
    }

    class C:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            if url.endswith("/mailboxSettings"):
                return _Resp(data={"automaticRepliesSetting": zustand["setting"],
                                   "language": {"locale": zustand["locale"]}})
            if url.endswith("/contacts"):
                return _Resp(zustand["kontakte_status"], {"value": zustand["kontakte"]})
            raise AssertionError(url)

    def senden(frm, rcpts, roh):
        zustand["gesendet"].append((frm, list(rcpts), roh))
        return True

    monkeypatch.setattr(oof_ablage.httpx, "Client", C)
    monkeypatch.setattr(oof_ablage, "_STATE", tmp_path / "oof.json")
    monkeypatch.setattr(graph_client, "_acquire_token", lambda: "T")
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime", senden)
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"a@t.de", "b@t.de"})
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)
    return zustand


def _lauf(absender="kunde@extern.de", roh=ORIG):
    return oof_ablage._antworten("b@t.de", absender, roh)


def test_externe_post_bekommt_externen_text(graph):
    assert _lauf() == "gesendet"
    (frm, an, roh), = graph["gesendet"]
    assert frm == "b@t.de" and an == ["kunde@extern.de"]
    m = email.message_from_bytes(roh, policy=email.policy.default)
    assert m["Auto-Submitted"] == "auto-replied"
    # Graph sendMail verwirft Auto-Submitted (gemessen 11.10.2026); nur X-Köpfe
    # kommen an. Ohne X-Sig-Applied lief die Antwort durchs Gateway und wurde
    # ein zweites Mal signiert.
    import loop_detector
    assert m["X-Sig-Applied"] == loop_detector.marker_wert()
    assert m["X-Auto-Response-Suppress"] == "All"
    assert m["In-Reply-To"] == "<orig-1@extern.de>" == m["References"]
    assert m["Subject"] == "Automatische Antwort: Angebot für Sie"
    assert "extern weg" in m.get_body(("html",)).get_content()
    assert b"\r\n" in roh and b"\n" not in roh.replace(b"\r\n", b"")


def test_interner_absender_bekommt_internen_text(graph):
    assert _lauf("a@t.de") == "gesendet"
    roh = graph["gesendet"][0][2]
    html = email.message_from_bytes(roh, policy=email.policy.default).get_body(("html",)).get_content()
    assert "intern weg" in html and "extern weg" not in html


def test_englisches_postfach_bekommt_englischen_betreff(graph):
    graph["locale"] = "en-US"
    _lauf()
    m = email.message_from_bytes(graph["gesendet"][0][2], policy=email.policy.default)
    assert m["Subject"].startswith("Automatic reply: ")


def test_je_absender_nur_einmal_bis_sich_die_abwesenheit_aendert(graph):
    assert _lauf() == "gesendet"
    assert _lauf() == "nicht: schon beantwortet"
    assert _lauf("zweiter@extern.de") == "gesendet"
    graph["setting"] = dict(graph["setting"], externalReplyMessage="<p>neuer Text</p>")
    assert _lauf() == "gesendet"
    assert len(graph["gesendet"]) == 3


@pytest.mark.parametrize("setting,erwartet", [
    ({"status": "disabled"}, "nicht: nicht abwesend"),
    ({"status": "scheduled",
      "scheduledStartDateTime": {"dateTime": "2000-01-01T00:00:00", "timeZone": "UTC"},
      "scheduledEndDateTime": {"dateTime": "2000-01-02T00:00:00", "timeZone": "UTC"}},
     "nicht: nicht abwesend"),
    ({"status": "alwaysEnabled", "externalAudience": "none",
      "externalReplyMessage": "<p>x</p>"}, "nicht: extern ausgeschaltet"),
    ({"status": "alwaysEnabled", "externalAudience": "all",
      "externalReplyMessage": "  "}, "nicht: leerer Text"),
])
def test_keine_antwort(graph, setting, erwartet):
    graph["setting"] = setting
    assert _lauf() == erwartet
    assert graph["gesendet"] == []


def test_geplante_abwesenheit_im_fenster_antwortet(graph):
    graph["setting"] = {"status": "scheduled", "externalAudience": "all",
                        "externalReplyMessage": "<p>x</p>",
                        "scheduledStartDateTime": {"dateTime": "2000-01-01T00:00:00", "timeZone": "UTC"},
                        "scheduledEndDateTime": {"dateTime": "2999-01-01T00:00:00", "timeZone": "UTC"}}
    assert _lauf() == "gesendet"


def test_nur_kontakte(graph):
    graph["setting"] = dict(graph["setting"], externalAudience="contactsOnly")
    assert _lauf() == "nicht: kein Kontakt"
    graph["kontakte_status"] = 403                       # keine Berechtigung → im Zweifel nein
    assert _lauf() == "nicht: kein Kontakt"
    graph["kontakte_status"], graph["kontakte"] = 200, [{"id": "c1"}]
    assert _lauf() == "gesendet"


@pytest.mark.parametrize("kopf", [
    b"Auto-Submitted: auto-replied", b"Auto-Submitted: auto-generated",
    b"Precedence: bulk", b"Precedence: list", b"List-Id: <liste.example>",
    b"X-Auto-Response-Suppress: OOF", b"X-Auto-Response-Suppress: DR, All",
])
def test_automatisch_erzeugte_post_wird_nicht_beantwortet(graph, kopf):
    assert _lauf(roh=kopf + b"\r\n" + ORIG).startswith("nicht: ")
    assert graph["gesendet"] == []


def test_auto_submitted_no_wird_beantwortet(graph):
    assert _lauf(roh=b"Auto-Submitted: no\r\n" + ORIG) == "gesendet"


@pytest.mark.parametrize("absender", ["", "mailer-daemon@x.de", "postmaster@x.de",
                                      "owner-liste@x.de", "liste-request@x.de",
                                      "noreply@x.de", "b@t.de"])
def test_nach_ablage_ignoriert_null_system_und_eigene_adresse(monkeypatch, absender):
    monkeypatch.setattr(oof_ablage, "_antworten",
                        lambda *a: pytest.fail("darf nicht antworten"))
    oof_ablage.nach_ablage(absender, "b@t.de", ORIG, warten=True)


def test_nach_ablage_wirft_nie(monkeypatch):
    def kaputt(*a):
        raise RuntimeError("Graph weg")
    monkeypatch.setattr(oof_ablage, "_antworten", kaputt)
    oof_ablage.nach_ablage("kunde@extern.de", "b@t.de", ORIG, warten=True)


def test_dedup_datei_hat_600(graph):
    _lauf()
    assert (oof_ablage._STATE.stat().st_mode & 0o777) == 0o600
    assert json.loads(oof_ablage._STATE.read_text())["b@t.de"]["an"] == ["kunde@extern.de"]


# ── Struktur: JEDE Ablage-Stelle ruft nach_ablage ──────────────────────────────

ABLAGE = [("app/smtp_submit.py", "deliver_inbound_imap"),
          ("app/graph_reinject.py", "deliver_to_mailbox_mime"),
          ("app/graph_reinject.py", "deliver_to_mailbox"),
          # sendMail ALS der Empfänger: Umschlag-Absender ist der Empfänger
          # selbst, Exchanges Antwort erreicht den echten Absender nicht.
          ("app/graph_reinject.py", "_deliver_via_recipient_sendmail")]


def _funktion(datei, name):
    baum = ast.parse((ROOT / datei).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef) and n.name == name)


@pytest.mark.parametrize("datei,name", ABLAGE)
def test_jede_ablage_ruft_nach_ablage(datei, name):
    rufe = [n for n in ast.walk(_funktion(datei, name))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "nach_ablage"]
    assert rufe, f"{name} legt ab, ohne die Abwesenheitsantwort anzustoßen"


def test_keine_weitere_ablage_ohne_hook():
    """Wer einen neuen Weg baut, der in den Posteingang SCHREIBT (inbox/messages
    POST oder IMAP append), muss ihn in ABLAGE eintragen — sonst fehlt dort die
    Abwesenheitsantwort wieder."""
    bekannte = {name for _, name in ABLAGE}
    for datei in sorted((ROOT / "app").rglob("*.py")):
        baum = ast.parse(datei.read_text(encoding="utf-8"))
        for f in ast.walk(baum):
            if not isinstance(f, ast.FunctionDef):
                continue
            quelle = ast.get_source_segment(datei.read_text(encoding="utf-8"), f) or ""
            schreibt = (".append(\"INBOX\"" in quelle
                        or ("inbox/messages" in quelle and ".post(" in quelle))
            if schreibt:
                assert f.name in bekannte, f"{datei.name}:{f.name} legt in den Posteingang ab"


def test_reinject_reicht_den_absender_an_imap_weiter():
    src = (ROOT / "app/reinject.py").read_text(encoding="utf-8")
    aufrufe = [z for z in src.splitlines() if "deliver_inbound_imap(" in z]
    assert aufrufe and all("mail_from" in z for z in aufrufe), aufrufe


def test_imap_ablage_stoesst_antwort_mit_absender_an(monkeypatch):
    import smtp_submit

    class Imap:
        def __init__(self, *a, **kw):
            pass

        def authenticate(self, *a):
            pass

        def append(self, *a):
            return "OK", [b""]

        def logout(self):
            pass

    gesehen = []
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"b@t.de"})
    monkeypatch.setattr(smtp_submit, "_acquire_imap_tokens", lambda: ["T"])
    monkeypatch.setattr(smtp_submit.imaplib, "IMAP4_SSL", Imap)
    monkeypatch.setattr(oof_ablage, "nach_ablage", lambda *a, **kw: gesehen.append(a))
    assert smtp_submit.deliver_inbound_imap("b@t.de", ORIG, "kunde@extern.de")
    assert gesehen == [("kunde@extern.de", "b@t.de", ORIG)]
