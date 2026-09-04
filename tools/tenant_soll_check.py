#!/usr/bin/env python3
"""Soll-Abgleich der gateway-relevanten Tenant-Konfiguration.

Vergleicht den IST-Zustand eines Tenants (read-only erfasst via
app/scripts/get_tenant_state.ps1) gegen den ausformulierten SOLL-Zustand und
meldet jede Abweichung. Deckt die TENANT-Seite ab (Config-Zustand) — nicht das
Mailfluss-Verhalten, nicht den Gateway-internen Zustand.

Drei Tiers je Feld:
  INV  — Invariante, fest, tenant-unabhängig.
  ABL  — abgeleitet aus einer freien Eingabe: Ist == abgeleitet(Eingabe).
  FREI — freie Eingabe: nur Existenz/Form.

Die Vergleichslogik `pruefe()` ist REIN und ohne Tenant testbar — genau darum
läuft der Regressionstest (tests/test_tenant_soll_check.py) in der CI und würde
den Vorfall vom 04.09. (Regel aktiv, Gate leer) fangen.

Aufruf:
  python3 tools/tenant_soll_check.py --state abzug.json      # offline
  python3 tools/tenant_soll_check.py --live                  # erfasst selbst (braucht pwsh + Cert)
Exit: 0 sauber/nur Hinweise · 1 Abweichung(en) (KRITISCH oder WARNUNG).
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path

KRITISCH = "KRITISCH"
WARNUNG = "WARNUNG"
HINWEIS = "HINWEIS"


# ── Ableitungen (Tier ABL) ────────────────────────────────────────────────────
def ableiten(inputs: dict) -> dict:
    gw = inputs["gateway_name"]
    host = inputs.get("public_hostname", "")
    return {
        "regel_name":    f"Route via {gw}",
        "dg_name":       f"{gw} - Enabled Mailboxes",
        "dg_alias":      f"{gw} - Enabled Mailboxes".replace(" ", ""),  # EXO entfernt Leerzeichen
        "connector_out": f"{gw} - Outbound",
        "connector_in":  f"{gw} - Inbound",
        "smarthost":     host,
        "tls_cert_name": host,
    }


def _gate_leer(from_member_of) -> bool:
    """FromMemberOf gilt als leer, wenn keine nicht-leere Kennung enthalten ist.
    (Die PS-Serialisierung liefert für ein leeres Gate [""], nicht [].)"""
    return not any((x or "").strip() for x in (from_member_of or []))


def _regel(state, name):
    for r in state.get("transportregeln", []):
        if r.get("Name") == name:
            return r
    return None


# ── Prüfung (REIN, testbar) ───────────────────────────────────────────────────
def pruefe(state: dict, inputs: dict) -> list[dict]:
    """Vergleicht IST (state) gegen SOLL(inputs). Liefert Befundliste."""
    ab = ableiten(inputs)
    mode = inputs.get("reinject_mode", "smtp")
    loop = inputs.get("loop_header", "X-Sig-Applied")
    f: list[dict] = []

    def add(schwere, objekt, text):
        f.append({"schwere": schwere, "objekt": objekt, "text": text})

    # ── Route-Regel ───────────────────────────────────────────────────────────
    r = _regel(state, ab["regel_name"])
    if not r:
        add(KRITISCH, ab["regel_name"], "Route-Transportregel fehlt.")
    else:
        obj = ab["regel_name"]
        gate_leer = _gate_leer(r.get("FromMemberOf"))
        # DER Vorfall: aktiv ohne Gate → matcht jeden internen Absender.
        if r.get("State") == "Enabled" and gate_leer:
            add(KRITISCH, obj,
                "Regel AKTIV, aber FromMemberOf leer — leitet JEDEN internen Absender "
                "durchs Gateway (Totalausfall-Risiko).")
        if not gate_leer:
            if not any((x or "").lower().startswith(ab["dg_alias"].lower())
                       for x in r.get("FromMemberOf", [])):
                add(WARNUNG, obj,
                    f"FromMemberOf zeigt nicht auf die erwartete DG ({ab['dg_alias']}): "
                    f"{r.get('FromMemberOf')}")
        # Empfängerbedingung ist verboten (Bifurkation / Karen-Bug).
        if (r.get("SentToScope") or "").strip():
            add(KRITISCH, obj,
                f"SentToScope gesetzt ({r.get('SentToScope')}) — empfängerbezogene "
                "Bedingung bifurkiert die interne Fork am Gateway vorbei.")
        if r.get("FromScope") != "InOrganization":
            add(WARNUNG, obj, f"FromScope={r.get('FromScope')} (Soll: InOrganization).")
        if r.get("RouteMessageOutboundConnector") != ab["connector_out"]:
            add(WARNUNG, obj,
                f"Connector={r.get('RouteMessageOutboundConnector')} (Soll: {ab['connector_out']}).")
        if r.get("ExceptIfHeaderMatchesMessageHeader") != loop:
            add(WARNUNG, obj,
                f"Loop-Header={r.get('ExceptIfHeaderMatchesMessageHeader')} (Soll: {loop}).")
        if r.get("ExceptIfMessageTypeMatches") != "Calendaring":
            add(HINWEIS, obj, "Kalender-Ausnahme (ExceptIfMessageTypeMatches=Calendaring) fehlt.")
        if r.get("Mode") != "Enforce":
            add(HINWEIS, obj, f"Mode={r.get('Mode')} (Soll: Enforce).")

    # ── S/MIME-Inbound-Regeln (falls vorhanden) — Gate MUSS hier leer sein ────
    for typ in ("Signed", "Encrypted"):
        name = f"{inputs['gateway_name']} - SMIME {typ} Inbound"
        sr = _regel(state, name)
        if not sr:
            continue  # S/MIME optional
        if sr.get("FromScope") != "NotInOrganization":
            add(WARNUNG, name, f"FromScope={sr.get('FromScope')} (Soll: NotInOrganization).")
        if sr.get("MessageTypeMatches") != typ:
            add(WARNUNG, name, f"MessageTypeMatches={sr.get('MessageTypeMatches')} (Soll: {typ}).")
        if not _gate_leer(sr.get("FromMemberOf")):
            add(WARNUNG, name,
                "S/MIME-Regel hat ein DG-Gate — sie muss über Typ+Scope greifen, nicht über eine DG.")
        if not sr.get("StopRuleProcessing"):
            add(HINWEIS, name, "StopRuleProcessing nicht gesetzt (Soll: true).")

    # ── Outbound-Connector ────────────────────────────────────────────────────
    outs = state.get("outbound_connectoren", [])
    reste = [c for c in outs if "_Test_" in (c.get("Name") or "")
             and (c.get("Name") or "").startswith(inputs["gateway_name"])]
    for c in reste:
        add(WARNUNG, c["Name"], "Test-Mode-Connector-Rest (Validierungs-Leiche) — löschen.")
    prod = next((c for c in outs if c.get("Name") == ab["connector_out"]), None)
    if not prod:
        add(KRITISCH, ab["connector_out"], "Outbound-Connector fehlt.")
    else:
        obj = ab["connector_out"]
        if not prod.get("Enabled"):
            add(KRITISCH, obj, "Outbound-Connector deaktiviert.")
        if prod.get("IsValidated") is False:
            add(WARNUNG, obj, "Outbound-Connector nicht validiert (IsValidated=False).")
        if prod.get("TlsSettings") != "DomainValidation":
            add(WARNUNG, obj, f"TlsSettings={prod.get('TlsSettings')} (Soll: DomainValidation).")
        if ab["smarthost"] and prod.get("SmartHosts") != [ab["smarthost"]]:
            add(WARNUNG, obj, f"SmartHosts={prod.get('SmartHosts')} (Soll: [{ab['smarthost']}]).")
        if prod.get("IsTransportRuleScoped") is not True:
            add(WARNUNG, obj, "IsTransportRuleScoped nicht true.")

    # ── Inbound-Connector (nur im smtp-Modus) ─────────────────────────────────
    if mode == "smtp":
        ins = state.get("inbound_connectoren", [])
        pin = next((c for c in ins if c.get("Name") == ab["connector_in"]), None)
        if not pin:
            add(KRITISCH, ab["connector_in"], "Inbound-Connector fehlt (smtp-Modus).")
        else:
            obj = ab["connector_in"]
            if not pin.get("RequireTls"):
                add(WARNUNG, obj, "RequireTls nicht gesetzt.")
            if ab["tls_cert_name"] and pin.get("TlsSenderCertificateName") != ab["tls_cert_name"]:
                add(WARNUNG, obj,
                    f"TlsSenderCertificateName={pin.get('TlsSenderCertificateName')} "
                    f"(Soll: {ab['tls_cert_name']}).")

    # ── Enabled-Mailboxes-DG ──────────────────────────────────────────────────
    dg = next((d for d in state.get("verteilerlisten", []) if d.get("Name") == ab["dg_name"]), None)
    if not dg:
        add(KRITISCH, ab["dg_name"], "Enabled-Mailboxes-Verteilerliste fehlt.")
    else:
        if dg.get("MemberJoinRestriction") != "Closed" or dg.get("MemberDepartRestriction") != "Closed":
            add(WARNUNG, ab["dg_name"], "Mitgliedschafts-Restriktionen nicht 'Closed'.")

    # ── RemoteDomain Castle (falls vorhanden) ─────────────────────────────────
    for rd in state.get("remotedomain_castle", []):
        obj = rd.get("Name") or "Castle ACME"
        if rd.get("ByteEncoderTypeFor7BitCharsets") != "Use7Bit":
            add(WARNUNG, obj, "ByteEncoderTypeFor7BitCharsets != Use7Bit (ACME-Token-Risiko).")
        if rd.get("ContentType") != "MimeText":
            add(WARNUNG, obj, f"ContentType={rd.get('ContentType')} (Soll: MimeText).")
        if rd.get("TNEFEnabled") is True:   # null/false ok — nur erzwungenes TNEF ist das Problem
            add(WARNUNG, obj, "TNEFEnabled=True — korrumpiert den ACME-Token (Soll: nicht true).")

    # ── App-Rechte (nur wenn erfasst) ─────────────────────────────────────────
    if "app" not in state:
        add(HINWEIS, "app_registrierung",
            "App-/Graph-Rechte nicht erfasst — brauchen Directory-Token (Setup/Abnahme) "
            "oder Prüfung im Entra-Portal.")

    return f


# ── CLI ───────────────────────────────────────────────────────────────────────
def _settings(data_dir: str) -> dict:
    p = Path(data_dir) / "settings.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _live_state(inputs, data_dir) -> dict:
    skript = Path(__file__).resolve().parent.parent / "app" / "scripts" / "get_tenant_state.ps1"
    cert = str(Path(data_dir) / "auth.pfx")
    org = inputs.get("tenant_domain") or ""
    app_id = inputs.get("client_id") or ""
    roh = subprocess.run(
        ["pwsh", "-NoLogo", "-NonInteractive", "-File", str(skript),
         "-AppId", app_id, "-Organization", org, "-CertPath", cert],
        capture_output=True, text=True, timeout=180,
    ).stdout
    return json.loads(roh[roh.index("{"):])


def main() -> int:
    ap = argparse.ArgumentParser(description="Soll-Abgleich der Tenant-Konfiguration")
    ap.add_argument("--state", help="IST-Abzug als JSON (offline)")
    ap.add_argument("--live", action="store_true", help="IST selbst read-only erfassen (pwsh + Cert)")
    ap.add_argument("--data-dir", default="/app/data")
    for k in ("gateway-name", "public-hostname", "tenant-domain", "reinject-mode", "loop-header"):
        ap.add_argument(f"--{k}")
    args = ap.parse_args()

    s = _settings(args.data_dir)
    inputs = {
        "gateway_name":    args.gateway_name    or s.get("GATEWAY_NAME") or "EXO Signature Gateway",
        "public_hostname": args.public_hostname or s.get("PUBLIC_HOSTNAME") or "",
        "tenant_domain":   args.tenant_domain   or s.get("TENANT_DOMAIN") or "",
        "reinject_mode":   args.reinject_mode   or s.get("REINJECT_MODE") or "smtp",
        "loop_header":     args.loop_header     or s.get("LOOP_HEADER") or "X-Sig-Applied",
        "client_id":       s.get("CLIENT_ID") or "",
    }

    if args.state:
        state = json.loads(Path(args.state).read_text())
    elif args.live:
        state = _live_state(inputs, args.data_dir)
    else:
        print("Fehler: --state <datei> oder --live angeben.", file=sys.stderr)
        return 2

    befunde = pruefe(state, inputs)
    krit = [b for b in befunde if b["schwere"] == KRITISCH]
    warn = [b for b in befunde if b["schwere"] == WARNUNG]
    for b in befunde:
        print(f"  [{b['schwere']:8}] {b['objekt']}: {b['text']}")
    print(f"\n{len(krit)} kritisch, {len(warn)} Warnung(en), "
          f"{len(befunde)-len(krit)-len(warn)} Hinweis(e).")
    return 1 if (krit or warn) else 0


if __name__ == "__main__":
    sys.exit(main())
