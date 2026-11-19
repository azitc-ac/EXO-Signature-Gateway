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
    neu = settings_store.get("EWS_ONPREM_KENNWORT")
    assert len(neu) >= 32 and neu == v.objekte[f"CN=svc-neu,{OU}"]["_kennwort"]
    assert "EWS_ONPREM_KENNWORT" in settings_store.SECRET_KEYS
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
