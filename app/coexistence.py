"""On-prem-Empfangsconnector für die Hybrid-Koexistenz — Auf-/Abbau als Skript.

Das Gateway liefert bearbeitete Post per SMTP an einen on-prem-Exchange
(Domänen-Routing, `reinject._relay_to_target`). Damit der on-prem-Exchange diese
Post als *intern* annimmt (`X-MS-Exchange-Organization-AuthAs: Internal`),
braucht er einen eigenen Empfangsconnector, der der **Quell-IP** des Gateways —
bzw. des davor liegenden Load-Balancers — vertraut.

Dieses Modul erzeugt daraus ein **PowerShell-Skript zum Kopieren** (Aufbau UND
Abriss). Das Gateway führt es NICHT selbst aus: der on-prem-Exchange ist ein
fremdes System hinter einer fremden Verwaltung; der Admin führt das Skript in der
Exchange Management Shell aus. So bleibt der Vorgang nachvollziehbar und
umkehrbar, ohne dass das Gateway Zugriff auf die on-prem-Server braucht.

Der Connector heißt ``Inbound from <Gateway> (Coexistence)``.

⚠️ Sicherheitsvertrag (steht auch im README und in der Oberfläche):
`ExternalAuthoritative` vertraut **jedem** Host hinter der angegebenen Quell-IP.
Das ist nur vertretbar, wenn der Load-Balancer eingehende Post **ausschließlich**
vom Gateway erhält (eine einzige Route).

⚠️ `Tls` MUSS in `AuthMechanism` bleiben — sonst bietet der Connector kein
STARTTLS an, das Gateway bricht mit `QUIT` ab → Mailstopp. Deshalb ist die
Reihenfolge im Skript fest `Tls,ExternalAuthoritative`.
"""
from __future__ import annotations

import ipaddress
import re

import settings_store

# Der cert-genaue Weg (TlsDomainCapabilities/Direct Trust) ist bewusst
# zurückgestellt (XOORG / unsupported AD). Der IP-Weg ist die stabile Basis.
NAME_VORLAGE = "Inbound from {gw} (Coexistence)"

# Ein Exchange-Servername ist ein Hostname-Label — eng halten, damit nichts
# Unerwartetes ins PowerShell-Skript gerät (die Werte landen in Single-Quote-
# Strings, aber ein sauberer Filter ist die erste Verteidigung).
_SERVER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _gw_name(gateway_name: str) -> str:
    gw = (gateway_name or "").strip()
    if not gw:
        raise ValueError("Gateway-Name fehlt.")
    if len(gw) > 64 or any(c in gw for c in "\r\n"):
        raise ValueError("Gateway-Name ist ungültig.")
    return gw


def _quell_ip(source_ip: str) -> str:
    ip = (source_ip or "").strip()
    if not ip:
        raise ValueError("Quell-IP fehlt.")
    # Einzel-IP oder CIDR-Netz. Bereiche „a-b" sind hier unüblich (die LB-Quelle
    # ist eine feste Adresse); wer sie braucht, trägt sie on-prem von Hand nach.
    try:
        if "/" in ip:
            ipaddress.ip_network(ip, strict=False)
        else:
            ipaddress.ip_address(ip)
    except ValueError as exc:
        raise ValueError(f"{ip!r} ist keine gültige IP oder CIDR.") from exc
    return ip


def _server_liste(servers) -> list[str]:
    """Serverliste normalisieren. Eingabe: Liste ODER Komma/Leerzeichen-String.
    Leer ist erlaubt → das Skript nimmt den lokalen Server ($env:COMPUTERNAME)."""
    if isinstance(servers, str):
        roh = re.split(r"[,\s]+", servers)
    else:
        roh = list(servers or [])
    out: list[str] = []
    for s in roh:
        s = str(s).strip()
        if not s:
            continue
        if not _SERVER_RE.match(s):
            raise ValueError(f"{s!r} ist kein gültiger Servername.")
        if s not in out:
            out.append(s)
    return out


def _ps_str(wert: str) -> str:
    """Als PowerShell-Single-Quote-Literal einbetten (das einzige Sonderzeichen
    dort ist das einfache Anführungszeichen, verdoppelt es sich)."""
    return "'" + wert.replace("'", "''") + "'"


def _ps_array(werte: list[str]) -> str:
    return "@(" + ", ".join(_ps_str(w) for w in werte) + ")"


def baue_skript(gateway_name: str, source_ip: str, servers=None) -> dict:
    """Aus (Gateway-Name, Quell-IP, Server) das Auf-/Abbau-Skript bauen.

    Rein — kein Seiteneffekt. Wirft `ValueError` bei ungültiger Eingabe, damit die
    Oberfläche eine klare Meldung zeigt, statt ein kaputtes Skript auszugeben.
    """
    gw = _gw_name(gateway_name)
    ip = _quell_ip(source_ip)
    srv = _server_liste(servers)
    name = NAME_VORLAGE.format(gw=gw)

    server_zeile = (_ps_array(srv) if srv
                    else "@($env:COMPUTERNAME)   # kein Server angegeben → lokaler Server")

    build = f"""# {name} — AUFBAU
# In der Exchange Management Shell (on-prem) ausführen.
# Legt je Server einen Empfangsconnector an, der der Quell-IP des Gateways
# vertraut, sodass dessen Post als intern (AuthAs: Internal) ankommt.
#
# ⚠️ ExternalAuthoritative vertraut JEDEM Host hinter dieser Quell-IP — nur
#    vertretbar, wenn der Load-Balancer eingehende Post ausschließlich vom
#    Gateway erhält (eine einzige Route).
# ⚠️ 'Tls' MUSS in -AuthMechanism bleiben, sonst kein STARTTLS → Mailstopp.

$name    = {_ps_str(name)}
$quellIp = {_ps_str(ip)}
$server  = {server_zeile}

foreach ($s in $server) {{
  $vorhanden = Get-ReceiveConnector -Server $s -ErrorAction SilentlyContinue |
               Where-Object {{ $_.Name -eq $name }}
  if ($vorhanden) {{
    Set-ReceiveConnector -Identity $vorhanden.Identity `
      -RemoteIPRanges $quellIp `
      -PermissionGroups ExchangeServers `
      -AuthMechanism Tls,ExternalAuthoritative `
      -Enabled $true
    Write-Host "aktualisiert: $s\\$name"
  }} else {{
    New-ReceiveConnector -Server $s -Name $name `
      -TransportRole FrontendTransport `
      -Bindings '0.0.0.0:25' `
      -RemoteIPRanges $quellIp `
      -PermissionGroups ExchangeServers `
      -AuthMechanism Tls,ExternalAuthoritative `
      -Enabled $true | Out-Null
    Write-Host "angelegt: $s\\$name"
  }}
}}
"""

    teardown = f"""# {name} — ABRISS
# Entfernt den Empfangsconnector wieder (je Server). Die Post läuft danach wie
# vor der Koexistenz-Einrichtung.

$name   = {_ps_str(name)}
$server = {server_zeile}

foreach ($s in $server) {{
  Get-ReceiveConnector -Server $s -ErrorAction SilentlyContinue |
    Where-Object {{ $_.Name -eq $name }} |
    Remove-ReceiveConnector -Confirm:$false
  Write-Host "entfernt (falls vorhanden): $s\\$name"
}}
"""
    return {"name": name, "build": build, "teardown": teardown}


# ── gespeicherte Konfiguration ────────────────────────────────────────────────
# Die Parameter stehen in COEX_CONFIG, damit sich das Skript später erneut
# erzeugen lässt (Abriss + Wiederaufbau), ohne die Quell-IP neu einzutippen.

def konfig() -> dict:
    c = settings_store.get("COEX_CONFIG") or {}
    return {
        "gateway_name": (c.get("gateway_name") or "").strip(),
        "source_ip": (c.get("source_ip") or "").strip(),
        "servers": list(c.get("servers") or []),
    }


def ansicht(gateway_fallback: str = "") -> dict:
    """Was die Oberfläche braucht: die gespeicherten Parameter und — sofern eine
    Quell-IP vorliegt — das erzeugte Skript. Ohne Quell-IP `bereit=False`."""
    c = konfig()
    gw = c["gateway_name"] or (gateway_fallback or "").strip()
    out = {"gateway_name": gw, "source_ip": c["source_ip"],
           "servers": c["servers"], "bereit": False,
           "name": "", "build": "", "teardown": ""}
    if c["source_ip"] and gw:
        try:
            out.update(baue_skript(gw, c["source_ip"], c["servers"]))
            out["bereit"] = True
        except ValueError:
            pass          # unplausibel gespeichert → als „nicht bereit" zeigen
    return out


def speichern(gateway_name: str, source_ip: str, servers) -> dict:
    """Parameter prüfen (via `baue_skript`) und speichern. Gibt die Ansicht mit
    frischem Skript zurück. NUR aus dem Web-Prozess aufrufen (settings_store)."""
    skript = baue_skript(gateway_name, source_ip, servers)   # validiert
    settings_store.update({"COEX_CONFIG": {
        "gateway_name": _gw_name(gateway_name),
        "source_ip": _quell_ip(source_ip),
        "servers": _server_liste(servers),
    }})
    c = konfig()
    return {"gateway_name": c["gateway_name"], "source_ip": c["source_ip"],
            "servers": c["servers"], "bereit": True, **skript}
