"""Eingehende S/MIME-Signaturen werden geprüft, bevor das Gateway ihnen glaubt.

ANLASS (v1.10.110)
------------------
Bis v1.10.109 kam der Name im Betreff-Vermerk „signiert von …“ aus dem
beigelegten Zertifikat, ohne dass die Signatur geprüft wurde; die Signatur
wurde danach entfernt. Ein selbst ausgestelltes Zertifikat mit frei gewähltem
Namen und ein nach dem Signieren veränderter Inhalt ergaben denselben Vermerk
wie eine echte Signatur. Beim Einsammeln fehlten Adress- und Ausstellerprüfung:
Ein Zertifikat für eine fremde Adresse kam unter dem Absender in den Bestand,
ebenso eines, das ein Endzertifikat „ausgestellt“ hatte.

Invarianten:
1. Entfernt und vermerkt wird nur bei gültiger Inhaltssignatur, Zertifikat
   auf den From-Kopf (seit v1.10.115; Sender-Kopf reicht nur zum Einsammeln,
   RFC 8550 §3) und tragfähiger, bekannter Kette.
2. Sonst geht die Mail signiert weiter, ohne Vermerk.
3. In den Bestand führt nur `_uebernehmen` — Adresse, Zertifikatsart, Kette.
4. Der Nachlauf verschiebt Bestandszertifikate mit Adress- oder Strukturmangel
   in den Wartestand, abgelaufene und unbekannt ausgestellte bleiben. Altbestand
   (ohne Herkunft) ohne bekannten Aussteller kommt zur Durchsicht (v1.10.115).
5. Jedes Empfängerzertifikat trägt seine Herkunft — jeder Aufrufer nennt sie.
"""
from __future__ import annotations

import ast
import asyncio
import email
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import cert_wartestand
import smime_harvest
import smime_store
import trust_store

JETZT = datetime.now(timezone.utc).replace(microsecond=0)
APP = Path(__file__).resolve().parent.parent / "app"


def _mk(cn, aussteller=None, aussteller_key=None, ca=False, pfadlaenge=None,
        san=None, eku=None, bis_tage=30):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    b = (x509.CertificateBuilder().subject_name(name)
         .issuer_name(aussteller.subject if aussteller else name)
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(JETZT - timedelta(days=2))
         .not_valid_after(JETZT + timedelta(days=bis_tage))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=pfadlaenge), critical=True))
    if san:
        b = b.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(san)]), critical=False)
    if eku:
        b = b.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    return b.sign(aussteller_key or key, hashes.SHA256()), key


def _signiert(von, cert, key, mitgeliefert=(), text=b"Bitte 50.000 EUR ueberweisen.",
              envelope_kopf=b""):
    inhalt = b"Content-Type: text/plain\r\n\r\n" + text + b"\r\n"
    p7 = pkcs7.PKCS7SignatureBuilder().set_data(inhalt).add_signer(cert, key, hashes.SHA256())
    for c in mitgeliefert:
        p7 = p7.add_certificate(c)
    roh = p7.sign(serialization.Encoding.SMIME, [pkcs7.PKCS7Options.DetachedSignature])
    roh = roh.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return (b"From: " + von.encode() + b"\r\nTo: chefin@firma.de\r\nSubject: Angebot\r\n"
            + envelope_kopf + roh)


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    werte = {"TRUST_MS_ROOTS": False}
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(cert_wartestand.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(trust_store.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(smime_store, "RECIPIENT_DIR", tmp_path / "recipients")
    trust_store._speicher_leeren()
    freigaben = {}
    monkeypatch.setattr(trust_store, "freigaben", lambda: dict(freigaben))
    import crl_check
    monkeypatch.setattr(crl_check, "ausstellerzertifikat", lambda c: None)   # kein Netz
    import notification
    monkeypatch.setattr(notification, "send_cert_waiting", lambda *a, **kw: False)
    import stats
    monkeypatch.setattr(stats, "increment", lambda k: None)
    ca, ca_key = _mk("Partner-CA", ca=True)
    freigaben[trust_store.abdruck(ca)] = "Partner-CA"
    return {"ca": ca, "ca_key": ca_key, "freigaben": freigaben, "werte": werte,
            "tmp": tmp_path, "mp": monkeypatch}


def _pruefe(roh):
    msg = email.message_from_bytes(roh)
    return smime_harvest.strip_and_harvest(msg, smime_harvest.kopf_adressen(msg), raw=roh)


def _bestand(adresse):
    return smime_store.get_recipient_cert_path(adresse) is not None


def _wartend():
    return {e["adresse"]: e["grund"] for e in cert_wartestand.liste()}


# ── 1. Gültig ────────────────────────────────────────────────────────────────

def test_gueltige_signatur_wird_entfernt_vermerkt_und_eingesammelt(umgebung):
    cert, key = _mk("Paula Partner", umgebung["ca"], umgebung["ca_key"],
                    san="paula@partner.example")
    innen, name, grund = _pruefe(_signiert("Paula <paula@partner.example>", cert, key,
                                           [umgebung["ca"]]))
    assert grund is None and name == "Paula Partner"
    assert b"50.000" in innen
    assert _bestand("paula@partner.example")


# ── 2. Inhalt manipuliert ────────────────────────────────────────────────────

def test_veraenderter_inhalt_wird_nicht_bestaetigt_und_nicht_eingesammelt(umgebung):
    cert, key = _mk("Paula Partner", umgebung["ca"], umgebung["ca_key"],
                    san="paula@partner.example")
    roh = _signiert("paula@partner.example", cert, key, [umgebung["ca"]])
    roh = roh.replace(b"50.000", b"99.000")
    innen, name, grund = _pruefe(roh)
    assert innen is None and name is None and grund
    assert not _bestand("paula@partner.example")
    assert _wartend() == {}, "ohne gültige Signatur gibt es nichts zu entscheiden"


# ── 3. Selbst signiert, frei gewählter Name ──────────────────────────────────

def test_selbst_signiertes_zertifikat_bekommt_keinen_vermerk(umgebung):
    cert, key = _mk("Dr. Vorstand Mueller (CEO)", san="chef@partner.example")
    innen, name, grund = _pruefe(_signiert("chef@partner.example", cert, key))
    assert innen is None and name is None
    assert not _bestand("chef@partner.example")
    assert "chef@partner.example" in _wartend()


# ── 4. Adresse passt nicht ───────────────────────────────────────────────────

def test_zertifikat_fuer_fremde_adresse_kommt_nicht_in_den_bestand(umgebung):
    cert, key = _mk("Angreifer", umgebung["ca"], umgebung["ca_key"],
                    san="angreifer@evil.example")
    innen, name, grund = _pruefe(_signiert("einkauf@partner.example", cert, key,
                                           [umgebung["ca"]]))
    assert innen is None and name is None
    assert "angreifer@evil.example" in grund
    assert not _bestand("einkauf@partner.example")
    assert "einkauf@partner.example" in _wartend()


def test_fremde_adresse_laesst_sich_nicht_per_klick_freigeben(umgebung):
    cert, key = _mk("Angreifer", umgebung["ca"], umgebung["ca_key"],
                    san="angreifer@evil.example")
    _pruefe(_signiert("einkauf@partner.example", cert, key, [umgebung["ca"]]))
    fp = cert_wartestand.liste()[0]["fingerabdruck"]
    ergebnis = cert_wartestand.freigeben(fp, auch_aussteller=False)
    assert not ergebnis["ok"] and ergebnis["fehler"].startswith("nicht freigebbar")
    assert not _bestand("einkauf@partner.example")


def test_massgeblich_ist_der_from_kopf_nicht_der_envelope(umgebung):
    """Im Produktivbestand lagen Zertifikate unter VERP-Bounce-Adressen, weil
    der Envelope-Absender zählte. Der From-Kopf ist, was der Empfänger sieht."""
    cert, key = _mk("Versand", umgebung["ca"], umgebung["ca_key"],
                    san="no-reply@shop.example")
    roh = _signiert("Shop <no-reply@shop.example>", cert, key, [umgebung["ca"]])
    msg = email.message_from_bytes(roh)
    innen, _, grund = smime_harvest.strip_and_harvest(
        msg, smime_harvest.kopf_adressen(msg), raw=roh)
    assert grund is None
    assert _bestand("no-reply@shop.example")
    assert not _bestand("bounce+abc@mailer.shop.example")


def test_sender_kopf_reicht_zum_einsammeln_nicht_fuer_den_vermerk(umgebung):
    """Der Leser sieht From. Ein Vermerk aus einem Zertifikat, das nur auf den
    Sender-Kopf lautet, bescheinigte einer anderen Adresse die Echtheit — und
    ohne Signatur könnte Outlook den Widerspruch nicht mehr zeigen."""
    cert, key = _mk("Dr. Vorstand Mueller", umgebung["ca"], umgebung["ca_key"],
                    san="angreifer2@evil.example")
    roh = _signiert("chef@partner.example", cert, key, [umgebung["ca"]],
                    envelope_kopf=b"Sender: angreifer2@evil.example\r\n")
    innen, name, grund = _pruefe(roh)
    assert innen is None and name is None
    assert "Sender-Kopf" in grund
    assert _bestand("angreifer2@evil.example"), "eingesammelt unter seiner eigenen Adresse"
    assert not _bestand("chef@partner.example")


# ── 5. Aussteller darf nicht ausstellen ──────────────────────────────────────

def test_von_einem_endzertifikat_ausgestellt_wird_abgelehnt(umgebung):
    blatt, blatt_key = _mk("Angreifer", umgebung["ca"], umgebung["ca_key"],
                           san="angreifer@evil.example")
    unter, unter_key = _mk("Fake", blatt, blatt_key, san="buchhaltung@partner.example")
    innen, _, grund = _pruefe(_signiert("buchhaltung@partner.example", unter, unter_key,
                                        [blatt, umgebung["ca"]]))
    assert innen is None and "keine Zertifizierungsstelle" in grund
    assert not _bestand("buchhaltung@partner.example")


def test_pfadlaenge_wird_eingehalten(umgebung):
    wurzel, wurzel_key = _mk("Wurzel", ca=True, pfadlaenge=0)
    umgebung["freigaben"][trust_store.abdruck(wurzel)] = "Wurzel"
    zwischen, zwischen_key = _mk("Zwischen", wurzel, wurzel_key, ca=True)
    cert, key = _mk("Paula", zwischen, zwischen_key, san="paula@partner.example")
    _, _, grund = _pruefe(_signiert("paula@partner.example", cert, key, [zwischen, wurzel]))
    assert grund and "Kette" in grund
    assert not _bestand("paula@partner.example")


def test_zertifikat_ohne_email_zweck_wird_abgelehnt(umgebung):
    cert, key = _mk("Server", umgebung["ca"], umgebung["ca_key"], san="paula@partner.example",
                    eku=[ExtendedKeyUsageOID.SERVER_AUTH])
    _, _, grund = _pruefe(_signiert("paula@partner.example", cert, key, [umgebung["ca"]]))
    assert grund == "nicht für E-Mail-Schutz ausgestellt"
    assert not _bestand("paula@partner.example")


def test_strukturmangel_wartet_auch_bei_auto_modus(umgebung):
    umgebung["werte"]["TRUST_UNKNOWN_MODE"] = "auto"
    blatt, blatt_key = _mk("Angreifer", umgebung["ca"], umgebung["ca_key"],
                           san="angreifer@evil.example")
    unter, unter_key = _mk("Fake", blatt, blatt_key, san="buchhaltung@partner.example")
    _pruefe(_signiert("buchhaltung@partner.example", unter, unter_key, [blatt, umgebung["ca"]]))
    assert not _bestand("buchhaltung@partner.example")


# ── 6. Nur ein Weg in den Bestand ────────────────────────────────────────────

def test_nur_uebernehmen_schreibt_in_den_bestand():
    baum = ast.parse((APP / "smime_harvest.py").read_text(encoding="utf-8"))
    aufrufer = []
    for fn in ast.walk(baum):
        if isinstance(fn, ast.FunctionDef):
            for knoten in ast.walk(fn):
                if (isinstance(knoten, ast.Attribute)
                        and knoten.attr == "store_recipient_cert"):
                    aufrufer.append(fn.name)
    assert aufrufer == ["_uebernehmen"], aufrufer


def test_jeder_weg_in_den_bestand_nennt_die_herkunft():
    """Ohne Herkunft liesse sich ein späterer Nachlauf wieder nicht zwischen
    eingesammelt und hochgeladen unterscheiden."""
    fehlt, gefunden = [], 0
    for datei in APP.rglob("*.py"):
        for k in ast.walk(ast.parse(datei.read_text(encoding="utf-8"))):
            if (isinstance(k, ast.Call) and isinstance(k.func, ast.Attribute)
                    and k.func.attr == "store_recipient_cert"):
                gefunden += 1
                if not any(kw.arg == "herkunft" for kw in k.keywords):
                    fehlt.append(f"{datei.relative_to(APP)}:{k.lineno}")
    assert gefunden >= 4 and not fehlt, (gefunden, fehlt)


def test_eingesammeltes_zertifikat_traegt_herkunft(umgebung):
    cert, key = _mk("Paula Partner", umgebung["ca"], umgebung["ca_key"],
                    san="paula@partner.example")
    _pruefe(_signiert("paula@partner.example", cert, key, [umgebung["ca"]]))
    assert smime_store.herkunft("paula@partner.example") == "eingesammelt"
    smime_store.delete_recipient_cert("paula@partner.example")
    assert not (umgebung["tmp"] / "recipients" / "paula@partner.example").exists()


# ── 7. Handler: unbestätigt bleibt signiert ──────────────────────────────────

class _Sitzung:
    peer = ("40.92.0.1", 5000)
    ssl = {"cipher": "x"}


class _Umschlag:
    def __init__(self, inhalt, von):
        self.mail_from = von
        self.rcpt_tos = ["chefin@firma.de"]
        self.content = inhalt


@pytest.fixture
def handler_lauf(umgebung):
    import handler
    import reinject
    import smtp_acl
    import exo_mailboxes
    import mail_audit
    mp = umgebung["mp"]
    umgebung["werte"].update({"REINJECT_MODE": "smtp", "SMIME_STRIP_INBOUND": True,
                              "TENANT_DOMAIN": "firma.onmicrosoft.com"})
    mp.setattr(smtp_acl, "is_allowed", lambda ip: True)
    mp.setattr(exo_mailboxes, "known_addresses", lambda: {"chefin@firma.de"})
    zugestellt = []
    mp.setattr(reinject, "send", lambda *a, **kw: zugestellt.append(a))
    mp.setattr(mail_audit, "log_event", lambda **kw: None)

    def lauf(roh, envelope_von):
        asyncio.run(handler.SignatureHandler().handle_DATA(
            None, _Sitzung(), _Umschlag(roh, envelope_von)))
        return email.message_from_bytes(zugestellt[-1][2])
    return lauf


def test_handler_vermerkt_nur_bestaetigte_signatur(umgebung, handler_lauf):
    cert, key = _mk("Paula Partner", umgebung["ca"], umgebung["ca_key"],
                    san="paula@partner.example")
    raus = handler_lauf(_signiert("paula@partner.example", cert, key, [umgebung["ca"]]),
                        "bounce+123@mailer.partner.example")
    assert "signiert von Paula Partner" in raus["Subject"]
    assert raus.get_content_type() == "text/plain"
    assert _bestand("paula@partner.example")


def test_handler_liefert_falsche_signatur_signiert_und_ohne_vermerk(umgebung, handler_lauf):
    cert, key = _mk("Dr. Vorstand Mueller (CEO)", san="chef@partner.example")
    roh = _signiert("chef@partner.example", cert, key).replace(b"50.000", b"99.000")
    raus = handler_lauf(roh, "chef@partner.example")
    assert "signiert von" not in raus["Subject"]
    assert raus.get_content_type() == "multipart/signed", \
        "unbestätigt muss die Signatur bleiben, damit Outlook selbst urteilt"


# ── 8. Nachlauf über den Bestand ─────────────────────────────────────────────

def _ablegen(adresse, cert, herkunft=None):
    """Bestand wie vor v1.10.115: ohne Herkunft, sofern keine angegeben."""
    if herkunft:
        smime_store.store_recipient_cert(adresse, cert.public_bytes(serialization.Encoding.PEM),
                                         herkunft=herkunft)
        return
    d = smime_store.RECIPIENT_DIR / adresse
    d.mkdir(parents=True, exist_ok=True)
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_nachlauf_verschiebt_nur_echte_maengel(umgebung):
    ca, ca_key = umgebung["ca"], umgebung["ca_key"]
    passend, _ = _mk("Paula", ca, ca_key, san="paula@partner.example")
    fremd, _ = _mk("No-Reply", ca, ca_key, san="no-reply@shop.example")
    abgelaufen, _ = _mk("Alt", ca, ca_key, san="alt@partner.example", bis_tage=-1)
    unbekannt, _ = _mk("Eigen", san="eigen@partner.example")     # Hochladen = Entscheidung
    _ablegen("paula@partner.example", passend)
    _ablegen("bounce+x@mailer.shop.example", fremd)
    _ablegen("alt@partner.example", abgelaufen)
    _ablegen("eigen@partner.example", unbekannt)

    verschoben = smime_harvest.bestand_nachpruefen_einmal()
    assert [v["adresse"] for v in verschoben] == ["bounce+x@mailer.shop.example"]
    assert not _bestand("bounce+x@mailer.shop.example")
    assert "Nachprüfung" in _wartend()["bounce+x@mailer.shop.example"]
    for bleibt in ("paula@partner.example", "alt@partner.example", "eigen@partner.example"):
        assert _bestand(bleibt), bleibt
    assert [d["adresse"] for d in smime_harvest.durchsicht()] == ["eigen@partner.example"]
    assert smime_harvest.durchsicht()[0]["selbst_ausgestellt"]
    assert smime_harvest.bestand_nachpruefen_einmal() is None, "läuft nur einmal"


def test_durchsicht_kennt_die_herkunft(umgebung):
    """Hochgeladen ist eine Entscheidung — nur Bestand ohne Herkunft wird vorgelegt."""
    for adr, h in (("alt@partner.example", None), ("hoch@partner.example", "hochgeladen")):
        cert, _ = _mk("Eigen", san=adr)
        _ablegen(adr, cert, h)
    smime_harvest.bestand_nachpruefen()
    assert [d["adresse"] for d in smime_harvest.durchsicht()] == ["alt@partner.example"]


def test_durchsicht_behalten_und_entfernen(umgebung):
    import abnahme
    for adr in ("a@partner.example", "b@partner.example"):
        cert, _ = _mk("Eigen", san=adr)
        _ablegen(adr, cert)
    smime_harvest.bestand_nachpruefen()
    assert abnahme._bestand_durchsicht()["zustand"] == abnahme.OFFEN
    assert smime_harvest.durchsicht_entscheiden("a@partner.example", behalten=True)
    assert smime_store.herkunft("a@partner.example") == "bestaetigt" and _bestand("a@partner.example")
    assert smime_harvest.durchsicht_entscheiden("B@partner.example", behalten=False)
    assert not _bestand("b@partner.example")
    assert smime_harvest.durchsicht() == []
    assert abnahme._bestand_durchsicht()["zustand"] == abnahme.OK
    assert not smime_harvest.durchsicht_entscheiden("fremd@x.example", behalten=False), \
        "nur was zur Durchsicht steht, lässt sich hierüber entfernen"


def test_durchsicht_ist_eine_aufgabe():
    import abnahme
    import aufgaben
    assert any(a.pruefung is abnahme._bestand_durchsicht and a.seit == "1.10.115"
               for a in aufgaben.AUFGABEN)


def test_wartende_zertifikate_sind_eine_aufgabe(umgebung):
    import abnahme
    import aufgaben
    assert abnahme._wartende_zertifikate()["zustand"] == abnahme.OK
    cert, _ = _mk("Eigen", san="eigen@partner.example")
    cert_wartestand.merken("eigen@partner.example",
                           cert.public_bytes(serialization.Encoding.PEM), "unbekannt")
    assert abnahme._wartende_zertifikate()["zustand"] == abnahme.OFFEN
    assert any(a.pruefung is abnahme._wartende_zertifikate for a in aufgaben.AUFGABEN)
