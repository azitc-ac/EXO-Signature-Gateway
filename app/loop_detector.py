import email.message
import hashlib

import settings_store

_DEFAULT_HEADER = "X-Sig-Applied"


def marker_wert() -> str:
    """Der Wert der Markierung — JE INSTALLATION (genauer: je Tenant) eigen.

    Bis v1.10.43 war er überall `1`. Eine Mail von einem ANDEREN Gateway
    (anderer Kunde, anderer Tenant) trug deshalb schon die Markierung, und die
    eingehenden S/MIME-Regeln des Empfängers nahmen sie aus: kein Zertifikat
    übernommen, verschlüsselte Post nicht entschlüsselt (gemessen 23.10.2026,
    a-az@azitc.org → alexander@zarenko.net). Jetzt erkennt jedes Gateway nur
    seine eigene Markierung.

    ⚠️ Beginnt mit `1-`: Regeln vor der Umstellung prüfen das UNVERANKERTE
    Muster `1` und nehmen die neue Markierung damit weiter aus, bis sie
    angeglichen sind (regel_kennung.py). Ohne diesen Präfix kreiste nach dem
    Update jede Mail, bis die Regeln nachgezogen sind.

    Zwei Gateways im selben Tenant (z. B. Test und Betrieb) teilen den Wert —
    gewollt: Post, die eines bearbeitet hat, soll die Regel des anderen nicht
    erneut umleiten. Ohne Tenant (frische Anlage, noch keine Regeln): `1`.
    """
    import config
    return wert_fuer(config.TENANT_ID or settings_store.get("TENANT_ID") or "")


def wert_fuer(tenant: str) -> str:
    """Die Markierung zu einer Tenant-ID — rein, ohne Einstellungen. Die EINE
    Ableitung für Gateway und tools/tenant_soll_check.py."""
    tenant = (tenant or "").strip().lower()
    if not tenant:
        return "1"
    return "1-" + hashlib.sha256(("exo-sig-loop:" + tenant).encode()).hexdigest()[:12]


def muster_fuer(tenant: str) -> str:
    """Das verankerte Regelmuster zu einer Tenant-ID (siehe marker_muster)."""
    return f"^{wert_fuer(tenant)}$"


def marker_muster() -> str:
    """Das Muster für die Ausnahme der Transportregeln (EXO wertet es als
    regulären Ausdruck aus) — VERANKERT, damit eine fremde Markierung nicht
    zufällig passt. Der Wert enthält nur 0–9, a–f und `-`."""
    return f"^{marker_wert()}$"


def header_name() -> str:
    """Der aktuell konfigurierte Loop-Header-Name (`LOOP_HEADER`).

    Öffentlich, damit jeder Rückweg, der eine Mail zurück in den Transport gibt
    (SMTP, Graph), denselben Namen setzt. Ein hartkodierter Name auf einem der
    Wege lässt Gateway und EXO-Regel auseinanderlaufen."""
    return settings_store.get("LOOP_HEADER") or _DEFAULT_HEADER


def _header() -> str:
    return header_name()


def is_signed(message: email.message.Message) -> bool:
    """Trägt die Mail UNSERE Markierung? Eine fremde zählt nicht."""
    return (message.get(_header()) or "").strip() == marker_wert()


def mark_as_signed(message: email.message.Message) -> None:
    h = _header()
    if h in message:
        del message[h]               # auch eine fremde Markierung ersetzen
    message[h] = marker_wert()


def mark_as_signed_bytes(raw: bytes) -> bytes:
    """Prepend the loop-detection header to a raw MIME byte string.

    PREPENDING (making it the very FIRST header) is the only insertion point
    that can never split a folded header. Inbound Exchange mail almost always
    leads with a multi-line (folded) Received: header; the previous
    "insert after the first CRLF" approach dropped the new header INTO that
    fold, which:
      - corrupted the original folded header, AND
      - folded the following continuation into this header's value, turning
        'X-Sig-Applied: 1' into 'X-Sig-Applied: 1 by <host> ... id ...;'.
    That corrupted value no longer exactly matched the transport-rule loop
    exception (pattern '1'), so the exception stopped firing and the message
    was re-routed back through the gateway -> loop (root cause of the
    2026-07-07 mail loops). Verified via raw-header inspection of a
    round-tripped message.

    Uses the message's own line ending so it never introduces a bare LF
    (which Exchange rejects: 550 5.6.11 BareLinefeedsAreIllegal). Idempotent:
    skips if OUR marker is already present at the top level. A FOREIGN marker
    (another gateway, other tenant — see marker_wert) is removed and replaced:
    keeping it would leave the mail unmarked for our own rules -> loop.
    """
    name = _header().encode()
    # Determine the header/body boundary and the message's line ending.
    boundary = raw.find(b"\r\n\r\n")
    eol = b"\r\n"
    if boundary == -1:
        nn = raw.find(b"\n\n")
        if nn != -1:
            boundary = nn
            eol = b"\n"
    header_block = raw[:boundary] if boundary != -1 else raw

    wert = marker_wert().encode()
    # Top-level lines of this header (folded continuations start with
    # whitespace, so a line-prefix check is correct here).
    prefix = name.lower() + b":"
    zeilen = header_block.split(eol)
    vorhanden = [z for z in zeilen if z.lower().startswith(prefix)]
    if vorhanden and all(z.split(b":", 1)[1].strip() == wert for z in vorhanden):
        return raw                                   # schon unsere Markierung
    if vorhanden:
        behalten, im_kopf = [], False
        for z in zeilen:
            if z.lower().startswith(prefix):
                im_kopf = True
                continue
            if im_kopf and z[:1] in (b" ", b"\t"):
                continue                             # Fortsetzung der alten Zeile
            im_kopf = False
            behalten.append(z)
        rest = raw[boundary:] if boundary != -1 else b""
        raw = eol.join(behalten) + rest

    return name + b": " + wert + eol + raw
