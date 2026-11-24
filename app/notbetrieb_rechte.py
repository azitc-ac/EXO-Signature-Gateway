"""Notbetrieb: Abbild der Postfachrechte — wer darf im Störfall welches Postfach.

WOZU
----
Im Notbetrieb fragt niemand Exchange, wer auf ein geteiltes Postfach zugreifen
darf. Das Gateway hält deshalb laufend ein Abbild und setzt es im Webmail durch:

  voll      Vollzugriff (FullAccess)       → Postfach sehen und lesen
  als       Senden als (SendAs)            → als dieses Postfach senden
  auftrag   Senden im Auftrag (GrantSendOnBehalfTo)

Im Störfall gilt der zuletzt gesehene Stand. Ein fehlgeschlagener Abruf lässt
das bestehende Abbild unangetastet — sonst verlöre im schlimmsten Moment jeder
den Zugang zu seinen geteilten Postfächern.

Graph kennt diese Rechte nicht (kein Endpunkt für FullAccess/SendAs); die
Quelle ist EXO-PowerShell, wie bei `exo_mailusers`. Gruppen mit Vollzugriff
werden EINE Ebene tief in ihre Mitglieder aufgelöst; verschachtelte Gruppen
nicht (bekannte Lücke, im Stand als `gruppen_tief` nicht gezählt).

Gespeichert in der Schatten-Datenbank (`notbetrieb._conn`) — gelöscht mit dem
Schatten, neu abzugleichen aus Exchange. `settings_store.update()` wird hier
nicht gerufen; geschrieben wird nur aus dem Web-/Hauptprozess.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import config
import settings_store

log = logging.getLogger(__name__)

_AUTH_CERT_PATH = Path(config.DATA_DIR) / "auth.pfx"
RECHTE = ("voll", "als", "auftrag")


def _ps_script(app_id: str, org: str) -> str:
    import ps_text
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "Import-Module ExchangeOnlineManagement",
        "$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(",
        f"    '{ps_text.lit(_AUTH_CERT_PATH)}', [string]$null,",
        "    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))",
        f"Connect-ExchangeOnline -AppId '{ps_text.lit(app_id)}' -Certificate $cert -Organization '{ps_text.lit(org)}'"
        " -ShowBanner:$false | Out-Null",
        "$ErrorActionPreference = 'Continue'",
        # Auflösung einer Identität zu SMTP-Adressen; Gruppen eine Ebene tief.
        "$cache = @{}",
        "function Aufloesen($id) {",
        "  if (-not $id) { return ,@() }",
        "  $k = \"$id\"",
        "  if ($cache.ContainsKey($k)) { return ,$cache[$k] }",
        "  $r = Get-EXORecipient -Identity $k -ErrorAction SilentlyContinue",
        "  if (-not $r) { $res = @() }",
        "  elseif (\"$($r.RecipientTypeDetails)\" -like '*Group*') {",
        "    $res = @(Get-DistributionGroupMember -Identity \"$($r.Identity)\" -ResultSize Unlimited"
        " -ErrorAction SilentlyContinue | ForEach-Object { \"$($_.PrimarySmtpAddress)\".ToLower() }"
        " | Where-Object { $_ }) }",
        "  else { $res = @(\"$($r.PrimarySmtpAddress)\".ToLower()) }",
        "  $cache[$k] = $res",
        "  return ,$res",
        "}",
        "$mbx = @(Get-EXOMailbox -RecipientTypeDetails UserMailbox,SharedMailbox -ResultSize Unlimited"
        " -Properties GrantSendOnBehalfTo,HiddenFromAddressListsEnabled)",
        "$konten = @(); $rechte = @(); $offen = 0",
        "foreach ($m in $mbx) {",
        "  $pf = \"$($m.PrimarySmtpAddress)\".ToLower()",
        "  $konten += [pscustomobject]@{ primary = $pf; upn = \"$($m.UserPrincipalName)\".ToLower();"
        " typ = \"$($m.RecipientTypeDetails)\"; versteckt = [bool]$m.HiddenFromAddressListsEnabled }",
        "  foreach ($p in @(Get-EXOMailboxPermission -Identity $m.UserPrincipalName -ErrorAction SilentlyContinue)) {",
        "    if ($p.IsInherited -or $p.Deny -or -not ($p.AccessRights -contains 'FullAccess')) { continue }",
        "    if (\"$($p.User)\" -like 'NT AUTHORITY*' -or \"$($p.User)\" -like 'S-1-5-*') { continue }",
        "    $wer = Aufloesen $p.User",
        "    if (-not $wer.Count) { $offen++ }",
        "    foreach ($w in $wer) { $rechte += [pscustomobject]@{ postfach = $pf; nutzer = $w; recht = 'voll' } }",
        "  }",
        "  foreach ($p in @(Get-EXORecipientPermission -Identity $m.UserPrincipalName -ErrorAction SilentlyContinue)) {",
        "    if (-not ($p.AccessRights -contains 'SendAs') -or \"$($p.Trustee)\" -like 'NT AUTHORITY*') { continue }",
        "    $wer = Aufloesen $p.Trustee",
        "    if (-not $wer.Count) { $offen++ }",
        "    foreach ($w in $wer) { $rechte += [pscustomobject]@{ postfach = $pf; nutzer = $w; recht = 'als' } }",
        "  }",
        "  foreach ($t in @($m.GrantSendOnBehalfTo)) {",
        "    if (-not $t) { continue }",
        "    $wer = Aufloesen $t",
        "    if (-not $wer.Count) { $offen++ }",
        "    foreach ($w in $wer) { $rechte += [pscustomobject]@{ postfach = $pf; nutzer = $w; recht = 'auftrag' } }",
        "  }",
        "}",
        # Verteiler, Gruppen, externe Benutzer/Kontakte: Adressen und DIREKTE
        # Mitglieder — verschachtelte Gruppen löst `parse` auf (mit Zyklenschutz).
        "function Adressen($o) { @(@($o.EmailAddresses) | ForEach-Object { \"$_\" }"
        " | Where-Object { $_ -match '^smtp:' } | ForEach-Object { $_.Substring(5).ToLower() }) }",
        "function Smtp($liste) { @($liste | ForEach-Object { \"$($_.PrimarySmtpAddress)\".ToLower() }"
        " | Where-Object { $_ }) }",
        "$gruppen = @()",
        "function Gruppe($g, $art, $mit) {",
        "  [pscustomobject]@{ adresse = \"$($g.PrimarySmtpAddress)\".ToLower(); aliase = @(Adressen $g);"
        " name = \"$($g.DisplayName)\"; art = $art; versteckt = [bool]$g.HiddenFromAddressListsEnabled;"
        " nur_intern = [bool]$g.RequireSenderAuthenticationEnabled; mitglieder = @($mit) }",
        "}",
        "foreach ($g in @(Get-DistributionGroup -ResultSize Unlimited)) {",
        # Raumlisten sind Verteiler aus Räumen — keine Post-Empfänger.
        "  if (\"$($g.RecipientTypeDetails)\" -eq 'RoomList') { continue }",
        "  $gruppen += Gruppe $g 'verteiler' (Smtp (Get-DistributionGroupMember -Identity \"$($g.PrimarySmtpAddress)\""
        " -ResultSize Unlimited -ErrorAction SilentlyContinue)) }",
        "foreach ($g in @(Get-DynamicDistributionGroup -ResultSize Unlimited)) {",
        "  $gruppen += Gruppe $g 'dynamisch' (Smtp (Get-DynamicDistributionGroupMember -Identity"
        " \"$($g.PrimarySmtpAddress)\" -ResultSize Unlimited -ErrorAction SilentlyContinue)) }",
        "foreach ($g in @(Get-UnifiedGroup -ResultSize Unlimited)) {",
        "  $gruppen += Gruppe $g 'gruppe' (Smtp (Get-UnifiedGroupLinks -Identity \"$($g.PrimarySmtpAddress)\""
        " -LinkType Members -ResultSize Unlimited -ErrorAction SilentlyContinue)) }",
        "$extern = @()",
        "foreach ($x in @(Get-EXORecipient -RecipientTypeDetails MailUser,MailContact -ResultSize Unlimited"
        " -Properties HiddenFromAddressListsEnabled,ExternalEmailAddress,EmailAddresses,DisplayName)) {",
        "  $ziel = \"$($x.ExternalEmailAddress)\"; if ($ziel -match '^smtp:') { $ziel = $ziel.Substring(5) }",
        "  $extern += [pscustomobject]@{ adresse = \"$($x.PrimarySmtpAddress)\".ToLower(); aliase = @(Adressen $x);"
        " name = \"$($x.DisplayName)\"; art = $(if (\"$($x.RecipientTypeDetails)\" -eq 'MailContact')"
        " { 'kontakt' } else { 'extern' }); versteckt = [bool]$x.HiddenFromAddressListsEnabled;"
        " ziel = $ziel.ToLower() }",
        "}",
        # Hybrid-Eingangsconnectoren (OnPremises): Kennt einer die Ausgangsadresse
        # des Direktversands, ordnet Microsoft Notfall-Post dem eigenen Tenant zu
        # (notbetrieb_bereitschaft._hybrid). $null = nicht lesbar, nie „keine“.
        "$connectoren = $null",
        "try { $connectoren = @(Get-InboundConnector -ErrorAction Stop | Where-Object {"
        " \"$($_.ConnectorType)\" -eq 'OnPremises' -and $_.Enabled } | ForEach-Object {"
        " [pscustomobject]@{ name = \"$($_.Name)\"; ips = @($_.SenderIPAddresses | ForEach-Object { \"$_\" });"
        " zertifikat = \"$($_.TlsSenderCertificateName)\" } }) } catch {"
        " [Console]::Error.WriteLine(\"Get-InboundConnector: $_\") }",
        "[pscustomobject]@{ konten = $konten; rechte = $rechte; nicht_aufgeloest = $offen;"
        " gruppen = $gruppen; extern = $extern; connectoren = $connectoren }"
        " | ConvertTo-Json -Depth 5 -Compress",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ])


def _liste(wert) -> list:
    """ConvertTo-Json macht aus einer Ein-Element-Liste ein Objekt, aus einer
    leeren null — beides abfangen."""
    if wert is None:
        return []
    return wert if isinstance(wert, list) else [wert]


def parse(roh: str) -> dict | None:
    """PowerShell-JSON → {konten, rechte, nicht_aufgeloest}. Rein & testbar.
    `None` bei unlesbarer Ausgabe (der Aufrufer behält dann den alten Stand)."""
    try:
        daten = json.loads(roh)
    except (ValueError, TypeError) as exc:
        log.warning("Notbetrieb-Rechte: JSON nicht lesbar: %s", exc)
        return None
    if not isinstance(daten, dict):
        return None
    konten = {}
    for k in _liste(daten.get("konten")):
        if not isinstance(k, dict):
            continue
        primary = str(k.get("primary") or "").strip().lower()
        if "@" in primary:
            konten[primary] = {"primary": primary, "upn": str(k.get("upn") or "").strip().lower(),
                               "typ": str(k.get("typ") or ""),
                               # In Adresslisten ausgeblendet: erreichbar, aber nicht vorgeschlagen.
                               "versteckt": k.get("versteckt") is True}
    rechte = set()
    for r in _liste(daten.get("rechte")):
        if not isinstance(r, dict):
            continue
        pf = str(r.get("postfach") or "").strip().lower()
        wer = str(r.get("nutzer") or "").strip().lower()
        recht = str(r.get("recht") or "")
        # Sich selbst Rechte am eigenen Postfach: kein Zugewinn, nur Rauschen.
        if pf in konten and "@" in wer and recht in RECHTE and wer != pf:
            rechte.add((pf, wer, recht))
    return {"konten": list(konten.values()), "rechte": sorted(rechte),
            "nicht_aufgeloest": int(daten.get("nicht_aufgeloest") or 0),
            "connectoren": _connectoren(daten),
            "empfaenger": _empfaenger(daten, {a.rsplit("@", 1)[1] for k in konten.values()
                                              for a in (k["primary"], k["upn"]) if "@" in a})}


def _connectoren(daten: dict) -> list[dict] | None:
    """Hybrid-Eingangsconnectoren (OnPremises, aktiv). `None` = nicht gelesen
    (Schlüssel fehlt oder Abruf scheiterte) — ausdrücklich nicht „keine“."""
    if "connectoren" not in daten or daten["connectoren"] is None:
        return None
    out = []
    for c in _liste(daten["connectoren"]):
        if isinstance(c, dict):
            out.append({"name": str(c.get("name") or ""),
                        "ips": [str(i).strip() for i in _liste(c.get("ips")) if str(i).strip()],
                        "zertifikat": str(c.get("zertifikat") or "").strip()})
    return out


ARTEN = ("verteiler", "dynamisch", "gruppe", "extern", "kontakt")


def _adr(wert) -> str:
    a = str(wert or "").strip().lower()
    return a if "@" in a else ""


def _empfaenger(daten: dict, domaenen: set) -> list[dict]:
    """Verteiler, Gruppen und externe Benutzer/Kontakte → je Eintrag die
    flach aufgelösten `ziele`: Postfach- oder externe Adressen, nie eine
    Gruppe. Verschachtelte Gruppen werden durchlaufen, jede höchstens einmal
    (Zyklen: A enthält B enthält A).

    `aliase` = nur Adressen in eigenen Domänen (`domaenen`: die der Postfächer
    und Gruppen). Ein externer Benutzer führt seine externe Adresse oft auch als
    Proxyadresse, ein Kontakt hat gar keine andere — nähme der Notfall-Eingang
    Post für sie an, leitete er Fremdpost nach außen weiter (offenes Relay)."""
    roh = []
    for quelle, arten in (("gruppen", ARTEN[:3]), ("extern", ARTEN[3:])):
        for e in _liste(daten.get(quelle)):
            if isinstance(e, dict) and _adr(e.get("adresse")) and e.get("art") in arten:
                roh.append(e)
    domaenen = set(domaenen) | {_adr(e["adresse"]).rsplit("@", 1)[1] for e in roh if e["art"] in ARTEN[:3]}
    eintraege, index = [], {}
    for e in roh:
        adressen = {_adr(a) for a in _liste(e.get("aliase")) if _adr(a)} | {_adr(e["adresse"])}
        e = {"adresse": _adr(e["adresse"]), "name": str(e.get("name") or ""), "art": e["art"],
             "versteckt": e.get("versteckt") is True, "nur_intern": e.get("nur_intern") is True,
             "aliase": sorted(a for a in adressen if a.rsplit("@", 1)[1] in domaenen),
             "mitglieder": [_adr(m) for m in _liste(e.get("mitglieder")) if _adr(m)],
             "ziel": _adr(e.get("ziel"))}
        # Externer Benutzer, dessen externe Adresse eine EIGENE ist (gemeinsamer
        # Namensraum mit einer On-Prem-Organisation): Das Postfach liegt on-prem.
        # Nur MailUser — ein Kontakt ist nie ein Postfach dieser Organisation.
        e["onprem"] = e["art"] == "extern" and bool(e["ziel"]) and e["ziel"] in e["aliase"]
        eintraege.append(e)
        for a in e["aliase"]:
            index.setdefault(a, e)

    def flach(adresse: str, gesehen: set) -> list[str]:
        e = index.get(adresse)
        if e is None:
            return [adresse]
        if e["adresse"] in gesehen:
            return []
        gesehen.add(e["adresse"])
        if e["onprem"]:
            # On-Prem-Postfach: die Adresse selbst ist das Ziel; wohin sie im
            # Notfall geht, entscheidet notbetrieb.aufloesen (On-Prem-Ziel).
            # Bis 11/2026 lief das in den Selbstbezug unten und ergab [] — auch
            # jeder Verteiler verlor solche Mitglieder still.
            return [e["ziel"]]
        if e["art"] in ARTEN[3:]:
            # Externer Benutzer ohne externe Adresse: nicht erreichbar.
            return flach(e["ziel"], gesehen) if e["ziel"] else []
        return [z for m in e["mitglieder"] for z in flach(m, gesehen)]

    # Erst alle auflösen, dann aufräumen — eine spätere Gruppe kann eine schon
    # aufgelöste als Mitglied haben.
    ziele = {e["adresse"]: list(dict.fromkeys(flach(e["adresse"], set()))) for e in eintraege}
    return [{k: v for k, v in e.items() if k not in ("mitglieder", "ziel")} | {"ziele": ziele[e["adresse"]]}
            for e in eintraege]


def fetch() -> dict | None:
    """EXO JETZT abfragen (blockierend; je Postfach zwei Abfragen — bei vielen
    Postfächern Minuten). `None` bei Fehler."""
    app_id = settings_store.get("CLIENT_ID") or ""
    org = settings_store.get("TENANT_DOMAIN") or ""
    if not (app_id and org and _AUTH_CERT_PATH.exists()):
        log.warning("Notbetrieb-Rechte: übersprungen — CLIENT_ID/TENANT_DOMAIN/auth-Zertifikat fehlt")
        return None
    with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False) as f:
        f.write(_ps_script(app_id, org))
        ps_path = f.name
    try:
        proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", ps_path],
                              capture_output=True, text=True, timeout=3600)
    except Exception as exc:                              # noqa: BLE001
        log.error("Notbetrieb-Rechte: Abruf fehlgeschlagen: %s", exc)
        return None
    finally:
        try:
            Path(ps_path).unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        log.error("Notbetrieb-Rechte: rc=%s: %s", proc.returncode, (proc.stderr or "")[:300])
        return None
    out = proc.stdout or ""
    start = out.find("{")
    return parse(out[start:]) if start >= 0 else None


# ── Ablage ──────────────────────────────────────────────────────────────────

def speichern(abbild: dict, ts: str | None = None) -> None:
    """Abbild ERSETZEN — nur mit einem erfolgreichen Abruf aufrufen."""
    import notbetrieb
    ts = ts or datetime.now(timezone.utc).isoformat()
    with notbetrieb._lock, notbetrieb._conn() as conn:
        conn.execute("DELETE FROM konten")
        conn.execute("DELETE FROM rechte")
        conn.executemany("INSERT INTO konten (primary_adr, upn, typ, versteckt) VALUES (?,?,?,?)",
                         [(k["primary"], k["upn"], k["typ"], 1 if k.get("versteckt") else 0)
                          for k in abbild["konten"]])
        conn.executemany("INSERT INTO rechte (postfach, nutzer, recht) VALUES (?,?,?)", abbild["rechte"])
        empfaenger = abbild.get("empfaenger") or []
        conn.execute("DELETE FROM empfaenger")
        # Je eigene Adresse eine Zeile (annehmen=1); die Hauptadresse eines
        # Kontakts in fremder Domäne nur fürs Adressbuch (annehmen=0).
        conn.executemany("INSERT OR IGNORE INTO empfaenger (adresse, haupt, name, art, versteckt, nur_intern,"
                         " ziele, annehmen, onprem) VALUES (?,?,?,?,?,?,?,?,?)",
                         [(a, e["adresse"], e["name"], e["art"], 1 if e["versteckt"] else 0,
                           1 if e["nur_intern"] else 0, json.dumps(e["ziele"]), 1 if a in e["aliase"] else 0,
                           1 if e.get("onprem") else 0)
                          for e in empfaenger for a in dict.fromkeys(e["aliase"] + [e["adresse"]])])
        je_art = {a: sum(1 for e in empfaenger if e["art"] == a) for a in ARTEN}
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('rechte_stand', ?)",
                     (json.dumps({"ts": ts, "konten": len(abbild["konten"]), "rechte": len(abbild["rechte"]),
                                  "nicht_aufgeloest": abbild.get("nicht_aufgeloest", 0),
                                  "empfaenger": {a: n for a, n in je_art.items() if n}}),))
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('hybrid_connectoren', ?)",
                     (json.dumps({"ts": ts, "connectoren": abbild.get("connectoren")}),))


def abgleichen() -> dict:
    """Abruf + Ersetzen. Bei Fehler bleibt der alte Stand (ok=False)."""
    abbild = fetch()
    if abbild is None:
        return {"ok": False, **stand()}
    speichern(abbild)
    log.info("Notbetrieb-Rechte: %s Postfächer, %s Rechte, %s nicht auflösbar, %s Verteiler/Gruppen/Externe",
             len(abbild["konten"]), len(abbild["rechte"]), abbild["nicht_aufgeloest"],
             len(abbild.get("empfaenger") or []))
    return {"ok": True, **stand()}


def stand() -> dict:
    import notbetrieb
    if not notbetrieb._db_pfad().exists():
        return {"ts": "", "konten": 0, "rechte": 0, "nicht_aufgeloest": 0}
    with notbetrieb._lock, notbetrieb._conn() as conn:
        z = conn.execute("SELECT v FROM meta WHERE k='rechte_stand'").fetchone()
    return json.loads(z["v"]) if z else {"ts": "", "konten": 0, "rechte": 0, "nicht_aufgeloest": 0}


def hybrid_connectoren() -> dict:
    """{ts, connectoren}: Stand des letzten Abgleichs. connectoren None = nicht
    gelesen; {} wenn noch nie abgeglichen."""
    import notbetrieb
    if not notbetrieb._db_pfad().exists():
        return {}
    with notbetrieb._lock, notbetrieb._conn() as conn:
        z = conn.execute("SELECT v FROM meta WHERE k='hybrid_connectoren'").fetchone()
    return json.loads(z["v"]) if z else {}


def _konto(conn, adresse: str):
    a = adresse.strip().lower()
    return conn.execute("SELECT * FROM konten WHERE primary_adr=? OR upn=?", (a, a)).fetchone()


def zugriff(anmeldung: str) -> list[dict]:
    """Welche Postfächer darf jemand im Notbetrieb nutzen — EINE Auflösung für
    Webmail und Anzeige. `anmeldung` = UPN oder Adresse. Das eigene Postfach
    zuerst, mit allen Rechten; danach alles mit mindestens einem Recht."""
    import notbetrieb
    if not notbetrieb._db_pfad().exists():
        return []
    with notbetrieb._lock, notbetrieb._conn() as conn:
        k = _konto(conn, anmeldung)
        if not k:
            return []
        eigen = k["primary_adr"]
        fremd: dict[str, set] = {}
        for z in conn.execute("SELECT postfach, recht FROM rechte WHERE nutzer=?", (eigen,)):
            fremd.setdefault(z["postfach"], set()).add(z["recht"])
        # Nur die Typen der eigenen Postfächer: Das läuft bei JEDER Anfrage
        # des Webmails (darf_lesen) — alle Konten zu lesen kostete im Lasttest
        # vom 16.11.2026 rund ein Achtel der Rechenzeit.
        pfe = [eigen, *fremd]
        typen = {z["primary_adr"]: z["typ"] for z in conn.execute(
            f"SELECT primary_adr, typ FROM konten WHERE primary_adr IN ({','.join('?' * len(pfe))})", pfe)}
    out = [{"postfach": eigen, "eigen": True, "typ": typen.get(eigen, ""), "voll": True, "als": True, "auftrag": False}]
    for pf in sorted(fremd):
        out.append({"postfach": pf, "eigen": False, "typ": typen.get(pf, ""),
                    **{r: r in fremd[pf] for r in RECHTE}})
    return out


def alle_postfaecher() -> list[str]:
    """Jedes Benutzer- und geteilte Postfach aus dem Abbild (leer, solange
    keins gelesen ist)."""
    import notbetrieb
    if not notbetrieb._db_pfad().exists():
        return []
    with notbetrieb._lock, notbetrieb._conn() as conn:
        return [z["primary_adr"] for z in conn.execute("SELECT primary_adr FROM konten ORDER BY primary_adr")]


def zusatz_postfaecher(nutzer: list[str]) -> list[str]:
    """Postfächer, auf die einer der `nutzer` Vollzugriff hat — sie kommen mit in
    den Schatten, sonst stünde das geteilte Postfach im Notbetrieb leer da."""
    import notbetrieb
    if not nutzer or not notbetrieb._db_pfad().exists():
        return []
    frage = ",".join("?" * len(nutzer))
    with notbetrieb._lock, notbetrieb._conn() as conn:
        return sorted({z["postfach"] for z in conn.execute(
            f"SELECT DISTINCT postfach FROM rechte WHERE recht='voll' AND nutzer IN ({frage})",
            [n.lower() for n in nutzer])} - {n.lower() for n in nutzer})


def uebersicht() -> list[dict]:
    """Je Postfach mit fremden Rechten: wer darf was — für die Seite."""
    import notbetrieb
    if not notbetrieb._db_pfad().exists():
        return []
    with notbetrieb._lock, notbetrieb._conn() as conn:
        typen = {z["primary_adr"]: z["typ"] for z in conn.execute("SELECT primary_adr, typ FROM konten")}
        je: dict[str, dict[str, list]] = {}
        for z in conn.execute("SELECT postfach, nutzer, recht FROM rechte ORDER BY postfach, nutzer"):
            je.setdefault(z["postfach"], {r: [] for r in RECHTE})[z["recht"]].append(z["nutzer"])
    return [{"postfach": pf, "typ": typen.get(pf, ""), **r} for pf, r in sorted(je.items())]
