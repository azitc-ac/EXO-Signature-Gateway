"""Widerrufsprüfung über Sperrlisten (CRL) — Stufe 2 der Zertifikatsprüfung.

Stufe 1 (`test_cert_gueltigkeit.py`) prüft Ablauf und Lesbarkeit und braucht
kein Netz. Hier kommt der Teil, der beim Trustcenter nachfragt — und damit die
Frage, was gilt, wenn das Trustcenter schweigt.

DIE ENTSCHEIDUNG DAHINTER
-------------------------
Nicht erreichbar → **Portal statt S/MIME**. Hart abweisen hielte den Versand
bei einer fremden Störung an, Durchwinken machte die Prüfung wertlos. Weil das
Gateway einen sicheren Ersatzweg hat, kostet Vorsicht nur das Verfahren, nicht
die Zustellung.

⚠️ Davon zu unterscheiden: ein Zertifikat, das gar keinen Verteilungspunkt
nennt. Das ist eine Eigenschaft des Zertifikats, keine Störung — es wird
durchgelassen und gezählt.

Zertifikate UND Sperrlisten werden hier echt erzeugt (`cryptography`), nicht
nachgebildet. Ein Test, der seine Eingabe selbst erfindet, prüft die eigene
Annahme über ein fremdes Format.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import crl_check

JETZT = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
CRL_URL = "http://trustcenter.invalid/ca.crl"


@pytest.fixture
def ca():
    """Eine echte CA, mit der Zertifikate und Sperrlisten signiert werden."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test-CA")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(JETZT - timedelta(days=365))
            .not_valid_after(JETZT + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return {"key": key, "name": name, "cert": cert}


def _empfaenger(tmp_path, ca, dateiname: str, mit_crl: bool = True, url: str = CRL_URL):
    """Ein echtes Empfängerzertifikat, wahlweise mit CRL-Verteilungspunkt.

    ⚠️ `url` ist wichtiger, als es aussieht: Zwei Zertifikate mit DERSELBEN
    Adresse teilen sich den Zwischenspeicher. Wer im zweiten Teil eines Tests
    eine Störung nachstellen will, braucht eine andere Adresse — sonst wird die
    Sperrliste gar nicht erst abgerufen und die Störung bleibt wirkungslos.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dateiname)]))
         .issuer_name(ca["name"])
         .public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(JETZT - timedelta(days=1))
         .not_valid_after(JETZT + timedelta(days=365)))
    if mit_crl:
        b = b.add_extension(x509.CRLDistributionPoints([
            x509.DistributionPoint(
                full_name=[x509.UniformResourceIdentifier(url)],
                relative_name=None, reasons=None, crl_issuer=None)]), critical=False)
    cert = b.sign(ca["key"], hashes.SHA256())
    p = tmp_path / f"{dateiname}.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return p, cert


def _sperrliste(ca, widerrufen=(), naechste=None, jetzt=JETZT):
    """Eine echte CRL, die die übergebenen Seriennummern führt."""
    b = (x509.CertificateRevocationListBuilder()
         .issuer_name(ca["name"])
         .last_update(jetzt - timedelta(hours=1))
         .next_update(naechste or (jetzt + timedelta(days=7))))
    for seriennummer in widerrufen:
        b = b.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(seriennummer)
            .revocation_date(jetzt - timedelta(days=2))
            .build())
    return b.sign(ca["key"], hashes.SHA256())


@pytest.fixture(autouse=True)
def cache_ins_wegwerfverzeichnis(tmp_path, monkeypatch):
    """Niemals in das echte Datenverzeichnis schreiben."""
    monkeypatch.setattr(crl_check.config, "DATA_DIR", str(tmp_path / "daten"))


@pytest.fixture
def netz(monkeypatch):
    """Der Abruf wird ersetzt — was ausgeliefert wird, ist eine ECHTE CRL.

    Ersetzt wird also nur der Transportweg, nicht das geprüfte Format.
    """
    zustand = {"antwort": None, "abrufe": 0}

    def abrufen(url: str):
        # Ersetzt wird NUR der Transport. Ausgeliefert wird eine echte,
        # signierte CRL — Format und Auswertung bleiben ungetäuscht. Und der
        # Zwischenspeicher bleibt scharf: Er sitzt eine Ebene höher.
        zustand["abrufe"] += 1
        return zustand["antwort"]

    monkeypatch.setattr(crl_check, "_abrufen", abrufen)
    return zustand


def _als_der(crl):
    return crl.public_bytes(serialization.Encoding.DER)


# ── Der Kern: widerrufen oder nicht ──────────────────────────────────────────

def test_gueltiges_zertifikat_darf_verschluesseln(tmp_path, ca, netz):
    pfad, cert = _empfaenger(tmp_path, ca, "gut")
    netz["antwort"] = _als_der(_sperrliste(ca, widerrufen=[]))
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert ok and grund == "", grund


def test_widerrufenes_zertifikat_wird_abgelehnt(tmp_path, ca, netz):
    pfad, cert = _empfaenger(tmp_path, ca, "gesperrt")
    netz["antwort"] = _als_der(_sperrliste(ca, widerrufen=[cert.serial_number]))
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert not ok
    assert "widerrufen" in grund and "14.08.2026" in grund, grund


def test_fremder_widerruf_trifft_nicht_das_eigene_zertifikat(tmp_path, ca, netz):
    """Eine Sperrliste mit vielen Einträgen darf nicht pauschal sperren."""
    pfad, cert = _empfaenger(tmp_path, ca, "unbeteiligt")
    netz["antwort"] = _als_der(_sperrliste(ca, widerrufen=[cert.serial_number + 1,
                                                          cert.serial_number + 2]))
    ok, _ = crl_check.widerruf_geprueft(pfad, JETZT)
    assert ok


# ── Die Entscheidung: Störung führt zum Portal ───────────────────────────────

def test_nicht_erreichbare_sperrliste_verhindert_verschluesselung(tmp_path, ca, netz):
    """Der Kern der Entscheidung vom 13.08.2026: im Zweifel Portal."""
    pfad, _ = _empfaenger(tmp_path, ca, "unerreichbar")
    netz["antwort"] = None          # Trustcenter schweigt
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert not ok
    assert "nicht erreichbar" in grund


def test_unbrauchbare_antwort_gilt_wie_keine(tmp_path, ca, netz, monkeypatch):
    """Eine Fehlerseite statt einer Sperrliste darf nicht als „geprüft" gelten."""
    pfad, _ = _empfaenger(tmp_path, ca, "muell")
    monkeypatch.setattr(crl_check, "_abrufen", lambda url: b"<html>404</html>")
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert not ok and "nicht erreichbar" in grund


def test_zertifikat_ohne_verteilungspunkt_wird_durchgelassen(tmp_path, ca, netz):
    """Eigenschaft des Zertifikats, keine Störung — aber mit Vermerk."""
    pfad, _ = _empfaenger(tmp_path, ca, "ohne-crl", mit_crl=False)
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert ok
    assert "ohne Sperrlisten-Adresse" in grund
    assert netz["abrufe"] == 0, "ohne Verteilungspunkt darf gar nichts abgerufen werden"


def test_unlesbares_zertifikat_wird_abgelehnt(tmp_path, netz):
    p = tmp_path / "kaputt.pem"
    p.write_bytes(b"kein zertifikat")
    ok, grund = crl_check.widerruf_geprueft(p, JETZT)
    assert not ok and "nicht lesbar" in grund


# ── Zwischenspeicher ─────────────────────────────────────────────────────────

def test_zweite_abfrage_kommt_aus_dem_zwischenspeicher(tmp_path, ca, netz):
    """Sonst zahlte jede Nachricht den Abruf — der Grund, CRL statt OCSP zu nehmen."""
    pfad, _ = _empfaenger(tmp_path, ca, "cache")
    netz["antwort"] = _als_der(_sperrliste(ca))
    crl_check.widerruf_geprueft(pfad, JETZT)
    crl_check.widerruf_geprueft(pfad, JETZT)
    crl_check.widerruf_geprueft(pfad, JETZT)
    assert netz["abrufe"] == 1, f"{netz['abrufe']} Abrufe statt einem"


def test_ueberfaellige_sperrliste_wird_neu_geholt(tmp_path, ca, netz):
    """`nextUpdate` ist die Zusage der CA, bis wann die Liste gilt.

    Wer sie danach weiterbenutzt, prüft gegen einen Stand, in dem der Widerruf
    von gestern noch fehlt.
    """
    pfad, _ = _empfaenger(tmp_path, ca, "alt")
    netz["antwort"] = _als_der(_sperrliste(ca, naechste=JETZT + timedelta(hours=1)))
    crl_check.widerruf_geprueft(pfad, JETZT)
    assert netz["abrufe"] == 1
    crl_check.widerruf_geprueft(pfad, JETZT + timedelta(hours=2))
    assert netz["abrufe"] == 2, "überfällige Sperrliste wurde weiterbenutzt"


def test_ueberfaellig_und_unerreichbar_fuehrt_zum_portal(tmp_path, ca, netz):
    """Der gefährliche Fall: alter Stand vorhanden, Trustcenter weg.

    Den alten Stand weiterzubenutzen wäre bequem und falsch — genau darin
    verstecken sich frische Widerrufe.
    """
    pfad, _ = _empfaenger(tmp_path, ca, "alt2")
    netz["antwort"] = _als_der(_sperrliste(ca, naechste=JETZT + timedelta(hours=1)))
    assert crl_check.widerruf_geprueft(pfad, JETZT)[0]
    netz["antwort"] = None
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT + timedelta(hours=2))
    assert not ok and "nicht erreichbar" in grund


# ── Adressen aus dem Zertifikat ──────────────────────────────────────────────

def test_ldap_verteilungspunkte_werden_uebergangen(tmp_path, ca):
    """Das Gateway spricht kein LDAP — ein Punkt, den man nicht abrufen kann,
    ist wie keiner."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ldap")]))
            .issuer_name(ca["name"]).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(JETZT - timedelta(days=1))
            .not_valid_after(JETZT + timedelta(days=10))
            .add_extension(x509.CRLDistributionPoints([
                x509.DistributionPoint(
                    full_name=[x509.UniformResourceIdentifier("ldap://verzeichnis.invalid/cn=ca")],
                    relative_name=None, reasons=None, crl_issuer=None)]), critical=False)
            .sign(ca["key"], hashes.SHA256()))
    assert crl_check.crl_adressen(cert) == []


def test_vorwaermen_holt_jede_adresse_nur_einmal(tmp_path, ca, netz):
    """Im Tageslauf teilen sich viele Empfänger dieselbe CA."""
    netz["antwort"] = _als_der(_sperrliste(ca))
    pfade = [_empfaenger(tmp_path, ca, f"e{i}")[0] for i in range(5)]
    ergebnis = crl_check.vorwaermen(pfade)
    assert ergebnis == {"adressen": 1, "geholt": 1, "fehlgeschlagen": 0}
    assert netz["abrufe"] == 1


# ── Der Verschlüsselungspfad selbst ──────────────────────────────────────────
#
# ⚠️ Der Modultest oben zeigt nur, dass die Prüfung richtig antwortet. Ob sie im
# Versandweg überhaupt GEFRAGT wird, ist eine andere Sache — und genau daran ist
# die Ablaufprüfung jahrelang vorbeigelaufen: Die Ablaufdaten wurden berechnet,
# aber der Verschlüsselungspfad sah nie hin.

def test_encrypt_lehnt_widerrufenes_zertifikat_ab(tmp_path, ca, netz, monkeypatch):
    """Der Empfänger muss als »fehlend« zurückkommen — nur dann greift im
    Aufrufer der Portal-Weg."""
    import smime_encrypt
    pfad, cert = _empfaenger(tmp_path, ca, "gesperrt-e2e")
    netz["antwort"] = _als_der(_sperrliste(ca, widerrufen=[cert.serial_number]))
    monkeypatch.setattr(smime_encrypt.smime_store, "get_recipient_cert_path", lambda r: pfad)
    daten, fehlend = smime_encrypt.encrypt(b"From: a@b.de\r\n\r\nHallo\r\n",
                                           ["partner@example.org"])
    assert daten is None, "es wurde an ein widerrufenes Zertifikat verschlüsselt"
    assert fehlend == ["partner@example.org"]


def test_encrypt_lehnt_bei_unerreichbarer_sperrliste_ab(tmp_path, ca, netz, monkeypatch):
    """Die Entscheidung vom 13.08.2026, am echten Weg geprüft."""
    import smime_encrypt
    pfad, _ = _empfaenger(tmp_path, ca, "unerreichbar-e2e")
    netz["antwort"] = None
    monkeypatch.setattr(smime_encrypt.smime_store, "get_recipient_cert_path", lambda r: pfad)
    daten, fehlend = smime_encrypt.encrypt(b"From: a@b.de\r\n\r\nHallo\r\n",
                                           ["partner@example.org"])
    assert daten is None and fehlend == ["partner@example.org"]


def test_encrypt_zaehlt_widerruf_und_stoerung_getrennt(tmp_path, ca, netz, monkeypatch):
    """Beide führen zum Portal, verlangen aber verschiedene Handlungen: das eine
    ein neues Zertifikat vom Partner, das andere einen Blick auf die Firewall."""
    import stats, smime_encrypt
    pfad, cert = _empfaenger(tmp_path, ca, "zaehler")
    monkeypatch.setattr(smime_encrypt.smime_store, "get_recipient_cert_path", lambda r: pfad)

    netz["antwort"] = _als_der(_sperrliste(ca, widerrufen=[cert.serial_number]))
    vorher_w = stats.get().get("cert_widerrufen", 0)
    smime_encrypt.encrypt(b"From: a@b.de\r\n\r\nx\r\n", ["p@example.org"])
    assert stats.get().get("cert_widerrufen", 0) == vorher_w + 1

    # Andere Adresse: sonst antwortet der Zwischenspeicher und die Stoerung
    # kommt gar nicht zum Tragen.
    pfad2, _ = _empfaenger(tmp_path, ca, "zaehler2",
                           url="http://zweites-trustcenter.invalid/ca.crl")
    monkeypatch.setattr(smime_encrypt.smime_store, "get_recipient_cert_path", lambda r: pfad2)
    netz["antwort"] = None
    vorher_s = stats.get().get("cert_crl_unerreichbar", 0)
    smime_encrypt.encrypt(b"From: a@b.de\r\n\r\nx\r\n", ["p@example.org"])
    assert stats.get().get("cert_crl_unerreichbar", 0) == vorher_s + 1


def test_abgeschaltete_pruefung_laesst_durch(tmp_path, ca, netz, monkeypatch):
    """`CRL_CHECK=False` ist für Umgebungen ohne ausgehendes HTTP gedacht.

    Dann wird der Widerruf NICHT geprüft — und keine Sperrliste abgerufen.
    """
    import smime_encrypt
    pfad, cert = _empfaenger(tmp_path, ca, "abgeschaltet")
    netz["antwort"] = _als_der(_sperrliste(ca, widerrufen=[cert.serial_number]))
    monkeypatch.setattr(smime_encrypt.smime_store, "get_recipient_cert_path", lambda r: pfad)
    import settings_store
    echt = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: False if k == "CRL_CHECK" else echt(k, *a, **kw))
    vorher = netz["abrufe"]
    smime_encrypt.encrypt(b"From: a@b.de\r\n\r\nx\r\n", ["p@example.org"])
    assert netz["abrufe"] == vorher, "trotz abgeschalteter Prüfung wurde abgerufen"


def test_zaehler_sind_deklariert():
    import stats
    for k in ("cert_widerrufen", "cert_crl_unerreichbar", "cert_ohne_crl"):
        assert k in stats.KEYS, k


def test_tagesbericht_zeigt_alle_drei_faelle():
    from pathlib import Path
    quelle = (Path(__file__).resolve().parent.parent / "app" / "notification.py").read_text()
    for k in ("cert_widerrufen", "cert_crl_unerreichbar", "cert_ohne_crl"):
        assert f'dval("{k}")' in quelle, f"{k} erscheint nicht im Tagesbericht"


# ── Zwei Ebenen des Zwischenspeichers ────────────────────────────────────────

@pytest.fixture(autouse=True)
def speicher_leeren():
    """Die Speicher-Zwischenlager leben im Modul und überdauern sonst jeden Test.

    ⚠️ Auch das der Ausstellerzertifikate: Es ist nach ADRESSE geschlüsselt, und
    die Testfälle benutzen dieselbe. Ohne Leeren erbte ein Test das
    Ausstellerzertifikat des vorigen — und schlug mit „hat das
    Empfängerzertifikat nicht ausgestellt" fehl, was wie ein Fehler in der
    Prüfung aussieht und keiner ist.
    """
    crl_check._im_speicher.clear()
    crl_check._groessen.clear()
    crl_check._ca_im_speicher.clear()
    yield
    crl_check._im_speicher.clear()
    crl_check._groessen.clear()
    crl_check._ca_im_speicher.clear()


def test_zweiter_zugriff_kommt_ohne_dateizugriff_aus(tmp_path, ca, netz, monkeypatch):
    """Die Datei erspart den Netzabruf, nicht das Auswerten — 127 ms bei der
    grössten gemessenen Sperrliste, und zwar bei JEDER Nachricht."""
    pfad, _ = _empfaenger(tmp_path, ca, "speicher")
    netz["antwort"] = _als_der(_sperrliste(ca))
    crl_check.widerruf_geprueft(pfad, JETZT)

    gelesen = {"n": 0}
    echt = crl_check._crl_laden
    monkeypatch.setattr(crl_check, "_crl_laden",
                        lambda roh: (gelesen.__setitem__("n", gelesen["n"] + 1), echt(roh))[1])
    crl_check.widerruf_geprueft(pfad, JETZT)
    assert gelesen["n"] == 0, "die Sperrliste wurde erneut zerlegt"


def test_speicher_gibt_ueberfaellige_liste_nicht_weiter(tmp_path, ca, netz):
    """Sonst überlebte ein veralteter Stand im Arbeitsspeicher jede Auffrischung."""
    pfad, _ = _empfaenger(tmp_path, ca, "speicher-alt")
    netz["antwort"] = _als_der(_sperrliste(ca, naechste=JETZT + timedelta(hours=1)))
    crl_check.widerruf_geprueft(pfad, JETZT)
    assert netz["abrufe"] == 1
    crl_check.widerruf_geprueft(pfad, JETZT + timedelta(hours=2))
    assert netz["abrufe"] == 2, "überfällige Liste kam aus dem Arbeitsspeicher"


def test_speicher_waechst_nicht_unbegrenzt(tmp_path, ca, netz, monkeypatch):
    """⚠️ Begrenzt wird der PLATZ, nicht die Anzahl.

    Die erste Fassung zählte sechs Einträge — in der Annahme, sie seien klein.
    Gemessen am echten Verkehr: SwissSign liefert 24,3 MB, geparst rund 32 MB
    Arbeitsspeicher. Sechs davon wären fast 200 MB auf einem Kleinrechner.
    """
    netz["antwort"] = _als_der(_sperrliste(ca))
    # Jede Liste zählt als 20 MB — nach vier ist das Budget erschöpft.
    monkeypatch.setattr(crl_check, "_SPEICHER_BUDGET", 64 * 1024 * 1024)
    for i in range(8):
        crl_check.sperrliste(f"http://ca{i}.invalid/x.crl", JETZT)
        crl_check._groessen[f"http://ca{i}.invalid/x.crl"] = 20 * 1024 * 1024
    crl_check._merken("http://letzte.invalid/x.crl",
                      _sperrliste(ca), 20 * 1024 * 1024)
    belegt = sum(crl_check._groessen.values())
    assert belegt <= crl_check._SPEICHER_BUDGET, f"{belegt/1024/1024:.0f} MB im Speicher"
    assert "http://letzte.invalid/x.crl" in crl_check._im_speicher, \
        "die zuletzt gebrauchte Liste muss bleiben"


def test_eine_einzige_riesige_liste_bleibt_trotzdem(ca, monkeypatch):
    """Auch wenn sie allein das Budget sprengt: Sie gerade wieder zu verwerfen
    hiesse, sie bei jeder Nachricht neu zu laden."""
    monkeypatch.setattr(crl_check, "_SPEICHER_BUDGET", 1024)
    crl_check._merken("http://riesig.invalid/x.crl", _sperrliste(ca), 99 * 1024 * 1024)
    assert "http://riesig.invalid/x.crl" in crl_check._im_speicher


def test_groessengrenze_deckt_echte_sperrlisten_ab():
    """SwissSign liefert 24,3 MB (gemessen im Produktivbetrieb, 16.08.2026).
    Mit der ursprünglichen Grenze von 20 MB galten zwei Empfängerzertifikate
    als nicht prüfbar — und an sie ging Portal statt Verschlüsselung."""
    assert crl_check.MAX_GROESSE >= 32 * 1024 * 1024


# ── Gehört die Sperrliste zu diesem Zertifikat? ──────────────────────────────

def test_fremde_sperrliste_wird_verworfen(tmp_path, netz, ca):
    """Ohne diesen Abgleich genügte IRGENDEINE gültige Liste.

    Eine leere Liste einer fremden CA erklärte sonst jedes Zertifikat für
    unwiderrufen — der billigste Weg, die ganze Prüfung auszuhebeln.
    """
    fremde_ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fremde = {"key": fremde_ca_key,
              "name": x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fremde-CA")])}
    pfad, cert = _empfaenger(tmp_path, ca, "unterschoben")
    # Echte, gültige Liste — nur eben von der falschen Stelle.
    netz["antwort"] = _als_der(_sperrliste(fremde, widerrufen=[]))
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert not ok, "eine fremde Sperrliste wurde als Auskunft akzeptiert"
    assert "nicht erreichbar" in grund


def test_eigene_sperrliste_wird_akzeptiert(tmp_path, netz, ca):
    """Gegenprobe — sonst wäre der Abgleich nur eine Bremse."""
    pfad, _ = _empfaenger(tmp_path, ca, "eigene")
    netz["antwort"] = _als_der(_sperrliste(ca, widerrufen=[]))
    assert crl_check.widerruf_geprueft(pfad, JETZT)[0]


def test_indirekte_sperrliste_darf_von_anderer_stelle_kommen(tmp_path, netz, ca):
    """RFC 5280 §5.2.6: Nennt der Verteilungspunkt einen eigenen `crl_issuer`,
    führt eine ANDERE Stelle die Widerrufe. Dann ist Gleichheit falsch."""
    andere_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    andere_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Indirekte-CRL-Stelle")])
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "indirekt")]))
            .issuer_name(ca["name"]).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(JETZT - timedelta(days=1))
            .not_valid_after(JETZT + timedelta(days=10))
            .add_extension(x509.CRLDistributionPoints([
                x509.DistributionPoint(
                    full_name=[x509.UniformResourceIdentifier("http://indirekt.invalid/x.crl")],
                    relative_name=None, reasons=None,
                    crl_issuer=[x509.DirectoryName(andere_name)])]), critical=False)
            .sign(ca["key"], hashes.SHA256()))
    p = tmp_path / "indirekt.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    netz["antwort"] = _als_der(_sperrliste({"key": andere_key, "name": andere_name}))
    ok, grund = crl_check.widerruf_geprueft(p, JETZT)
    assert ok, f"indirekte Sperrliste wurde abgelehnt: {grund}"


# ── Signatur der Sperrliste ──────────────────────────────────────────────────
#
# Der Aussteller-Abgleich oben prüft nur den NAMEN. Ein Angreifer, der eine
# Sperrliste unterschiebt, schreibt aber selbstverständlich den richtigen Namen
# hinein — den kann er abschreiben. Was er nicht kann: sie mit dem Schlüssel der
# echten CA signieren.

def _mit_aia(tmp_path, ca, dateiname, crl_url=CRL_URL, aia_url="http://ca.invalid/ca.crt"):
    """Empfängerzertifikat, das die Adresse seines Ausstellers nennt."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.x509.oid import AuthorityInformationAccessOID
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dateiname)]))
            .issuer_name(ca["name"]).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(JETZT - timedelta(days=1))
            .not_valid_after(JETZT + timedelta(days=365))
            .add_extension(x509.CRLDistributionPoints([
                x509.DistributionPoint(full_name=[x509.UniformResourceIdentifier(crl_url)],
                                       relative_name=None, reasons=None, crl_issuer=None)]),
                critical=False)
            .add_extension(x509.AuthorityInformationAccess([
                x509.AccessDescription(AuthorityInformationAccessOID.CA_ISSUERS,
                                       x509.UniformResourceIdentifier(aia_url))]),
                critical=False)
            .sign(ca["key"], hashes.SHA256()))
    p = tmp_path / f"{dateiname}.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return p, cert


@pytest.fixture
def netz_mehrteilig(monkeypatch):
    """Antworten je Adresse — Sperrliste UND Ausstellerzertifikat."""
    antworten: dict[str, bytes | None] = {}

    def abrufen(url: str):
        return antworten.get(url)

    monkeypatch.setattr(crl_check, "_abrufen", abrufen)
    return antworten


def _als_der_zert(cert):
    return cert.public_bytes(serialization.Encoding.DER)


def test_echte_sperrliste_besteht_die_signaturpruefung(tmp_path, ca, netz_mehrteilig):
    pfad, _ = _mit_aia(tmp_path, ca, "echt")
    netz_mehrteilig[CRL_URL] = _als_der(_sperrliste(ca))
    netz_mehrteilig["http://ca.invalid/ca.crt"] = _als_der_zert(ca["cert"])
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert ok, grund


def test_untergeschobene_liste_mit_richtigem_namen_wird_erkannt(tmp_path, ca, netz_mehrteilig):
    """⚠️ DER Fall, für den die Signaturprüfung da ist.

    Die falsche Liste trägt denselben Aussteller-Namen wie die echte — der
    Namensabgleich greift also NICHT. Nur die Signatur verrät sie.
    """
    boese_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    boese = {"key": boese_key, "name": ca["name"]}      # gleicher Name, fremder Schlüssel
    pfad, cert = _mit_aia(tmp_path, ca, "untergeschoben")
    # Der Angreifer verschweigt den Widerruf, den die echte Liste führt.
    netz_mehrteilig[CRL_URL] = _als_der(_sperrliste(boese, widerrufen=[]))
    netz_mehrteilig["http://ca.invalid/ca.crt"] = _als_der_zert(ca["cert"])
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert not ok, "eine fremd signierte Sperrliste wurde als Auskunft akzeptiert"
    assert "nicht erreichbar" in grund


def test_untergeschobenes_ausstellerzertifikat_wird_erkannt(tmp_path, ca, netz_mehrteilig):
    """Auch das Ausstellerzertifikat kommt über HTTP. Es wird nur benutzt, wenn
    es das Empfängerzertifikat tatsächlich ausgestellt hat."""
    fremd_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fremd_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test-CA")])
    fremd = (x509.CertificateBuilder()
             .subject_name(fremd_name).issuer_name(fremd_name)
             .public_key(fremd_key.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(JETZT - timedelta(days=10))
             .not_valid_after(JETZT + timedelta(days=10))
             .sign(fremd_key, hashes.SHA256()))
    pfad, _ = _mit_aia(tmp_path, ca, "fremde-ca")
    netz_mehrteilig[CRL_URL] = _als_der(_sperrliste({"key": fremd_key, "name": fremd_name}))
    netz_mehrteilig["http://ca.invalid/ca.crt"] = _als_der_zert(fremd)
    ok, _ = crl_check.widerruf_geprueft(pfad, JETZT)
    assert not ok


def test_unerreichbarer_aussteller_verwirft_die_liste(tmp_path, ca, netz_mehrteilig):
    """Sonst genügte es, den Abruf des Ausstellers zu blockieren, um eine
    untergeschobene Liste durchzubringen."""
    pfad, _ = _mit_aia(tmp_path, ca, "aussteller-weg")
    netz_mehrteilig[CRL_URL] = _als_der(_sperrliste(ca))
    netz_mehrteilig["http://ca.invalid/ca.crt"] = None
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert not ok


def test_ohne_ausstelleradresse_bleibt_es_bei_der_bisherigen_pruefung(tmp_path, ca, netz):
    """Kein AIA ist eine Eigenschaft des Zertifikats, keine Störung — sonst
    fielen alle Zertifikate ohne diese Erweiterung aus der Verschlüsselung."""
    pfad, _ = _empfaenger(tmp_path, ca, "ohne-aia")
    netz["antwort"] = _als_der(_sperrliste(ca))
    assert crl_check.widerruf_geprueft(pfad, JETZT)[0]


def test_widerruf_wird_auch_mit_signaturpruefung_gefunden(tmp_path, ca, netz_mehrteilig):
    """Gegenprobe: Die Verschärfung darf den eigentlichen Zweck nicht verdecken."""
    pfad, cert = _mit_aia(tmp_path, ca, "gesperrt-mit-aia")
    netz_mehrteilig[CRL_URL] = _als_der(_sperrliste(ca, widerrufen=[cert.serial_number]))
    netz_mehrteilig["http://ca.invalid/ca.crt"] = _als_der_zert(ca["cert"])
    ok, grund = crl_check.widerruf_geprueft(pfad, JETZT)
    assert not ok and "widerrufen" in grund


def test_ausstellerzertifikat_wird_nur_einmal_geholt(tmp_path, ca, netz_mehrteilig, monkeypatch):
    """Ohne diesen Zwischenspeicher zahlt JEDE Nachricht den Abruf — am
    laufenden Container 1.688 ms, obwohl die Sperrliste längst im Speicher lag."""
    crl_check._ca_im_speicher.clear()
    abrufe = {"n": 0}
    echt = crl_check._abrufen
    monkeypatch.setattr(crl_check, "_abrufen",
                        lambda url: (abrufe.__setitem__("n", abrufe["n"] + 1), echt(url))[1])
    pfad, _ = _mit_aia(tmp_path, ca, "ca-cache")
    netz_mehrteilig[CRL_URL] = _als_der(_sperrliste(ca))
    netz_mehrteilig["http://ca.invalid/ca.crt"] = _als_der_zert(ca["cert"])
    for _ in range(3):
        assert crl_check.widerruf_geprueft(pfad, JETZT)[0]
    assert abrufe["n"] == 2, f"{abrufe['n']} Abrufe statt zwei (Sperrliste + Aussteller, je einmal)"


def test_abgelaufenes_ausstellerzertifikat_wird_nicht_weiterbenutzt(ca):
    """Die CA hat dann längst ein neues — und die Sperrliste stammt von diesem."""
    crl_check._ca_im_speicher.clear()
    alt_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Alte-CA")])
    abgelaufen = (x509.CertificateBuilder()
                  .subject_name(name).issuer_name(name)
                  .public_key(alt_key.public_key())
                  .serial_number(x509.random_serial_number())
                  .not_valid_before(JETZT - timedelta(days=800))
                  .not_valid_after(JETZT - timedelta(days=10))
                  .sign(alt_key, hashes.SHA256()))
    crl_check._ca_merken("http://alt.invalid/ca.crt", abgelaufen)
    assert crl_check._ca_aus_speicher("http://alt.invalid/ca.crt") is None


def test_signatur_wird_nicht_bei_jeder_nachricht_neu_geprueft(tmp_path, ca, netz_mehrteilig, monkeypatch):
    """Die Signatur deckt die GANZE Liste — sie zu prüfen heisst, mehrere
    Megabyte zu hashen. Am laufenden Container 1.275 ms je Nachricht, obwohl
    Liste und Ausstellerzertifikat längst im Speicher lagen."""
    crl_check._signatur_ok.clear()
    pruefungen = {"n": 0}
    echte_crl = _sperrliste(ca)

    class Zaehlend:
        """Umhüllt die echte Sperrliste und zählt die Signaturprüfungen."""
        def __init__(self, crl): self._crl = crl
        def __getattr__(self, name): return getattr(self._crl, name)
        def is_signature_valid(self, key):
            pruefungen["n"] += 1
            return self._crl.is_signature_valid(key)

    monkeypatch.setattr(crl_check, "_crl_laden", lambda roh: Zaehlend(echte_crl))
    pfad, _ = _mit_aia(tmp_path, ca, "sig-cache")
    netz_mehrteilig[CRL_URL] = _als_der(echte_crl)
    netz_mehrteilig["http://ca.invalid/ca.crt"] = _als_der_zert(ca["cert"])
    for _ in range(3):
        assert crl_check.widerruf_geprueft(pfad, JETZT)[0]
    assert pruefungen["n"] == 1, f"{pruefungen['n']} Signaturprüfungen statt einer"


def test_neu_geladene_liste_wird_wieder_geprueft(tmp_path, ca, netz_mehrteilig):
    """Sonst gälte die Prüfung der alten Liste für eine neue weiter — und genau
    darin könnte der untergeschobene Inhalt stecken."""
    crl_check._signatur_ok.clear()
    pfad, _ = _mit_aia(tmp_path, ca, "sig-neu")
    netz_mehrteilig[CRL_URL] = _als_der(_sperrliste(ca, naechste=JETZT + timedelta(hours=1)))
    netz_mehrteilig["http://ca.invalid/ca.crt"] = _als_der_zert(ca["cert"])
    assert crl_check.widerruf_geprueft(pfad, JETZT)[0]
    assert CRL_URL in crl_check._signatur_ok

    # Die Liste läuft ab, und die Gegenstelle liefert eine fremd signierte.
    boese_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    netz_mehrteilig[CRL_URL] = _als_der(_sperrliste({"key": boese_key, "name": ca["name"]},
                                                    jetzt=JETZT + timedelta(hours=2)))
    ok, _ = crl_check.widerruf_geprueft(pfad, JETZT + timedelta(hours=2))
    assert not ok, "die fremd signierte Nachfolge-Liste wurde ungeprüft übernommen"
