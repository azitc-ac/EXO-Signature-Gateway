"""Vorschau (Richtlinienergebnis) = Mailweg — EINE Entscheidung (v1.12.103).

ANLASS: Die Vorschauen zeigten nicht immer, was das Gateway anhängt. Der
Vorlagen-Editor renderte die bearbeitete Vorlage als Signatur, ob das Postfach
sie bekam oder nicht, und hängte Banner und Disclaimer auch an Antworten, an
denen der Mailweg sie nach KETTE_BEHAELT weglässt. Die Self-Service-Seite
rechnete die wirksame Vorlage im Browser nach (`wirksam()`) und meldete „Keine
Antwort-Signatur“, wenn ein Slot weder wählbar noch fest war.

Seitdem entscheidet `policies.entscheiden` für den Mailweg (handler.py) UND
`vorschau.zusammenstellen` (Editor, Self-Service). Diese Datei prüft:

* Gleichheit am ECHTEN `handle_DATA` gegen die Vorschau, über viele
  Konfigurationen — und gegen festgeschriebene Erwartungen, damit nicht beide
  gemeinsam falsch sein können.
* Zwei Mailweg-Befunde, die dabei auftauchten (Test zuerst, gegen v1.12.102
  rot): Ein Postfach nur mit S/MIME bekam die volle Signatur samt Banner und
  Disclaimer; eine Kampagne prüfte bei „im Auftrag von“ die Art des Banners der
  schreibenden PERSON statt des geteilten Postfachs.
* Editor: wirksam / nicht wirksam, mit Grund; Kampagne als Herkunft.
* Self-Service: Antwortfall nach KETTE_BEHAELT, nicht wählbare Slots aus der
  Richtlinie.
"""
import asyncio
import email
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import handler  # noqa: E402

ABS = "max@firma.de"
TEAM = "team@firma.de"
INTERN = "erika@firma.de"
EXTERN = "kunde@extern.de"


class _Sitzung:
    peer = ("40.92.10.10", 5000)
    ssl = None


class _Umschlag:
    def __init__(self, von, an, content):
        self.mail_from = von
        self.rcpt_tos = an
        self.content = content


def _grund_werte():
    return {
        "REINJECT_MODE": "smtp", "SMTP_RELAY_ENABLED": False, "RELAY_TENANT_CHECK": False,
        "SMIME_SIGNING_ENABLED": False, "SKIP_SIG_IN_THREAD": True,
        "NOSIG_TRIGGER": "#nosig", "NODIGSIG_TRIGGER": "#nodigsig", "ENC_TRIGGER": "#enc",
        "SIGN_INTERNAL_ONLY_MAIL": False, "INTERN_OHNE_ADDIN": False, "INTERN_VOR_ANTWORT": False,
        "SECURE_PORTAL_ENABLED": False,
        "TEMPLATE_POLICIES": {"sig": "StdSig", "min": "StdMin", "banner": "StdBan",
                              "disclaimer": "StdDis"},
        "MAILBOX_CONFIG": {ABS: {"sig": True}},
        "CUSTOM_POLICIES": [], "INTERNAL_GROUPS": {}, "BANNER_CAMPAIGNS": [],
    }


@pytest.fixture
def anlage(monkeypatch):
    werte = _grund_werte()
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: True)
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)
    import intern
    monkeypatch.setattr(intern, "_postfaecher", lambda: {ABS, TEAM, INTERN})
    monkeypatch.setattr(intern, "_domaenen", lambda: {"firma.de"})
    zugestellt = []
    import reinject
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: zugestellt.append(a) or True)
    import mail_audit
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: None)
    import graph_client

    async def _user(e):
        return graph_client.UserData(mail=e)
    monkeypatch.setattr(graph_client, "get_user", _user)

    async def _kein_token():
        return None
    monkeypatch.setattr(graph_client, "_acquire_token_async", _kein_token)
    import signature_engine
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, **kw: (f"<p>[{template_name}]</p>",
                                                             f"[{template_name}]"))
    import sig_thread
    kette = {"an": False}
    monkeypatch.setattr(sig_thread, "kennt", lambda s, b: kette["an"])
    monkeypatch.setattr(sig_thread, "merken", lambda s, m: None)
    werte["_zugestellt"] = zugestellt
    werte["_kette"] = kette
    return werte


def _mail(an, betreff="Hallo", von_kopf=ABS, sender_kopf=""):
    kopf = f"From: {von_kopf}\r\n" + (f"Sender: {sender_kopf}\r\n" if sender_kopf else "")
    return (kopf + f"To: {', '.join(an)}\r\nSubject: {betreff}\r\n"
            "Message-ID: <m1@firma.de>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n"
            "<html><body><p>Text</p></body></html>\r\n").encode()


def _namen(html: str) -> list[str]:
    return re.findall(r"\[([^\]\s]+)\]", html)


def _versand(anlage, fall, betreff="Hallo", absender=ABS, von_kopf=ABS, sender_kopf="") -> list[str]:
    """Namen der Vorlagen in der zugestellten Mail, in ihrer Reihenfolge."""
    an = [INTERN] if fall == "intern" else [EXTERN]
    anlage["_kette"]["an"] = fall == "antwort"
    anlage["_zugestellt"].clear()
    asyncio.run(handler.SignatureHandler().handle_DATA(
        None, _Sitzung(), _Umschlag(absender, an, _mail(an, betreff, von_kopf, sender_kopf))))
    assert anlage["_zugestellt"], "nichts zugestellt"
    roh = next(a for a in anlage["_zugestellt"][-1] if isinstance(a, (bytes, bytearray)))
    msg = email.message_from_bytes(roh)
    teil = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    return _namen(teil.get_payload(decode=True).decode())


def _vorschau(fall, nosig=False, **kw):
    import vorschau
    erg = asyncio.run(vorschau.zusammenstellen(ABS, fall, per_nosig=nosig, **kw))
    return erg


# ── Gleichheit Mailweg ⇔ Vorschau ────────────────────────────────────────────

def _gruppe(w, fest=True, **vorlagen):
    w["CUSTOM_POLICIES"] = [{"condition_type": "group", "group_name": "Vertrieb",
                             "vorlagen": vorlagen}]
    w["INTERNAL_GROUPS"] = {"Vertrieb": [ABS]}
    w["RICHTLINIE_GRUPPE_FEST"] = fest


def _k_standard(w):
    pass


def _k_gruppe_fest(w):
    _gruppe(w, sig="GrpSig", banner="GrpBan")
    w["MAILBOX_CONFIG"][ABS].update(template="Eigen", banner_template="EigenBan")


def _k_gruppe_offen(w):
    _gruppe(w, fest=False, sig="GrpSig", banner="GrpBan")
    w["MAILBOX_CONFIG"][ABS].update(template="Eigen")


def _k_ausserhalb_mit_wahl(w):
    _gruppe(w, sig="GrpSig")
    w["INTERNAL_GROUPS"] = {"Vertrieb": ["andere@firma.de"]}
    w["MAILBOX_CONFIG"][ABS].update(template="Eigen", banner_template="EigenBan")


def _k_kampagne(w):
    w["BANNER_CAMPAIGNS"] = [{"id": "kmp_aaaaaa", "name": "Sommer", "banner": "KmpBan",
                              "enabled": True, "fuer": ["richtlinie", "eigen"]}]


def _k_ohne_min(w):
    w["MAILBOX_CONFIG"][ABS]["min_template"] = ""


def _k_kette_beide(w):
    w["KETTE_BEHAELT"] = "beide"


def _k_ohne_min_kette_disc(w):
    w["MAILBOX_CONFIG"][ABS]["min_template"] = ""
    w["KETTE_BEHAELT"] = "disclaimer"


def _k_nosig_banner(w):
    w["NOSIG_BEHAELT"] = "banner"


def _k_intern(w):
    w["MAILBOX_CONFIG"][ABS]["intern_template"] = "InternSig"
    w["INTERN_OHNE_ADDIN"] = True


def _k_intern_vor_antwort(w):
    _k_intern(w)
    w["INTERN_VOR_ANTWORT"] = True


def _k_nur_smime(w):
    w["MAILBOX_CONFIG"][ABS] = {"sig": False, "smime": True}


FAELLE = [
    # (Name, Konfiguration, Fall, #nosig, erwartete Vorlagen in der Mail)
    ("standard-neu", _k_standard, "neu", False, ["StdSig", "StdBan", "StdDis"]),
    ("standard-antwort", _k_standard, "antwort", False, ["StdMin"]),
    ("gruppe-fest", _k_gruppe_fest, "neu", False, ["GrpSig", "GrpBan", "StdDis"]),
    ("gruppe-offen", _k_gruppe_offen, "neu", False, ["Eigen", "GrpBan", "StdDis"]),
    ("ausserhalb", _k_ausserhalb_mit_wahl, "neu", False, ["Eigen", "EigenBan", "StdDis"]),
    ("kampagne", _k_kampagne, "neu", False, ["StdSig", "KmpBan", "StdDis"]),
    ("kampagne-antwort-kette-beide", lambda w: (_k_kampagne(w), _k_kette_beide(w)),
     "antwort", False, ["StdMin", "KmpBan", "StdDis"]),
    ("ohne-min", _k_ohne_min, "antwort", False, []),
    ("kette-beide", _k_kette_beide, "antwort", False, ["StdMin", "StdBan", "StdDis"]),
    ("ohne-min-kette-disc", _k_ohne_min_kette_disc, "antwort", False, ["StdDis"]),
    ("nosig", _k_nosig_banner, "neu", True, ["StdBan"]),
    ("nosig-vorgabe", _k_standard, "neu", True, []),
    ("intern", _k_intern, "intern", False, ["InternSig", "StdBan", "StdDis"]),
    ("intern-unveraendert", _k_standard, "intern", False, []),
    ("nur-smime", _k_nur_smime, "neu", False, []),
    ("nur-smime-antwort", _k_nur_smime, "antwort", False, []),
]


@pytest.mark.parametrize("name,konf,fall,nosig,erwartet", FAELLE, ids=[f[0] for f in FAELLE])
def test_mailweg_und_vorschau_gleich(anlage, name, konf, fall, nosig, erwartet):
    konf(anlage)
    mail = _versand(anlage, fall, betreff="Hallo #nosig" if nosig else "Hallo")
    erg = _vorschau(fall, nosig=nosig)
    vorschau = [t["vorlage"] for t in erg["teile"]]
    assert mail == erwartet, f"Mailweg: {mail}"
    assert vorschau == mail, f"Vorschau {vorschau} ≠ Mailweg {mail}"
    assert _namen(erg["html"]) == mail


def test_intern_vor_antwort_in_der_kette(anlage):
    """INTERN_VOR_ANTWORT: rein interne Antwort in der Kette → interne Signatur.
    Die Vorschau kennt den Fall „Antwort an Interne“ nicht; die Entscheidung
    gilt trotzdem für beide gleich."""
    _k_intern_vor_antwort(anlage)
    anlage["_kette"]["an"] = True
    an = [INTERN]
    asyncio.run(handler.SignatureHandler().handle_DATA(
        None, _Sitzung(), _Umschlag(ABS, an, _mail(an, "AW: x"))))
    roh = anlage["_zugestellt"][-1][2]
    html = next(p for p in email.message_from_bytes(roh).walk()
                if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    import policies
    e = policies.entscheiden(ABS, in_kette=True, alle_intern=True)
    assert _namen(html)[0] == e["signatur"]["vorlage"] == "InternSig"


# ── Befund 1: Postfach nur mit S/MIME ────────────────────────────────────────

def test_nur_smime_bekommt_keine_signatur_und_keinen_zusatz(anlage):
    """Gegen v1.12.102 rot: Die Mail trug „[StdSig][StdBan][StdDis]“.
    Absicht laut Postfachliste: Antwort-, interne Signatur, Banner und
    Disclaimer hängen am Haken „Signatur“ (ohne ihn ausgegraut)."""
    _k_nur_smime(anlage)
    assert _versand(anlage, "neu") == []
    erg = _vorschau("neu")
    assert erg["signatur_unterdrueckt"] and erg["grund"] == "postfach_ohne_signatur"
    assert "S/MIME" in erg["grund_text"]


# ── Befund 2: Kampagne bei „im Auftrag von“ ──────────────────────────────────

def test_kampagne_prueft_das_geteilte_postfach_nicht_die_person(anlage):
    """Gegen v1.12.102 rot: TEAM wählt seinen Banner selbst, die Kampagne gilt
    nur für Postfächer mit Richtlinien-Banner — sie ersetzte TEAMs Banner
    trotzdem, weil die Art an MAX (folgt der Richtlinie) bestimmt wurde."""
    anlage["MAILBOX_CONFIG"][TEAM] = {"sig": True, "template": "Team", "banner_template": "TeamBan"}
    anlage["BANNER_CAMPAIGNS"] = [{"id": "kmp_aaaaaa", "name": "Sommer", "banner": "KmpBan",
                                   "enabled": True, "fuer": ["richtlinie"]}]
    namen = _versand(anlage, "neu", absender=ABS, von_kopf=TEAM, sender_kopf=ABS)
    assert namen == ["Team", "TeamBan", "StdDis"], namen
    # Gegenprobe: MAX selbst folgt der Richtlinie und bekommt die Kampagne.
    assert _versand(anlage, "neu") == ["StdSig", "KmpBan", "StdDis"]


def test_kampagne_fuer_eine_gruppe_trifft_das_geteilte_postfach(anlage):
    """Die Zielgruppe einer Kampagne gilt für das Postfach, dessen Banner es
    ist. Gegen v1.12.102 rot: Gesucht wurde die schreibende Person."""
    anlage["MAILBOX_CONFIG"][TEAM] = {"sig": True, "template": "Team"}
    anlage["INTERNAL_GROUPS"] = {"Geteilt": [TEAM]}
    anlage["BANNER_CAMPAIGNS"] = [{"id": "kmp_aaaaaa", "name": "Sommer", "banner": "KmpBan",
                                   "enabled": True, "group": "Geteilt"}]
    assert _versand(anlage, "neu", absender=ABS, von_kopf=TEAM, sender_kopf=ABS) == [
        "Team", "KmpBan", "StdDis"]
    assert _versand(anlage, "neu") == ["StdSig", "StdBan", "StdDis"]


# ── Editor: /api/preview-data ohne `explizit` ────────────────────────────────

@pytest.fixture
def client(anlage, monkeypatch, tmp_path):
    pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")
    from starlette.testclient import TestClient
    import config
    import signature_engine
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    monkeypatch.setattr(signature_engine, "vorlagen_art",
                        lambda n: {"Ban2": "banner", "StdBan": "banner"}.get(n, "signatur"))
    monkeypatch.setattr(signature_engine, "templates_nach_art",
                        lambda: {"signatur": ["StdSig", "StdMin", "Eigen"], "oof": []})
    from webui.app import app, _check_auth
    from webui.deps import _require_self
    app.dependency_overrides[_check_auth] = lambda: "testadmin"
    app.dependency_overrides[_require_self] = lambda: ABS
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _editor(client, template, fall="neu"):
    r = client.get("/api/preview-data", params={"email": ABS, "template": template, "fall": fall})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("name,konf,fall,nosig,erwartet",
                         [f for f in FAELLE if not f[3]], ids=[f[0] for f in FAELLE if not f[3]])
def test_editor_zeigt_das_richtlinienergebnis(anlage, client, name, konf, fall, nosig, erwartet):
    """Mit einer Vorlage, die das Postfach NICHT hat: Das Ergebnis bleibt das
    des Mailwegs. Gegen v1.12.102 rot: dort stand „Fremd“ als Signatur."""
    konf(anlage)
    mail = _versand(anlage, fall)
    d = _editor(client, "Fremd", fall)
    assert [t["vorlage"] for t in d["teile"]] == mail
    assert _namen(d["html"] + d["banner_html"] + d["disclaimer_html"]) == mail
    assert d["override_wirksam"] is False
    assert "Fremd" in d["override"]["html"]


def test_editor_wirksame_vorlage(client):
    d = _editor(client, "StdSig")
    assert d["override_wirksam"] is True
    assert d["override"]["hinweis"].startswith("Wirksam als Signatur")
    assert [(t["slot"], t["quelle"]) for t in d["teile"]] == [
        ("sig", "Standard"), ("banner", "Standard"), ("disclaimer", "Standard")]


def test_editor_nicht_wirksam_nennt_quelle_und_festschreibung(anlage, client):
    _k_gruppe_fest(anlage)
    d = _editor(client, "Eigen")
    assert d["override_wirksam"] is False
    assert d["override"]["hinweis"] == (f"{ABS} bekommt diese Vorlage hier nicht — wirksam ist "
                                        "„GrpSig“ (Gruppe Vertrieb, festgeschrieben).")
    sig = d["teile"][0]
    assert (sig["quelle"], sig["fest"]) == ("Gruppe Vertrieb", True)


def test_editor_antwort_nennt_die_antwort_signatur(client):
    d = _editor(client, "StdSig", "antwort")
    assert d["override_wirksam"] is False
    assert "wirksam ist „StdMin“ (Standard)" in d["override"]["hinweis"]
    assert d["banner_html"] == "" and d["disclaimer_html"] == ""      # KETTE_BEHAELT ""


def test_editor_banner_von_kampagne_ersetzt(anlage, client):
    _k_kampagne(anlage)
    d = _editor(client, "StdBan")
    assert d["override_wirksam"] is False
    assert "Kampagne „Sommer“ ersetzt den Banner: wirksam ist „KmpBan“" in d["override"]["hinweis"]
    ban = next(t for t in d["teile"] if t["slot"] == "banner")
    assert ban["quelle"] == "Kampagne Sommer"


def test_editor_banner_entfaellt_in_der_antwort(client):
    d = _editor(client, "StdBan", "antwort")
    assert d["override_wirksam"] is False
    assert "Antworten in derselben Kette" in d["override"]["hinweis"]


def test_editor_hat_intern_nur_mit_interner_signatur(anlage, client):
    assert _editor(client, "StdSig")["hat_intern"] is False
    _k_intern(anlage)
    assert _editor(client, "StdSig")["hat_intern"] is True


# ── Self-Service: /api/self/preview ──────────────────────────────────────────

def _self(client, **params):
    r = client.get("/api/self/preview", params=params)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def selbst(anlage):
    anlage["MAILBOX_CONFIG"][ABS]["self_templates"] = True
    anlage["SELF_TEMPLATE_KATEGORIEN"] = ["sig", "min"]
    return anlage


def test_self_antwort_nach_kette_behaelt(selbst, client):
    d = _self(client)
    assert _namen(d["antwort"]["html"]) == ["StdMin"]
    assert _namen(d["neu"]["html"]) == ["StdSig", "StdBan", "StdDis"]
    selbst["KETTE_BEHAELT"] = "beide"
    assert _namen(_self(client)["antwort"]["html"]) == ["StdMin", "StdBan", "StdDis"]


def test_self_nicht_waehlbarer_slot_kommt_aus_der_richtlinie(selbst, client):
    """Gegen v1.12.102: `wirksam('min','')` ergab "" (weder wählbar noch fest)
    → „Keine Antwort-Signatur“, obwohl die Richtlinie StdMin lieferte. Ein
    geschickter Wert für einen nicht wählbaren Slot zählt nicht."""
    selbst["SELF_TEMPLATE_KATEGORIEN"] = ["sig"]
    d = _self(client, min="")
    assert d["antwort"]["signatur_unterdrueckt"] is False
    assert _namen(d["antwort"]["html"]) == ["StdMin"]


def test_self_ungespeicherte_wahl(selbst, client):
    selbst["KETTE_BEHAELT"] = "disclaimer"
    d = _self(client, sig="StdMin", min="")
    assert _namen(d["neu"]["html"])[0] == "StdMin"
    assert d["antwort"]["signatur_unterdrueckt"] is True
    assert _namen(d["antwort"]["html"]) == ["StdDis"]         # Disclaimer bleibt laut Einstellung
    # Nichts gespeichert
    assert "template" not in selbst["MAILBOX_CONFIG"][ABS]


# ── Was-wäre-wenn-Vorschau: Textfassung mit Banner und Disclaimer ────────────

def test_was_waere_wenn_textfassung_traegt_banner_und_disclaimer(client):
    """Die Mail trägt Banner und Disclaimer auch im Textteil (handler, seit
    v1.9.142); die Seite /preview zeigte bis v1.12.102 nur die Signatur."""
    r = client.get("/api/preview-data", params={
        "email": ABS, "template": "Eigen", "banner": "B1", "disclaimer": "D1", "explizit": "1"})
    d = r.json()
    assert d["txt"] == "[Eigen]"
    assert d["txt_gesamt"] == "[Eigen]\n\n[B1]\n\n[D1]\n"
