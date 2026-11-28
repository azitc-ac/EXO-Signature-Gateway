import smtplib
import ssl
import logging
import contextlib
import contextvars
import threading
import time as _time

import config
import settings_store
import stats

log = logging.getLogger(__name__)

# Mixed-fork "send_to_all" registry: Message-ID -> monotonic timestamp of a
# CONFIRMED send-to-all. When Exchange bifurcates a mixed internal/external
# mail, both forks reach the gateway with the same Message-ID. In send_to_all
# mode the FIRST fork signs and sends to ALL header recipients (one delivery
# per recipient, verified); the sibling then finds the MID here and drops
# (its recipients are already covered). Only CONFIRMED (Graph-accepted) sends
# register — a failed send never registers, so a sibling never drops into a
# hole (fail-safe: it retries or delivers scoped, never loss).
_mixed_fork_registry: dict[str, float] = {}
_mixed_fork_lock = threading.Lock()
_MIXED_FORK_TTL = 180  # seconds


def _message_id(content_bytes: bytes) -> str:
    import email as _em
    try:
        return (_em.message_from_bytes(content_bytes).get("Message-ID") or "").strip()
    except Exception:
        return ""


def _handle_mixed_fork(mail_from: str, rcpt_tos: list[str],
                        content_bytes: bytes, force_mime: bool) -> bool:
    """send_to_all handling of ONE bifurcated fork.

    Returns True  -> fork fully handled (this fork became the send-to-all
                     carrier, OR a sibling already did a confirmed send-to-all
                     so this fork is safely dropped).
    Returns False -> caller must deliver THIS fork normally (scoped): the
                     fail-safe path when no confirmed send-to-all exists (no
                     Message-ID, no header recipients, or the send-to-all
                     Graph call failed). Never drops without a confirmed
                     alternative delivery -> never loses mail.
    """
    import email as _em
    import graph_reinject

    mid = _message_id(content_bytes)
    if not mid:
        return False  # cannot dedup safely -> deliver scoped (fail-safe)

    now = _time.monotonic()
    with _mixed_fork_lock:
        for k in [k for k, t in _mixed_fork_registry.items() if now - t > _MIXED_FORK_TTL]:
            del _mixed_fork_registry[k]
        if mid in _mixed_fork_registry:
            log.info("Mixed-fork: sibling fork for MID %.24s already covered by a "
                     "confirmed send-to-all — dropping %s (Reply-All preserved)",
                     mid, rcpt_tos)
            return True

    header_rcpts = _header_recipients(content_bytes)
    if not header_rcpts:
        return False  # nothing to expand to -> deliver scoped (fail-safe)

    _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
    log.info("Mixed-fork: first fork for MID %.24s — send-to-all %s -> %s "
             "(signed, full headers)", mid, rcpt_tos, header_rcpts)
    if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
        ok = graph_reinject.send_via_graph_mime(mail_from, header_rcpts, content_bytes)
    else:
        ok = graph_reinject.send_via_graph(mail_from, header_rcpts, content_bytes)

    if ok:
        stats.increment("graph_api_calls")
        with _mixed_fork_lock:
            _mixed_fork_registry[mid] = _time.monotonic()  # confirm -> siblings drop
        return True

    # Send-to-all FAILED — do NOT register (so a sibling can retry and no fork
    # drops into a hole). Fall through to scoped delivery of THIS fork.
    log.warning("Mixed-fork: send-to-all failed for MID %.24s — delivering this "
                "fork scoped instead (fail-safe, no loss)", mid)
    return False


def _header_recipients(content_bytes: bytes) -> list[str]:
    """All To/Cc addresses from the MIME headers, lowercased, deduped."""
    import email as _em
    import email.utils as _eu
    try:
        msg = _em.message_from_bytes(content_bytes)
        seen: set[str] = set()
        out: list[str] = []
        for hdr in ("To", "Cc"):
            for _, addr in _eu.getaddresses([msg.get(hdr) or ""]):
                a = addr.strip().lower()
                if a and a not in seen:
                    seen.add(a)
                    out.append(a)
        return out
    except Exception:
        return []


def _is_bifurcated(rcpt_tos: list[str], content_bytes: bytes) -> bool:
    """True when the message's To/Cc headers list recipients that are NOT in
    this transaction's SMTP envelope — the signature of an Exchange-bifurcated
    mixed internal/external send (each fork's envelope covers a subset while
    the headers still show the full original recipient list)."""
    header_addrs = set(_header_recipients(content_bytes))
    if not header_addrs:
        return False
    rcpt_set = {r.strip().lower() for r in rcpt_tos}
    return bool(header_addrs - rcpt_set)


def _fail_delivery(mail_from: str, rcpt_tos: list[str],
                   content_bytes: bytes, grund: str, queue: bool) -> None:
    """Zustellfehler behandeln.

    queue=True (automatischer Weg): Fallnetz — Mail NICHT verlieren, sondern in die
    Warteschlange legen (Grund „delivery_failed"). Aufrufer kehrt normal zurück →
    Exchange bekommt 250 OK, die Mail wartet auf einen Retry (Freigabe in der
    Oberfläche). Sichtbar über `delivery_queued` und die Warteschlangen-Übersicht.

    queue=False (manueller Retry aus der Oberfläche): NICHT erneut einreihen,
    sondern werfen — damit die Freigabe ehrlich „fehlgeschlagen" meldet und die
    Mail in der Queue bleibt, statt „OK" vorzutäuschen.

    Scheitert selbst das Einreihen, wird laut geworfen — lieber ein Fehler als ein
    stiller Verlust.
    """
    if not queue:
        raise RuntimeError(f"Zustellung fehlgeschlagen ({grund})")
    eingereiht = _eingereiht.get()
    if eingereiht is not None:
        eingereiht.update(r.strip().lower() for r in rcpt_tos)
    try:
        import held_mails
        mid = held_mails.hold(mail_from, list(rcpt_tos), content_bytes,
                              reason="delivery_failed")
        stats.increment("delivery_queued")
        log.warning("Zustellung fehlgeschlagen (%s) — Mail in Warteschlange "
                    "gelegt (id=%s, from=%s, to=%s). Automatischer Retry (Exchange-Kadenz) "
                    "oder Freigabe über die Oberfläche.",
                    grund, mid, mail_from, rcpt_tos)
    except Exception as exc:                                  # noqa: BLE001
        log.error("Unzustellbare Mail konnte NICHT eingereiht werden (Grund %s): %s",
                  grund, exc)
        raise RuntimeError(f"Zustellung fehlgeschlagen ({grund}); Einreihen ebenfalls: {exc}")


# Rückgabe von send(): Die Post lief über Graph sendMail mit MIME, und dabei
# legt Exchange IMMER eine Kopie in „Gesendete Elemente" des Absenders an. Der
# Aufrufer räumt das Duplikat auf, wo es sonst niemand tut. Alle anderen Wege
# geben None zurück (wie bisher).
WEG_GRAPH_MIT_KOPIE = "graph_mit_kopie"


# Empfänger, die während des laufenden send() in die Warteschlange gingen
# (_fail_delivery). Was nicht darin steht, gilt nach normaler Rückkehr als
# zugestellt — dafür trägt held_mails gleiche, noch wartende Kopien aus.
_eingereiht: contextvars.ContextVar[set | None] = contextvars.ContextVar(
    "reinject_eingereiht", default=None)
_zuletzt: contextvars.ContextVar[bool] = contextvars.ContextVar("reinject_zuletzt", default=False)


def zuletzt_eingereiht() -> bool:
    """Hat der letzte `send()` in diesem Kontext (Task) Empfänger in die
    Warteschlange gelegt? Der Handler protokolliert die Mail dann als
    „zustellfehler“ statt als zugestellt."""
    return _zuletzt.get()


WEG_NOTBETRIEB = "notbetrieb"

# Notbetrieb: Das Webmail lässt seine Post durch den Handler laufen (Signatur,
# Banner, Disclaimer, S/MIME wie im Normalbetrieb). Exchange ist dann nicht
# erreichbar — die fertige Nachricht geht stattdessen an `ziel(mail_from,
# rcpt_tos, content_bytes)`. Gesetzt NUR für die Dauer dieses einen Aufrufs
# (notbetrieb_senden); der reguläre Mailfluss sieht die Variable nie.
_umleitung: contextvars.ContextVar = contextvars.ContextVar("reinject_umleitung", default=None)


@contextlib.contextmanager
def umleiten(ziel):
    marke = _umleitung.set(ziel)
    try:
        yield
    finally:
        _umleitung.reset(marke)


def umgeleitet() -> bool:
    return _umleitung.get() is not None


def _notfall_ausgerufen() -> bool:
    try:
        import notbetrieb
        return notbetrieb.ausgerufen()
    except Exception:                                               # noqa: BLE001
        return False


def send(mail_from: str, rcpt_tos: list[str], content_bytes: bytes,
         force_mime: bool = False, queue_on_failure: bool = True) -> str | None:
    """Zustellen (siehe `_senden`) und danach festhalten, wer die Nachricht
    bekommen hat. Eine Kopie, die wegen eines früheren Zustellfehlers noch in
    der Warteschlange liegt, würde sonst ein zweites Mal zugestellt."""
    ziel = _umleitung.get()
    if ziel is not None:
        ziel(mail_from, list(rcpt_tos), content_bytes)
        return WEG_NOTBETRIEB
    if _notfall_ausgerufen():
        # Exchange ist ausgefallen: nicht zurück an Exchange, sondern in die
        # Kopie bzw. in den Direktversand. Scheitert das, geht die Mail in die
        # Warteschlange wie jeder Zustellfehler — nach dem Notfall stellt die
        # Freigabe sie dann regulär zu.
        import notbetrieb
        try:
            notbetrieb.zustellen(mail_from, list(rcpt_tos), content_bytes, einreihen=queue_on_failure)
            log.info("NOTFALL: in die Kopie bzw. Direktversand: from=%s to=%s", mail_from, rcpt_tos)
        except Exception as exc:                                    # noqa: BLE001
            log.warning("NOTFALL: Zustellung im Notbetrieb fehlgeschlagen: %s", exc)
            _fail_delivery(mail_from, rcpt_tos, content_bytes, f"notbetrieb: {exc}", queue_on_failure)
        return WEG_NOTBETRIEB
    eingereiht: set = set()
    marke = _eingereiht.set(eingereiht)
    try:
        weg = _senden(mail_from, rcpt_tos, content_bytes, force_mime, queue_on_failure)
    finally:
        _eingereiht.reset(marke)
    _zuletzt.set(bool(eingereiht))
    zugestellt = [r for r in rcpt_tos if r.strip().lower() not in eingereiht]
    if zugestellt:
        try:
            import held_mails
            held_mails.merke_zugestellt(content_bytes, zugestellt)
        except Exception as exc:                              # noqa: BLE001
            log.warning("held_mails: Zustellung nicht vermerkt: %s", exc)
    return weg


def _senden(mail_from: str, rcpt_tos: list[str], content_bytes: bytes,
            force_mime: bool = False, queue_on_failure: bool = True) -> str | None:
    """
    Re-inject a mail.  Mode is controlled by REINJECT_MODE setting (canonical
    values via settings_store.reinject_mode(); "imap"/"smtp587" are legacy
    aliases for "graph_plus"):
      "smtp"       — forward via SMTP to EXO smarthost (requires port 25 outbound)
      "graph"      — "Graph-only": Graph API sendMail only (HTTPS, Azure).
                     Bifurcation handled by the Graph send-to-all heuristic, NO 587.
      "graph_plus" — "Graph++": graph as the workhorse PLUS two gap-fillers —
                     IMAP APPEND (inbound decrypted S/MIME) and port 587
                     (deterministic delivery of bifurcated mixed-recipient mail).

    force_mime=True routes through the raw-MIME Graph path even for plain
    multipart messages (e.g. inbound decrypted mail that must preserve
    attachments and avoid lossy JSON reconstruction).

    Bifurcated transactions (To/Cc headers list more recipients than this
    transaction's envelope — mixed internal/external sends split by Exchange;
    BOTH forks reach the gateway with FromMemberOf-only transport rules).
    OUTBOUND ONLY — the sender must be a tenant mailbox; inbound external mail
    is delivered by the normal per-mode path below (see the _sender_internal
    guard):

      1. Preferred: authenticated SMTP submission (port 587, XOAUTH2 as the
         sender, requires SMTP.SendAsApp). Only SMTP separates envelope
         recipients from displayed headers — every fork delivers exactly to
         its own envelope while everyone sees the full original To/Cc
         (Reply-All intact).
      2. Graph-only fallback (no SMTP.SendAsApp): Graph cannot separate
         delivery from display, so instead the fork that carries EXTERNAL
         envelope recipients sends to the FULL header list (send-to-all:
         correct headers AND correct delivery for everyone in one shot,
         Message-ID dedup prevents double sends across forks), while forks
         whose envelope is entirely covered by another fork's send-to-all
         (e.g. the internal-only fork of a mixed send) are DROPPED here —
         their recipients already receive the send-to-all copy.
    """
    mode = settings_store.reinject_mode()

    # ── Domänen-Routing: eigene Next-Hop-Ziele vorab abspalten ───────────────
    # Empfänger, deren Domäne auf ein konfiguriertes Ziel (≠ EXO) zeigt, werden
    # UNVERÄNDERT dorthin relayt (kein DKIM-Strip, kein Graph/Modus-Pfad — ein
    # Fremdserver ist über Graph nicht erreichbar und signiert nicht neu). Der
    # Rest läuft den normalen Modus-Pfad unten. Eine Transaktion kann Empfänger
    # mehrerer Domänen tragen → je Ziel eine eigene SMTP-Sitzung.
    #
    # NUR im smtp-Modus, aus zwei Gründen: (1) das Relay setzt den Smarthost-Weg
    # ohnehin voraus (durchgesetzt in smtp_relay.pruefe); (2) in den Graph-Modi
    # würde das Abspalten die Bifurkations-`send_to_all`-Heuristik täuschen — sie
    # sendet an die volle Header-Liste und stellte die schon gerouteten Empfänger
    # ein zweites Mal zu.
    import domain_routing
    if mode == "smtp":
        _gruppen = domain_routing.gruppiere(rcpt_tos)
        if any(zid != domain_routing.EXO for zid in _gruppen):
            import relay_stats
            for _zid, _rs in _gruppen.items():
                if _zid == domain_routing.EXO:
                    continue
                try:
                    _relay_to_target(_zid, mail_from, _rs, content_bytes)
                    relay_stats.merke_routing(_zid, len(content_bytes), ok=True,
                                              absender=mail_from, empfaenger=_rs,
                                              inhalt=content_bytes)
                except Exception as exc:                      # noqa: BLE001
                    relay_stats.merke_routing(_zid, len(content_bytes), ok=False,
                                              absender=mail_from, empfaenger=_rs,
                                              inhalt=content_bytes,
                                              fehler_text=f"{type(exc).__name__}: {exc}")
                    _fail_delivery(mail_from, _rs, content_bytes,
                                   f"Routing→{_zid}: {exc}", queue_on_failure)
            rcpt_tos = _gruppen.get(domain_routing.EXO, [])
            if not rcpt_tos:
                return
    elif domain_routing.ziele() and any(
            domain_routing.route_fuer(d) != domain_routing.EXO
            for d in {(r or "").rsplit("@", 1)[-1].lower() for r in rcpt_tos if "@" in (r or "")}):
        log.warning("Domänen-Routing ist konfiguriert, aber der Rückweg steht auf "
                    "%r — Routing wirkt nur im smtp-Modus; Post geht an EXO", mode)

    # ── Hybrid-Nutzer: IMMER über den Smarthost ──────────────────────────────
    # Absender mit Postfach in der lokalen Exchange-Organisation (in EXO nur ein
    # MailUser). Graph sendMail und 587 SendAsApp senden ALS ein EXO-Postfach,
    # das es nicht gibt (ErrorInvalidUser); IMAP APPEND legte die Post nur in
    # Posteingänge ab, statt sie zuzustellen. Der einzige Rückweg ist der
    # Smarthost — wie im Modus `smtp`, X-Sig-Applied setzt der Aufrufer. Die
    # Option wirkt nur in `smtp` (exo_mailboxes.hybrid_aktiv); kommt in einem
    # Graph-Modus doch Post an (Modus nachträglich umgestellt, Verteilerliste
    # noch nicht nachgezogen), bleibt der Smarthost der einzige Versuch —
    # scheitert er, geht die Mail in die Warteschlange, nicht verloren.
    import exo_mailboxes
    if exo_mailboxes.ist_hybrid(mail_from):
        if mode != "smtp":
            log.warning("Hybrid-Absender %s im Rückweg %r — Zustellung nur über den "
                        "Smarthost möglich; Hybrid-Nutzer wirken nur im Modus smtp",
                        mail_from, mode)
        try:
            _send_smtp(mail_from, rcpt_tos, content_bytes)
        except Exception as exc:                              # noqa: BLE001
            _fail_delivery(mail_from, rcpt_tos, content_bytes,
                           f"Hybrid-Absender, Smarthost: {exc}", queue_on_failure)
        return None

    # Bifurcation handling (587 as-sender + send_to_all) is an OUTBOUND-only
    # feature: it only makes sense for mail SENT BY a tenant mailbox that
    # Exchange split into forks. Inbound external mail (e.g. an S/MIME-signed
    # message from a CA with an external Cc) can ALSO show To/Cc header
    # recipients outside this fork's envelope, but it must NEVER enter this
    # path — the sender isn't ours, so 587 as-sender auth fails and send_to_all
    # degrades into per-recipient mailbox injects that double-deliver
    # (2026-07-08: an inbound SwissSign mail with an external Cc was delivered
    # to the internal recipient TWICE). Empty known-set = "unknown" -> treat as
    # NOT internal (conservative: at worst scoped delivery, never a duplicate).
    _known = exo_mailboxes.known_addresses()
    _sender_internal = bool(_known) and (mail_from or "").strip().lower() in _known

    # ── Sende-Identitäten: Post bevorzugt über Graph, unabhängig vom Modus ──────
    # Exchange erzeugt die Nachricht dann aus dem eigenen Haus (kein SMTP-Hop mit
    # Gateway-IP) → SPF/DKIM/DMARC greifen ohne DNS-Eintrag fürs Gateway. Abschalter
    # IDENT_DELIVER_VIA_GRAPH (Vorgabe an, in „Erweitert"). Scheitert Graph (App-Scope
    # noch nicht propagiert o.ä.), fällt es auf den normalen Modus-Pfad unten zurück
    # — dann geht die Post über den Smarthost, nur ohne den SPF-Vorteil.
    if settings_store.get("IDENT_DELIVER_VIA_GRAPH") is not False:
        _ist_ident = False
        try:
            import sende_identitaeten
            _ist_ident = sende_identitaeten.ist_identitaets_adresse(mail_from)
        except Exception:                                     # noqa: BLE001
            _ist_ident = False
        if _ist_ident:
            import graph_reinject
            # IMMER Raw-MIME: Identitätspost kommt fertig vom Gerät — sie 1:1 an
            # Graph zu geben bewahrt Aufbau/Anhänge und vermeidet die
            # JSON-Rekonstruktion (`send_via_graph`), die einfache Nachrichten als
            # TNEF/`winmail.dat` verpackt bzw. in Outlook Classic weiß darstellt
            # (CLAUDE.md: „Immer Raw-MIME verwenden").
            ok = graph_reinject.send_via_graph_mime(mail_from, rcpt_tos, content_bytes)
            if ok:
                stats.increment("graph_api_calls")
                log.info("Identitätspost über Graph zugestellt: from=%s to=%s",
                         mail_from, rcpt_tos)
                return
            log.warning("Identitätspost: Graph-Versand fehlgeschlagen für %s — "
                        "Rückfall auf Modus %s (ohne SPF-Vorteil)", mail_from, mode)
            # fällt durch zum normalen Modus-Pfad unten

    if (mode in ("graph", "graph_plus")
            and _sender_internal
            and _is_bifurcated(rcpt_tos, content_bytes)):
        # 587 (deterministische Zustellung ALS der Absender) gehört zu Graph++
        # (`graph_plus`). Im reinen `graph` (Graph-only) gibt es 587 bewusst
        # NICHT — dort trägt allein die send-to-all-Heuristik die gemischten
        # Empfänger (volle Reply-All, verlustfrei; siehe unten).
        if mode == "graph_plus":
            import smtp_submit
            log.info("Bifurcated transaction (envelope ⊂ headers) — trying SMTP "
                     "587 as-sender for header-preserving delivery: to=%s", rcpt_tos)
            if smtp_submit.deliver_outbound_as_sender(mail_from, rcpt_tos, content_bytes):
                return

        # ── Graph send-to-all für bifurkierte Forks ──────────────────────────
        # Der EINZIGE Weg im Modus `graph`; im Modus `graph_plus` der Fallback,
        # falls 587 nicht greift. GRAPH_MIXED_FORK_MODE (siehe settings_store):
        #   "send_to_all" — erste Fork signiert + sendet an ALLE Header-Empfänger
        #      (genau eine Zustellung je Empfänger über die X-Sig-Applied-
        #      Ausnahme; Geschwister-Fork verwirft sich per Message-ID, sobald der
        #      Versand BESTÄTIGT ist; scheitert er, wird beschnitten zugestellt —
        #      nie Verlust). Volle Reply-All. Leichte Verzögerung möglich, wenn
        #      die Forks ein paar Sekunden auseinander eintreffen.
        #   "scoped" — fällt auf den Modus-Pfad unten durch (Header auf die
        #      Envelope-Empfänger beschnitten): kein Duplikat, aber Reply-All
        #      für diese Fork unvollständig.
        # Vorgabe: "send_to_all" (volle Reply-All, fail-safe).
        fork_mode = (settings_store.get("GRAPH_MIXED_FORK_MODE") or "send_to_all").strip().lower()
        if fork_mode == "send_to_all":
            if _handle_mixed_fork(mail_from, rcpt_tos, content_bytes, force_mime):
                return
            log.info("Mixed-fork send_to_all not applicable/failed — scoped %s "
                     "delivery for %s (fail-safe)", mode, rcpt_tos)
        else:
            log.info("GRAPH_MIXED_FORK_MODE=scoped — scoped %s delivery for %s "
                     "(Reply-All incomplete)", mode, rcpt_tos)

    if mode == "graph":
        import email as _em
        import graph_reinject
        _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
        # S/MIME payloads and any caller that requests MIME fidelity go via
        # the raw-MIME path; the JSON reconstruction path cannot handle PKCS7
        # payloads and loses attachments for complex multipart structures.
        if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
            ok = graph_reinject.send_via_graph_mime(mail_from, rcpt_tos, content_bytes)
        else:
            ok = graph_reinject.send_via_graph(mail_from, rcpt_tos, content_bytes)
        if ok:
            stats.increment("graph_api_calls")
            return
        if settings_store.get("GRAPH_SMTP_FALLBACK"):
            log.warning("Graph re-inject failed — falling back to SMTP (GRAPH_SMTP_FALLBACK enabled)")
            try:
                _send_smtp(mail_from, rcpt_tos, content_bytes)
            except Exception as exc:                          # noqa: BLE001
                _fail_delivery(mail_from, rcpt_tos, content_bytes, f"graph+smtp: {exc}", queue_on_failure)
        else:
            _grund = graph_reinject.fehlergrund(mail_from)
            _fail_delivery(mail_from, rcpt_tos, content_bytes,
                           "Graph fehlgeschlagen, SMTP-Fallback aus"
                           + (f" — {_grund}" if _grund else ""), queue_on_failure)
    elif mode == "graph_plus":
        import email as _em
        # ── Absender im Tenant → Graph sendMail ──────────────────────────────
        # Die Post nimmt so den Exchange-Zustellweg: Abwesenheitsantwort und
        # Posteingangsregeln greifen. IMAP APPEND legt nur in den Ordner ab —
        # bis v1.9.138 lief JEDE Post an einen Tenant-Empfänger so, auch rein
        # interne Mails zwischen aktivierten Postfächern, und keine einzige
        # davon löste eine Abwesenheitsantwort aus (Testlauf 11.10.2026).
        # Immer Raw-MIME: Die Post kommt fertig an, sie 1:1 weiterzugeben bewahrt
        # Aufbau und Anhänge (keine JSON-Rekonstruktion, CLAUDE.md).
        if _sender_internal:
            import graph_reinject
            if graph_reinject.send_via_graph_mime(mail_from, rcpt_tos, content_bytes):
                stats.increment("graph_api_calls")
                return WEG_GRAPH_MIT_KOPIE
            log.warning("Graph++: sendMail für %s fehlgeschlagen — Rückfall auf "
                        "Ablage im Posteingang (ohne Posteingangsregeln)", mail_from)
            imap_failed = [r for r in rcpt_tos
                           if not _eingang_ablegen(r, content_bytes, mail_from)]
            if imap_failed:
                _grund = graph_reinject.fehlergrund(mail_from)
                _fail_delivery(mail_from, imap_failed, content_bytes,
                               "Graph sendMail und Ablage im Posteingang fehlgeschlagen"
                               + (f" — {_grund}" if _grund else ""),
                               queue_on_failure)
            return None
        # ── Absender außerhalb → IMAP APPEND ─────────────────────────────────
        # Graph kann nicht im Namen einer fremden Adresse senden. Das betrifft
        # eingehende S/MIME-Post (entschlüsselt, Signatur entfernt, oder bei
        # gescheiterter Entschlüsselung unverändert). Die Abwesenheitsantwort
        # schickt dann das Gateway selbst (oof_ablage, seit v1.9.144);
        # Posteingangsregeln laufen für diese Mails nicht (bekannte Grenze).
        # Ablage geht nur in Postfächer des Tenants (IMAP APPEND wie Graph-Anlage).
        # Externe Empfänger fallen auf Graph sendMail zurück.
        imap_failed = [r for r in rcpt_tos
                       if not _eingang_ablegen(r, content_bytes, mail_from)]
        if imap_failed:
            import graph_reinject
            _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
            if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
                ok = graph_reinject.send_via_graph_mime(mail_from, imap_failed, content_bytes)
            else:
                ok = graph_reinject.send_via_graph(mail_from, imap_failed, content_bytes)
            if ok:
                stats.increment("graph_api_calls")
            else:
                _fail_delivery(
                    mail_from, imap_failed, content_bytes,
                    "IMAP APPEND und Graph beide fehlgeschlagen "
                    "(IMAP.AccessAsApp / Graph sendMail prüfen)", queue_on_failure)
    else:
        try:
            _send_smtp(mail_from, rcpt_tos, content_bytes)
        except Exception as exc:                              # noqa: BLE001
            _fail_delivery(mail_from, rcpt_tos, content_bytes, f"SMTP-Reinject: {exc}", queue_on_failure)


def _eingang_ablegen(rcpt_to: str, content_bytes: bytes, mail_from: str) -> bool:
    """Graph++: Post eines externen Absenders in den Posteingang EINES Empfängers legen.

    Weg nach `settings_store.graph_plus_eingang()`:

    * ``"imap"`` (Vorgabe) — IMAP APPEND, wie bisher.
    * ``"graph"`` — Graph-Anlage mit PR_MESSAGE_FLAGS beim Anlegen
      (`graph_reinject.deliver_to_mailbox`; Message-ID, Zeiten und Thread-Bezug
      seit v1.12.32). Gemessen 15.11.2026: trägt HTML mit Inline-Bild, große
      Anhänge, Nur-Text und Threading. Verschlüsselte oder signierte Einladungen
      externer Absender kommen über diesen Weg als .ics-Anlage an.

    Noch verschlüsselte oder noch signierte Post (``application/pkcs7-mime``,
    ``multipart/signed``) geht IMMER per IMAP: Der JSON-Nachbau kann sie nicht
    abbilden — aus einer Mail, die das Gateway mangels Schlüssel verschlüsselt
    durchreicht, würde eine Anlage „smime.p7m", die Outlook nicht mehr
    entschlüsselt. Das ist dieselbe Regel wie im Modus ``graph`` (MIME-Weg).

    Scheitert die Graph-Anlage, wird IMAP versucht; scheitert auch das, liefert
    die Funktion False und der Aufrufer geht seinen bisherigen Weg (Graph
    sendMail bzw. Warteschlange) — verloren geht nichts.
    """
    import smtp_submit
    if settings_store.graph_plus_eingang() == "graph":
        import email as _em
        _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
        if _ct in ("multipart/signed", "application/pkcs7-mime",
                   "application/x-pkcs7-mime"):
            log.info("Eingang per Graph: %s für %s — S/MIME-Hülle, daher IMAP APPEND",
                     _ct, rcpt_to)
        else:
            import exo_mailboxes
            known = exo_mailboxes.known_addresses()
            if known and rcpt_to.strip().lower() not in known:
                return False      # nicht im Tenant: wie beim IMAP-Weg → Graph sendMail
            import graph_reinject
            if graph_reinject.deliver_to_mailbox(mail_from, [rcpt_to], content_bytes):
                stats.increment("eingang_graph")
                log.info("Eingang per Graph abgelegt: to=%s from=%s", rcpt_to, mail_from)
                return True
            log.warning("Eingang per Graph für %s fehlgeschlagen — Rückfall auf "
                        "IMAP APPEND", rcpt_to)
    if smtp_submit.deliver_inbound_imap(rcpt_to, content_bytes, mail_from):
        stats.increment("eingang_imap")
        return True
    return False


# Veraltete Signatur-Kopfzeilen, die vor dem smtp-Reinject weg müssen.
_STALE_SIGN_HEADERS = (b"dkim-signature", b"arc-seal",
                       b"arc-message-signature", b"arc-authentication-results")


def _strip_stale_signatures(content_bytes: bytes) -> bytes:
    """Entfernt veraltete DKIM-/ARC-Kopfzeilen — byte-genau, nur im Kopf.

    Warum: Beim smtp-Reinject läuft die Mail ZURÜCK durch Exchange, das beim
    finalen Ausgang ohnehin neu DKIM-/ARC-signiert. Die vor dem Gateway gesetzte
    DKIM passt nach dem Body-Umbau aber nicht mehr (`body hash did not verify`)
    und die ARC-Kette bricht (`cv=fail`) — beide bleiben als kaputte Signaturen
    kleben und drücken die Zustellbarkeit (bei strengen Empfängern → Spam).

    Der Body wird NICHT angefasst (kein Reparsen/Reserialisieren) — eine
    S/MIME-Signatur, die den Inhalt deckt, bleibt damit intakt. DKIM/ARC sind
    äußere Nachrichten-Kopfzeilen und nicht Teil des S/MIME-signierten Inhalts.
    """
    sep = b"\r\n\r\n"
    idx = content_bytes.find(sep)
    if idx < 0:
        sep = b"\n\n"
        idx = content_bytes.find(sep)
        if idx < 0:
            return content_bytes  # kein Kopf/Body-Trenner → unangetastet
    head = content_bytes[:idx]
    rest = content_bytes[idx:]                       # Trenner + Body, unverändert
    nl = b"\r\n" if b"\r\n" in head else b"\n"
    out: list[bytes] = []
    skip = False
    for line in head.split(nl):
        if line[:1] in (b" ", b"\t"):               # Fortsetzungszeile (gefalteter Header)
            if not skip:
                out.append(line)
            continue
        name = line.split(b":", 1)[0].strip().lower()
        skip = name in _STALE_SIGN_HEADERS
        if not skip:
            out.append(line)
    return nl.join(out) + rest


def _send_smtp(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> None:
    content_bytes = _strip_stale_signatures(content_bytes)
    port = settings_store.get("EXO_PORT") or config._ENV_SEEDS["EXO_PORT"]
    smarthost = config.EXO_SMARTHOST or settings_store.get("EXO_SMARTHOST") or ""

    if not smarthost:
        raise RuntimeError("EXO_SMARTHOST not configured")

    relay_user = settings_store.get("RELAY_USER") or ""
    relay_pass = settings_store.get("RELAY_PASSWORD") or ""

    # Present our proxy TLS cert during STARTTLS so EXO's inbound connector
    # (TlsSenderCertificateName = gateway.contoso.com) matches and bypasses
    # spam filtering for stripped inbound mails. Das Zertifikat des Smarthosts
    # wird geprüft (tls_ausgehend) — *.mail.protection.outlook.com, gemessen
    # 10.11.2026; ein Smarthost als IP-Adresse scheitert daran.
    import tls_ausgehend
    tls_ctx = tls_ausgehend.kontext(pruefen=True, client_zert=True)

    try:
        import aussenadresse
        # EHLO mit der FQDN des Gateways statt der internen Docker-IP (die sonst
        # als [172.x.x.x] im Empfänger-Trace steht); None → smtplib-Default.
        with smtplib.SMTP(smarthost, port, timeout=30,
                          local_hostname=aussenadresse.ehlo_hostname()) as smtp:
            smtp.ehlo()
            smtp.starttls(context=tls_ctx)
            smtp.ehlo()
            if relay_user and relay_pass:
                smtp.login(relay_user, relay_pass)
            smtp.sendmail(mail_from, rcpt_tos, content_bytes)
        log.info("SMTP re-inject OK: from=%s to=%s via %s:%s", mail_from, rcpt_tos, smarthost, port)
    except ssl.SSLCertVerificationError as exc:
        log.error("SMTP re-inject failed: Zertifikat des Smarthosts %s nicht gültig (%s)%s",
                  smarthost, exc.verify_message,
                  " — Smarthost als Namen eintragen, nicht als IP" if tls_ausgehend.ist_ip(smarthost) else "")
        raise
    except Exception as exc:
        log.error("SMTP re-inject failed: from=%s to=%s: %s", mail_from, rcpt_tos, exc)
        raise


def _an_postfix(mail_from: str, rcpt_tos: list[str], content_bytes: bytes,
                host: str, port: int) -> None:
    """Übergabe an den eigenen Postfix (Direktversand): Exchange-interne
    Kopfzeilen entfernen, DKIM-signieren, ohne TLS im internen Docker-Netz
    einliefern. Zustellen, Wiederholen und Unzustellbarkeitsberichte macht
    Postfix (siehe `direktversand.py`)."""
    import direktversand
    import dkim_signatur
    # Alte DKIM/ARC von vor dem Gateway entfernen (wie _send_smtp und der
    # 587-Weg): Der Rumpf ist verändert, sie bestünden beim Empfänger nicht
    # („dkim=fail“ neben der eigenen, ARC cv=fail). Fehlte hier bis v1.12.146.
    inhalt, ergebnis = dkim_signatur.signieren(
        _strip_stale_signatures(direktversand.kopf_bereinigen(content_bytes)))
    with smtplib.SMTP(host, port, timeout=30,
                      local_hostname="exo-signature-gateway") as smtp:
        smtp.ehlo()
        smtp.sendmail(mail_from, rcpt_tos, inhalt)
    log.info("Direktversand übergeben: from=%s to=%s (%s)", mail_from, rcpt_tos, ergebnis)


# Wartezeit auf die Antworten im Nachrichtenteil der TENANT-Weiterleitung an ein
# zweites Gateway (MAIL, RCPT, DATA, Ende der Daten). Die 30 s davor (Verbindung,
# EHLO, STARTTLS) bleiben, damit ein nicht erreichbares Ziel schnell als Fehler
# gilt. RFC 5321 §4.5.3.2 empfiehlt nach dem Datenende 10 Minuten — hier ist die
# eigene Annahmeseite die Grenze: aiosmtpd (1.4.6) setzt seinen Zähler
# (SMTP(timeout=300)) beim DATA-Befehl zurück, nicht bei den Datenzeilen und
# nicht während handle_DATA — 300 s nach DATA schließt es die Verbindung.
# Wartete die Weiterleitung länger, ginge das „250“ an Exchange verloren und
# Exchange lieferte erneut ein. 240 s lassen Raum für Übertragung und Prüfung.
# ⚠️ Nur in einem eigenen Thread (handler: asyncio.to_thread). handle_DATA läuft
# in der einen Ereignisschleife des SMTP-Servers; ein blockierender Aufruf hält
# dort JEDE andere Verbindung an. Die Aufrufe aus send() behalten deshalb 30 s.
# Messung 12.11.2026: Das Ziel antwortete nach 33 s (eigener Rückweg 30 s
# blockiert, dann eingereiht), die Weiterleitung hatte nach 30 s aufgegeben —
# Exchange lieferte erneut ein, die erste Fassung lag schon in der Warteschlange.
ANTWORT_NACH_DATEN_S = 240


def _ziel_sitzung(ziel_id: str, cfg: dict) -> smtplib.SMTP:
    """Verbindung zu einem Routing-Ziel bis nach STARTTLS und Anmeldung — EIN
    Weg für die Weiterleitung und die Probe (notbetrieb.onprem_probe), damit
    die Probe genau das prüft, was die Zustellung benutzt."""
    import tls_ausgehend
    tls_ctx = tls_ausgehend.kontext(pruefen=cfg["tls_pruefen"], client_zert=True)
    if cfg["starttls"] and not cfg["tls_pruefen"]:
        log.info("Routing-Ziel %s: TLS ohne Zertifikatsprüfung (so eingestellt)", ziel_id)
    import aussenadresse
    smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30, local_hostname=aussenadresse.ehlo_hostname())
    try:
        smtp.ehlo()
        if cfg["starttls"]:
            smtp.starttls(context=tls_ctx)
            smtp.ehlo()
        if cfg["user"] and cfg["pass"]:
            smtp.login(cfg["user"], cfg["pass"])
    except Exception:
        smtp.close()
        raise
    return smtp


def _relay_to_target(ziel_id: str, mail_from: str, rcpt_tos: list[str],
                     content_bytes: bytes, antwort_s: float = 30) -> None:
    """Pass-through-Relay an ein konfiguriertes Next-Hop-Ziel (NICHT EXO).

    Anders als `_send_smtp`: KEIN `_strip_stale_signatures` — die Mail wird
    byte-genau durchgereicht. Ein Fremdserver signiert nicht neu; ein Strip
    zerstörte gültige DKIM-/S-MIME-Signaturen. Kein `X-Sig-Applied` nötig: der
    Hop verlässt den Exchange-Transport, die FromMemberOf-Regel sieht ihn nie
    wieder.

    Wie beim EXO-Weg präsentieren wir das Proxy-TLS-Zertifikat — ein
    on-prem-Receive-Connector kann uns darüber als vertrauenswürdigen Partner
    erkennen (Kern des Hybrid-Vertrauens; die Betreiber-Seite dazu ist Sache der
    Connector-/Zert-Konfiguration auf beiden Seiten, nicht dieses Hops).
    """
    import domain_routing
    cfg = domain_routing.aufloesen(ziel_id)
    host, port = cfg["host"], cfg["port"]
    if not host:
        raise RuntimeError(f"Routing-Ziel {ziel_id!r} hat keinen Host konfiguriert")

    if ziel_id == domain_routing.DIREKT:
        _an_postfix(mail_from, rcpt_tos, content_bytes, host, port)
        return

    # Client-Zert für den on-prem-Hop: das gemeinsame Zert. Das on-prem-Vertrauen
    # läuft IP-basiert (ExternalAuthoritative-Receive-Connector auf die LB-Quell-IP),
    # NICHT über den Zertnamen — daher braucht der Hop kein separates SMTP-Zert.
    # (Der cert-genaue Weg via TlsDomainCapabilities/XOORG ist bewusst zurückgestellt
    # — siehe Forschungsnotiz; STARTTLS selbst braucht nur irgendein gültiges Zert.)
    #
    # Das Zertifikat des ZIELS wird geprüft, ausser das Ziel ist ausdrücklich
    # anders eingestellt (tls_pruefen; Bestandsziele vor v1.10.112 übernommen
    # ohne Prüfung, bis sie bewusst gespeichert werden).
    with _ziel_sitzung(ziel_id, cfg) as smtp:
        # Ab hier wartet das Ziel ggf. auf seinen eigenen Rückweg (ein zweites
        # Gateway verarbeitet und liefert erst ein, bevor es antwortet). Gibt die
        # äußere Stelle früher auf als die innere, liefert der Absender erneut
        # ein, obwohl das Ziel die Nachricht schon angenommen hat.
        smtp.sock.settimeout(antwort_s)
        smtp.sendmail(mail_from, rcpt_tos, content_bytes)
    log.info("Routing re-inject OK: from=%s to=%s via Ziel %s (%s:%s)",
             mail_from, rcpt_tos, ziel_id, host, port)
