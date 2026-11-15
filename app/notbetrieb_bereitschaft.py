"""Notbetrieb: Ist alles bereit für den Ernstfall?

Ein Notbetrieb, der erst im Ernstfall geprüft wird, fällt im Ernstfall aus.
Deshalb jederzeit abrufbar (Seite Notbetrieb, Abnahme):

- Kopie aktuell (letzter Abgleich, Fehler)
- Rechte-Abbild und Profile frisch (wer darf was; Signaturdaten ohne Graph)
- Anmeldung: Domänencontroller verschlüsselt erreichbar
- Versand nach außen: Direktversand eingerichtet
- TLS-Zertifikat gültig (Webmail und SMTP)

Dazu der Stand des MX je eigener Domäne (`mx_stand`): zeigt er auf Exchange
Online oder schon auf das Gateway? Das ist der Schritt, den im Ernstfall ein
Mensch beim DNS-Anbieter macht — das Gateway kann ihn nur prüfen.

Jede Prüfung liefert dieselbe Form wie `abnahme._punkt`; UNBEKANNT ist
ausdrücklich kein OK.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

KOPIE_MAX_H = 1          # Abgleich alle 15 min — eine Stunde Pause ist ein Befund
ABBILD_MAX_H = 48        # täglich — zwei Tage ohne ist ein Befund


def _alter_h(iso) -> float | None:
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _p(name, zustand, befund="", zu_tun=""):
    import abnahme
    return abnahme._punkt(name, zustand, befund, zu_tun)


def _kopie(st: dict) -> dict:
    import abnahme
    letzter = st.get("letzter_lauf") or {}
    alter = _alter_h(letzter.get("ts"))
    if alter is None:
        return _p("Kopie", abnahme.OFFEN, "Noch kein Abgleich gelaufen.", "Notbetrieb → „Jetzt abgleichen“.")
    if letzter.get("kein_token"):
        return _p("Kopie", abnahme.OFFEN, "Letzter Abgleich ohne Graph-Zugang.", "Einrichtung → Anmeldung prüfen.")
    fehler = int(letzter.get("fehler", 0) or 0) + int(letzter.get("postfaecher_fehler", 0) or 0)
    if alter > KOPIE_MAX_H:
        return _p("Kopie", abnahme.OFFEN, f"Letzter Abgleich vor {alter:.1f} h — er läuft sonst alle 15 Minuten.",
                  "Protokoll auf „Notbetrieb-Abgleich fehlgeschlagen“ durchsehen.")
    if fehler:
        return _p("Kopie", abnahme.OFFEN, f"{fehler} Fehler beim letzten Abgleich.",
                  "Protokoll auf „Notbetrieb:“ durchsehen.")
    return _p("Kopie", abnahme.OK, f"{st.get('mails', 0)} Mails aus {st.get('postfaecher_im_schatten', 0)} "
              f"Postfächern, Abgleich vor {alter * 60:.0f} min.")


def _abbild() -> dict:
    import abnahme
    import notbetrieb_rechte
    alter = _alter_h(notbetrieb_rechte.stand().get("ts"))
    if alter is None:
        return _p("Rechte-Abbild", abnahme.OFFEN, "Noch nicht gelesen — ohne es sieht niemand geteilte Postfächer.",
                  "Notbetrieb → „Rechte jetzt lesen“.")
    if alter > ABBILD_MAX_H:
        return _p("Rechte-Abbild", abnahme.OFFEN, f"Zuletzt vor {alter:.0f} h gelesen (sonst täglich).",
                  "Notbetrieb → „Rechte jetzt lesen“; Protokoll prüfen.")
    return _p("Rechte-Abbild", abnahme.OK, f"Gelesen vor {alter:.0f} h.")


def _profile(st: dict) -> dict:
    import abnahme
    alter = _alter_h(st.get("profile_stand"))
    if alter is None:
        return _p("Profile für Signaturen", abnahme.OFFEN,
                  "Noch nicht gesichert — fällt Graph aus, wären die Signaturen leer.",
                  "Wird mit dem nächsten Abgleich gesichert; sonst Protokoll prüfen.")
    if alter > ABBILD_MAX_H:
        return _p("Profile für Signaturen", abnahme.OFFEN, f"Zuletzt vor {alter:.0f} h gesichert (sonst täglich).",
                  "Protokoll auf „Profile nicht gesichert“ prüfen.")
    return _p("Profile für Signaturen", abnahme.OK, f"{st.get('profile', 0)} Profile, gesichert vor {alter:.0f} h.")


def _anmeldung(mit_netz: bool) -> dict:
    import abnahme
    import notbetrieb_anmeldung
    if not notbetrieb_anmeldung.eingerichtet():
        return _p("Anmeldung (Domänencontroller)", abnahme.OFFEN,
                  "Nicht eingerichtet — im Notfall kann sich niemand anmelden.",
                  "Notbetrieb → Anmeldung: Domänencontroller und Basis-DN eintragen.")
    if not mit_netz:
        return _p("Anmeldung (Domänencontroller)", abnahme.UNBEKANNT, "Eingetragen; Erreichbarkeit nicht geprüft.",
                  "Notbetrieb → Bereitschaft → „Jetzt prüfen“.")
    ok, text = notbetrieb_anmeldung.erreichbar()
    return _p("Anmeldung (Domänencontroller)", abnahme.OK if ok else abnahme.OFFEN, text,
              "" if ok else "Notbetrieb → Anmeldung: Server, CA-Zertifikat und Firewall prüfen.")


def _versand() -> dict:
    import abnahme
    import direktversand
    if direktversand.aktiv():
        # Nicht „eingerichtet“ heißt ok, sondern „die letzte Probe kam an“
        # (notbetrieb_probe) — ein unbenutzter Weg fällt sonst unbemerkt aus.
        import notbetrieb_probe
        ampel, text = notbetrieb_probe.bewertung()
        zustand = {"ok": abnahme.OK, "fehler": abnahme.OFFEN}.get(ampel, abnahme.UNBEKANNT)
        return _p("Versand nach außen", zustand, "Direktversand eingerichtet; Probe: " + text + ".",
                  "" if zustand == abnahme.OK else "Notbetrieb → Direktversand-Probe: Postfach eintragen, "
                                                     "„Probe jetzt“ und das Ergebnis ansehen.")
    return _p("Versand nach außen", abnahme.OFFEN,
              "Kein Direktversand — im Notfall bleibt Post an externe Empfänger in der Warteschlange.",
              "Einrichtung → Direktversand einrichten.")


def _kontakte() -> dict:
    import abnahme
    import notbetrieb_pim
    if notbetrieb_pim.kontakte_recht_fehlt():
        return _p("Kontakte", abnahme.OFFEN,
                  "Der App fehlt Contacts.Read — im Notfall-Webmail fehlen die persönlichen Kontakte.",
                  "Einrichtung → Betriebsmodus → „Fehlende Berechtigungen erteilen“.")
    return _p("Kontakte", abnahme.OK, "Lesbar.")


def _zweitfaktor() -> dict:
    """Wie viele könnten sich im Notfall anmelden? Mit Bezugsgröße — „0“
    allein wäre nicht zu deuten."""
    import abnahme
    import notbetrieb
    import notbetrieb_faktor
    if not notbetrieb_faktor.pflicht():
        return _p("Zweiter Faktor", abnahme.ENTFAELLT, "Nicht vorgeschrieben — Anmeldung nur mit Kennwort.")
    eingerichtet = {f["postfach"] for f in notbetrieb_faktor.uebersicht() if not f["gesperrt"]}
    with notbetrieb._lock, notbetrieb._conn() as conn:
        nutzer = {z["primary_adr"].lower() for z in conn.execute(
            "SELECT primary_adr FROM konten WHERE typ='UserMailbox'")}
    befund = f"{len(eingerichtet & nutzer)} von {len(nutzer)} Benutzerpostfächern eingerichtet"
    if notbetrieb_faktor.im_notfall_ohne():
        befund += "; im ausgerufenen Notfall geht es auch ohne"
    elif not eingerichtet & nutzer:
        return _p("Zweiter Faktor", abnahme.OFFEN, befund + " — im Notfall könnte sich niemand anmelden.",
                  "Notbetrieb → Anmeldung: Link zum Einrichten an alle verteilen.")
    return _p("Zweiter Faktor", abnahme.OK, befund + ".")


def _mailprogramme() -> dict:
    """IMAP (993) und SMTP (587) sind Zusatzwege — aus heißt „entfällt“, nicht
    „nicht bereit“. Beide Listener binden nur mit TLS-Zertifikat — das prüft
    die Zeile „TLS-Zertifikat“."""
    import abnahme
    import config
    import settings_store
    an = [n for n, k in (("IMAP lesen (Port %d)" % config.NOTBETRIEB_IMAP_PORT, "NOTBETRIEB_IMAP"),
                         ("SMTP senden (Port %d)" % config.SUBMISSION_PORT, "NOTBETRIEB_SMTP"))
          if settings_store.get(k) is True]
    if not an:
        return _p("Mailprogramme", abnahme.ENTFAELLT, "Aus — Outlook und Handy nur über das Webmail.")
    return _p("Mailprogramme", abnahme.OK, ", ".join(an) + " — Firewall muss die Ports durchlassen.")


def _rueckweg(st: dict) -> dict:
    """Dauerbetrieb: Läuft der Weg zurück nach Exchange? Derselbe Code wie
    nach einer Störung — steht er, fiele es erst im Ernstfall auf."""
    import abnahme
    import notbetrieb
    import notbetrieb_aenderungen
    import settings_store
    if not (notbetrieb.lesen_alle() and settings_store.get("NOTBETRIEB_SENDEN_ALLE") is True):
        return _p("Rückweg nach Exchange", abnahme.ENTFAELLT, "Webmail im Normalbetrieb nicht zum Senden frei.")
    if st.get("ausgerufen"):
        return _p("Rückweg nach Exchange", abnahme.ENTFAELLT, "Notfall ausgerufen — der Rückweg ruht bis danach.")
    aend = notbetrieb_aenderungen.offen()
    stau = int(st.get("lokal_stau", 0) or 0)
    befund = (f"{stau} Mails länger als {notbetrieb.STAU_MIN} min nicht zurückgespielt, "
              f"{aend['offen']} Änderungen wartend ({aend['mit_fehler']} mit Fehler)")
    if stau or aend["mit_fehler"]:
        return _p("Rückweg nach Exchange", abnahme.OFFEN, befund + ".",
                  "Protokoll nach „Rückspielen“ / „Änderung … nicht zurückgeschrieben“ durchsehen.")
    return _p("Rückweg nach Exchange", abnahme.OK, befund + ".")


def pruefen(mit_netz: bool = True) -> dict:
    """{punkte, bereit}. `mit_netz=False` nur lokale Prüfungen (Abnahme-Übersicht)."""
    import abnahme
    import notbetrieb
    st = notbetrieb.status()
    punkte = []
    for f in (lambda: _kopie(st), _abbild, lambda: _profile(st), lambda: _anmeldung(mit_netz), _versand,
              _kontakte, _zweitfaktor, _mailprogramme, lambda: _rueckweg(st), abnahme._tls):
        try:
            punkte.append(f())
        except Exception as exc:                                    # noqa: BLE001
            punkte.append(_p("Prüfung gescheitert", abnahme.UNBEKANNT, str(exc)[:150]))
    return {"punkte": punkte, "bereit": all(p["zustand"] in (abnahme.OK, abnahme.ENTFAELLT) for p in punkte)}


# ── MX ──────────────────────────────────────────────────────────────────────

def _mx(domain: str) -> list[str]:
    """MX-Ziele (ohne Punkt am Ende), nach Vorrang. Tests ersetzen diese Funktion."""
    import dns.resolver
    try:
        antwort = dns.resolver.resolve(domain, "MX", lifetime=8)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return []
    return [str(r.exchange).rstrip(".").lower() for r in sorted(antwort, key=lambda r: r.preference)]


def _adressen(name: str) -> set[str]:
    import socket
    try:
        return {a[4][0] for a in socket.getaddrinfo(name, 25, proto=socket.IPPROTO_TCP)}
    except OSError:
        return set()


def mx_stand() -> list[dict]:
    """Je eigener Domäne: wohin zeigt der MX? `ziel` = exchange | gateway | anders."""
    import aussenadresse
    import notbetrieb_rechte
    domaenen = sorted({a.rsplit("@", 1)[1].lower() for a in notbetrieb_rechte.alle_postfaecher()
                       if "@" in a and not a.lower().endswith(".onmicrosoft.com")})
    eigen = (aussenadresse.ehlo_hostname() or "").lower()
    eigen_ip = _adressen(eigen) if eigen else set()
    out = []
    for d in domaenen:
        try:
            ziele = _mx(d)
        except Exception as exc:                                    # noqa: BLE001
            out.append({"domain": d, "mx": [], "ziel": "unbekannt", "fehler": str(exc)[:120]})
            continue
        erstes = ziele[0] if ziele else ""
        if not erstes:
            ziel = "keiner"
        elif erstes.endswith(".mail.protection.outlook.com"):
            ziel = "exchange"
        elif erstes == eigen or (eigen_ip and _adressen(erstes) & eigen_ip):
            ziel = "gateway"
        else:
            ziel = "anders"
        out.append({"domain": d, "mx": ziele, "ziel": ziel})
    return out
