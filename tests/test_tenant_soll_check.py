"""Der Soll-Abgleich muss die Tenant-Fehlerklasse fangen — ohne Tenant.

`pruefe()` ist rein: gefüttert mit einem erfassten Zustand + freien Eingaben
liefert es Befunde. Damit läuft genau die Prüfung, die den Vorfall vom 04.09.
(Route-Regel aktiv, FromMemberOf leer → aller interner Mailverkehr durchs
Gateway) in der CI reproduzierbar fängt.
"""
import copy
import importlib.util
from pathlib import Path

_pfad = Path(__file__).resolve().parent.parent / "tools" / "tenant_soll_check.py"
_spec = importlib.util.spec_from_file_location("tenant_soll_check", _pfad)
tsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tsc)

INPUTS = {
    "gateway_name": "EXO Signature Gateway",
    "public_hostname": "sig.zarenko.net",
    "tenant_domain": "zarenko.onmicrosoft.com",
    "reinject_mode": "smtp",
    "loop_header": "X-Sig-Applied",
    "tenant_id": "75d1ad31-0000-4000-8000-000000000001",
}
MUSTER = tsc.muster_fuer(INPUTS["tenant_id"])


def gut() -> dict:
    """Ein korrekter, prod-naher Zustand (frische Kopie je Aufruf)."""
    return copy.deepcopy({
        "transportregeln": [
            {"Name": "Route via EXO Signature Gateway (Signatur)", "State": "Enabled", "Priority": 1,
             "Mode": "Enforce", "FromScope": "InOrganization", "SentToScope": "",
             "FromMemberOf": ["EXOSignatureGateway-EnabledMailboxes@zarenko.onmicrosoft.com"],
             "RouteMessageOutboundConnector": "EXO Signature Gateway - Outbound",
             "ExceptIfHeaderMatchesMessageHeader": "X-Sig-Applied", "ExceptIfHeaderMatchesPatterns": [MUSTER],
             "ExceptIfHeaderContainsMessageHeader": "Auto-Submitted",
             "ExceptIfHeaderContainsWords": ["auto"],
             "ExceptIfMessageTypeMatches": "Calendaring", "MessageTypeMatches": "",
             "StopRuleProcessing": False},
            {"Name": "EXO Signature Gateway - SMIME Signed Inbound", "State": "Enabled", "Priority": 14,
             "Mode": "Enforce", "FromScope": "NotInOrganization", "SentToScope": "",
             "FromMemberOf": [""], "RouteMessageOutboundConnector": "EXO Signature Gateway - Outbound",
             "ExceptIfHeaderMatchesMessageHeader": "X-Sig-Applied", "ExceptIfHeaderMatchesPatterns": [MUSTER], "ExceptIfMessageTypeMatches": "",
             "MessageTypeMatches": "Signed", "StopRuleProcessing": True},
            {"Name": "EXO Signature Gateway - SMIME Encrypted Inbound", "State": "Enabled", "Priority": 15,
             "Mode": "Enforce", "FromScope": "NotInOrganization", "SentToScope": "",
             "FromMemberOf": [""], "RouteMessageOutboundConnector": "EXO Signature Gateway - Outbound",
             "ExceptIfHeaderMatchesMessageHeader": "X-Sig-Applied", "ExceptIfHeaderMatchesPatterns": [MUSTER], "ExceptIfMessageTypeMatches": "",
             "MessageTypeMatches": "Encrypted", "StopRuleProcessing": True},
        ],
        "outbound_connectoren": [
            {"Name": "EXO Signature Gateway - Outbound", "ConnectorType": "OnPremises", "Enabled": True,
             "IsValidated": True, "UseMXRecord": False, "SmartHosts": ["sig.zarenko.net"],
             "TlsSettings": "DomainValidation", "TlsDomain": "sig.zarenko.net", "IsTransportRuleScoped": True},
        ],
        "inbound_connectoren": [
            {"Name": "EXO Signature Gateway - Inbound", "ConnectorType": "OnPremises", "Enabled": True,
             "SenderDomains": ["smtp:*;1"], "RequireTls": True, "TlsSenderCertificateName": "sig.zarenko.net"},
        ],
        "verteilerlisten": [
            {"Name": "EXO Signature Gateway - Enabled Mailboxes", "Alias": "EXOSignatureGateway-EnabledMailboxes",
             "MemberJoinRestriction": "Closed", "MemberDepartRestriction": "Closed",
             "RequireSenderAuthenticationEnabled": True, "Mitglieder": ["a@zarenko.net"]},
        ],
        "remotedomain_castle": [
            {"Name": "Castle ACME", "DomainName": "castle.cloud",
             "ByteEncoderTypeFor7BitCharsets": "Use7Bit", "ContentType": "MimeText", "TNEFEnabled": None},
        ],
    })


def _schweren(befunde, schwere):
    return [b for b in befunde if b["schwere"] == schwere]


def test_guter_zustand_ohne_kritisch_oder_warnung():
    b = tsc.pruefe(gut(), INPUTS)
    assert _schweren(b, tsc.KRITISCH) == []
    assert _schweren(b, tsc.WARNUNG) == []


def test_vorfall_regel_aktiv_ohne_gate_ist_kritisch():
    """DER Vorfall: Route-Regel aktiv, FromMemberOf leer."""
    st = gut()
    st["transportregeln"][0]["FromMemberOf"] = [""]   # Gate geleert, State bleibt Enabled
    b = tsc.pruefe(st, INPUTS)
    krit = _schweren(b, tsc.KRITISCH)
    assert any("FromMemberOf leer" in x["text"] for x in krit), krit


def test_senttoscope_bifurkation_ist_kritisch():
    st = gut()
    st["transportregeln"][0]["SentToScope"] = "NotInOrganization"
    b = tsc.pruefe(st, INPUTS)
    assert any("SentToScope" in x["text"] for x in _schweren(b, tsc.KRITISCH))


def test_testmode_connector_wird_gemeldet():
    st = gut()
    st["outbound_connectoren"].append(
        {"Name": "EXO Signature Gateway - Outbound_Test_2026-06-27T15:08:16Z", "Enabled": True,
         "IsValidated": False, "SmartHosts": [], "TlsSettings": "", "IsTransportRuleScoped": True})
    b = tsc.pruefe(st, INPUTS)
    assert any("Test-Mode" in x["text"] for x in _schweren(b, tsc.WARNUNG))


def test_smime_regel_mit_leerem_gate_ist_ok():
    """Leeres FromMemberOf ist bei S/MIME-Regeln KORREKT — kein Befund dafür."""
    b = tsc.pruefe(gut(), INPUTS)
    assert not any("SMIME" in x["objekt"] for x in b)


def test_fehlende_route_regel_ist_kritisch():
    st = gut()
    st["transportregeln"] = [r for r in st["transportregeln"] if "Route via" not in r["Name"]]
    b = tsc.pruefe(st, INPUTS)
    assert any("fehlt" in x["text"] for x in _schweren(b, tsc.KRITISCH))


def test_fehlende_auto_antwort_ausnahme_ist_warnung():
    """Ohne die Auto-Submitted-Ausnahme läuft die OOF eines DL-Postfachs durchs
    Gateway und scheitert extern (451, leerer Absender) — muss als WARNUNG
    auffallen. Gegenprobe zum Gut-Zustand, der die Ausnahme trägt."""
    st = gut()
    for r in st["transportregeln"]:
        if "Route via" in r["Name"]:
            r["ExceptIfHeaderContainsMessageHeader"] = ""
            r["ExceptIfHeaderContainsWords"] = []
    b = tsc.pruefe(st, INPUTS)
    assert any("Auto-Antwort-Ausnahme" in x["text"] for x in _schweren(b, tsc.WARNUNG)), b


def _regel_von(zustand, name):
    return next(r for r in zustand["transportregeln"] if r["Name"] == name)


def test_altes_loop_muster_an_smime_inbound_ist_warnung():
    """Der Fall vom 23.10.: Muster „1“ nimmt die Markierung JEDES Gateways aus —
    signierte Post eines fremden Gateways umging die Inbound-Regel."""
    for name in ("EXO Signature Gateway - SMIME Signed Inbound",
                 "Route via EXO Signature Gateway (Signatur)"):
        z = gut()
        _regel_von(z, name)["ExceptIfHeaderMatchesPatterns"] = ["1"]
        w = _schweren(tsc.pruefe(z, INPUTS), tsc.WARNUNG)
        assert any(b["objekt"] == name and "Loop-Muster" in b["text"] for b in w), (name, w)


def test_loop_muster_eines_anderen_tenants_ist_warnung():
    z = gut()
    _regel_von(z, "EXO Signature Gateway - SMIME Encrypted Inbound")[
        "ExceptIfHeaderMatchesPatterns"] = [tsc.muster_fuer("anderer-tenant")]
    w = _schweren(tsc.pruefe(z, INPUTS), tsc.WARNUNG)
    assert any("Loop-Muster" in b["text"] for b in w)


def test_ohne_tenant_id_nur_hinweis():
    """Ohne TENANT_ID ist kein Soll ableitbar — nicht still grün, aber auch kein Alarm."""
    ein = dict(INPUTS, tenant_id="")
    befunde = tsc.pruefe(gut(), ein)
    assert not [b for b in befunde if "Loop-Muster" in b["text"] and b["schwere"] != tsc.HINWEIS]
    assert any("Loop-Muster nicht geprüft" in b["text"] for b in befunde)


def test_werkzeug_und_gateway_leiten_gleich_ab(monkeypatch):
    """EINE Ableitung: das Werkzeug muss genau das Muster erwarten, das das
    Gateway setzt (marker_muster)."""
    import config, loop_detector
    monkeypatch.setattr(config, "TENANT_ID", INPUTS["tenant_id"])
    assert loop_detector.marker_muster() == MUSTER
