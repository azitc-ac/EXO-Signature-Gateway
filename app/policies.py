"""Auflösung der Vorlagen-Richtlinien für einen Absender.

Herausgelöst aus handler.py (v1.9.68), damit der Signatur-Hot-Path UND die
zentrale Abwesenheitsnotiz (abwesenheit.py) DIESELBE Auflösung teilen — sonst
driften zwei Gruppenlogiken auseinander, und ein Postfach bekäme im OOO eine
andere Gruppenzuordnung als in der Signatur.

Rangfolge je Slot (sig/min/banner/disclaimer/oof):
  Gruppen-Richtlinie (CUSTOM_POLICIES, first-match-wins) übersteuert die globale
  Richtlinie (TEMPLATE_POLICIES) — aber nur, wenn das Postfach den Richtlinien
  folgt (use_policy=true). Bei use_policy=false liest der Aufrufer die Vorlage aus
  dem Postfach-Eintrag selbst (z.B. `banner_template`); dann sind hier nur die
  globalen Richtlinien maßgeblich und `use_pol` ist False.
"""
from __future__ import annotations

import mailbox_match
import settings_store


def resolve_policies(sender: str, mailbox_cfg: dict | None = None,
                     sender_cfg: dict | None = None) -> tuple[dict, bool]:
    """`(policies, use_pol)` für einen Absender.

    `policies` = TEMPLATE_POLICIES, bei use_pol überlagert von den Gruppen-Treffern.
    `use_pol`  = folgt das Postfach den Richtlinien? Bei False liest der Aufrufer
                 die Vorlage aus `sender_cfg` (Postfach-eigene Felder).

    `mailbox_cfg`/`sender_cfg` können vorberechnet übergeben werden (Hot-Path in
    handler.py hat sie bereits); sonst werden sie hier ermittelt.
    """
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    if sender_cfg is None:
        sender_cfg = mailbox_match.match_sender(mailbox_cfg, sender)

    policies: dict = dict(settings_store.get("TEMPLATE_POLICIES") or {})
    use_pol = sender_cfg.get("use_policy", True)
    if use_pol:
        custom = settings_store.get("CUSTOM_POLICIES") or []
        groups = settings_store.get("INTERNAL_GROUPS") or {}
        if custom and groups:
            sender_key = mailbox_match.match_sender_key(mailbox_cfg, sender)
            overrides: dict[str, str] = {}
            for pol in custom:
                if pol.get("condition_type") == "group":
                    guids = groups.get(pol.get("group_name", ""), [])
                    if sender_key and sender_key in guids:
                        slot = pol.get("applies_to", "")
                        if slot and slot not in overrides:  # first-match-wins
                            overrides[slot] = pol.get("template", "")
            if overrides:
                policies = {**policies, **overrides}
    return policies, use_pol


def group_vars_for(sender: str, mailbox_cfg: dict | None = None) -> dict:
    """Custom-Variablenwerte, die den Gruppen des Absenders zugewiesen sind.

    `GROUP_VARS = {Gruppenname: {var: wert}}`. Ist der Absender in mehreren Gruppen,
    gewinnt der ERSTE Treffer je Variable (first-match, wie bei den Richtlinien) —
    die Reihenfolge folgt `INTERNAL_GROUPS`. Rangfolge insgesamt (aufgelöst beim
    Aufrufer): Entra-Feld < Gruppen-Wert < Postfach-Override.
    """
    group_vars = settings_store.get("GROUP_VARS") or {}
    groups = settings_store.get("INTERNAL_GROUPS") or {}
    if not group_vars or not groups:
        return {}
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    key = mailbox_match.match_sender_key(mailbox_cfg, sender)
    if not key:
        return {}
    ergebnis: dict = {}
    for gname, mitglieder in groups.items():
        if key in (mitglieder or []):
            for vname, vwert in (group_vars.get(gname) or {}).items():
                if vname and vwert and vname not in ergebnis:  # first-match-wins
                    ergebnis[vname] = vwert
    return ergebnis
