"""
S/MIME public key harvesting and inbound signature stripping.

Two operating modes:
  1. Harvest-only  — called when SMIME_HARVEST_RCPT is in rcpt_tos.
                     Extracts sender cert, mail is discarded.
  2. Full-process  — called for all external multipart/signed mails arriving
                     via an EXO transport rule (RouteMessageOutboundConnector).
                     Strips the signature wrapper, harvests the cert, and
                     returns the inner MIME body + signer display name so the
                     handler can re-deliver with a subject tag.

ERST PRÜFEN, DANN ENTFERNEN
---------------------------
Bis v1.10.109 wurde die Signatur nie kryptografisch geprüft: Der Name für den
Betreff-Vermerk „signiert von …“ kam aus dem beigelegten Zertifikat, die
Signatur wurde entfernt und die Mail ausgeliefert — auch wenn der Inhalt nach
dem Signieren verändert oder das Zertifikat selbst ausgestellt war. Der
Vermerk bescheinigte damit eine Prüfung, die nie stattfand, und das Entfernen
nahm Outlook die Möglichkeit, selbst zu warnen.

Seitdem gilt (`pruefen()`):

1. Die Signatur über den Inhalt muss stimmen (`openssl cms -verify`).
2. Das Zertifikat muss zum Absender passen — From- oder Sender-Kopf, nicht
   der Envelope (RFC 8550 §3). Der Envelope trägt bei Versanddiensten eine
   Bounce-Adresse (VERP); im Produktivbestand lagen deshalb Zertifikate unter
   `bounce_…@`-Adressen.
   Für Entfernen und Vermerk muss es auf den **From**-Kopf lauten (seit
   v1.10.115). Der Leser sieht From als Absender; ein Vermerk aus einem
   Zertifikat, das nur auf den Sender-Kopf passt, bescheinigte einer anderen
   Adresse die Echtheit, und ohne Signatur könnte Outlook den Widerspruch
   nicht mehr zeigen. Eingesammelt wird es trotzdem (unter seiner Adresse).
3. Die Kette muss strukturell taugen und auf einen bekannten oder
   freigegebenen Aussteller führen (`trust_store`).

Nur wenn alles stimmt, wird die Signatur entfernt und der Vermerk gesetzt.
Sonst geht die Mail unverändert signiert weiter, und Outlook urteilt selbst.

Eingesammelt wird jedes Zertifikat mit gültiger Inhaltssignatur — aber nur
über `_uebernehmen()`; das ist der EINZIGE Weg in den Empfängerbestand
(`tests/test_smime_signatur_pruefung.py`).
"""
import email.utils
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path

import smime_store

log = logging.getLogger(__name__)


# ── Prüfung ────────────────────────────────────────────────────────────────────

@dataclass
class Pruefung:
    inhalt_ok: bool = False          # Signatur über den Inhalt stimmt
    signer_pem: bytes | None = None
    signer: object = None            # x509.Certificate
    kette: list = field(default_factory=list)
    adresse: str | None = None       # Kopfadresse, auf die das Zertifikat lautet
    inhalt: bytes | None = None      # signierter Inhalt (openssl -out)
    grund: str = ""                  # warum NICHT bestätigt; leer = bestätigt

    @property
    def bestaetigt(self) -> bool:
        return self.inhalt_ok and not self.grund

    @property
    def name(self) -> str | None:
        if not self.bestaetigt or self.signer is None:
            return None
        from cryptography.x509.oid import NameOID
        for oid in (NameOID.COMMON_NAME, NameOID.EMAIL_ADDRESS):
            werte = self.signer.subject.get_attributes_for_oid(oid)
            if werte:
                return str(werte[0].value)
        return self.adresse


def kopf_adressen(msg: Message) -> list[str]:
    """From- und Sender-Adresse, klein geschrieben — gegen sie wird das
    Zertifikat geprüft (RFC 8550 §3), nicht gegen den Envelope."""
    adressen = []
    for kopf in ("From", "Sender"):
        _, adr = email.utils.parseaddr(str(msg.get(kopf, "") or ""))
        adr = adr.strip().lower()
        if adr and adr not in adressen:
            adressen.append(adr)
    return adressen


def _openssl_pruefen(raw: bytes) -> tuple[bool, bytes, bytes, bytes, str]:
    """`(ok, signer_pem, inhalt, mitgelieferte_pem, fehler)`.

    `-noverify` schaltet NUR die Kettenprüfung von openssl ab — die Signatur
    über den Inhalt prüft openssl trotzdem. Die Kette prüft `trust_store`,
    gegen dieselbe Vertrauensliste wie das Einsammeln.
    """
    with tempfile.TemporaryDirectory() as tmp:
        signer = Path(tmp) / "signer.pem"
        inhalt = Path(tmp) / "inhalt"
        certs = Path(tmp) / "certs.pem"
        try:
            ergebnis = subprocess.run(
                ["openssl", "cms", "-verify", "-noverify",
                 "-signer", str(signer), "-certsout", str(certs), "-out", str(inhalt)],
                input=raw, capture_output=True, timeout=15,
            )
        except Exception as exc:
            return False, b"", b"", b"", str(exc)
        if ergebnis.returncode != 0 or not signer.is_file():
            return False, b"", b"", b"", ergebnis.stderr.decode(errors="replace")[:200]
        lesen = lambda p: p.read_bytes() if p.is_file() else b""  # noqa: E731
        return True, signer.read_bytes(), lesen(inhalt), lesen(certs), ""


def pruefen(raw: bytes, adressen: list[str]) -> Pruefung:
    """Signierte Nachricht (multipart/signed oder signed-data) prüfen.

    *adressen* wie aus `kopf_adressen()`: die ERSTE ist die angezeigte
    (From); nur auf sie hin wird bestätigt."""
    import trust_store
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    p = Pruefung()
    ok, signer_pem, inhalt, certs_pem, fehler = _openssl_pruefen(raw)
    if not ok:
        p.grund = "Signatur ungültig oder nicht lesbar"
        log.info("S/MIME-Signatur von %s nicht bestätigt: %s", adressen, fehler.strip())
        return p
    try:
        signer = x509.load_pem_x509_certificates(signer_pem)[0]
        mitgelieferte = x509.load_pem_x509_certificates(certs_pem) if certs_pem else []
    except Exception as exc:
        p.grund = f"Zertifikat nicht lesbar ({exc.__class__.__name__})"
        return p
    p.inhalt_ok = True
    p.inhalt = inhalt
    p.signer = signer
    p.signer_pem = signer.public_bytes(Encoding.PEM)
    eigene = set(trust_store.zertifikat_adressen(signer))
    p.adresse = next((a for a in adressen if a in eigene), None)
    p.kette = _kette_aus_mitgeliefertem(signer, mitgelieferte) or trust_store.kette_bauen(signer)
    mangel = trust_store.blatt_maengel(signer, adressen) or trust_store.kette_maengel(p.kette)
    if mangel:
        p.grund = mangel
    else:
        bekannt, grund = trust_store.bewerten(p.kette)
        if not bekannt:
            p.grund = grund
    if not p.grund and p.adresse != adressen[0]:
        p.grund = (f"Zertifikat lautet auf {p.adresse} (Sender-Kopf), "
                   f"nicht auf den angezeigten Absender {adressen[0]}")
    if p.grund:
        log.info("S/MIME-Signatur von %s nicht bestätigt: %s", adressen, p.grund)
    return p


def _einsammeln(p: Pruefung, adressen: list[str]) -> None:
    """Zertifikat einer inhaltlich gültigen Signatur dem Bestand anbieten."""
    if not p.inhalt_ok or not p.signer_pem or not adressen:
        return
    _uebernehmen(p.adresse or adressen[0], p.signer_pem, kette=p.kette, adressen=adressen)


# ── Public API ─────────────────────────────────────────────────────────────────

def _ist_signiert(msg: Message) -> bool:
    ct = msg.get_content_type().lower().replace("application/x-pkcs7-mime",
                                                "application/pkcs7-mime")
    if ct == "multipart/signed":
        return "pkcs7" in (msg.get_param("protocol") or "").lower()
    return (ct == "application/pkcs7-mime"
            and (msg.get_param("smime-type") or "").lower() == "signed-data")


def extract_and_store(msg: Message, raw: bytes | None = None) -> str | None:
    """Harvest-only: extract sender cert, return sender email or None."""
    adressen = kopf_adressen(msg)
    if not adressen or not _ist_signiert(msg):
        return None
    p = pruefen(raw if raw is not None else msg.as_bytes(), adressen)
    _einsammeln(p, adressen)
    return p.adresse or adressen[0]


def strip_signed_data(msg_bytes: bytes, adressen: list[str]
                      ) -> tuple[bytes | None, str | None, str | None]:
    """
    Strip application/pkcs7-mime; smime-type=signed-data (one-step S/MIME signing).
    Used by Hotmail and some clients instead of the standard multipart/signed wrapper.
    Returns (inner_mime_bytes, signer_display_name, grund) — inner only if the
    signature is confirmed; otherwise (None, None, reason).
    """
    p = pruefen(msg_bytes, adressen)
    _einsammeln(p, adressen)
    if not p.bestaetigt or not p.inhalt:
        return None, None, p.grund or "kein Inhalt"
    log.info("S/MIME signed-data stripped for sender=%s (%d→%d bytes)",
             p.adresse, len(msg_bytes), len(p.inhalt))
    return p.inhalt, p.name, None


def strip_and_harvest(msg: Message, adressen: list[str], raw: bytes | None = None
                      ) -> tuple[bytes | None, str | None, str | None]:
    """
    Full-process: check, harvest, strip. Returns (inner_mime_bytes,
    signer_display_name, None) when the signature is confirmed, otherwise
    (None, None, reason) — the caller then delivers the mail still signed.

    *raw* are the bytes as received. Re-serialising the parsed message can
    change the signed part (folding, line ends) and break a valid signature.
    """
    if msg.get_content_type().lower() != "multipart/signed" or not _ist_signiert(msg):
        return None, None, "kein S/MIME"
    parts = msg.get_payload()
    if not isinstance(parts, list) or len(parts) < 2:
        return None, None, "unvollständig"
    p = pruefen(raw if raw is not None else msg.as_bytes(), adressen)
    _einsammeln(p, adressen)
    if not p.bestaetigt:
        return None, None, p.grund
    try:
        return parts[0].as_bytes(), p.name, None
    except Exception as exc:
        log.error("S/MIME strip: could not serialize inner body for %s: %s", adressen, exc)
        return None, None, "Inhalt nicht lesbar"


def nur_entfernen(msg: Message) -> bytes | None:
    """Signaturhülle abnehmen, ohne Prüfung und ohne Einsammeln.

    Nur für die EIGENE, über eine Fremdweiterleitung zurückgekehrte Post: Sie
    wird nicht als „signiert von …“ ausgewiesen, und ihr Zertifikat gehört
    nicht in den Bestand der Kommunikationspartner.
    """
    parts = msg.get_payload()
    if msg.get_content_type().lower() != "multipart/signed" or not isinstance(parts, list) or not parts:
        return None
    try:
        return parts[0].as_bytes()
    except Exception:
        return None



# ── Nachlauf über den vorhandenen Bestand ──────────────────────────────────────

# Einmal je Regelstand. Wird die Regel verschärft, bekommt der Nachlauf eine
# neue Kennung und läuft erneut.
NACHLAUF_KENNUNG = "1.10.115"


def _nachlauf_marke() -> Path:
    import config
    return Path(config.DATA_DIR) / "smime" / f"nachlauf-{NACHLAUF_KENNUNG}.erledigt"


def bestand_nachpruefen() -> list[dict]:
    """Den Empfängerbestand mit den Regeln von `blatt_maengel`/`kette_maengel`
    nachprüfen; Treffer wandern in den Wartestand.

    Bis v1.10.109 kamen Zertifikate ohne Adressprüfung in den Bestand, und der
    openssl-Rückfall ging ganz am Vertrauensspeicher vorbei. Was damals
    hineinkam, prüft dieser Lauf mit denselben Regeln wie heute ein Neuzugang —
    mit zwei bewussten Ausnahmen:

    * **Gültigkeit** zählt nicht: Abgelaufene Empfängerzertifikate bleiben im
      Bestand stehen (Hinweis „früher verschlüsselt“).
    * **Unbekannte Aussteller** verschieben nicht: Bestand von vor v1.10.115
      trägt keine Herkunft, eingesammelte und hochgeladene Zertifikate sind
      darin nicht zu unterscheiden, und ein Hochladen ist eine
      Vertrauensentscheidung des Administrators. Solche Zertifikate kommen
      stattdessen auf die **Durchsichtsliste** (`durchsicht()`): Genau über
      den alten openssl-Rückfall konnte ein selbst ausgestelltes Zertifikat
      mit passender Adresse in den Bestand kommen. Der Administrator
      entscheidet je Eintrag: behalten oder entfernen.

    Jeder verschobene Treffer meldet sich über `cert_wartestand.merken` beim
    Administrator.
    """
    import trust_store
    import cert_wartestand
    from cryptography import x509

    verschoben, durchsehen = [], []
    for pfad in smime_store.alle_empfaenger_cert_pfade():
        adresse = pfad.parent.name
        try:
            pem = pfad.read_bytes()
            cert = x509.load_pem_x509_certificate(pem)
        except Exception as exc:
            log.warning("Nachprüfung: Zertifikat für %s nicht lesbar: %s", adresse, exc)
            continue
        try:
            mangel = trust_store.blatt_maengel(cert, [adresse], gueltigkeit=False)
            kette = [] if mangel else trust_store.kette_bauen(cert)
            if not mangel:
                mangel = trust_store.kette_maengel(kette, gueltigkeit=False)
            if not mangel and smime_store.herkunft(adresse) is None:
                bekannt, grund = trust_store.bewerten(kette)
                if not bekannt:
                    durchsehen.append(_durchsicht_eintrag(adresse, cert, kette, grund))
        except Exception as exc:
            log.warning("Nachprüfung: %s nicht prüfbar: %s", adresse, exc)
            continue
        if not mangel:
            continue
        if cert_wartestand.merken(adresse, pem, f"Nachprüfung: {mangel}"):
            smime_store.delete_recipient_cert(adresse)
            verschoben.append({"adresse": adresse, "grund": mangel})
            log.warning("Nachprüfung: Zertifikat für %s in den Wartestand (%s)", adresse, mangel)
    _durchsicht_schreiben(durchsehen)
    if durchsehen:
        log.warning("Nachprüfung: %d Bestandszertifikat(e) ohne bekannten Aussteller "
                    "zur Durchsicht vorgelegt", len(durchsehen))
    return verschoben


# ── Durchsicht: Altbestand ohne bekannten Aussteller ──────────────────────────

def _durchsicht_datei() -> Path:
    import config
    return Path(config.DATA_DIR) / "smime" / "durchsicht.json"


def _durchsicht_eintrag(adresse: str, cert, kette: list, grund: str) -> dict:
    import trust_store
    oben = kette[-1] if kette else cert
    return {"adresse": adresse, "fingerabdruck": trust_store.abdruck(cert),
            "aussteller": cert.issuer.rfc4514_string()[:200],
            "selbst_ausgestellt": cert.subject == cert.issuer,
            "bis": cert.not_valid_after_utc.isoformat(),
            "grund": grund or f"Kette endet bei {oben.subject.rfc4514_string()[:120]}"}


def _durchsicht_schreiben(eintraege: list[dict]) -> None:
    import json
    ziel = _durchsicht_datei()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_suffix(".tmp")
    tmp.write_text(json.dumps(eintraege, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(ziel)


def durchsicht() -> list[dict]:
    """Offene Einträge: nur solange genau dieses Zertifikat noch im Bestand
    liegt und niemand entschieden hat (dann trägt es eine Herkunft)."""
    import json
    import trust_store
    from cryptography import x509
    try:
        eintraege = json.loads(_durchsicht_datei().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    offen = []
    for e in eintraege:
        pfad = smime_store.get_recipient_cert_path(e.get("adresse", ""))
        if pfad is None or smime_store.herkunft(e["adresse"]) is not None:
            continue
        try:
            if trust_store.abdruck(x509.load_pem_x509_certificate(pfad.read_bytes())) \
                    != e.get("fingerabdruck"):
                continue
        except Exception:
            continue
        offen.append(e)
    return offen


def durchsicht_entscheiden(adresse: str, behalten: bool) -> bool:
    adresse = (adresse or "").lower().strip()
    if not any(e["adresse"] == adresse for e in durchsicht()):
        return False
    if behalten:
        smime_store.herkunft_setzen(adresse, "bestaetigt")
    else:
        smime_store.delete_recipient_cert(adresse)
    log.info("Durchsicht: Zertifikat für %s %s", adresse,
             "behalten" if behalten else "entfernt")
    return True


def bestand_nachpruefen_einmal() -> list[dict] | None:
    """Für den Scheduler: läuft genau einmal je `NACHLAUF_KENNUNG`."""
    marke = _nachlauf_marke()
    if marke.is_file():
        return None
    ergebnis = bestand_nachpruefen()
    marke.parent.mkdir(parents=True, exist_ok=True)
    marke.write_text(f"{len(ergebnis)} verschoben\n", encoding="utf-8")
    log.info("Nachprüfung des Empfängerbestands: %d in den Wartestand", len(ergebnis))
    return ergebnis

# ── Internals ──────────────────────────────────────────────────────────────────

def _uebernehmen(sender: str, cert_pem: bytes, mitgelieferte=None, kette=None,
                 adressen=None) -> None:
    """In den Bestand — oder in den Wartestand, je nach Zertifikat und Aussteller.

    ⚠️ Hier entscheidet sich, WESSEN Zertifikate benutzt werden. Bis v1.7.200
    landete jedes eingesammelte Zertifikat unbesehen im Bestand: Wer eine
    signierte Nachricht schicken konnte, bestimmte damit, mit welchem Schlüssel
    künftig an diese Adresse verschlüsselt wird. Der Absender einer Mail ist
    keine geprüfte Angabe.

    ⚠️ Bis v1.10.109 prüfte diese Stelle nur den Aussteller. Ein echtes
    Zertifikat einer bekannten Stelle für eine FREMDE Adresse kam so unter dem
    Absender in den Bestand. Jetzt muss es auf den Absender lauten, ein
    Personenzertifikat für E-Mail sein und gelten (`trust_store.blatt_maengel`).

    ⚠️ Die **mitgelieferte Kette** wird bevorzugt. Eine signierte Nachricht
    enthält die Zwischenzertifikate in aller Regel gleich mit; sie zu benutzen
    erspart Netzabrufe und funktioniert auch dort, wo die Ausstelleradresse
    fehlt. Vertrauen entsteht dadurch nicht — die Kette wird Glied für Glied
    geprüft, und am Ende muss eine bekannte Wurzel stehen.
    """
    import trust_store
    import cert_wartestand
    import stats
    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except Exception as exc:
        log.warning("Eingesammeltes Zertifikat für %s nicht lesbar: %s", sender, exc)
        return

    mangel = trust_store.blatt_maengel(cert, adressen or [sender])
    if mangel:
        art, grund = trust_store.WARTEN, mangel
    else:
        kette = kette or _kette_aus_mitgeliefertem(cert, mitgelieferte) or trust_store.kette_bauen(cert)
        art, grund = trust_store.entscheiden(kette)
    if art == trust_store.ANNEHMEN:
        smime_store.store_recipient_cert(sender, cert_pem, herkunft="eingesammelt")
        return

    stats.increment("cert_wartet")
    cert_wartestand.merken(sender, cert_pem, grund)


def _kette_aus_mitgeliefertem(cert, mitgelieferte) -> list | None:
    """Kette aus den Zertifikaten der Nachricht selbst, soweit sie trägt.

    Jedes Glied muss das vorige ausgestellt haben — sonst ist es nur ein
    Zertifikat, das jemand beigelegt hat.
    """
    if not mitgelieferte:
        return None
    import trust_store
    kette = [cert]
    aktuell = cert
    for _ in range(trust_store.MAX_KETTENLAENGE):
        naechster = None
        for kandidat in mitgelieferte:
            if trust_store.abdruck(kandidat) in {trust_store.abdruck(k) for k in kette}:
                continue
            try:
                aktuell.verify_directly_issued_by(kandidat)
            except Exception:
                continue
            naechster = kandidat
            break
        if naechster is None:
            break
        kette.append(naechster)
        aktuell = naechster
    # Die Nachricht liefert die Wurzel meist nicht mit — dort weitersuchen,
    # wo sie steht.
    if aktuell.subject != aktuell.issuer:
        rest = trust_store.kette_bauen(aktuell)
        kette.extend(rest[1:])
    return kette if len(kette) > 1 else None
