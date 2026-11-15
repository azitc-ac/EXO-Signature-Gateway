"""Notbetrieb: IMAP-Zugang zur Kopie (Etappe 2) — lesend.

Outlook Classic (Zusatzkonto im bestehenden Profil) und Mail-Apps auf dem
Handy lesen die Kopie per IMAP. Gesendet wird weiter über das Webmail; IMAP
nimmt keine Post an (kein APPEND/COPY/MOVE), nur das Gelesen-Flag gilt —
lokal in der Kopie, es geht nicht nach Exchange zurück.

SICHERHEIT
----------
- Nur implizites TLS (993); ohne Zertifikat startet der Zugang nicht.
- Anmeldung = Webmail-Anmeldung (`notbetrieb_anmeldung.anmelden`: DC, Drossel
  je Name und Adresse); Postfächer = Rechte-Abbild (eigenes + Vollzugriff).
- Vor dem Notfall: Anmelden geht, sichtbar ist nur ein leerer Posteingang —
  außer für die Probegruppe (`notfall.inhalte_erlaubt`). So lässt sich das
  Konto vorab auf allen Geräten einrichten.
- Grenzen: Zeilenlänge, Literalgröße, Leerlauf; Fehlversuche je Verbindung.
- Schalter `NOTBETRIEB_IMAP` wird je Verbindung geprüft — live umschaltbar.

ABBILDUNG
---------
Ordner = Ordnerbaum der Kopie; Posteingang heißt INBOX, Unterordner mit „/“.
Geteilte Postfächer unter „Geteilt/<adresse>/…“. Namen in modifiziertem UTF-7
(RFC 3501 §5.1.3). UIDs je Postfach+Ordner in `imap_uid`, aufsteigend in der
Reihenfolge, in der das Gateway sie zuerst sieht — eine verschobene Mail
bekommt im Zielordner eine neue, höhere UID (RFC 3501 §2.3.1.1). UIDVALIDITY
je Ordner fest ab der ersten Zuteilung.
"""
from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import email.utils
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import notbetrieb
import settings_store

log = logging.getLogger(__name__)

PORT = 993
MAX_ZEILE = 64 * 1024
MAX_LITERAL = 64 * 1024        # Anmeldung/Suche
MAX_APPEND = 40 * 1024 * 1024  # APPEND nach der Anmeldung (Gesendet-Kopie des Programms)
LEERLAUF_S = 30 * 60
MAX_FEHLVERSUCHE = 3
TRENNER = "/"
FAEHIGKEITEN = "IMAP4rev1 AUTH=PLAIN LITERAL+ IDLE NAMESPACE ID UNSELECT SPECIAL-USE"
_SPEZIAL = {"sentitems": "\\Sent", "junkemail": "\\Junk", "deleteditems": "\\Trash", "drafts": "\\Drafts"}


def aktiv() -> bool:
    return notbetrieb.funktion_aktiv() and settings_store.get("NOTBETRIEB_IMAP") is True


# ── modifiziertes UTF-7 (RFC 3501 §5.1.3) ───────────────────────────────────

def mutf7(text: str) -> str:
    out, puffer = [], []

    def leeren():
        if puffer:
            roh = "".join(puffer).encode("utf-16-be")
            out.append("&" + base64.b64encode(roh).decode().rstrip("=").replace("/", ",") + "-")
            puffer.clear()
    for ch in text:
        if 0x20 <= ord(ch) <= 0x7e:
            leeren()
            out.append("&-" if ch == "&" else ch)
        else:
            puffer.append(ch)
    leeren()
    return "".join(out)


def mutf7_zurueck(text: str) -> str:
    def ersetzen(m):
        teil = m.group(1)
        if not teil:
            return "&"
        teil = teil.replace(",", "/")
        return base64.b64decode(teil + "=" * (-len(teil) % 4)).decode("utf-16-be")
    return re.sub(r"&([A-Za-z0-9+,]*)-", ersetzen, text)


# ── Daten aus der Kopie ─────────────────────────────────────────────────────

def _tabellen(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS imap_uid (
            postfach TEXT NOT NULL, ordner TEXT NOT NULL, graph_id TEXT NOT NULL, uid INTEGER NOT NULL,
            PRIMARY KEY (postfach, ordner, graph_id));
        CREATE TABLE IF NOT EXISTS imap_ordner (
            postfach TEXT NOT NULL, ordner TEXT NOT NULL, validitaet INTEGER NOT NULL, naechste INTEGER NOT NULL,
            PRIMARY KEY (postfach, ordner));
    """)


def ordner_baum(postfach: str, vorsatz: str = "") -> list[dict]:
    """[{name (IMAP, unkodiert), id, wk}] — INBOX für den Posteingang."""
    import notbetrieb_lesen
    liste = notbetrieb_lesen.ordner(postfach)
    nach_id = {o["id"]: o for o in liste}

    def pfad(o, tiefe=0):
        if o["wk"] == "inbox" and not vorsatz:
            return "INBOX"
        name = o["name"].replace(TRENNER, "-")
        eltern = nach_id.get(o["eltern"])
        if eltern and tiefe < 16:
            return pfad(eltern, tiefe + 1) + TRENNER + name
        return vorsatz + name
    return [{"name": pfad(o), "id": o["id"], "wk": o["wk"]} for o in liste]


def _uids(postfach: str, ordner_id: str, wk: str) -> tuple[int, int, list[dict]]:
    """(uidvalidity, uidnext, Nachrichten aufsteigend nach UID). Neue Mails
    bekommen beim Aufruf ihre UID."""
    ids = [ordner_id] + ([wk] if wk else [])
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabellen(conn)
        z = conn.execute("SELECT validitaet, naechste FROM imap_ordner WHERE postfach=? AND ordner=?",
                         (postfach, ordner_id)).fetchone()
        if z is None:
            validitaet, naechste = int(time.time()), 1
            conn.execute("INSERT INTO imap_ordner VALUES (?,?,?,?)", (postfach, ordner_id, validitaet, naechste))
        else:
            validitaet, naechste = z["validitaet"], z["naechste"]
        mails = conn.execute(
            f"SELECT m.graph_id, m.empfangen, m.gelesen, m.datei, u.uid FROM mails m "
            f"LEFT JOIN imap_uid u ON u.postfach=m.postfach AND u.ordner=? AND u.graph_id=m.graph_id "
            f"WHERE m.postfach=? AND m.ordner IN ({','.join('?' * len(ids))}) ORDER BY m.empfangen",
            [ordner_id, postfach, *ids]).fetchall()
        # Mails, die den Ordner verlassen haben, verlieren ihre UID hier — kehrt
        # eine zurück, ist sie für den Client eine NEUE Nachricht (höhere UID),
        # nicht die alte, deren Verschwinden er schon gesehen hat.
        da = {m["graph_id"] for m in mails}
        weg = [z["graph_id"] for z in conn.execute(
            "SELECT graph_id FROM imap_uid WHERE postfach=? AND ordner=?", (postfach, ordner_id))
            if z["graph_id"] not in da]
        conn.executemany("DELETE FROM imap_uid WHERE postfach=? AND ordner=? AND graph_id=?",
                         [(postfach, ordner_id, g) for g in weg])
        neu = [m for m in mails if m["uid"] is None]
        for m in neu:
            conn.execute("INSERT INTO imap_uid VALUES (?,?,?,?)", (postfach, ordner_id, m["graph_id"], naechste))
            naechste += 1
        if neu:
            conn.execute("UPDATE imap_ordner SET naechste=? WHERE postfach=? AND ordner=?",
                         (naechste, postfach, ordner_id))
            mails = conn.execute(
                f"SELECT m.graph_id, m.empfangen, m.gelesen, m.datei, u.uid FROM mails m "
                f"JOIN imap_uid u ON u.postfach=m.postfach AND u.ordner=? AND u.graph_id=m.graph_id "
                f"WHERE m.postfach=? AND m.ordner IN ({','.join('?' * len(ids))})",
                [ordner_id, postfach, *ids]).fetchall()
    return validitaet, naechste, sorted((dict(m) for m in mails), key=lambda m: m["uid"])


def _roh(m: dict) -> bytes:
    roh = Path(m["datei"]).read_bytes()
    return roh.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


# ── Ausgabe-Bausteine ───────────────────────────────────────────────────────

def _q(wert) -> bytes:
    """Zeichenkette als quoted oder Literal; None → NIL."""
    if wert is None:
        return b"NIL"
    b = wert if isinstance(wert, bytes) else str(wert).encode("utf-8", "replace")
    if any(c in b for c in (b"\r", b"\n", b'"', b"\\")) or any(x > 0x7e for x in b):
        return b"{%d}\r\n" % len(b) + b
    return b'"' + b + b'"'


def _adressen(werte) -> bytes:
    if not werte:
        return b"NIL"
    out = []
    for name, adr in email.utils.getaddresses([str(w) for w in werte]):
        lokal, _, host = adr.rpartition("@") if "@" in adr else (adr, "", "")
        out.append(b"(" + _q(name or None) + b" NIL " + _q(lokal or None) + b" " + _q(host or None) + b")")
    return b"(" + b"".join(out) + b")" if out else b"NIL"


def envelope(msg) -> bytes:
    k = lambda n: msg.get(n)                                        # noqa: E731
    absender = msg.get_all("From")
    return b"(" + b" ".join([
        _q(k("Date")), _q(k("Subject")), _adressen(absender), _adressen(msg.get_all("Sender") or absender),
        _adressen(msg.get_all("Reply-To") or absender), _adressen(msg.get_all("To")), _adressen(msg.get_all("Cc")),
        _adressen(msg.get_all("Bcc")), _q(k("In-Reply-To")), _q(k("Message-ID"))]) + b")"


def _teil_roh(teil) -> tuple[bytes, bytes]:
    """(Kopf, Rumpf) eines Teils, wie er in der Nachricht steht."""
    roh = teil.as_bytes(policy=email.policy.compat32.clone(linesep="\r\n"))
    kopf, _, rumpf = roh.partition(b"\r\n\r\n")
    return kopf + b"\r\n\r\n", rumpf


def bodystructure(teil) -> bytes:
    if teil.is_multipart():
        kinder = b"".join(bodystructure(t) for t in teil.get_payload())
        grenze = teil.get_param("boundary")
        return (b"(" + kinder + b" " + _q(teil.get_content_subtype().upper())
                + (b" (\"BOUNDARY\" " + _q(grenze) + b")" if grenze else b" NIL") + b" NIL NIL)")
    haupt, unter = teil.get_content_maintype().upper(), teil.get_content_subtype().upper()
    if haupt == "MESSAGE":
        # Eingebettete Nachricht als Anhang ausweisen — ein vollständiges
        # MESSAGE/RFC822 verlangte Envelope und Struktur des Inneren.
        haupt, unter = "APPLICATION", "OCTET-STREAM"
    params = [(k, email.utils.collapse_rfc2231_value(v)) for k, v in (teil.get_params() or [])[1:] if k]
    p = b"(" + b" ".join(_q(k.upper()) + b" " + _q(v) for k, v in params) + b")" if params else b"NIL"
    _, rumpf = _teil_roh(teil)
    cte = (teil.get("Content-Transfer-Encoding") or "7BIT").upper()
    teile = [_q(haupt), _q(unter), p, _q(teil.get("Content-ID")), _q(teil.get("Content-Description")),
             _q(cte), str(len(rumpf)).encode()]
    if haupt == "TEXT":
        teile.append(str(rumpf.count(b"\r\n")).encode())
    disp = teil.get_content_disposition()
    if disp:
        datei = teil.get_filename()
        teile += [b"NIL", b"(" + _q(disp.upper()) + (b" (\"FILENAME\" " + _q(datei) + b")" if datei else b" NIL") + b")"]
    return b"(" + b" ".join(teile) + b")"


def _abschnitt(msg, roh: bytes, angabe: str) -> bytes:
    """BODY[angabe]: '' | HEADER | TEXT | HEADER.FIELDS (..) | n[.m][.MIME|.HEADER|.TEXT]."""
    angabe = angabe.strip()
    kopf, _, rumpf = roh.partition(b"\r\n\r\n")
    if not angabe:
        return roh
    oben = angabe.upper()
    if oben == "HEADER":
        return kopf + b"\r\n\r\n"
    if oben == "TEXT":
        return rumpf
    m = re.match(r"(?i)^HEADER\.FIELDS(\.NOT)?\s*\((.*)\)$", angabe)
    if m:
        namen = {n.lower() for n in m.group(2).split()}
        zeilen, behalten = [], False
        for z in re.split(rb"(?<=\r\n)", kopf + b"\r\n"):
            if not z:
                continue
            if z[:1] in (b" ", b"\t"):
                if behalten:
                    zeilen.append(z)
                continue
            name = z.split(b":", 1)[0].decode("latin-1").strip().lower()
            behalten = (name in namen) != bool(m.group(1))
            if behalten:
                zeilen.append(z)
        return b"".join(zeilen) + b"\r\n"
    m = re.match(r"(?i)^([\d.]+?)(?:\.(MIME|HEADER|TEXT))?$", angabe)
    if not m:
        raise ValueError("Abschnitt unbekannt")
    teil = msg
    for nr in [int(x) for x in m.group(1).split(".")]:
        if teil.is_multipart():
            kinder = teil.get_payload()
            if not 1 <= nr <= len(kinder):
                return b""
            teil = kinder[nr - 1]
        elif nr != 1:
            return b""
    k, r = _teil_roh(teil)
    art = (m.group(2) or "").upper()
    return k if art in ("MIME", "HEADER") else r


# ── Befehlszeile zerlegen ───────────────────────────────────────────────────

class Fehler(Exception):
    pass


_ZK_ENDE = re.compile(r'["\\]')


def zerlegen(text: str) -> list:
    """Atome, Zeichenketten und Klammerlisten (Literale sind schon eingesetzt)."""
    out, stapel, i = [], [], 0
    aktuell = out
    while i < len(text):
        c = text[i]
        if c == " ":
            i += 1
        elif c == '"':
            # Stückweise bis zum nächsten " oder \ — ein APPEND-Literal hat Megabytes.
            j, puffer = i + 1, []
            while True:
                k = _ZK_ENDE.search(text, j)
                if not k:
                    raise Fehler("Zeichenkette nicht geschlossen")
                puffer.append(text[j:k.start()])
                if k.group() == '"':
                    i = k.start() + 1
                    break
                puffer.append(text[k.start() + 1:k.start() + 2])
                j = k.start() + 2
            aktuell.append("".join(puffer))
        elif c == "(":
            neu = []
            aktuell.append(neu)
            stapel.append(aktuell)
            aktuell = neu
            i += 1
        elif c == ")":
            if not stapel:
                raise Fehler("Klammer")
            aktuell = stapel.pop()
            i += 1
        else:
            j = i
            tiefe = 0
            while j < len(text) and (text[j] not in " ()" or tiefe):
                if text[j] == "[":
                    tiefe += 1
                elif text[j] == "]":
                    tiefe -= 1
                j += 1
            aktuell.append(text[i:j])
            i = j
    if stapel:
        raise Fehler("Klammer offen")
    return out


def menge(angabe: str, hoechstes: int) -> set[int]:
    """Sequenz-/UID-Menge „1:3,7,9:*“."""
    werte: set[int] = set()
    for teil in angabe.split(","):
        a, _, b = teil.partition(":")
        von = hoechstes if a == "*" else int(a)
        bis = (hoechstes if b == "*" else int(b)) if b else von
        von, bis = min(von, bis), max(von, bis)
        if bis - von > 1_000_000:
            raise Fehler("Menge zu groß")
        werte.update(range(von, bis + 1))
    return werte


# ── Sitzung ─────────────────────────────────────────────────────────────────

_APPEND = re.compile(rb"^\S+ APPEND ", re.I)

class Sitzung:
    def __init__(self, leser: asyncio.StreamReader, schreiber: asyncio.StreamWriter, quelle: str):
        self.r, self.w, self.quelle = leser, schreiber, quelle
        self.benutzer = ""
        self.inhalte = False
        self.postfaecher: list[str] = []
        self.gewaehlt: dict | None = None
        self.nur_lesen = True
        self.fehlversuche = 0

    async def senden(self, *teile: bytes) -> None:
        self.w.write(b"".join(teile))
        await self.w.drain()

    async def zeile(self) -> str | None:
        """Eine Befehlszeile samt eingesetzter Literale (LITERAL+ und synchron)."""
        stuecke = []
        while True:
            try:
                z = await asyncio.wait_for(self.r.readuntil(b"\r\n"), LEERLAUF_S)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                return None
            except asyncio.LimitOverrunError:
                raise Fehler("Zeile zu lang")
            z = z[:-2]
            m = re.search(rb"\{(\d+)(\+?)\}$", z)
            if not m:
                stuecke.append(z)
                return b"".join(stuecke).decode("utf-8", "replace")
            n = int(m.group(1))
            grenze = MAX_APPEND if (self.benutzer and _APPEND.match(b"".join(stuecke) + z)) else MAX_LITERAL
            if n > grenze:
                raise Fehler("Literal zu groß")
            stuecke.append(z[:m.start()])
            if not m.group(2):
                await self.senden(b"+ weiter\r\n")
            daten = await self.r.readexactly(n)
            stuecke.append(b'"' + daten.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"')

    # Postfächer und Ordner der angemeldeten Person
    def _ordnerliste(self) -> list[dict]:
        if not self.inhalte:
            return [{"name": "INBOX", "id": "", "wk": "inbox", "postfach": self.benutzer, "leer": True}]
        out = []
        for i, pf in enumerate(self.postfaecher):
            vorsatz = "" if i == 0 else f"Geteilt{TRENNER}{pf}{TRENNER}"
            for o in ordner_baum(pf, vorsatz):
                out.append({**o, "postfach": pf})
        return out

    def _finden(self, name: str) -> dict | None:
        name = mutf7_zurueck(name)
        if name.upper() == "INBOX":
            name = "INBOX"
        return next((o for o in self._ordnerliste() if o["name"] == name), None)

    def _inhalt(self, o: dict) -> tuple[int, int, list[dict]]:
        if o.get("leer"):
            return 1, 1, []
        return _uids(o["postfach"], o["id"], o["wk"])

    async def ausfuehren(self, tag: str, befehl: str, args: list) -> None:
        b = befehl.upper()
        if b == "CAPABILITY":
            await self.senden(f"* CAPABILITY {FAEHIGKEITEN}\r\n{tag} OK CAPABILITY\r\n".encode())
        elif b == "NOOP" or b == "CHECK":
            if self.gewaehlt:
                await self._exists_melden()
            await self.senden(f"{tag} OK {b}\r\n".encode())
        elif b == "LOGOUT":
            await self.senden(b"* BYE Abmeldung\r\n" + f"{tag} OK LOGOUT\r\n".encode())
            raise ConnectionAbortedError
        elif b == "ID":
            await self.senden(b'* ID ("name" "Notbetrieb")\r\n' + f"{tag} OK ID\r\n".encode())
        elif b in ("LOGIN", "AUTHENTICATE"):
            await self._anmelden(tag, b, args)
        elif not self.benutzer:
            await self.senden(f"{tag} NO Bitte erst anmelden\r\n".encode())
        elif b == "NAMESPACE":
            await self.senden(f'* NAMESPACE (("" "{TRENNER}")) NIL NIL\r\n{tag} OK NAMESPACE\r\n'.encode())
        elif b in ("LIST", "LSUB"):
            await self._liste(tag, b, args)
        elif b == "STATUS":
            await self._status(tag, args)
        elif b in ("SELECT", "EXAMINE"):
            await self._waehlen(tag, b, args)
        elif b in ("CLOSE", "UNSELECT"):
            self.gewaehlt = None
            await self.senden(f"{tag} OK {b}\r\n".encode())
        elif b == "IDLE":
            await self._idle(tag)
        elif b in ("FETCH", "STORE", "SEARCH") or (b == "UID" and args):
            if not self.gewaehlt:
                await self.senden(f"{tag} NO Kein Ordner gewählt\r\n".encode())
                return
            per_uid = b == "UID"
            if per_uid:
                b, args = str(args[0]).upper(), args[1:]
            if b == "FETCH":
                await self._fetch(tag, args, per_uid)
            elif b == "STORE":
                await self._store(tag, args, per_uid)
            elif b == "SEARCH":
                await self._search(tag, args, per_uid)
            elif b in ("COPY", "MOVE", "EXPUNGE"):
                await self.senden(f"{tag} NO [CANNOT] Im Notbetrieb nur lesend\r\n".encode())
            else:
                await self.senden(f"{tag} BAD Unbekannt\r\n".encode())
        elif b in ("EXPUNGE",):
            await self.senden(f"{tag} OK EXPUNGE\r\n".encode())
        elif b == "APPEND":
            await self._append(tag, args)
        elif b in ("SUBSCRIBE", "UNSUBSCRIBE"):
            # Ohne Wirkung — LSUB liefert ohnehin alle Ordner. Outlook Classic
            # abonniert beim ersten Abgleich und bricht bei NO ab (800CCC16).
            await self.senden(f"{tag} OK {b}\r\n".encode())
        elif b in ("COPY", "MOVE", "CREATE", "DELETE", "RENAME"):
            await self.senden(f"{tag} NO [CANNOT] Im Notbetrieb nur lesend\r\n".encode())
        else:
            await self.senden(f"{tag} BAD Unbekannter Befehl\r\n".encode())

    async def _append(self, tag: str, args: list) -> None:
        """Nur die Gesendet-Kopie einer Mail, die das Gateway selbst gesendet
        und schon abgelegt hat (587): angenommen ohne zweite Ablage. Outlook und
        iOS legen nach dem Senden per APPEND ab und melden sonst einen Fehler."""
        o = self._finden(str(args[0])) if args and isinstance(args[0], str) else None
        inhalt = args[-1] if len(args) >= 2 and isinstance(args[-1], str) else ""
        if o and not o.get("leer") and o["wk"] == "sentitems" and inhalt:
            kopf = email.message_from_string(inhalt.split("\r\n\r\n", 1)[0] + "\r\n\r\n",
                                             policy=email.policy.compat32)
            if notbetrieb.lokal_vorhanden(o["postfach"], "sentitems", str(kopf.get("Message-ID") or "")):
                await self.senden(f"{tag} OK APPEND (schon abgelegt)\r\n".encode())
                return
        await self.senden(f"{tag} NO [CANNOT] Im Notbetrieb nur lesend\r\n".encode())

    async def _anmelden(self, tag: str, b: str, args: list) -> None:
        if self.benutzer:
            await self.senden(f"{tag} BAD Schon angemeldet\r\n".encode())
            return
        if b == "AUTHENTICATE":
            if not args or str(args[0]).upper() != "PLAIN":
                await self.senden(f"{tag} NO Nur PLAIN\r\n".encode())
                return
            if len(args) > 1:
                antwort = str(args[1])
            else:
                await self.senden(b"+ \r\n")
                antwort = (await self.zeile() or "").strip()
            try:
                teile = base64.b64decode(antwort, validate=True).split(b"\x00")
                name, kennwort = teile[1].decode(), teile[2].decode()
            except Exception:                                       # noqa: BLE001
                await self.senden(f"{tag} BAD Ungültig\r\n".encode())
                return
        else:
            if len(args) != 2:
                await self.senden(f"{tag} BAD LOGIN name kennwort\r\n".encode())
                return
            name, kennwort = str(args[0]), str(args[1])
        import notbetrieb_anmeldung
        import notbetrieb_faktor
        if not notbetrieb_faktor.nur_kennwort_erlaubt():
            # Zweiter Faktor ist Pflicht — IMAP kann keinen abfragen. Vor dem
            # Bind abgewiesen: die Antwort verrät nichts über das Kennwort.
            log.info("Notbetrieb-IMAP: Anmeldung abgewiesen — zweiter Faktor Pflicht (von %s)", self.quelle)
            await self.senden(f"{tag} NO [AUTHENTICATIONFAILED] Zweiter Faktor Pflicht - bitte das "
                              "Notfall-Webmail nutzen\r\n".encode())
            return
        erg = await asyncio.to_thread(notbetrieb_anmeldung.anmelden, name, kennwort, self.quelle)
        if not erg.get("ok"):
            self.fehlversuche += 1
            await asyncio.sleep(1)                                   # bremst Raten je Verbindung
            await self.senden(f"{tag} NO [AUTHENTICATIONFAILED] Anmeldung fehlgeschlagen\r\n".encode())
            if self.fehlversuche >= MAX_FEHLVERSUCHE:
                raise ConnectionAbortedError
            return
        from webui.routen import notfall
        self.benutzer = erg["postfach"]
        self.inhalte = notfall.inhalte_erlaubt(self.benutzer)
        self.postfaecher = [p["postfach"] for p in erg.get("zugriff") or [] if p.get("voll")] or [self.benutzer]
        log.info("Notbetrieb-IMAP: %s angemeldet (von %s, Inhalte %s)", self.benutzer, self.quelle,
                 "ja" if self.inhalte else "nein — bereit")
        await self.senden(f"{tag} OK [CAPABILITY {FAEHIGKEITEN}] Angemeldet\r\n".encode())

    async def _liste(self, tag: str, b: str, args: list) -> None:
        if len(args) < 2:
            await self.senden(f"{tag} BAD {b} referenz muster\r\n".encode())
            return
        muster = mutf7_zurueck(str(args[0]) + str(args[1]))
        if muster == "":
            await self.senden(f'* {b} (\\Noselect) "{TRENNER}" ""\r\n{tag} OK {b}\r\n'.encode())
            return
        rx = re.compile("^" + re.escape(muster).replace(r"\*", ".*").replace("%", f"[^{TRENNER}]*") + "$",
                        re.IGNORECASE if muster.upper().startswith("INBOX") else 0)
        zeilen = []
        namen = {o["name"] for o in self._ordnerliste()}
        # Zwischenebenen ohne eigenen Ordner („Geteilt“, „Geteilt/<postfach>“)
        # als \Noselect (RFC 3501 §6.3.8) — Outlook wählte sie sonst an und
        # meldete bei jedem Abgleich 8004DF03.
        for ebene in sorted({n.rsplit(TRENNER, k)[0] for n in namen for k in range(1, n.count(TRENNER) + 1)}
                            - namen):
            if rx.match(ebene):
                zeilen.append(f'* {b} (\\Noselect \\HasChildren) "{TRENNER}" '.encode() + _q(mutf7(ebene)) + b"\r\n")
        for o in self._ordnerliste():
            if not rx.match(o["name"]):
                continue
            attr = []
            if any(n.startswith(o["name"] + TRENNER) for n in namen):
                attr.append("\\HasChildren")
            else:
                attr.append("\\HasNoChildren")
            if o["wk"] in _SPEZIAL and o["name"] != "INBOX":
                attr.append(_SPEZIAL[o["wk"]])
            zeilen.append(f'* {b} ({" ".join(attr)}) "{TRENNER}" '.encode() + _q(mutf7(o["name"])) + b"\r\n")
        await self.senden(*zeilen, f"{tag} OK {b}\r\n".encode())

    async def _status(self, tag: str, args: list) -> None:
        o = self._finden(str(args[0])) if args else None
        if not o:
            await self.senden(f"{tag} NO [NONEXISTENT] Ordner unbekannt\r\n".encode())
            return
        validitaet, naechste, mails = await asyncio.to_thread(self._inhalt, o)
        werte = {"MESSAGES": len(mails), "RECENT": 0, "UIDNEXT": naechste, "UIDVALIDITY": validitaet,
                 "UNSEEN": sum(1 for m in mails if not m["gelesen"])}
        felder = [str(x).upper() for x in (args[1] if len(args) > 1 and isinstance(args[1], list) else [])]
        text = " ".join(f"{f} {werte[f]}" for f in felder if f in werte)
        await self.senden(b"* STATUS " + _q(mutf7(o["name"])) + f" ({text})\r\n{tag} OK STATUS\r\n".encode())

    async def _waehlen(self, tag: str, b: str, args: list) -> None:
        o = self._finden(str(args[0])) if args else None
        if not o:
            self.gewaehlt = None
            await self.senden(f"{tag} NO [NONEXISTENT] Ordner unbekannt\r\n".encode())
            return
        validitaet, naechste, mails = await asyncio.to_thread(self._inhalt, o)
        self.gewaehlt = {**o, "mails": mails, "validitaet": validitaet}
        self.nur_lesen = b == "EXAMINE" or bool(o.get("leer"))
        ungelesen = next((i + 1 for i, m in enumerate(mails) if not m["gelesen"]), None)
        await self.senden(
            b"* FLAGS (\\Seen)\r\n",
            f"* {len(mails)} EXISTS\r\n* 0 RECENT\r\n".encode(),
            f"* OK [UIDVALIDITY {validitaet}] UIDs gültig\r\n* OK [UIDNEXT {naechste}] nächste UID\r\n".encode(),
            (f"* OK [UNSEEN {ungelesen}] erste ungelesene\r\n".encode() if ungelesen else b""),
            f"* OK [PERMANENTFLAGS ({'' if self.nur_lesen else chr(92) + 'Seen'})] Gelesen-Flag nur in der Kopie\r\n".encode(),
            f"{tag} OK [{'READ-ONLY' if self.nur_lesen else 'READ-WRITE'}] {b}\r\n".encode())

    async def _exists_melden(self) -> None:
        o = self.gewaehlt
        _, _, mails = await asyncio.to_thread(self._inhalt, o)
        alt = {m["uid"] for m in o["mails"]}
        neu = {m["uid"] for m in mails}
        # Weggefallene (verschoben/abgelaufen) von hinten melden, damit die
        # Sequenznummern der übrigen stimmen.
        for nr in sorted((i + 1 for i, m in enumerate(o["mails"]) if m["uid"] not in neu), reverse=True):
            await self.senden(f"* {nr} EXPUNGE\r\n".encode())
        if neu != alt:
            o["mails"] = mails
            await self.senden(f"* {len(mails)} EXISTS\r\n".encode())

    def _auswahl(self, angabe: str, per_uid: bool) -> list[tuple[int, dict]]:
        mails = self.gewaehlt["mails"]
        if not mails:
            return []
        if per_uid:
            uids = menge(angabe, mails[-1]["uid"])
            return [(i + 1, m) for i, m in enumerate(mails) if m["uid"] in uids]
        nrn = menge(angabe, len(mails))
        return [(i + 1, m) for i, m in enumerate(mails) if i + 1 in nrn]

    async def _fetch(self, tag: str, args: list, per_uid: bool) -> None:
        if len(args) < 2:
            await self.senden(f"{tag} BAD FETCH menge elemente\r\n".encode())
            return
        elemente = args[1] if isinstance(args[1], list) else args[1:]
        elemente = [str(e) for e in elemente]
        kurz = {"ALL": ["FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE"],
                "FAST": ["FLAGS", "INTERNALDATE", "RFC822.SIZE"],
                "FULL": ["FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE", "BODY"]}
        if len(elemente) == 1 and elemente[0].upper() in kurz:
            elemente = kurz[elemente[0].upper()]
        if per_uid and not any(e.upper() == "UID" for e in elemente):
            elemente = ["UID", *elemente]
        try:
            auswahl = self._auswahl(str(args[0]), per_uid)
        except (Fehler, ValueError):
            await self.senden(f"{tag} BAD Menge\r\n".encode())
            return
        for nr, m in auswahl:
            teile, gelesen_setzen = [], False
            roh = msg = None
            for e in elemente:
                oben = e.upper()
                if oben == "UID":
                    teile.append(b"UID %d" % m["uid"])
                elif oben == "FLAGS":
                    teile.append(b"FLAGS (\\Seen)" if m["gelesen"] else b"FLAGS ()")
                elif oben == "INTERNALDATE":
                    try:
                        dt = datetime.fromisoformat(m["empfangen"].replace("Z", "+00:00"))
                    except ValueError:
                        dt = datetime.now(timezone.utc)
                    teile.append(b'INTERNALDATE "' + dt.strftime("%d-%b-%Y %H:%M:%S +0000").encode() + b'"')
                else:
                    if roh is None:
                        roh = await asyncio.to_thread(_roh, m)
                        msg = email.message_from_bytes(roh, policy=email.policy.compat32)
                    if oben == "RFC822.SIZE":
                        teile.append(b"RFC822.SIZE %d" % len(roh))
                    elif oben == "ENVELOPE":
                        teile.append(b"ENVELOPE " + envelope(msg))
                    elif oben in ("BODYSTRUCTURE", "BODY"):
                        teile.append(oben.encode() + b" " + bodystructure(msg))
                    elif oben in ("RFC822", "RFC822.HEADER", "RFC822.TEXT"):
                        teil = {"RFC822": "", "RFC822.HEADER": "HEADER", "RFC822.TEXT": "TEXT"}[oben]
                        daten = _abschnitt(msg, roh, teil)
                        teile.append(oben.encode() + b" {%d}\r\n" % len(daten) + daten)
                        gelesen_setzen |= oben != "RFC822.HEADER"
                    else:
                        mm = re.match(r"(?i)^(BODY(?:\.PEEK)?)\[(.*)\](?:<(\d+)(?:\.(\d+))?>)?$", e)
                        if not mm:
                            await self.senden(f"{tag} BAD Element {e[:40]}\r\n".encode())
                            return
                        try:
                            daten = _abschnitt(msg, roh, mm.group(2))
                        except ValueError:
                            await self.senden(f"{tag} BAD Abschnitt\r\n".encode())
                            return
                        name = f"BODY[{mm.group(2)}]"
                        if mm.group(3) is not None:
                            anfang = int(mm.group(3))
                            daten = daten[anfang:anfang + int(mm.group(4))] if mm.group(4) else daten[anfang:]
                            name += f"<{anfang}>"
                        teile.append(name.encode() + b" {%d}\r\n" % len(daten) + daten)
                        gelesen_setzen |= not mm.group(1).upper().endswith(".PEEK")
            if gelesen_setzen and not m["gelesen"] and not self.nur_lesen:
                await asyncio.to_thread(_gelesen, self.gewaehlt["postfach"], m["graph_id"], True)
                m["gelesen"] = 1
                teile.append(b"FLAGS (\\Seen)")
            await self.senden(b"* %d FETCH (" % nr + b" ".join(teile) + b")\r\n")
        await self.senden(f"{tag} OK FETCH\r\n".encode())

    async def _store(self, tag: str, args: list, per_uid: bool) -> None:
        if self.nur_lesen:
            await self.senden(f"{tag} NO [READ-ONLY] Ordner nur lesend geöffnet\r\n".encode())
            return
        if len(args) < 3:
            await self.senden(f"{tag} BAD STORE menge art flags\r\n".encode())
            return
        art = str(args[1]).upper()
        flags = [str(f).lower() for f in (args[2] if isinstance(args[2], list) else args[2:])]
        try:
            auswahl = self._auswahl(str(args[0]), per_uid)
        except (Fehler, ValueError):
            await self.senden(f"{tag} BAD Menge\r\n".encode())
            return
        seen = "\\seen" in flags
        for nr, m in auswahl:
            ziel = m["gelesen"]
            if art.startswith("+FLAGS") and seen:
                ziel = 1
            elif art.startswith("-FLAGS") and seen:
                ziel = 0
            elif art.startswith("FLAGS"):
                ziel = 1 if seen else 0
            if ziel != m["gelesen"]:
                await asyncio.to_thread(_gelesen, self.gewaehlt["postfach"], m["graph_id"], bool(ziel))
                m["gelesen"] = ziel
            if not art.endswith(".SILENT"):
                uid = b" UID %d" % m["uid"] if per_uid else b""
                await self.senden(b"* %d FETCH (FLAGS (%s)%s)\r\n" % (nr, b"\\Seen" if ziel else b"", uid))
        await self.senden(f"{tag} OK STORE\r\n".encode())

    async def _search(self, tag: str, args: list, per_uid: bool) -> None:
        mails = self.gewaehlt["mails"]
        try:
            treffer = [i for i, m in enumerate(mails) if _passt(args, m, i + 1, mails)]
        except (Fehler, ValueError, IndexError):
            await self.senden(f"{tag} BAD Suche nicht unterstützt\r\n".encode())
            return
        werte = [mails[i]["uid"] if per_uid else i + 1 for i in treffer]
        await self.senden(("* SEARCH" + "".join(f" {w}" for w in werte) + f"\r\n{tag} OK SEARCH\r\n").encode())

    async def _idle(self, tag: str) -> None:
        await self.senden(b"+ warte\r\n")
        ende = time.monotonic() + LEERLAUF_S
        while time.monotonic() < ende:
            try:
                z = await asyncio.wait_for(self.r.readuntil(b"\r\n"), 30)
                if z.strip().upper() == b"DONE":
                    break
            except asyncio.TimeoutError:
                if self.gewaehlt:
                    await self._exists_melden()
            except (asyncio.IncompleteReadError, ConnectionError):
                raise ConnectionAbortedError
        await self.senden(f"{tag} OK IDLE\r\n".encode())


def _gelesen(postfach: str, graph_id: str, wert: bool) -> None:
    with notbetrieb._lock, notbetrieb._conn() as conn:
        conn.execute("UPDATE mails SET gelesen=? WHERE postfach=? AND graph_id=?", (1 if wert else 0, postfach, graph_id))


def _datum(text: str) -> datetime:
    return datetime.strptime(text, "%d-%b-%Y").replace(tzinfo=timezone.utc)


def _passt(args: list, m: dict, nr: int, mails: list) -> bool:
    """SEARCH-Kriterien (UND-verknüpft): ALL SEEN UNSEEN DELETED UNDELETED
    SINCE BEFORE ON UID <menge> NOT <k> OR <k> <k> <sequenzmenge> CHARSET x."""
    i = 0
    erg = True
    while i < len(args):
        k, i = _kriterium(args, i, m, nr, mails)
        erg = erg and k
    return erg


def _kriterium(args, i, m, nr, mails):
    a = args[i]
    if isinstance(a, list):
        return _passt(a, m, nr, mails), i + 1
    o = str(a).upper()
    empf = datetime.fromisoformat(m["empfangen"].replace("Z", "+00:00"))
    if o in ("ALL", "UNDELETED", "UNDRAFT", "UNFLAGGED", "UNANSWERED", "OLD"):
        return True, i + 1
    if o in ("DELETED", "DRAFT", "FLAGGED", "ANSWERED", "NEW", "RECENT"):
        return False, i + 1
    if o == "SEEN":
        return bool(m["gelesen"]), i + 1
    if o == "UNSEEN":
        return not m["gelesen"], i + 1
    if o == "CHARSET":
        return True, i + 2
    if o == "SINCE":
        return empf.date() >= _datum(str(args[i + 1])).date(), i + 2
    if o == "BEFORE":
        return empf.date() < _datum(str(args[i + 1])).date(), i + 2
    if o == "ON":
        return empf.date() == _datum(str(args[i + 1])).date(), i + 2
    if o == "UID":
        return m["uid"] in menge(str(args[i + 1]), mails[-1]["uid"] if mails else 0), i + 2
    if o == "NOT":
        k, j = _kriterium(args, i + 1, m, nr, mails)
        return not k, j
    if o == "OR":
        k1, j = _kriterium(args, i + 1, m, nr, mails)
        k2, j = _kriterium(args, j, m, nr, mails)
        return k1 or k2, j
    if re.match(r"^[\d*:,]+$", o):
        return nr in menge(o, len(mails)), i + 1
    raise Fehler(f"Kriterium {o}")


# ── Server ──────────────────────────────────────────────────────────────────

async def _verbindung(leser, schreiber) -> None:
    quelle = (schreiber.get_extra_info("peername") or ("?",))[0]
    s = Sitzung(leser, schreiber, quelle)
    try:
        if not aktiv():
            await s.senden(b"* BYE IMAP-Zugang nicht eingeschaltet\r\n")
            return
        await s.senden(f"* OK [CAPABILITY {FAEHIGKEITEN}] Notbetrieb bereit\r\n".encode())
        while True:
            z = await s.zeile()
            if z is None:
                return
            try:
                teile = zerlegen(z)
            except Fehler:
                await s.senden(b"* BAD Befehl nicht lesbar\r\n")
                continue
            if len(teile) < 2 or isinstance(teile[0], list) or isinstance(teile[1], list):
                await s.senden(b"* BAD Befehl fehlt\r\n")
                continue
            await s.ausfuehren(str(teile[0]), str(teile[1]), teile[2:])
    except (ConnectionAbortedError, ConnectionError, Fehler):
        pass
    except Exception as exc:                                        # noqa: BLE001
        log.warning("Notbetrieb-IMAP: Sitzung %s abgebrochen: %s", quelle, exc)
    finally:
        try:
            schreiber.close()
        except Exception:                                           # noqa: BLE001
            pass


async def starten(tls_kontext, port: int = PORT, host: str = "0.0.0.0", *, ohne_tls_nur_test: bool = False):
    """Listener starten. Ohne TLS nur, wenn ein Test es ausdrücklich verlangt."""
    if tls_kontext is None and not ohne_tls_nur_test:
        log.info("Notbetrieb-IMAP nicht gestartet — kein TLS-Zertifikat")
        return None
    server = await asyncio.start_server(_verbindung, host, port, ssl=tls_kontext, limit=MAX_ZEILE)
    log.info("Notbetrieb-IMAP auf Port %s (TLS: %s)", port, "ja" if tls_kontext else "NEIN — Test")
    return server
