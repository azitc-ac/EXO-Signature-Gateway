"""Anwendungsberechtigungen der Gateway-App: Soll gegen Ist — EINE Quelle.

Soll: `setup_wizard._GRAPH_PERMISSION_SPECS` / `_EXO_PERMISSION_SPECS` (daraus wird
auch der API-Antrag gebaut). Ist: die `roles`-Ansprüche im ANWENDUNGSTOKEN — also
das, was Entra dem Gateway tatsächlich erteilt hat, nicht was im Portal beantragt
ist. Das ist der Unterschied, auf den es ankommt: Eine beantragte, aber nicht per
Admin-Consent erteilte Rolle steht in der Portal-Liste und fehlt im Token.

Beheben: `setup_wizard.berechtigungen_nachziehen()` über eine Admin-Anmeldung
(flow „berechtigungen" in anmeldung.py) — NUR Rollen, kein neues Secret, kein
neues Zertifikat (anders als der volle Setup-Lauf).
"""
from __future__ import annotations

import base64
import json


def token_rollen(token: str) -> set[str] | None:
    """Die `roles`-Ansprüche (Anwendungsberechtigungen) aus einem JWT.

    Ohne Signaturprüfung — es geht nur darum, WELCHE Rollen erteilt sind, nicht
    um Vertrauen in das Token (das kommt direkt von MSAL). `None`, wenn das Token
    nicht als JWT lesbar ist."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:                                          # noqa: BLE001
        return None
    roles = data.get("roles")
    if not isinstance(roles, list):
        return set()
    return {str(r) for r in roles}


def graph_rollen() -> set[str] | None:
    """Rollen im Graph-Anwendungstoken; `None` ohne Token/Zugangsdaten."""
    import graph_client
    token = graph_client._acquire_token()
    return token_rollen(token) if token else None


def exo_rollen() -> set[str] | None:
    """Rollen im EXO-Token (Audience outlook.office365.com) — Exchange.ManageAsApp
    und IMAP.AccessAsApp stehen NICHT im Graph-Token. `None` ohne Token."""
    import graph_client
    app = graph_client._get_msal_app()
    if app is None:
        return None
    try:
        result = app.acquire_token_for_client(scopes=["https://outlook.office365.com/.default"])
    except Exception:                                          # noqa: BLE001
        return None
    tok = result.get("access_token") if isinstance(result, dict) else None
    return token_rollen(tok) if tok else None


def status(nur: list[str] | None = None) -> list[dict]:
    """[{name, api, zweck, erteilt}] je Soll-Rolle. `erteilt` ist True/False, oder
    None, wenn sich das Token nicht beschaffen ließ (dann ist NICHTS bekannt —
    nicht „fehlt"). `nur` beschränkt auf die genannten Rollen (Reihenfolge der Soll-Liste)."""
    import setup_wizard
    graph, exo = graph_rollen(), exo_rollen()
    ergebnis = []
    for api, specs, ist in (("Graph", setup_wizard._GRAPH_PERMISSION_SPECS, graph),
                            ("Exchange", setup_wizard._EXO_PERMISSION_SPECS, exo)):
        for s in specs:
            if nur and s["name"] not in nur:
                continue
            ergebnis.append({
                "name": s["name"], "api": api, "zweck": s.get("zweck", ""),
                "erteilt": None if ist is None else (s["name"] in ist),
            })
    return ergebnis
