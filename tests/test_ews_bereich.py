"""Zugriffsbereich des EWS-Dienstkontos als AD-Gruppe (ews_bereich, v1.12.92).

Gesichert wird, mit einem Verzeichnis im Speicher statt eines echten DC:

* Soll = Hybrid-Nutzer mit Signatur ODER S/MIME; EXO-Postfächer und
  „beides aus“ nie.
* Der Abgleich ändert GENAU die Differenz und protokolliert jede Änderung.
* Schalter aus → planmäßig nur messen, nichts ändern; die Aufgabe zeigt die
  Abweichung und verschwindet nach dem Abgleich.
* Das Admin-Kennwort steht in keinem Protokoll, keiner Antwort, keiner
  gespeicherten Einstellung — auch nicht, wenn die Bibliothek es in ihre
  Fehlermeldung übernimmt.
* Vorschau = Ausführung (eine Schrittliste, dieselben DNs).
* Eine feste Adressliste wird erkannt; ihre Entfernung steht erst nach
  Bestätigung in den Befehlen.
* Frisch aufgenommene Mitglieder: Ablehnung in der Wartezeit kein Fehler.
* Sicherheitsbeschreibung: ACE an der richtigen Stelle, Rundlauf, idempotent.

Testdaten erfunden (firma.at).
"""
import json
import logging
import re
import struct
from datetime import timedelta

import pytest

import abnahme
import aufgaben
import ews_bereich as eb
import health_check
import settings_store

BASIS = "DC=firma,DC=at"
OU = "OU=Dienste,DC=firma,DC=at"
GRUPPE_DN = f"CN=EXO-Gateway-EWS,{OU}"
MAX = "max.mustermann@firma.at"           # Hybrid, Signatur
ERIKA = "erika.beispiel@firma.at"         # Hybrid, S/MIME
PAUL = "paul.weg@firma.at"                # Hybrid, beides aus (steht in der Gruppe)
EVA = "eva.wolke@firma.at"                # EXO-Postfach, Signatur
ADMIN_KW = "Adm1n-GEHEIM-xq7-4711"


def _sid(rid: int) -> bytes:
    return struct.pack("<BB", 1, 5) + (5).to_bytes(6, "big") + struct.pack("<IIIII", 21, 1, 2, 3, rid)


def _ace_allgemein(sid: bytes, maske: int = 0x20094, geerbt: bool = False) -> eb.Ace:
    rumpf = struct.pack("<I", maske) + sid
    flags = eb.INHERITED_ACE if geerbt else 0
    return eb.Ace(0, flags, struct.pack("<BBH", 0, flags, 4 + len(rumpf)) + rumpf)


def _sd_start() -> bytes:
    sd = eb.Sd(control=eb.SE_DACL_PRESENT | eb.SE_SELF_RELATIVE, dacl_rev=2,
               aces=[_ace_allgemein(_sid(512), 0xF01FF), _ace_allgemein(_sid(513), geerbt=True)])
    return eb.sd_schreiben(sd)


class FalschesVerzeichnis:
    """AD im Speicher. Attribute roh wie ldap3 (Listen von bytes)."""

    def __init__(self, basis=BASIS):
        self.basis = basis
        self.objekte: dict[str, dict] = {}
        self.aenderungen: list[tuple] = []      # (art, dn, wert)
        self.angemeldet: list[tuple[str, str]] = []
        self.fehler_bei: dict[str, str] = {}    # art → Fehlertext (mit Kennwort!)
        self._rid = 1100

    # Aufbau
    def nutzer(self, adresse, sam=None):
        self._rid += 1
        dn = f"CN={sam or adresse.split('@')[0]},OU=Leute,{BASIS}"
        self.objekte[dn] = {"objectClass": [b"user"], "mail": [adresse.encode()],
                            "proxyAddresses": [b"SMTP:" + adresse.encode()],
                            "sAMAccountName": [(sam or adresse.split("@")[0]).encode()],
                            "objectSid": [_sid(self._rid)]}
        return dn

    def gruppe(self, dn=GRUPPE_DN, mitglieder=(), typ=eb.GRUPPE_UNIVERSAL_SICHERHEIT):
        self.objekte[dn] = {"objectClass": [b"group"], "sAMAccountName": [dn.split(",")[0][3:].encode()],
                            "groupType": [str(typ).encode()], "member": [m.encode() for m in mitglieder],
                            "nTSecurityDescriptor": [_sd_start()]}

    # Schnittstelle wie ews_bereich.Verzeichnis
    def _klasse(self, o):
        return {c.decode().lower() for c in o.get("objectClass", [])}

    def suchen(self, filt, attribute, basis=None, bereich="SUBTREE", steuerung=None):
        aus = []
        adressen = {a.lower() for a in re.findall(r"\(mail=([^)]*)\)", filt)}
        sams = {s.lower() for s in re.findall(r"\(sAMAccountName=([^)]*)\)", filt)}
        upns = {s.lower() for s in re.findall(r"\(userPrincipalName=([^)]*)\)", filt)}
        for dn, o in self.objekte.items():
            kl = self._klasse(o)
            if "objectClass=group" in filt and "group" not in kl:
                continue
            if "objectClass=user" in filt and "user" not in kl:
                continue
            treffer = False
            if adressen:
                eigene = {m.decode().lower() for m in o.get("mail", [])}
                eigene |= {p.decode()[5:].lower() for p in o.get("proxyAddresses", [])}
                treffer = bool(eigene & adressen)
            if sams and {s.decode().lower() for s in o.get("sAMAccountName", [])} & sams:
                treffer = True
            if upns and {s.decode().lower() for s in o.get("userPrincipalName", [])} & upns:
                treffer = True
            if treffer:
                aus.append({"dn": dn, **{k: v for k, v in o.items() if k in attribute}})
        return aus

    def lesen(self, dn, attribute, steuerung=None):
        o = next((v for k, v in self.objekte.items() if k.lower() == dn.lower()), None)
        if o is None:
            return None
        return {"dn": dn, **{k: v for k, v in o.items() if k in attribute}}

    def _fehler(self, art):
        if art in self.fehler_bei:
            raise eb.BereichFehler("ldap", self.fehler_bei[art])

    def anlegen(self, dn, klassen, attribute):
        self._fehler("anlegen")
        self.aenderungen.append(("anlegen", dn, dict(attribute)))
        o = {"objectClass": [k.encode() for k in klassen]}
        for k, v in attribute.items():
            o[k] = [str(v).encode()]
        if "user" in klassen:
            self._rid += 1
            o["objectSid"] = [_sid(self._rid)]
        if "group" in klassen:
            o["member"] = []
            o["nTSecurityDescriptor"] = [_sd_start()]
        self.objekte[dn] = o

    def ersetzen(self, dn, attribute, steuerung=None):
        self._fehler("ersetzen")
        self.aenderungen.append(("ersetzen", dn, sorted(attribute)))
        for k, v in attribute.items():
            self.objekte[dn][k] = [v if isinstance(v, bytes) else str(v).encode()]

    def mitglied(self, gruppe_dn, dn, hinzu):
        self._fehler("mitglied")
        self.aenderungen.append(("hinzu" if hinzu else "weg", gruppe_dn, dn))
        liste = self.objekte[gruppe_dn]["member"]
        if hinzu:
            liste.append(dn.encode())
        else:
            liste[:] = [m for m in liste if m.decode().lower() != dn.lower()]

    def kennwort_setzen(self, dn, kennwort):
        self._fehler("kennwort")
        self.aenderungen.append(("kennwort", dn, len(kennwort)))
        self.objekte[dn]["_kennwort"] = kennwort

    def sd_steuerung(self):
        return "sd-dacl"

    def schliessen(self):
        pass


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    settings_store.init()
    settings_store.update({
        "EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
        "EWS_ONPREM_KONTO": "svc-exo-gateway@firma.at", "EWS_ONPREM_KENNWORT": "dienst-testwert",
        "EWS_LDAP_SERVER": "dc01.firma.at", "EWS_BEREICH_OU": OU,
        "MAILBOX_CONFIG": {
            MAX: {"sig": True, "smime": False, "hybrid": True},
            "guid-erika": {"sig": False, "smime": True, "hybrid": True, "primary": ERIKA,
                           "known_addresses": [ERIKA]},
            EVA: {"sig": True, "smime": False},
        }})
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda a: False)   # nur die Marke zählt
    v = FalschesVerzeichnis()
    for a in (MAX, ERIKA, PAUL, EVA):
        v.nutzer(a)
    v.objekte["CN=svc-exo-gateway,OU=Dienste,DC=firma,DC=at"] = {
        "objectClass": [b"user"], "sAMAccountName": [b"svc-exo-gateway"],
        "userPrincipalName": [b"svc-exo-gateway@firma.at"], "objectSid": [_sid(4000)]}
    monkeypatch.setattr(eb, "_verbinden", lambda b, k: (v.angemeldet.append((b, k)), v)[1])
    monkeypatch.setattr(aufgaben, "_eingerichtet", lambda: True)
    aufgaben.vergessen()
    return v


def _dn(v, adresse):
    return next(dn for dn, o in v.objekte.items() if o.get("mail") == [adresse.encode()])


# ── Soll ──────────────────────────────────────────────────────────────────────

def test_soll_nur_hybrid_mit_signatur_oder_smime(umgebung):
    cfg = dict(settings_store.get("MAILBOX_CONFIG"))
    cfg[PAUL] = {"sig": False, "smime": False, "hybrid": True}
    assert eb.soll_mitglieder(cfg) == {MAX, ERIKA}
    cfg[MAX] = {"sig": False, "smime": False, "hybrid": True}
    assert eb.soll_mitglieder(cfg) == {ERIKA}


# ── Abgleich ──────────────────────────────────────────────────────────────────

def _eingerichtet(v, mitglieder=()):
    v.gruppe(mitglieder=[_dn(v, a) for a in mitglieder])
    eb._stand_setzen(gruppe_dn=GRUPPE_DN)


def test_abgleich_aendert_genau_die_differenz(umgebung, caplog):
    v = umgebung
    _eingerichtet(v, [MAX, PAUL])
    caplog.set_level(logging.INFO)
    erg = eb.abgleichen("Test")
    assert erg["ok"] and erg["hinzu"] == [ERIKA] and erg["weg"] == [PAUL]
    assert [a[0] for a in v.aenderungen] == ["hinzu", "weg"]
    assert v.aenderungen[0][2] == _dn(v, ERIKA) and v.aenderungen[1][2] == _dn(v, PAUL)
    audit = [(e["action"], e["mailbox"]) for e in settings_store.get("GATEWAY_AUDIT_LOG")]
    assert ("ews_bereich_mitglied_hinzu", ERIKA) in audit
    assert ("ews_bereich_mitglied_weg", PAUL) in audit
    assert not any(m == MAX for _a, m in audit)                       # unverändert = kein Eintrag
    assert ERIKA in caplog.text and PAUL in caplog.text
    # Zweiter Lauf: nichts mehr zu tun
    v.aenderungen.clear()
    assert eb.abgleichen("Test")["hinzu"] == [] and v.aenderungen == []


def test_unbekannte_adresse_aendert_nichts_und_bleibt_offen(umgebung):
    v = umgebung
    del v.objekte[_dn(v, ERIKA)]
    _eingerichtet(v, [MAX])
    erg = eb.abgleichen("Test")
    assert ERIKA in erg["nicht_gefunden"] and v.aenderungen == []
    assert abnahme._ews_gruppe()["zustand"] == abnahme.OFFEN


def test_leere_postfachliste_ohne_lesbarkeit_aendert_nichts(umgebung, monkeypatch):
    v = umgebung
    _eingerichtet(v, [MAX])
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k: None if k == "MAILBOX_CONFIG" else alt(k))
    assert not eb.abgleichen("Test")["ok"] and v.aenderungen == []


def test_schalter_aus_misst_nur(umgebung):
    v = umgebung
    _eingerichtet(v, [MAX, PAUL])
    assert eb.auto_an() is False
    erg = eb.planmaessig()
    assert erg["geaendert"] is False and v.aenderungen == []
    assert erg["fehlen"] == [ERIKA] and erg["zuviel"] == [PAUL]
    assert eb.nach_postfach_aenderung() == "aus"


def test_schalter_an_gleicht_planmaessig_ab(umgebung):
    v = umgebung
    _eingerichtet(v, [MAX])
    settings_store.update({"EWS_BEREICH_AUTO": True})
    assert eb.planmaessig()["hinzu"] == [ERIKA]


def test_aufgabe_bei_abweichung_und_weg_nach_abgleich(umgebung):
    v = umgebung
    _eingerichtet(v, [MAX])
    assert abnahme._ews_gruppe()["zustand"] == abnahme.UNBEKANNT        # noch nie gemessen
    eb.abgleichen("Test", aendern=False)
    p = abnahme._ews_gruppe()
    assert p["zustand"] == abnahme.OFFEN and ERIKA in p["befund"]
    aufgaben.vergessen()
    assert any(a["kennung"] == "ews-gruppe" for a in aufgaben.offene())
    eb.abgleichen("Test")
    assert abnahme._ews_gruppe()["zustand"] == abnahme.OK
    aufgaben.vergessen()
    assert not any(a["kennung"] == "ews-gruppe" for a in aufgaben.offene())
    # Neu aktivierter Hybrid-Nutzer: sofort offen, ohne neue Messung (nur lokal).
    cfg = dict(settings_store.get("MAILBOX_CONFIG"))
    cfg[PAUL] = {"sig": True, "hybrid": True}
    settings_store.update({"MAILBOX_CONFIG": cfg})
    p = abnahme._ews_gruppe()
    assert p["zustand"] == abnahme.OFFEN and PAUL in p["befund"]


def test_ohne_einrichtung_entfaellt(umgebung):
    assert abnahme._ews_gruppe()["zustand"] == abnahme.ENTFAELLT
    assert eb.planmaessig() is None


# ── Wartezeit ─────────────────────────────────────────────────────────────────

def test_frisches_mitglied_wartet_ohne_fehler(umgebung, monkeypatch):
    v = umgebung
    _eingerichtet(v, [MAX])
    eb.abgleichen("Test")                                  # nimmt ERIKA auf
    r = health_check._ews_fehlschlag(ERIKA, "ErrorImpersonateUserDenied", "EWS-Zugriff: x")
    assert r["status"] == "warn" and "ews_code" not in r and "wartet" in r["detail"]
    # MAX war schon Mitglied (nicht frisch) → Ablehnung ist ein Fehler
    r = health_check._ews_fehlschlag(MAX, "ErrorImpersonateUserDenied", "EWS-Zugriff: x")
    assert r["status"] == "error" and r["ews_code"] == "ErrorImpersonateUserDenied"
    # Nach der Wartezeit zählt die Ablehnung wieder
    spaeter = eb._jetzt() + timedelta(seconds=eb.WARTEZEIT_S + 60)
    monkeypatch.setattr(eb, "_jetzt", lambda: spaeter)
    r = health_check._ews_fehlschlag(ERIKA, "ErrorImpersonateUserDenied", "EWS-Zugriff: x")
    assert r["ews_code"] == "ErrorImpersonateUserDenied"


# ── Einrichtung ───────────────────────────────────────────────────────────────

def _alles_durchsuchen(umgebung, caplog, antwort) -> list[str]:
    funde = []
    if ADMIN_KW in caplog.text:
        funde.append("Protokoll")
    if ADMIN_KW in json.dumps(antwort, ensure_ascii=False):
        funde.append("Antwort")
    for datei in settings_store.SETTINGS_FILE.parent.iterdir():
        if ADMIN_KW in datei.read_text("utf-8", "replace"):
            funde.append(datei.name)
    if ADMIN_KW in json.dumps(settings_store.get_all(), ensure_ascii=False):
        funde.append("Einstellungen im Speicher")
    return funde


def test_admin_kennwort_nirgends(umgebung, caplog):
    v = umgebung
    caplog.set_level(logging.DEBUG)
    erg = eb.einrichten({"konto_anlegen": True, "konto": "svc-neu"}, "admin@firma.at", ADMIN_KW, "admin")
    assert erg["ok"], erg
    assert v.angemeldet[0] == ("admin@firma.at", ADMIN_KW)        # benutzt — und dann nirgends
    assert _alles_durchsuchen(v, caplog, erg) == []
    # Das neue Dienstkonto-Kennwort liegt im Geheimnis-Schlüssel, nicht im Protokoll.
    # Seit v1.12.96 zunächst AUSSTEHEND (es gibt ein bisheriges Konto).
    neu = settings_store.get("EWS_BEREICH_NEU_KENNWORT")
    assert len(neu) >= 32 and neu == v.objekte[f"CN=svc-neu,{OU}"]["_kennwort"]
    assert "EWS_BEREICH_NEU_KENNWORT" in settings_store.SECRET_KEYS
    assert neu not in caplog.text and neu not in json.dumps(erg)


def test_admin_kennwort_auch_nicht_im_fehlertext(umgebung, caplog):
    """Die Bibliothek übernimmt das Kennwort in ihre Meldung — geschwärzt."""
    v = umgebung
    v.fehler_bei["mitglied"] = f"Zugriff verweigert für Kennwort {ADMIN_KW}"
    v.fehler_bei["ersetzen"] = f"bind failed: password={ADMIN_KW}"
    caplog.set_level(logging.DEBUG)
    erg = eb.einrichten({"konto_anlegen": False}, "admin@firma.at", ADMIN_KW, "admin")
    assert not erg["ok"] and "••••" in erg["error"]
    assert _alles_durchsuchen(v, caplog, erg) == []


def test_vorschau_gleich_ausfuehrung(umgebung):
    v = umgebung
    daten = {"konto_anlegen": True, "konto": "svc-neu"}
    vorschau = eb.plan(eb.parameter(daten, basis=BASIS))
    erg = eb.einrichten(daten, "admin@firma.at", ADMIN_KW, "admin")
    assert [s["kennung"] for s in erg["schritte"]] == [s.kennung for s in vorschau if s.art == "ad"]
    assert [s["kennung"] for s in erg["plan"]] == [s.kennung for s in vorschau]
    angelegt = [a[1] for a in v.aenderungen if a[0] == "anlegen"]
    konto = next(s for s in vorschau if s.kennung == "konto")
    gruppe = next(s for s in vorschau if s.kennung == "gruppe")
    scope = next(s for s in vorschau if s.kennung == "scope")
    assert angelegt == [konto.werte["dn"], gruppe.werte["dn"]]
    assert f"MemberOfGroup -eq ''{gruppe.werte['dn']}''" in scope.befehl
    assert erg["befehle"] == eb.befehle_text([s for s in vorschau if s.art == "exchange"])
    # Delegation tatsächlich an der angelegten Gruppe, für das angelegte Konto
    sd = eb.sd_lesen(v.objekte[gruppe.werte["dn"]]["nTSecurityDescriptor"][0])
    sid = v.objekte[konto.werte["dn"]]["objectSid"][0]
    assert eb.darf_member_schreiben(sd, sid)
    assert eb.stand()["gruppe_dn"] == gruppe.werte["dn"]


def test_vorhandenes_konto_wird_nicht_ueberschrieben(umgebung):
    v = umgebung
    erg = eb.einrichten({"konto_anlegen": True, "konto": "svc-exo-gateway"}, "a@firma.at", ADMIN_KW, "admin")
    assert not erg["ok"] and erg["code"] == "bestand"
    assert not [a for a in v.aenderungen if a[0] == "kennwort"]
    assert settings_store.get("EWS_ONPREM_KENNWORT") == "dienst-testwert"


def test_fremde_gruppe_gleichen_namens_bleibt(umgebung):
    v = umgebung
    v.gruppe(typ=-2147483646)                       # global, Sicherheit — andere Art
    erg = eb.einrichten({"konto_anlegen": False}, "a@firma.at", ADMIN_KW, "admin")
    assert not erg["ok"] and erg["code"] == "bestand" and v.aenderungen == []


# ── Bestand ───────────────────────────────────────────────────────────────────

BESTAND = json.dumps({
    "Zuweisungen": [
        {"Name": "EWS Alt", "Konto": "svc-exo-gateway", "Bereich": "Hybrid Liste",
         "Schreibbereich": "CustomRecipientScope"},
        {"Name": "Fremd", "Konto": "jemand-anders", "Bereich": "", "Schreibbereich": "Organization"}],
    "Bereiche": [{"Name": "Hybrid Liste",
                  "Filter": f"((PrimarySmtpAddress -eq '{MAX}') -or (PrimarySmtpAddress -eq '{PAUL}'))"}]})


def test_feste_liste_erkannt(umgebung):
    b = eb.bestand_erkennen(BESTAND, "svc-exo-gateway@firma.at")
    assert [z["name"] for z in b["zuweisungen"]] == ["EWS Alt"]          # fremdes Konto nicht
    z = b["zuweisungen"][0]
    assert z["art"] == "liste" and z["adressen"] == [MAX, PAUL] and b["umbau"]


def test_eigene_gruppe_kein_umbau(umgebung):
    roh = json.dumps({"Zuweisungen": [{"Name": "N", "Konto": "FIRMA\\svc-exo-gateway", "Bereich": "S"}],
                      "Bereiche": [{"Name": "S", "Filter": f"MemberOfGroup -eq '{GRUPPE_DN}'"}]})
    b = eb.bestand_erkennen(roh, "svc-exo-gateway@firma.at", GRUPPE_DN)
    assert b["zuweisungen"][0]["art"] == "unsere" and not b["umbau"]


def test_ohne_scope_ist_ganze_organisation(umgebung):
    roh = json.dumps({"Zuweisungen": {"Name": "N", "Konto": "svc-exo-gateway", "Bereich": "",
                                      "Schreibbereich": "Organization"}, "Bereiche": []})
    assert eb.bestand_erkennen(roh, "svc-exo-gateway")["zuweisungen"][0]["art"] == "organisation"


def test_entfernen_nur_nach_bestaetigung(umgebung):
    v = umgebung
    alt = eb.bestand_erkennen(BESTAND, "svc-exo-gateway@firma.at")
    ohne = eb.befehle_text(eb.plan(eb.parameter({"alt": alt}, basis=BASIS)))
    mit = eb.befehle_text(eb.plan(eb.parameter({"alt": alt, "alt_entfernen": True}, basis=BASIS)))
    assert "Remove-" not in ohne
    assert "Remove-ManagementRoleAssignment -Identity 'EWS Alt'" in mit
    assert "Remove-ManagementScope -Identity 'Hybrid Liste'" in mit
    # Übernahme: alte Adressen kommen in die Gruppe — auch ohne Bestätigung.
    erg = eb.einrichten({"alt": alt}, "a@firma.at", ADMIN_KW, "admin")
    assert erg["ok"], erg
    mitglieder = [m.decode() for m in v.objekte[GRUPPE_DN]["member"]]
    assert sorted(mitglieder) == sorted([_dn(v, MAX), _dn(v, PAUL)])
    # Die Karte zählt die Übernahme sofort, nicht erst nach dem ersten Abgleich
    # (vorher „0 Mitglieder“ direkt nach der Einrichtung).
    assert eb.uebersicht()["mitglieder"] == 2


def test_bestand_kein_json(umgebung):
    with pytest.raises(eb.BereichFehler):
        eb.bestand_erkennen("Get-ManagementScope : nicht erkannt", "svc")


# ── Sicherheitsbeschreibung ───────────────────────────────────────────────────

def test_sd_rundlauf_und_ace_vor_den_geerbten():
    roh = _sd_start()
    assert eb.sd_schreiben(eb.sd_lesen(roh)) == roh
    sd = eb.sd_lesen(roh)
    sid = _sid(4000)
    assert not eb.darf_member_schreiben(sd, sid)
    assert eb.delegation_ergaenzen(sd, sid) is True
    assert [a.geerbt for a in sd.aces] == [False, False, True]
    assert sd.aces[1].zerlegt()["objekttyp"] == eb.MEMBER_GUID
    assert sd.dacl_rev == eb.ACL_REVISION_DS
    neu = eb.sd_lesen(eb.sd_schreiben(sd))
    assert eb.darf_member_schreiben(neu, sid)
    assert eb.delegation_ergaenzen(neu, sid) is False                   # idempotent
    assert not eb.darf_member_schreiben(neu, _sid(4001))                # nur dieses Konto


def test_filterwerte_maskiert():
    assert eb._esc("a*)(mail=*") == "a\\2a\\29\\28mail=\\2a"


def test_eingaben_ohne_sonderzeichen():
    assert eb.dn_pruefen("OU=Dienste,DC=firma,DC=at", "x") == ""
    assert eb.dn_pruefen("OU=O'Brien,DC=firma,DC=at", "x")
    assert eb.name_pruefen("EXO'Gateway", "x")
    with pytest.raises(eb.BereichFehler):
        eb.ldap_server_pruefen("dc01.firma.at; rm")


def test_tls_kontext_auch_ohne_eigene_ca():
    """LDAPS nutzt den Kontext der EWS-Anbindung auch ohne „Eigene CA“. Bis
    v1.12.89 lud er `cadata=""` und scheiterte („Empty certificate data“)."""
    import ssl
    import ews_onprem
    ctx = ews_onprem._tls_kontext("")
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.cert_store_stats()["x509_ca"] > 0


# ── Kontowechsel: neues Dienstkonto erst nach bestandener Prüfung (v1.12.96) ──
# Bis v1.12.94 ersetzte „AD-Teil einrichten“ mit neuem Konto sofort
# EWS_ONPREM_KONTO/_KENNWORT — bevor Scope und Rollenzuweisung bestanden. EWS
# fiel damit für ALLE Hybrid-Nutzer aus, auch für die, die mit dem alten Konto liefen.

ALT_KONTO, ALT_KW = "svc-exo-gateway@firma.at", "dienst-testwert"
NEU_KONTO = "svc-neu@firma.at"


class FalscheEws:
    """Exchange lokal als `_senden`: merkt, mit welchen Zugangsdaten jeder
    Aufruf ging (ews_onprem._zugang), und antwortet je Konto."""

    def __init__(self):
        self.berechtigt = {ALT_KONTO}
        self.abgewiesen: set[str] = set()         # NTLM lehnt ab (401)
        self.aufrufe: list[tuple[str, str]] = []

    def __call__(self, ziel, umschlag):
        import ews_onprem
        from test_ews_onprem import LAB_ANTWORT, LAB_VERWEIGERT
        konto, kw = ews_onprem._zugang()
        self.aufrufe.append((konto, kw))
        if konto in self.abgewiesen:
            return 401, b""
        if konto in self.berechtigt:
            return 200, LAB_ANTWORT.format(start="2026-11-01T00:00:00Z",
                                           ende="2026-11-02T00:00:00Z").encode()
        return 500, LAB_VERWEIGERT.encode()


@pytest.fixture
def ews(umgebung, monkeypatch):
    import ews_onprem
    fx = FalscheEws()
    monkeypatch.setattr(ews_onprem, "_senden", fx)
    return fx


def _neu_einrichten(v):
    erg = eb.einrichten({"konto_anlegen": True, "konto": "svc-neu"}, "admin@firma.at", ADMIN_KW, "admin")
    assert erg["ok"], erg
    return erg, v.objekte[f"CN=svc-neu,{OU}"]["_kennwort"]


def _nicht_gewechselt(neu_kw):
    assert settings_store.get("EWS_ONPREM_KONTO") == ALT_KONTO
    assert settings_store.get("EWS_ONPREM_KENNWORT") == ALT_KW
    assert settings_store.get("EWS_BEREICH_NEU_KONTO") == NEU_KONTO
    assert settings_store.get("EWS_BEREICH_NEU_KENNWORT") == neu_kw


def test_neues_konto_bleibt_ausstehend_altes_wirkt_weiter(umgebung, ews):
    import ews_onprem
    erg, neu_kw = _neu_einrichten(umgebung)
    _nicht_gewechselt(neu_kw)
    assert erg["konto_ausstehend"] == NEU_KONTO and erg["zugang_gewechselt"] is False
    assert eb.ausstehend() == NEU_KONTO
    # EWS läuft weiter mit dem ALTEN Konto — der eigentliche Befund aus dem Lab.
    assert ews_onprem.verbindung_testen(MAX)["ok"]
    assert ews.aufrufe[-1] == (ALT_KONTO, ALT_KW)
    # Die Karte zeigt beide Namen — und nie ein Kennwort.
    u = eb.uebersicht()
    assert u["ausstehend"] == NEU_KONTO and u["wirksam"] == ALT_KONTO
    assert neu_kw not in json.dumps(u) and ALT_KW not in json.dumps(u)
    assert abnahme._ews_kontowechsel()["zustand"] == abnahme.OFFEN
    aufgaben.vergessen()
    assert any(a["kennung"] == "ews-kontowechsel" for a in aufgaben.offene())
    audit = [e["action"] for e in settings_store.get("GATEWAY_AUDIT_LOG")]
    assert "ews_bereich_konto_ausstehend" in audit


def test_ersteinrichtung_ohne_altes_konto_sofort_wirksam(umgebung):
    settings_store.update({"EWS_ONPREM_KONTO": "", "EWS_ONPREM_KENNWORT": ""})
    erg, neu_kw = _neu_einrichten(umgebung)
    assert erg["zugang_gewechselt"] is True and erg["konto_ausstehend"] == ""
    assert settings_store.get("EWS_ONPREM_KONTO") == NEU_KONTO
    assert settings_store.get("EWS_ONPREM_KENNWORT") == neu_kw
    assert settings_store.get("EWS_BEREICH_NEU_KONTO") == ""
    assert settings_store.get("EWS_BEREICH_NEU_KENNWORT") == ""
    assert abnahme._ews_kontowechsel()["zustand"] == abnahme.OK


def test_pruefen_mit_fehler_uebernimmt_nichts(umgebung, ews):
    import ews_onprem
    v = umgebung
    _erg, neu_kw = _neu_einrichten(v)
    eb.abgleichen("Test")                                   # MAX, ERIKA in die Gruppe (frisch)
    # Exchange-Befehle noch nicht ausgeführt: das neue Konto wird abgelehnt.
    v.angemeldet.clear()
    erg = eb.pruefen("admin")
    assert not erg["ok"] and not erg.get("zugang_gewechselt")
    _nicht_gewechselt(neu_kw)
    # Geprüft wurde MIT DEN NEUEN Zugangsdaten — LDAP und EWS.
    assert v.angemeldet and all(a == (NEU_KONTO, neu_kw) for a in v.angemeldet)
    assert ews.aufrufe == [(NEU_KONTO, neu_kw)]
    wechsel = next(p for p in erg["punkte"] if p["name"] == "Kontowechsel")
    assert wechsel["ok"] is False and ALT_KONTO in wechsel["detail"] and "EWS-Zugriff" in wechsel["detail"]
    # Die gescheiterte Probe sperrt die Anbindung des alten Kontos nicht.
    assert ews_onprem.ausfall_stand() == {}
    assert ews_onprem.verbindung_testen(MAX)["ok"] and ews.aufrufe[-1] == (ALT_KONTO, ALT_KW)


def test_abgewiesene_probe_sperrt_altes_konto_nicht(umgebung, ews):
    """401 ist ein Ausfall-Code (sperrt die Anbindung AUSFALL_PAUSE_S lang). Kommt
    er von der Probe mit dem NEUEN Konto, darf das alte nicht gesperrt werden."""
    import ews_onprem
    _erg, neu_kw = _neu_einrichten(umgebung)
    ews.abgewiesen.add(NEU_KONTO)
    t = ews_onprem.verbindung_testen(MAX, sperre_umgehen=True, zugang=(NEU_KONTO, neu_kw))
    assert t["code"] == "anmeldung"
    assert ews_onprem.ausfall_stand() == {}
    # Ohne Sperre geht der nächste gewöhnliche Aufruf wirklich ans Netz — mit dem alten Konto.
    assert ews_onprem.verbindung_testen(MAX)["ok"] and ews.aufrufe[-1] == (ALT_KONTO, ALT_KW)


def test_pruefen_mit_fehlender_delegation_uebernimmt_nichts(umgebung, ews):
    v = umgebung
    _erg, neu_kw = _neu_einrichten(v)
    eb.abgleichen("Test")
    ews.berechtigt.add(NEU_KONTO)                           # EWS ginge …
    v.objekte[GRUPPE_DN]["nTSecurityDescriptor"] = [_sd_start()]   # … Delegation fehlt
    erg = eb.pruefen("admin")
    assert not erg.get("zugang_gewechselt")
    assert next(p for p in erg["punkte"] if p["name"] == "Delegation")["ok"] is False
    _nicht_gewechselt(neu_kw)


def test_pruefen_alles_ok_uebernimmt(umgebung, ews, caplog):
    import ews_onprem
    caplog.set_level(logging.DEBUG)
    _erg, neu_kw = _neu_einrichten(umgebung)
    eb.abgleichen("Test")
    ews.berechtigt.add(NEU_KONTO)                           # Scope + Zuweisung ausgeführt
    erg = eb.pruefen("admin")
    assert erg["ok"] and erg["zugang_gewechselt"] is True, erg
    assert settings_store.get("EWS_ONPREM_KONTO") == NEU_KONTO
    assert settings_store.get("EWS_ONPREM_KENNWORT") == neu_kw
    assert settings_store.get("EWS_BEREICH_NEU_KONTO") == ""
    assert settings_store.get("EWS_BEREICH_NEU_KENNWORT") == ""
    assert eb.ausstehend() == "" and eb.stand().get("ausstehend_seit") == ""
    gewechselt = [e for e in settings_store.get("GATEWAY_AUDIT_LOG")
                  if e["action"] == "ews_bereich_konto_gewechselt"]
    assert len(gewechselt) == 1
    assert f"von {ALT_KONTO} auf {NEU_KONTO}" in gewechselt[0]["detail"]
    # Ab jetzt läuft EWS mit dem neuen Konto.
    assert ews_onprem.verbindung_testen(MAX)["ok"] and ews.aufrufe[-1] == (NEU_KONTO, neu_kw)
    # Das neue Kennwort steht in keinem Protokoll, keiner Antwort, keiner Übersicht.
    assert neu_kw not in caplog.text
    assert neu_kw not in json.dumps(erg, ensure_ascii=False)
    assert neu_kw not in json.dumps(eb.uebersicht(), ensure_ascii=False)
    assert neu_kw not in json.dumps(settings_store.get("GATEWAY_AUDIT_LOG"), ensure_ascii=False)
    assert abnahme._ews_kontowechsel()["zustand"] == abnahme.OK


def test_verwerfen_altes_bleibt(umgebung, ews):
    v = umgebung
    _erg, neu_kw = _neu_einrichten(v)
    erg = eb.verwerfen("admin")
    assert erg["ok"] and erg["konto"] == NEU_KONTO and "AD" in erg["hinweis"]
    assert neu_kw not in json.dumps(erg)
    assert settings_store.get("EWS_ONPREM_KONTO") == ALT_KONTO
    assert settings_store.get("EWS_ONPREM_KENNWORT") == ALT_KW
    assert settings_store.get("EWS_BEREICH_NEU_KONTO") == ""
    assert settings_store.get("EWS_BEREICH_NEU_KENNWORT") == ""
    assert f"CN=svc-neu,{OU}" in v.objekte                  # AD-Konto bleibt
    assert "ews_bereich_konto_verworfen" in [e["action"] for e in settings_store.get("GATEWAY_AUDIT_LOG")]
    # Auf der Platte bleibt nichts vom verworfenen Kennwort.
    assert neu_kw not in settings_store.SETTINGS_FILE.read_text("utf-8")
    assert not eb.verwerfen("admin")["ok"]                  # zweimal: nichts mehr da
    # Danach prüft LDAP wieder mit dem alten Konto.
    v.angemeldet.clear()
    eb.pruefen("admin")
    assert v.angemeldet[0] == (ALT_KONTO, ALT_KW)


def test_neues_kennwort_geheim_und_nie_gerendert(umgebung):
    from pathlib import Path
    assert "EWS_BEREICH_NEU_KENNWORT" in settings_store.SECRET_KEYS
    _neu_einrichten(umgebung)
    assert settings_store.public_view()["EWS_BEREICH_NEU_KENNWORT"] == settings_store.MASK
    seite = (Path(__file__).resolve().parent.parent / "app/webui/templates/settings.html").read_text("utf-8")
    assert "EWS_BEREICH_NEU_KENNWORT" not in seite


def test_ldaps_tls_fehler_nennt_eigene_ca(umgebung, monkeypatch, tmp_path):
    """Echter TLS-Handshake gegen ein selbstsigniertes Zertifikat (wie im Lab):
    ohne „Eigene CA“ scheitert er, und die Meldung sagt, was zu tun ist. Mit dem
    Zertifikat unter „Eigene CA“ kommt TLS durch (danach scheitert nur noch LDAP,
    weil hier kein LDAP-Server spricht)."""
    import socket
    import ssl
    import threading
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    from test_ews_onprem import _pem, _schluessel, _zert
    k = _schluessel()
    z = _zert("localhost", k, dns="localhost", grund=False)
    (tmp_path / "c.pem").write_text(_pem(z))
    (tmp_path / "k.pem").write_bytes(k.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp_path / "c.pem"), str(tmp_path / "k.pem"))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def lauf():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                t = ctx.wrap_socket(c, server_side=True)
                t.recv(10)
                t.close()
            except (OSError, ssl.SSLError):
                c.close()

    threading.Thread(target=lauf, daemon=True).start()
    monkeypatch.setattr(eb, "_verbinden", lambda b, kw: eb.Verzeichnis(b, kw))
    settings_store.update({"EWS_LDAP_SERVER": f"localhost:{srv.getsockname()[1]}", "EWS_ONPREM_CA": ""})
    try:
        with pytest.raises(eb.BereichFehler) as fehler:
            eb._verbinden("a@firma.at", "x-kennwort")
        assert fehler.value.code == "tls"
        assert "Domänencontrollers" in fehler.value.detail and "„Eigene CA“" in fehler.value.detail
        assert "Exchange" not in fehler.value.detail
        settings_store.update({"EWS_ONPREM_CA": _pem(z)})
        with pytest.raises(eb.BereichFehler) as fehler:
            eb._verbinden("a@firma.at", "x-kennwort")
        assert fehler.value.code != "tls"
    finally:
        srv.close()


def test_karte_nennt_ca_schritt_und_ausstehendes_konto():
    from pathlib import Path
    seite = (Path(__file__).resolve().parent.parent / "app/webui/templates/settings.html").read_text("utf-8")
    vor_einrichten = seite[seite.index('id="ews-bereich"'):seite.index('id="ews-bereich-einrichten-btn"')]
    assert "„Eigene CA“" in vor_einrichten and "Domänencontrollers" in vor_einrichten
    assert 'id="ews-bereich-verwerfen-btn"' in seite and "/api/ews-bereich/verwerfen" in seite
    assert "wartet auf die Exchange-Befehle und „Prüfen“" in seite


def test_nicht_aufloesbarer_dc_name_meldet_dns(monkeypatch):
    """ldap3 sagt nur „invalid server address“; die Meldung muss DNS und den
    Ausweg (IP eintragen) nennen. Eine IP überspringt die Prüfung."""
    import socket
    import settings_store
    monkeypatch.setattr(eb, "_verbinden", lambda b, kw: eb.Verzeichnis(b, kw))
    settings_store.update({"EWS_LDAP_SERVER": "dc.gibt-es-nicht.invalid"})
    with pytest.raises(eb.BereichFehler) as fehler:
        eb._verbinden("a@firma.at", "x-kennwort")
    assert fehler.value.code == "dns"
    assert "dc.gibt-es-nicht.invalid" in fehler.value.detail and "IP-Adresse" in fehler.value.detail
    aufgerufen = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: aufgerufen.append(a))
    eb._namen_aufloesen("10.99.0.5", 636)
    assert aufgerufen == []


def test_befehle_vorschau_ohne_basis_liest_rootdse(umgebung, monkeypatch):
    """Basis-DN ist optional („leer = aus dem Verzeichnis“); die Vorschau vor
    der Einrichtung meldete trotzdem „Basis-DN fehlt“."""
    from fastapi.testclient import TestClient
    from webui import deps
    from webui.app import app
    settings_store.update({"EWS_LDAP_BASIS": "", "EWS_BEREICH_OU": ""})
    monkeypatch.setattr(eb, "basis_aus_verzeichnis", lambda: BASIS)
    app.dependency_overrides[deps._require_admin] = lambda: "admin"
    try:
        r = TestClient(app).post("/api/ews-bereich/befehle", json={}).json()
    finally:
        app.dependency_overrides.pop(deps._require_admin, None)
    assert r["ok"], r
    assert f"CN=Users,{BASIS}" in r["text"]


def test_kontowechsel_fuellt_die_gruppe_mit_dem_neuen_konto(umgebung, ews):
    """Lab: Nach den Exchange-Befehlen scheiterte „Prüfen“ an „es fehlen 9“,
    weil die Gruppe erst der Abgleich füllt — bei ausgeschaltetem Schalter nie.
    Jetzt nimmt das ausstehende Konto die fehlenden selbst auf (nur hinzu)."""
    v = umgebung
    _erg, neu_kw = _neu_einrichten(v)
    assert not eb.stand().get("mitglieder")            # Gruppe leer, kein Abgleich
    fremd = f"CN=Fremd,{OU}"
    v.objekte[GRUPPE_DN].setdefault("member", []).append(fremd.encode())
    ews.berechtigt.add(NEU_KONTO)
    erg = eb.pruefen("admin")
    namen = {p["name"]: p for p in erg["punkte"]}
    assert "Gruppe aufgefüllt" in namen, erg
    mitglieder = [m.decode() for m in v.objekte[GRUPPE_DN]["member"]]
    assert _dn(v, MAX) in mitglieder and _dn(v, ERIKA) in mitglieder
    assert fremd in mitglieder                           # nur hinzu, nichts entfernt
    # Mit dem NEUEN Konto geschrieben, nicht mit dem Admin
    assert v.angemeldet[-1][0] == NEU_KONTO or any(b == NEU_KONTO for b, _k in v.angemeldet)


def test_wartezeit_endet_beim_ersten_erfolgreichen_zugriff(umgebung, ews):
    import ews_onprem
    _eingerichtet(umgebung)
    eb.abgleichen("Test")
    assert eb.wartet(MAX)
    assert ews_onprem.verbindung_testen(MAX)["ok"]
    assert not eb.wartet(MAX)
    # Ein gescheiterter Zugriff beendet sie nicht.
    eb._stand_setzen(wartet={ERIKA: eb._iso()})
    ews.berechtigt.clear()
    assert not ews_onprem.verbindung_testen(ERIKA, sperre_umgehen=True)["ok"]
    assert eb.wartet(ERIKA)
