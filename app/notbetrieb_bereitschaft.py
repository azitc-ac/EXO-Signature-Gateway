"""Notbetrieb: Ist alles bereit für den Ernstfall?

Ein Notbetrieb, der erst im Ernstfall geprüft wird, fällt im Ernstfall aus.
Deshalb jederzeit abrufbar (Seite Notbetrieb, Abnahme):

- Kopie aktuell (letzter Abgleich, Fehler)
- Rechte-Abbild und Profile frisch (wer darf was; Signaturdaten ohne Graph)
- Anmeldung: Domänencontroller verschlüsselt erreichbar
- Versand nach außen: Direktversand eingerichtet
- On-Prem-Postfächer: Ziel vorhanden, letzte Probe angenommen
- Hybrid-Connector: kennt keiner die Ausgangsadresse des Direktversands
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
        # (direktversand_probe) — ein unbenutzter Weg fällt sonst unbemerkt aus.
        import direktversand_probe
        ampel, text = direktversand_probe.bewertung()
        zustand = {"ok": abnahme.OK, "fehler": abnahme.OFFEN}.get(ampel, abnahme.UNBEKANNT)
        return _p("Versand nach außen", zustand, "Direktversand eingerichtet; Probe: " + text + ".",
                  "" if zustand == abnahme.OK else "Einstellungen → Mail-Routing → Tägliche Probe: Postfach "
                                                     "eintragen, „Probe jetzt“ und das Ergebnis ansehen.")
    return _p("Versand nach außen", abnahme.OFFEN,
              "Kein Direktversand — im Notfall bleibt Post an externe Empfänger in der Warteschlange.",
              "Einrichtung → Direktversand einrichten.")


def _onprem() -> dict:
    """On-Prem-Postfächer (gemeinsamer Namensraum): Gibt es ein Ziel, und kam
    die letzte Probe an? Mit Bezugsgröße — die Zeile steht immer."""
    import abnahme
    import notbetrieb
    st = notbetrieb.onprem_stand()
    n, ohne = st["postfaecher"], st["ohne_ziel"]
    befund = f"{n} Postfächer liegen on-prem, {ohne} davon ohne Ziel"
    if not n:
        return _p("On-Prem-Postfächer", abnahme.ENTFAELLT, befund + " — keine Weiterleitung nötig.")
    if ohne:
        return _p("On-Prem-Postfächer", abnahme.OFFEN, befund + " — im Notfall nicht erreichbar (Warteschlange).",
                  "Notbetrieb → „On-Prem-Postfächer“: Ziel wählen (Zielserver unter Einstellungen → Mail-Routing).")
    probe = notbetrieb.onprem_probe_stand() or {}
    alter = _alter_h(probe.get("ts"))
    if alter is None:
        return _p("On-Prem-Postfächer", abnahme.OFFEN, befund + "; Weg noch nie geprüft.",
                  "Notbetrieb → „On-Prem-Postfächer“ → „Weg prüfen“.")
    if not probe.get("ok"):
        return _p("On-Prem-Postfächer", abnahme.OFFEN,
                  befund + f"; letzte Probe gescheitert: {probe.get('fehler') or probe.get('antwort', '')}.",
                  "Zielserver und Empfangsconnector on-prem prüfen, dann „Weg prüfen“.")
    return _p("On-Prem-Postfächer", abnahme.OK,
              befund + f"; Probe an {probe.get('ziel')} vor {alter:.0f} h angenommen ({probe.get('antwort', '')}).")


# ── Hybrid-Connector ────────────────────────────────────────────────────────
# Microsoft ordnet eine eingehende Verbindung ZUERST als „originating“ einem
# Tenant zu, wenn ein OnPremises-Eingangsconnector das Zertifikat des Senders
# (TlsSenderCertificateName) oder seine IP (SenderIPAddresses) kennt und die
# Absenderdomäne dort akzeptiert ist — gleich, welchen MX der Sender angewählt
# hat (Exchange Team Blog „Office 365 Message Attribution“; MC1226222). Die
# Absenderdomänen sind die eigenen, also zählen alle eigenen Connectoren.
# Gemessen 16.11.2026 (exohv): Direktversand an einen anderen Microsoft-Kunden
# lief so über den eigenen Tenant (DIR:OUT, DKIM selector1).

HYBRID = "Notfall-Post an Microsoft-Kunden"
HYBRID_WARUM = (
    "Deine während des Notfalls ausgehenden Mails, die an einen anderen Microsoft-Kunden gehen sollen, würden "
    "innerhalb von Microsoft deinem Tenant zugeordnet und diesen durchlaufen. Warum? Es gibt in deinem Tenant "
    "einen (Hybrid-)Eingangsconnector „{name}“, der {grund}. {vermutlich} Alle Mails würden deshalb behandelt "
    "wie von Exchange on-prem kommend und somit über deinen Tenant zum Ziel geschickt. Da dein Tenant im Notfall "
    "gestört ist (und ein anderer Tenant vielleicht nicht), ist das nicht sinnvoll.")
HYBRID_TUN = ("Am einfachsten sorgst du dafür, dass dein Gateway ein anderes Zertifikat hat als dein Exchange, und "
              "stellst den Hybrid-Connector auf Zertifikat um — das ist ohnehin die von Microsoft empfohlene "
              "Variante.")


def _ip_passt(ip: str, eintraege: list[str]) -> str | None:
    """Der Eintrag aus SenderIPAddresses, der `ip` enthält (einzelne Adresse
    oder CIDR-Bereich), sonst None. Unlesbare Einträge zählen nicht."""
    import ipaddress
    try:
        adresse = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for e in eintraege:
        try:
            if adresse in ipaddress.ip_network(e.strip(), strict=False):
                return e
        except ValueError:
            continue
    return None


def _name_passt(namen: list[str], muster: str) -> str | None:
    """Zertifikatsname des Senders, der zu TlsSenderCertificateName passt;
    `*.domain` deckt genau eine Ebene ab (wie bei TLS)."""
    m = (muster or "").strip().lower()
    if not m:
        return None
    for n in (x.strip().lower() for x in namen):
        if n == m:
            return n
        if m.startswith("*.") and n.endswith(m[1:]) and "." not in n[: -len(m) + 1]:
            return n
        if n.startswith("*.") and m.endswith(n[1:]) and "." not in m[: -len(n) + 1]:
            return n
    return None


def hybrid_treffer(connectoren: list[dict], ip: str, zertifikat_namen: list[str]) -> list[dict]:
    """Je Connector, der den Direktversand als seinen Exchange ansähe:
    {name, art: ip|zertifikat, wert}. Rein & testbar."""
    treffer = []
    for c in connectoren:
        n = _name_passt(zertifikat_namen, c.get("zertifikat", ""))
        if n:
            treffer.append({"name": c.get("name", ""), "art": "zertifikat", "wert": n})
            continue
        e = _ip_passt(ip, c.get("ips") or [])
        if e:
            treffer.append({"name": c.get("name", ""), "art": "ip", "wert": e})
    return treffer


def _gateway_zertifikat_namen() -> list[str]:
    """Namen des Client-Zertifikats, das der Direktversand zeigt. Postfix
    bekommt keins (direktversand.postconf_zeilen setzt kein smtp_tls_cert_file;
    tests/test_notbetrieb_hybrid.py hält das fest) — ändert sich das, gehört
    hier dessen CN/SAN hin."""
    return []


def _hybrid() -> dict:
    import abnahme
    import direktversand
    import notbetrieb_rechte
    if not direktversand.aktiv():
        return _p(HYBRID, abnahme.ENTFAELLT, "Kein Direktversand — siehe „Versand nach außen“.")
    e = direktversand.einstellungen()
    if e["modus"] != "mx":
        return _p(HYBRID, abnahme.ENTFAELLT, f"Direktversand über den Smarthost {e['smarthost']} — welche Adresse "
                  "Microsoft sieht, bestimmt der Smarthost.")
    ip = e["aussen_ip"]
    if not ip:
        return _p(HYBRID, abnahme.UNBEKANNT, "Ausgangsadresse des Direktversands nicht eingetragen — nicht prüfbar.",
                  "Einstellungen → Mail-Routing → Selbst zustellen: „Ausgangsadresse“ eintragen.")
    st = notbetrieb_rechte.hybrid_connectoren()
    connectoren = st.get("connectoren")
    alter = _alter_h(st.get("ts"))
    if connectoren is None or alter is None:
        return _p(HYBRID, abnahme.UNBEKANNT, "Eingangsconnectoren noch nicht gelesen." if alter is None else
                  "Eingangsconnectoren beim letzten Abgleich nicht lesbar (Protokoll: „Get-InboundConnector“).",
                  "Notbetrieb → „Rechte jetzt lesen“.")
    treffer = hybrid_treffer(connectoren, ip, _gateway_zertifikat_namen())
    if not treffer:
        return _p(HYBRID, abnahme.OK, f"{len(connectoren)} Hybrid-Eingangsconnectoren geprüft (vor {alter:.0f} h), "
                  f"keiner kennt die Ausgangsadresse {ip} des Gateways.")
    t = treffer[0]
    if t["art"] == "ip":
        grund = f"auf dieselbe IP hört ({t['wert']}), mit der sich auch das Gateway dort meldet ({ip})"
        vermutlich = "Vermutlich teilen sich das Gateway und Exchange on-prem dieselbe öffentliche IP."
    else:
        grund = f"dasselbe Zertifikat erwartet ({t['wert']}), das auch das Gateway dort vorzeigt"
        vermutlich = "Vermutlich nutzen das Gateway und Exchange on-prem dasselbe Zertifikat."
    befund = HYBRID_WARUM.format(name=t["name"], grund=grund, vermutlich=vermutlich)
    if len(treffer) > 1:
        befund += " Ebenso: " + ", ".join(f"„{x['name']}“" for x in treffer[1:]) + "."
    return _p(HYBRID, abnahme.OFFEN, befund, HYBRID_TUN)


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
    import notbetrieb_faktor
    if not notbetrieb_faktor.pflicht():
        return _p("Zweiter Faktor", abnahme.ENTFAELLT, "Nicht vorgeschrieben — Anmeldung nur mit Kennwort.")
    ab = notbetrieb_faktor.abdeckung()
    befund = f"{ab['eingerichtet']} von {ab['nutzer']} Benutzerpostfächern eingerichtet"
    if notbetrieb_faktor.im_notfall_ohne():
        befund += "; im ausgerufenen Notfall geht es auch ohne"
    elif not ab["eingerichtet"]:
        return _p("Zweiter Faktor", abnahme.OFFEN, befund + " — im Notfall könnte sich niemand anmelden.",
                  "Notbetrieb → Anmeldung: Link zum Einrichten an alle verteilen.")
    elif ab["eingerichtet"] < ab["nutzer"]:
        # Gelb, nicht offen: „1 von 18“ hieß bis 17.11.2026 grün „bereit“. Offen
        # (und damit eine Daueraufgabe im Sammelpunkt, bis wirklich jede Person
        # einen Faktor hat) wäre zu viel — `teilweise` färbt nur die Zeile.
        fehlt = ab["nutzer"] - ab["eingerichtet"]
        p = _p("Zweiter Faktor", abnahme.OK,
               befund + f" — {fehlt} könnte{'' if fehlt == 1 else 'n'} sich im Notfall nicht anmelden.",
               "Link zum Einrichten erneut verteilen — oder unter Anmeldung im Notbetrieb „im ausgerufenen "
               "Notfall auch ohne eingerichteten Faktor“ zulassen.")
        p["teilweise"] = True
        return p
    return _p("Zweiter Faktor", abnahme.OK, befund + ".")



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
              _onprem, _hybrid, _kontakte, _zweitfaktor, lambda: _rueckweg(st), abnahme._tls):
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
