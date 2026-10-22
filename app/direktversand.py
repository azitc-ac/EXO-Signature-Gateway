"""Direktversand: Post an externe Empfänger über den eigenen Postfix ins Internet.

Wozu: Exchange Online als letzter Hop fällt in manchen Lagen aus — ein
Test-/Trial-Tenant darf nicht ins Internet zustellen (550 5.7.708, Microsofts
Risiko-Pool), ein Betreiber ohne Exchange braucht einen Weg für Massenversand
ohne Microsofts Sendegrenzen. Dann übergibt das Gateway Post an externe
Empfänger einem eigenen Postfix (Container `mta`), der sie per MX oder über
einen Smarthost zustellt.

Einbindung: Das Gateway kennt ein EINGEBAUTES Weiterleitungsziel `direkt`
(`domain_routing.DIREKT`). Routen — nach Empfängerdomäne, für alle externen
Empfänger oder für einen fremden Tenant — können darauf zeigen; `reinject`
signiert dann per DKIM (`dkim_signatur`) und übergibt an Postfix. Postfix macht
die MTA-Arbeit: MX-Auflösung mit Ausweichen, Warteschlange und Wiederholung
über Tage, STARTTLS, Unzustellbarkeitsberichte. Nichts davon wird hier in
Python nachgebaut.

Dieses Modul schreibt die Konfiguration für den Container
(`data/mta/postconf.cf`, `data/mta/sasl_passwd`), liest dessen Statusdatei und
prüft, ob ausgehender Port 25 überhaupt offen ist (in Azure ist er gesperrt).

⚠️ Schreibt über `settings_store.update()` nur in `speichern()` — NUR aus dem
Web-Prozess aufrufen, nie aus einem Subprozess (CLAUDE.md).
"""
from __future__ import annotations

import json
import logging
import re
import socket
from pathlib import Path

import config
import secure_io
import settings_store

log = logging.getLogger(__name__)

# Dienstname im Compose-Netz; der Port ist nicht nach außen veröffentlicht.
MTA_HOST = "mta"
MTA_PORT = 25

MODI = ("mx", "smarthost")

# Hostnamen/HELO: nur, was in eine postconf-Zeile gehört — kein Zeilenumbruch,
# kein Komma, kein Gleichheitszeichen (sonst ließe sich eine weitere
# Einstellung einschleusen).
_HOST_MUSTER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

# Prüfziele für den Port-25-Test: große Postfachanbieter, deren MX dauerhaft
# antworten. Der erste, der 220 sagt, genügt.
_PROBE_DOMAENEN = ("gmail.com", "outlook.com", "web.de")


def mta_verzeichnis() -> Path:
    return Path(config.DATA_DIR) / "mta"


def aktiv() -> bool:
    """Direktversand eingeschaltet? (Bool aus JSON; nur echtes True zählt.)"""
    return settings_store.get("DIREKT_ENABLED") is True


def einstellungen() -> dict:
    """Die Einstellungen des Direktversands (ohne Klartext-Passwort)."""
    modus = (settings_store.get("DIREKT_MODUS") or "mx").strip().lower()
    return {
        "aktiv": aktiv(),
        "modus": modus if modus in MODI else "mx",
        "helo": (settings_store.get("DIREKT_HELO") or "").strip(),
        "smarthost": (settings_store.get("DIREKT_SMARTHOST") or "").strip(),
        "smarthost_port": int(settings_store.get("DIREKT_SMARTHOST_PORT") or 587),
        "smarthost_user": (settings_store.get("DIREKT_SMARTHOST_USER") or "").strip(),
        "hat_passwort": bool(settings_store.get("DIREKT_SMARTHOST_PW")),
        "aussen_ip": (settings_store.get("DIREKT_AUSSEN_IP") or "").strip(),
    }


def helo_name() -> str:
    """HELO-Name für ausgehende Verbindungen: ausdrücklich gesetzt, sonst der
    öffentliche Hostname des Gateways. Er sollte zum PTR der Ausgangsadresse
    passen — sonst werten Empfänger die Verbindung ab."""
    gesetzt = (settings_store.get("DIREKT_HELO") or "").strip()
    if gesetzt:
        return gesetzt
    import aussenadresse
    return aussenadresse.ehlo_hostname() or "localhost.localdomain"


def postconf_zeilen(e: dict, helo: str) -> list[str]:
    """Die Postfix-Einstellungen als Zeilen für `postconf -e` — reine Funktion.

    ⚠️ `disable_mime_output_conversion = yes` ist Pflicht: Ohne 8BITMIME bei der
    Gegenstelle wandelte Postfix sonst 8bit in quoted-printable — der Rumpf
    änderte sich nach der DKIM-Signatur, und die Signatur bräche.
    """
    if not _HOST_MUSTER.match(helo or ""):
        helo = "localhost.localdomain"
    zeilen = [
        f"myhostname = {helo}",
        "smtp_helo_name = $myhostname",
        "smtpd_banner = $myhostname ESMTP",
        "mydestination =",
        "local_transport = error:5.1.1 keine lokale Zustellung",
        "alias_maps =",
        "alias_database =",
        "inet_interfaces = all",
        "inet_protocols = ipv4",
        "mynetworks_style = subnet",
        "smtpd_relay_restrictions = permit_mynetworks, reject",
        "smtpd_recipient_restrictions = permit_mynetworks, reject",
        "disable_vrfy_command = yes",
        "message_size_limit = 36700160",
        "disable_mime_output_conversion = yes",
        "maximal_queue_lifetime = 5d",
        "bounce_queue_lifetime = 5d",
        "maillog_file = /var/log/postfix.log",
        "smtp_tls_loglevel = 1",
    ]
    if not e.get("aktiv"):
        return zeilen + [
            "default_transport = error:5.3.0 Direktversand ist ausgeschaltet",
            "relayhost =",
            "smtp_sasl_auth_enable = no",
        ]
    if e.get("modus") == "smarthost":
        host = e.get("smarthost") or ""
        port = int(e.get("smarthost_port") or 587)
        if not _HOST_MUSTER.match(host):
            # Ungültig konfiguriert → lieber nichts hinauslassen als per MX.
            return zeilen + [
                "default_transport = error:4.3.5 Smarthost ist nicht konfiguriert",
                "relayhost =",
                "smtp_sasl_auth_enable = no",
            ]
        mit_login = bool(e.get("smarthost_user"))
        return zeilen + [
            "default_transport = smtp",
            f"relayhost = [{host}]:{port}",
            # Zum Smarthost IMMER verschlüsselt — dort geht ein Passwort hinüber.
            "smtp_tls_security_level = encrypt",
            f"smtp_sasl_auth_enable = {'yes' if mit_login else 'no'}",
            "smtp_sasl_password_maps = texthash:/etc/postfix/sasl_passwd",
            "smtp_sasl_security_options = noanonymous",
            "smtp_sasl_tls_security_options = noanonymous",
        ]
    return zeilen + [
        "default_transport = smtp",
        "relayhost =",
        # Per MX: verschlüsseln, wo die Gegenstelle es anbietet. Strenger geht
        # nicht pauschal — viele MX haben kein für ihren Namen gültiges Zertifikat.
        "smtp_tls_security_level = may",
        "smtp_sasl_auth_enable = no",
    ]


def sasl_zeile(e: dict, passwort: str) -> str:
    """Inhalt von `sasl_passwd` (texthash) — leer ohne Smarthost-Login."""
    if e.get("modus") != "smarthost" or not e.get("smarthost_user"):
        return ""
    host = e.get("smarthost") or ""
    if not _HOST_MUSTER.match(host):
        return ""
    user = e["smarthost_user"]
    if any(c in user + passwort for c in "\r\n"):
        raise ValueError("Login und Passwort dürfen keinen Zeilenumbruch enthalten")
    return f"[{host}]:{int(e.get('smarthost_port') or 587)} {user}:{passwort}\n"


def schreiben() -> None:
    """Konfiguration für den Postfix-Container ablegen (Rechte 600, atomar).

    Wird beim Start und nach jeder Änderung aufgerufen; der Container übernimmt
    sie binnen Sekunden. Auch im ausgeschalteten Zustand schreiben — dann steht
    dort ausdrücklich „nichts hinauslassen“.
    """
    e = einstellungen()
    d = secure_io.ensure_dir(mta_verzeichnis())
    secure_io.write_secret_text(
        d / "postconf.cf",
        "# Vom Gateway erzeugt (direktversand.py) — Änderungen hier gehen verloren.\n"
        + "\n".join(postconf_zeilen(e, helo_name())) + "\n")
    pw = settings_store.get("DIREKT_SMARTHOST_PW") or ""
    secure_io.write_secret_text(d / "sasl_passwd", sasl_zeile(e, pw))


def speichern(daten: dict) -> dict:
    """Einstellungen aus der Oberfläche prüfen, speichern und an Postfix geben.

    Passwort: Feld leer = unverändert (wie bei den Weiterleitungszielen).
    Einschalten im MX-Modus setzt einen bestandenen Port-25-Test voraus — sonst
    läge alle externe Post tagelang in der Warteschlange und käme dann als
    Unzustellbarkeitsbericht zurück.
    """
    modus = (daten.get("modus") or "mx").strip().lower()
    if modus not in MODI:
        raise ValueError("Betriebsart: mx oder smarthost")
    helo = (daten.get("helo") or "").strip()
    if helo and not _HOST_MUSTER.match(helo):
        raise ValueError("HELO-Name: bitte ein Hostname wie mail.example.com")
    host = (daten.get("smarthost") or "").strip()
    try:
        port = int(daten.get("smarthost_port") or 587)
    except (TypeError, ValueError):
        raise ValueError("Port muss eine Zahl sein")
    if not (1 <= port <= 65535):
        raise ValueError("Port muss zwischen 1 und 65535 liegen")
    user = (daten.get("smarthost_user") or "").strip()
    if modus == "smarthost" and not _HOST_MUSTER.match(host):
        raise ValueError("Bitte den Smarthost als Hostnamen angeben")
    ip = (daten.get("aussen_ip") or "").strip()
    if ip:
        try:
            socket.inet_aton(ip)
        except OSError:
            raise ValueError("Öffentliche Adresse: bitte eine IPv4-Adresse")
    einschalten = bool(daten.get("aktiv"))
    if einschalten and modus == "mx":
        probe = port25_test()
        if not probe.get("ok"):
            raise ValueError("Ausgehender Port 25 ist gesperrt — Direktversand per MX "
                             "geht hier nicht. " + (probe.get("error") or "")
                             + " Nutze die Betriebsart Smarthost.")
    aenderung = {
        "DIREKT_ENABLED": einschalten,
        "DIREKT_MODUS": modus,
        "DIREKT_HELO": helo,
        "DIREKT_SMARTHOST": host,
        "DIREKT_SMARTHOST_PORT": port,
        "DIREKT_SMARTHOST_USER": user,
        "DIREKT_AUSSEN_IP": ip,
    }
    pw = daten.get("smarthost_passwort")
    if pw:
        if any(c in pw for c in "\r\n"):
            raise ValueError("Das Passwort darf keinen Zeilenumbruch enthalten")
        aenderung["DIREKT_SMARTHOST_PW"] = pw
    if any(c in user for c in "\r\n:"):
        raise ValueError("Login: kein Doppelpunkt, kein Zeilenumbruch")
    settings_store.update(aenderung)
    schreiben()
    return einstellungen()


def status() -> dict:
    """Zustand des Postfix-Containers für die Oberfläche.

    `erreichbar`: nimmt der Container Verbindungen an. `bericht`: was der
    Container zuletzt geschrieben hat (Zähler seit Mitternacht UTC,
    Warteschlange, letzte Fehlermeldungen). Fehlt die Datei oder ist sie alt,
    sagt das die Oberfläche — eine stille Null wäre hier irreführend.
    """
    erreichbar = False
    try:
        with socket.create_connection((MTA_HOST, MTA_PORT), timeout=2):
            erreichbar = True
    except OSError:
        pass
    bericht = None
    p = mta_verzeichnis() / "status.json"
    try:
        bericht = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        pass
    return {"erreichbar": erreichbar, "bericht": bericht}


def _mx_hosts(domain: str) -> list[str]:
    import dns.resolver
    antwort = dns.resolver.resolve(domain, "MX", lifetime=5)
    return [str(r.exchange).rstrip(".")
            for r in sorted(antwort, key=lambda r: r.preference)]


def port25_test(timeout: float = 8.0) -> dict:
    """Ist ausgehender Port 25 offen? Verbindet sich mit dem MX großer Anbieter
    und wartet auf deren Begrüßung (220). Schickt NICHTS — kein EHLO, keine Mail.
    """
    letzter = ""
    for domain in _PROBE_DOMAENEN:
        try:
            hosts = _mx_hosts(domain)
        except Exception as exc:                          # noqa: BLE001
            letzter = f"DNS für {domain}: {exc}"
            continue
        for host in hosts[:2]:
            try:
                with socket.create_connection((host, 25), timeout=timeout) as s:
                    s.settimeout(timeout)
                    gruss = s.recv(200).decode("ascii", "replace").strip()
                    try:
                        s.sendall(b"QUIT\r\n")
                    except OSError:
                        pass
                if gruss.startswith("220"):
                    return {"ok": True, "host": host, "antwort": gruss[:120]}
                letzter = f"{host}: unerwartete Antwort {gruss[:80]!r}"
            except OSError as exc:
                letzter = f"{host}:25 — {exc}"
    return {"ok": False, "error": letzter or "keine Gegenstelle erreichbar"}


def _kopf_und_rumpf(content: bytes) -> tuple[list[bytes], bytes]:
    for trenner in (b"\r\n\r\n", b"\n\n"):
        i = content.find(trenner)
        if i >= 0:
            kopf = content[:i + len(trenner) // 2]
            return kopf.splitlines(keepends=True), content[i + len(trenner) // 2:]
    return content.splitlines(keepends=True), b""


def kopf_bereinigen(content: bytes) -> bytes:
    """Organisationsinterne Exchange-Kopfzeilen entfernen, bevor die Mail das
    Haus direkt verlässt.

    `X-MS-Exchange-Organization-*` trägt Exchange nur innerhalb der Organisation
    (Spam-Bewertung, interne Kennungen, Absender-Metadaten) und entfernt es auf
    dem eigenen Weg ins Internet selbst. Geht die Mail stattdessen über den
    eigenen Postfix hinaus, muss das Gateway das übernehmen. Rumpf und übrige
    Kopfzeilen bleiben byte-genau — die DKIM-Signatur kommt erst danach.
    """
    zeilen, rumpf = _kopf_und_rumpf(content)
    raus, weg = [], False
    for z in zeilen:
        if z[:1] in (b" ", b"\t"):            # Fortsetzungszeile
            if not weg:
                raus.append(z)
            continue
        weg = z.lower().startswith(b"x-ms-exchange-organization-")
        if not weg:
            raus.append(z)
    return b"".join(raus) + rumpf


def _txt(name: str) -> list[str]:
    import dns.resolver
    try:
        antwort = dns.resolver.resolve(name, "TXT", lifetime=5)
    except Exception:                                     # noqa: BLE001
        return []
    return [b"".join(r.strings).decode("utf-8", "replace") for r in antwort]


def _spf_teile(eintrag: str) -> list[tuple[str, str]]:
    """SPF-Eintrag in (Qualifier-loser Name, Wert) zerlegen, ohne `v=spf1`.
    `a`, `a:host`, `a/24` → ("a", …); `redirect=x` → ("redirect", "x")."""
    out = []
    for teil in (eintrag or "").split()[1:]:
        t = teil.lstrip("+-~?").lower()
        if "=" in t and ":" not in t.split("=", 1)[0]:
            name, wert = t.split("=", 1)
        else:
            name = re.split(r"[:/]", t, 1)[0]
            wert = t[len(name):].lstrip(":")
        out.append((name, wert))
    return out


def _spf_dns(name, qtype, strict=True, timeout=20):
    """DNS-Abfrage für pyspf, über `dns.resolver.resolve`.

    pyspf 2.0.14 ruft `dns.resolver.query()`, das dnspython 2 nur noch mit
    Warnung führt. Rückgabeformat wie pyspfs eigene `DNSLookup_dnspython`:
    Liste von ((name, typ), wert). Tests ersetzen diese Funktion.
    """
    import dns.exception
    import dns.resolver
    import spf
    try:
        antwort = dns.resolver.resolve(name, qtype, lifetime=timeout)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return []
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        raise spf.TempError(f"DNS {exc}")
    out = []
    for r in antwort:
        if qtype in ("A", "AAAA"):
            out.append(((name, qtype), r.address))
        elif qtype == "MX":
            out.append(((name, qtype), (r.preference, r.exchange)))
        elif qtype == "PTR":
            out.append(((name, qtype), r.target.to_text(True)))
        elif qtype in ("TXT", "SPF"):
            out.append(((name, qtype), r.strings))
    return out


def spf_auswerten(domain: str, ip: str, helo: str = "") -> dict:
    """SPF so auswerten, wie ein Empfänger es tut — mit allen `include:`,
    `a`, `mx`, `redirect=` (pyspf, RFC 7208).

    `erlaubt`: True nur bei `pass`; None bei `temperror` (DNS gestört, später
    erneut); sonst False — auch `none` (kein Eintrag) und `neutral`, denn dann
    ist die Adresse nicht autorisiert.
    """
    if not ip:
        return {"ergebnis": "", "erklaerung": "", "erlaubt": None}
    import spf
    spf.DNSLookup = _spf_dns
    try:
        ergebnis, _code, erklaerung = spf.query(
            i=ip, s=f"postmaster@{domain}", h=helo or domain, querytime=15).check()
    except Exception as exc:                              # noqa: BLE001
        log.warning("SPF-Auswertung für %s fehlgeschlagen: %s", domain, exc)
        return {"ergebnis": "temperror", "erklaerung": str(exc), "erlaubt": None}
    return {"ergebnis": ergebnis, "erklaerung": erklaerung,
            "erlaubt": {"pass": True, "temperror": None}.get(ergebnis, False)}


def _spf_empfehlung(eintrag: str, ip: str) -> str:
    """Der SPF-Eintrag, den der Betreiber setzen soll, wenn `spf_auswerten`
    nicht `pass` meldet (der Aufrufer entscheidet das). Ein Test wertet jeden
    Vorschlag mit `spf_auswerten` aus und verlangt `pass` — Empfehlung und
    Urteil laufen so nicht auseinander. Der BESTEHENDE Eintrag wird
    erweitert, nie ersetzt: `ip4:` kommt unmittelbar vor das abschließende
    `all` (bzw. vor `redirect=`), Qualifier und alles andere bleiben. Ohne
    Eintrag: Exchange Online gehört dazu — der Tenant verschickt weiterhin auch
    über Exchange, ein Eintrag nur mit der eigenen Adresse ließe diese Post
    durchfallen.
    """
    if not ip:
        return ""
    if not eintrag:
        return f"v=spf1 ip4:{ip} include:spf.protection.outlook.com ~all"
    teile = eintrag.split()
    namen = [n for n, _ in _spf_teile(eintrag)]
    pos = len(teile)
    for ziel in ("all", "redirect"):
        if ziel in namen:
            pos = namen.index(ziel) + 1          # +1: teile[0] ist v=spf1
            break
    return " ".join(teile[:pos] + [f"ip4:{ip}"] + teile[pos:])


def dns_pruefen(domain: str) -> dict:
    """Was ein Empfänger über diese Absenderdomäne sieht: SPF, DKIM-Eintrag,
    PTR der Ausgangsadresse und ob der HELO-Name dazu passt.

    Liest NUR öffentliches DNS. Soll-Werte aus derselben Quelle wie das Signieren
    (`dkim_signatur.dns_eintrag`) — eine Prüfung, die gegen eine eigene Kopie
    der Erwartung prüft, meldet Abweichungen nicht.
    """
    import dkim_signatur
    domain = (domain or "").strip().lower()
    ip = (settings_store.get("DIREKT_AUSSEN_IP") or "").strip()
    helo = helo_name()
    spf_alle = [t for t in _txt(domain) if t.lower().split()[:1] == ["v=spf1"]]
    spf = spf_alle[0] if spf_alle else ""
    spf_urteil = spf_auswerten(domain, ip, helo)
    soll = dkim_signatur.dns_eintrag(domain)
    dkim_ist = _txt(soll["name"]) if soll else []

    def _p(wert: str) -> str:
        teile = dict(x.strip().split("=", 1) for x in wert.split(";") if "=" in x)
        return teile.get("p", "").replace(" ", "")

    ergebnis = {
        "domain": domain,
        # Mehrere SPF-Einträge sind ein Fehler (RFC 7208 §4.5: permerror) —
        # dann gibt es kein Urteil und keinen Vorschlag, nur die Aufforderung,
        # sie zu EINEM zusammenzuführen.
        "spf": {"eintrag": spf, "ip": ip, "mehrere": spf_alle if len(spf_alle) > 1 else [],
                **spf_urteil,
                "soll": _spf_empfehlung(spf, ip)
                        if len(spf_alle) <= 1 and spf_urteil["erlaubt"] is not True else ""},
        "dkim": {"name": soll["name"] if soll else "",
                 "soll": soll["wert"] if soll else "",
                 "gefunden": dkim_ist,
                 "passt": bool(soll) and any(_p(x) == _p(soll["wert"]) for x in dkim_ist)},
        "ptr": {"ip": ip, "name": "", "vorwaerts": False, "helo": helo, "helo_passt": False},
    }
    if ip:
        import dns.resolver
        import dns.reversename
        try:
            name = str(dns.resolver.resolve(dns.reversename.from_address(ip), "PTR",
                                            lifetime=5)[0]).rstrip(".")
            ergebnis["ptr"]["name"] = name
            vorwaerts = {str(r) for r in dns.resolver.resolve(name, "A", lifetime=5)}
            ergebnis["ptr"]["vorwaerts"] = ip in vorwaerts
            ergebnis["ptr"]["helo_passt"] = name.lower() == helo.lower()
        except Exception as exc:                          # noqa: BLE001
            ergebnis["ptr"]["fehler"] = str(exc)
    return ergebnis
