"""Widerrufsprüfung für Empfängerzertifikate über Sperrlisten (CRL).

Stufe 2 der Zertifikatsprüfung. Stufe 1 (zeitliche Gültigkeit, Lesbarkeit) steht
in `smime_store.zeitlich_gueltig()` und braucht kein Netz; hier kommt der Teil,
der beim Trustcenter nachfragen muss.

WARUM CRL UND NICHT OCSP
------------------------
Die übliche Reihenfolge ist umgekehrt — hier entscheidet der Datenschutz:

Eine OCSP-Abfrage verrät dem Trustcenter, dass **dieser Absender gerade mit
diesem Partner** kommuniziert, mitsamt Zeitpunkt. Bei einem Produkt mit
Auftragsverarbeitungsvertrag ist das erklärungsbedürftig. Eine Sperrliste hat
das Problem nicht: Man lädt die Liste **aller** Widerrufe der CA, und niemand
erfährt, welcher Eintrag interessiert hat.

Dazu kommt der Betrieb: Eine CRL wird einmal je CA und Gültigkeitszeitraum
geladen, OCSP einmal je Nachricht. Der Zwischenspeicher hier ist deshalb kein
Beiwerk, sondern der Grund, warum die Prüfung den Mailfluss nicht aufhält.

OCSP bleibt als Ergänzung für Zertifikate offen, die keinen CRL-Punkt tragen.

⚠️ FAIL CLOSED, ABER NICHT FAIL HARD
------------------------------------
Ist die Sperrliste nicht erreichbar oder nicht auswertbar, gilt das Zertifikat
als **nicht verwendbar** — die Nachricht geht dann über das Nachrichtenportal
hinaus statt verschlüsselt. Das ist die dritte Möglichkeit neben den beiden
schlechten: Hart abweisen hielte den Versand bei einer fremden Störung an,
Durchwinken machte die Prüfung wertlos. Weil dieses Gateway einen sicheren
Ersatzweg hat, kostet Vorsicht hier nur das Verfahren, nicht die Zustellung.

⚠️ Ein Zertifikat OHNE CRL-Verteilungspunkt ist etwas anderes als eine nicht
erreichbare CRL: Das eine ist eine Eigenschaft des Zertifikats, das andere eine
Störung. Ohne Verteilungspunkt gibt es nichts abzufragen — solche Zertifikate
werden durchgelassen und **gezählt** (`cert_ohne_crl`), damit nicht unbemerkt
bleibt, für welchen Teil des Bestands die Prüfung gar nicht greift. Eine
Zusicherung, deren Wirken nirgends sichtbar ist, fällt sonst unbemerkt aus.

WOHER DIE SPERRLISTE STAMMT — DREI STUFEN
-----------------------------------------
Eine abgerufene Liste ist zunächst nur eine Datei aus dem Netz. Sie wird auf
drei Arten an das Zertifikat gebunden, die aufeinander aufbauen:

1. **Aussteller-Name** — die Liste muss von der CA des Zertifikats stammen
   (oder von der Stelle, die der Verteilungspunkt ausdrücklich nennt). Fängt
   den Fall, dass irgendeine fremde Liste geliefert wird.
2. **Signatur der Liste** — geprüft mit dem öffentlichen Schlüssel der CA.
   Nötig, weil einen Namen jeder abschreiben kann; die Signatur nicht.
3. **Bindung des Ausstellerzertifikats** — auch das kommt über HTTP und wäre
   für sich wertlos. Es wird nur benutzt, wenn es das Empfängerzertifikat
   **tatsächlich ausgestellt hat** (`verify_directly_issued_by`). Das kann ein
   Angreifer nicht fälschen, ohne den privaten Schlüssel der echten CA zu haben.

Diese Prüfung beantwortet damit genau eine Frage: **Stammt die Auskunft von
derselben Stelle, die das Zertifikat ausgestellt hat?** Sie sagt NICHTS darüber,
ob dieser Stelle zu trauen ist.

⚠️ Das ist keine Spitzfindigkeit, sondern eine offene Flanke: Empfängerzertifikate
kommen zum Teil selbsttätig herein (`smime_harvest` aus eingehenden signierten
Mails), und dabei wird weder die Kette geprüft (`openssl smime -verify
-noverify`) noch ein Vertrauensspeicher befragt. Wer ein Zertifikat samt
Sperrliste aus einer selbst betriebenen CA mitbringt, besteht die Prüfung hier
tadellos. Die Frage, WELCHE Aussteller überhaupt in den Bestand dürfen, wird an
anderer Stelle beantwortet werden.

⚠️ Nennt das Zertifikat keine Adresse seines Ausstellers, entfallen 2 und 3;
die Liste wird dann nach Stufe 1 benutzt. Nennt es eine, die **nicht erreichbar**
ist, wird die Liste verworfen — sonst genügte es, den Abruf des Ausstellers zu
blockieren, um eine untergeschobene Liste durchzubringen.

Offen bleibt OCSP für Zertifikate ganz ohne Verteilungspunkt.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes

import config

log = logging.getLogger("crl")

# Der Mailfluss wartet auf diese Abfrage. Lieber das Portal als eine Nachricht,
# die sekundenlang im Versand hängt — die Zustellung leidet nicht, nur das
# Verfahren.
ABRUF_TIMEOUT = 5.0

# Sperrlisten grosser CAs sind einige hundert Kilobyte. Die Grenze schützt vor
# einer Gegenstelle, die endlos liefert; sie ist bewusst grosszügig.
MAX_GROESSE = 20 * 1024 * 1024

# Auch eine gültige Sperrliste wird nicht ewig weiterbenutzt: Sagt sie kein
# `nextUpdate`, gilt sie diese Zeitspanne lang.
OHNE_NEXTUPDATE_GUELTIG = 24 * 3600


# Geparste Sperrlisten im Arbeitsspeicher.
#
# WARUM ZWEI EBENEN
# -----------------
# Der Dateispeicher erspart den Netzabruf, nicht das Auswerten. Gemessen am
# 16.08.2026 im laufenden Container, gegen drei echte Trustcenter:
#
#     Sectigo   5,6 MB   Abruf 956 ms   aus der Datei  62 ms
#     DigiCert  0,6 MB   Abruf 118 ms   aus der Datei   6 ms
#     HARICA    9,3 MB   Abruf 1045 ms  aus der Datei 127 ms
#
# Die 127 ms fielen sonst bei JEDER verschlüsselten Nachricht an, nur um
# dieselbe Datei erneut zu zerlegen. Mit dieser Ebene kostet der zweite Zugriff
# nichts mehr.
#
# ⚠️ Die Anzahl ist begrenzt, nicht die Grösse: Eine Sperrliste kann zweistellig
# viele Megabyte belegen, und dieses Gateway läuft auch auf kleinen Geräten.
# Mehr als eine Handvoll Trustcenter kommen in einem Postfachbestand kaum vor;
# bei mehr fällt der älteste Eintrag heraus und wird beim nächsten Mal aus der
# Datei gelesen — dann kostet es wieder die Millisekunden oben, mehr nicht.
_MAX_IM_SPEICHER = 6
_im_speicher: dict[str, x509.CertificateRevocationList] = {}


def _cache_verzeichnis() -> Path:
    p = Path(config.DATA_DIR) / "crl_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_datei(url: str) -> Path:
    # Der Dateiname ist ein Hash, nicht die Adresse: CRL-Adressen enthalten
    # Pfade und Parameter, die als Dateiname nicht taugen.
    return _cache_verzeichnis() / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".crl")


def crl_punkte(cert: x509.Certificate) -> list[tuple[str, x509.Name | None]]:
    """Verteilungspunkte als `(Adresse, erwarteter Aussteller der Liste)`.

    Der zweite Wert ist fast immer `None` — dann muss die Sperrliste vom
    Aussteller des Zertifikats selbst stammen. Nennt der Verteilungspunkt einen
    eigenen `crl_issuer`, ist es eine **indirekte** Sperrliste (RFC 5280 §5.2.6):
    Dann führt eine andere Stelle die Widerrufe, und der Abgleich geht gegen
    diese.

    LDAP-Adressen kommen in älteren Zertifikaten vor und werden übergangen —
    das Gateway spricht kein LDAP, und ein Verteilungspunkt, den man nicht
    abrufen kann, ist wie keiner.
    """
    try:
        ext = cert.extensions.get_extension_for_class(x509.CRLDistributionPoints)
    except x509.ExtensionNotFound:
        return []
    punkte: list[tuple[str, x509.Name | None]] = []
    for punkt in ext.value:
        eigener_aussteller = None
        if punkt.crl_issuer:
            for name in punkt.crl_issuer:
                if isinstance(getattr(name, "value", None), x509.Name):
                    eigener_aussteller = name.value
        for name in (punkt.full_name or []):
            wert = getattr(name, "value", "")
            if isinstance(wert, str) and wert.lower().startswith(("http://", "https://")):
                punkte.append((wert, eigener_aussteller))
    return punkte


def crl_adressen(cert: x509.Certificate) -> list[str]:
    """Nur die Adressen — für den Vorlauf, dem der Aussteller gleich ist."""
    return [url for url, _ in crl_punkte(cert)]


def _crl_laden(rohdaten: bytes) -> x509.CertificateRevocationList | None:
    """Sperrliste aus DER oder PEM. Trustcenter liefern beides."""
    for lader in (x509.load_der_x509_crl, x509.load_pem_x509_crl):
        try:
            return lader(rohdaten)
        except Exception:
            continue
    return None


def _naechste_aktualisierung(crl: x509.CertificateRevocationList) -> datetime:
    try:
        wert = crl.next_update_utc
    except AttributeError:
        wert = crl.next_update
        if wert is not None:
            wert = wert.replace(tzinfo=timezone.utc)
    if wert is None:
        return datetime.now(timezone.utc).fromtimestamp(
            time.time() + OHNE_NEXTUPDATE_GUELTIG, timezone.utc)
    return wert


def _aus_cache(url: str, jetzt: datetime) -> x509.CertificateRevocationList | None:
    datei = _cache_datei(url)
    if not datei.is_file():
        return None
    crl = _crl_laden(datei.read_bytes())
    if crl is None:
        return None
    if _naechste_aktualisierung(crl) <= jetzt:
        log.info("CRL im Zwischenspeicher ist überfällig (%s) — wird neu geladen", url)
        return None
    return crl


def _abrufen(url: str) -> bytes | None:
    """Nur der Transport: HTTP holen, Grösse begrenzen, Rohdaten liefern.

    ⚠️ Bewusst frei von Zwischenspeicher-Logik. Lägen beide hier zusammen,
    liesse sich der Zwischenspeicher nicht mehr prüfen, ohne ihn zugleich zu
    umgehen: Ein Test, der den Abruf ersetzt, ersetzte dann auch das Schreiben —
    und misst hinterher sein eigenes Ersatzstück statt der Sache. Genau so ist
    die erste Fassung dieses Tests danebengegangen.
    """
    import httpx
    try:
        with httpx.Client(timeout=ABRUF_TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url) as antwort:
                antwort.raise_for_status()
                teile, gesamt = [], 0
                for stueck in antwort.iter_bytes():
                    gesamt += len(stueck)
                    if gesamt > MAX_GROESSE:
                        log.warning("CRL %s überschreitet %d Bytes — abgebrochen", url, MAX_GROESSE)
                        return None
                    teile.append(stueck)
        return b"".join(teile)
    except Exception as exc:
        log.warning("CRL nicht abrufbar (%s): %s: %s", url, exc.__class__.__name__, exc)
        return None


# Ausstellerzertifikate im Arbeitsspeicher.
#
# Sie sind winzig (ein bis zwei Kilobyte) und ändern sich über Jahre nicht —
# aber ohne diese Ebene lädt sie JEDE Prüfung neu, auch wenn die Sperrliste
# längst im Speicher liegt. Am laufenden Container gemessen: 1.688 ms für eine
# Nachricht bei warmem Sperrlisten-Cache, allein für diesen einen Abruf. Im
# Versandweg ist das nicht tragbar.
#
# Begrenzt wie dort über die Anzahl; hier grosszügiger, weil die Einträge
# tausendfach kleiner sind als eine Sperrliste.
_MAX_CA_IM_SPEICHER = 32
_ca_im_speicher: dict[str, x509.Certificate] = {}


def _ca_aus_speicher(url: str) -> x509.Certificate | None:
    ca = _ca_im_speicher.get(url)
    if ca is None:
        return None
    # Ein abgelaufenes Ausstellerzertifikat wird nicht weiterbenutzt: Die CA hat
    # dann längst ein neues, und die Sperrliste stammt von diesem.
    try:
        gueltig_bis = ca.not_valid_after_utc
    except AttributeError:
        gueltig_bis = ca.not_valid_after.replace(tzinfo=timezone.utc)
    if gueltig_bis <= datetime.now(timezone.utc):
        _ca_im_speicher.pop(url, None)
        return None
    return ca


def _ca_merken(url: str, ca: x509.Certificate) -> None:
    if len(_ca_im_speicher) >= _MAX_CA_IM_SPEICHER:
        _ca_im_speicher.pop(next(iter(_ca_im_speicher)))
    _ca_im_speicher[url] = ca


def ausstelleradressen(cert: x509.Certificate) -> list[str]:
    """HTTP(S)-Adressen des Ausstellerzertifikats (`AIA`, `caIssuers`)."""
    from cryptography.x509.oid import AuthorityInformationAccessOID
    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
    except x509.ExtensionNotFound:
        return []
    adressen = []
    for beschreibung in aia:
        if beschreibung.access_method != AuthorityInformationAccessOID.CA_ISSUERS:
            continue
        wert = getattr(beschreibung.access_location, "value", "")
        if isinstance(wert, str) and wert.lower().startswith(("http://", "https://")):
            adressen.append(wert)
    return adressen


def _zert_laden(rohdaten: bytes) -> x509.Certificate | None:
    """Ausstellerzertifikate kommen als DER (`.crt`, `.cer`) oder PEM."""
    for lader in (x509.load_der_x509_certificate, x509.load_pem_x509_certificate):
        try:
            return lader(rohdaten)
        except Exception:
            continue
    return None


def ausstellerzertifikat(cert: x509.Certificate) -> x509.Certificate | None:
    """Das Zertifikat der ausstellenden CA — geladen und AN *cert* GEBUNDEN.

    ⚠️ Der zweite Teil ist der entscheidende. Ein über HTTP geladenes Zertifikat
    ist zunächst nichts wert: Wer den Netzweg beherrscht, liefert ein beliebiges.
    Deshalb wird geprüft, ob es *cert* **tatsächlich signiert hat**
    (`verify_directly_issued_by`). Das kann ein Angreifer nicht fälschen, ohne
    den privaten Schlüssel der echten CA zu besitzen — er kann nur die echte CA
    liefern oder gar keine.

    Damit braucht es hier **keinen Vertrauensanker**: Geprüft wird nicht, ob die
    CA vertrauenswürdig ist (das hat entschieden, wer das Zertifikat in den
    Bestand liess), sondern nur, ob die Sperrliste von genau ihr stammt.
    """
    for url in ausstelleradressen(cert):
        # ⚠️ Zwei Anläufe je Adresse: erst der Zwischenspeicher, dann frisch.
        # Der Speicher ist nach ADRESSE geschlüsselt, die Bindung hängt aber am
        # konkreten Zertifikat. Liefert eine Adresse für verschiedene
        # Empfängerzertifikate verschiedene Aussteller — oder liegt dort ein
        # veralteter Eintrag —, wäre ein einzelner Anlauf ein Fehlschlag, und
        # die Nachricht ginge über das Portal, obwohl alles in Ordnung ist.
        for versuch, ca in enumerate((_ca_aus_speicher(url), None)):
            if ca is None:
                if versuch == 0:
                    continue          # nichts im Speicher → zweiter Durchgang lädt
                rohdaten = _abrufen(url)
                if rohdaten is None:
                    break
                ca = _zert_laden(rohdaten)
                if ca is None:
                    log.warning("Ausstellerzertifikat von %s ist weder DER noch PEM", url)
                    break
            try:
                cert.verify_directly_issued_by(ca)
            except Exception as exc:
                if versuch == 0:
                    # Gespeichertes passt nicht — verwerfen und frisch laden.
                    _ca_im_speicher.pop(url, None)
                    continue
                log.warning("Zertifikat von %s hat das Empfängerzertifikat NICHT "
                            "ausgestellt (%s) — verworfen", url, exc.__class__.__name__)
                break
            _ca_merken(url, ca)
            return ca
    return None


# Bereits geprüfte Signaturen: Adresse der Sperrliste → Fingerabdruck der CA,
# gegen die sie erfolgreich geprüft wurde.
#
# ⚠️ Ohne das wird bei JEDER Nachricht die gesamte Liste neu gehasht — die
# Signatur deckt schliesslich die ganze Datei. Am laufenden Container gemessen:
# 1.275 ms je Nachricht bei einer 9,3-MB-Liste, obwohl Liste UND
# Ausstellerzertifikat längst im Speicher lagen. Der Abruf war nie das Teure.
#
# Der Fingerabdruck gehört in den Schlüssel: Käme unter derselben Adresse eine
# andere CA, müsste neu geprüft werden. Beim Neuladen einer Liste fällt der
# Eintrag weg.
_signatur_ok: dict[str, bytes] = {}


def _signatur_geprueft(crl: x509.CertificateRevocationList,
                       cert: x509.Certificate, url: str = "") -> tuple[bool, str]:
    """Stammt die Sperrliste wirklich von der ausstellenden CA?

    Liefert `(True, Vermerk)`, wenn nichts dagegen spricht. Zwei Fälle sind
    auseinanderzuhalten:

    * Das Zertifikat nennt **keine** Adresse seines Ausstellers → die Signatur
      lässt sich nicht prüfen. Das ist eine Eigenschaft des Zertifikats, keine
      Störung; die Liste wird benutzt und der Fall vermerkt.
    * Es nennt eine, aber sie ist **nicht erreichbar** oder die Signatur passt
      **nicht** → verworfen. Sonst genügte es, den Abruf des Ausstellers zu
      blockieren, um eine untergeschobene Sperrliste durchzubringen.
    """
    if not ausstelleradressen(cert):
        return True, "ohne Ausstelleradresse"
    ca = ausstellerzertifikat(cert)
    if ca is None:
        return False, "Ausstellerzertifikat nicht prüfbar"
    abdruck = ca.fingerprint(hashes.SHA256())
    if url and _signatur_ok.get(url) == abdruck:
        return True, ""      # dieselbe Liste, dieselbe CA — bereits geprüft
    try:
        if not crl.is_signature_valid(ca.public_key()):
            return False, "Signatur der Sperrliste ungültig"
    except Exception as exc:
        return False, f"Signatur nicht prüfbar ({exc.__class__.__name__})"
    if url:
        _signatur_ok[url] = abdruck
    return True, ""


def _merken(url: str, crl: x509.CertificateRevocationList) -> None:
    if len(_im_speicher) >= _MAX_IM_SPEICHER:
        _im_speicher.pop(next(iter(_im_speicher)))
    _im_speicher[url] = crl


def sperrliste(url: str, jetzt: datetime | None = None) -> x509.CertificateRevocationList | None:
    """Sperrliste zu *url* — aus dem Speicher, sonst der Datei, sonst frisch."""
    jetzt = jetzt or datetime.now(timezone.utc)

    gemerkt = _im_speicher.get(url)
    if gemerkt is not None:
        if _naechste_aktualisierung(gemerkt) > jetzt:
            return gemerkt
        _im_speicher.pop(url, None)   # überfällig — nicht weiterbenutzen

    aus_cache = _aus_cache(url, jetzt)
    if aus_cache is not None:
        _merken(url, aus_cache)
        return aus_cache

    rohdaten = _abrufen(url)
    if rohdaten is None:
        return None
    crl = _crl_laden(rohdaten)
    if crl is None:
        log.warning("CRL von %s ist weder DER noch PEM — verworfen", url)
        return None
    if _naechste_aktualisierung(crl) <= jetzt:
        # Eine bereits überfällige Liste zu speichern hiesse, sie beim nächsten
        # Mal sofort wieder zu verwerfen — und sie zu benutzen hiesse, gegen
        # einen Stand zu prüfen, den die CA selbst für veraltet erklärt.
        log.warning("CRL von %s ist bereits überfällig — nicht verwendet", url)
        return None
    try:
        _cache_datei(url).write_bytes(rohdaten)
    except Exception as exc:      # Zwischenspeicher ist Beschleunigung, kein Muss
        log.warning("CRL konnte nicht zwischengespeichert werden: %s", exc)
    _signatur_ok.pop(url, None)   # neue Liste → alte Prüfung gilt nicht mehr
    _merken(url, crl)
    return crl


def widerruf_geprueft(cert_path, jetzt: datetime | None = None) -> tuple[bool, str]:
    """Darf mit diesem Zertifikat verschlüsselt werden?

    Liefert `(True, Vermerk)` wenn nichts dagegen spricht, sonst
    `(False, Grund)`. Der Vermerk bei Erfolg ist leer oder nennt den Grund,
    warum nicht geprüft werden konnte, ohne dass das gegen das Zertifikat
    spricht (kein Verteilungspunkt).

    ⚠️ Die Reihenfolge der Fälle ist Absicht:
    * widerrufen           → nein, mit Datum
    * keine CRL im Zert    → ja, mit Vermerk (Eigenschaft, keine Störung)
    * CRL nicht erreichbar → nein (Störung; Portal statt Verschlüsselung)
    """
    jetzt = jetzt or datetime.now(timezone.utc)
    try:
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    except Exception as exc:
        return False, f"nicht lesbar ({exc.__class__.__name__})"

    punkte = crl_punkte(cert)
    if not punkte:
        return True, "ohne Sperrlisten-Adresse"

    for url, eigener_aussteller in punkte:
        crl = sperrliste(url, jetzt)
        if crl is None:
            continue          # nächster Verteilungspunkt, viele CAs nennen mehrere

        # Gehört die Liste überhaupt zu diesem Zertifikat?
        #
        # Ohne diese Prüfung genügte es, IRGENDEINE gültige Sperrliste
        # unterzuschieben — eine leere fremde erklärte jedes Zertifikat für
        # unwiderrufen. Der Abgleich ersetzt die Prüfung der CRL-Signatur nicht
        # (dafür fehlt die Ausstellerkette), er kostet aber nichts und schliesst
        # den einfachsten Weg. An echten Trustcentern bestätigt: Sectigo und
        # DigiCert nennen in der Liste denselben Aussteller wie im Zertifikat.
        erwartet = eigener_aussteller or cert.issuer
        if crl.issuer != erwartet:
            log.warning("Sperrliste von %s stammt von %r, erwartet war %r — verworfen",
                        url, crl.issuer.rfc4514_string(), erwartet.rfc4514_string())
            continue

        # Der Name allein ist eine Behauptung — die Signatur ist der Beleg.
        # Nur bei direkten Sperrlisten prüfbar: Bei einer indirekten führt eine
        # andere Stelle die Liste, deren Zertifikat wir nicht über das
        # Empfängerzertifikat binden können.
        if eigener_aussteller is None:
            echt, vermerk = _signatur_geprueft(crl, cert, url)
            if not echt:
                log.warning("Sperrliste von %s verworfen — %s", url, vermerk)
                continue

        eintrag = crl.get_revoked_certificate_by_serial_number(cert.serial_number)
        if eintrag is not None:
            try:
                seit = eintrag.revocation_date_utc
            except AttributeError:
                seit = eintrag.revocation_date.replace(tzinfo=timezone.utc)
            return False, f"widerrufen am {seit:%d.%m.%Y}"
        return True, ""

    return False, "Sperrliste nicht erreichbar"


def vorwaermen(cert_pfade) -> dict:
    """Sperrlisten aller übergebenen Zertifikate in den Zwischenspeicher holen.

    Gedacht für den Tageslauf. Ohne das wartet die erste Nachricht an eine
    bislang unbekannte CA auf den Abruf — und schlimmstenfalls läuft sie in die
    Zeitüberschreitung und geht über das Portal, obwohl mit dem Zertifikat
    alles in Ordnung ist.
    """
    geholt = fehlgeschlagen = 0
    gesehen: set[str] = set()
    for pfad in cert_pfade:
        try:
            cert = x509.load_pem_x509_certificate(Path(pfad).read_bytes())
        except Exception:
            continue
        for url in crl_adressen(cert):
            if url in gesehen:
                continue
            gesehen.add(url)
            if sperrliste(url) is not None:
                geholt += 1
            else:
                fehlgeschlagen += 1
    if gesehen:
        log.info("CRL-Vorlauf: %d von %d Sperrlisten bereit", geholt, len(gesehen))
    return {"adressen": len(gesehen), "geholt": geholt, "fehlgeschlagen": fehlgeschlagen}
