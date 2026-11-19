"""Vorschau des Richtlinienergebnisses — was ein Postfach WIRKLICH bekommt.

Die Entscheidung trifft `policies.entscheiden`, dieselbe Funktion wie im
Mailweg (handler.py). Hier wird nur gerendert und beschriftet: je Teil
(Signatur, Banner, Disclaimer) die Vorlage, ihre Herkunft („Standard“,
„Gruppe X“, „Postfach“, „Kampagne Y“) und ob sie festgeschrieben ist — wie
das Richtlinienergebnis (RSoP) bei Gruppenrichtlinien.

Leser: Live-Vorschau im Vorlagen-Editor (`/api/preview-data` ohne
`explizit`) und die Self-Service-Seite (`/api/self/preview`). Die
Was-wäre-wenn-Vorschau (`/preview`, `explizit=1`) stellt dagegen frei zusammen
und geht NICHT hierüber.

Was nur der Mailweg weiss (Signatur des Add-ins im Entwurf, Korrekturen bei
geteilten Postfächern, die beantwortete Mail), kennt die Vorschau nicht; sie
zeigt die Fälle Neu, Antwort (in einer Kette, in der das Postfach schon
signiert hat) und Intern (alle Empfänger intern).
"""
from __future__ import annotations

import graph_client
import policies
import signature_engine

SLOT_NAME = {"sig": "Signatur", "min": "Antwort-Signatur", "intern": "Interne Signatur",
             "banner": "Banner", "disclaimer": "Disclaimer"}

GRUND_TEXT = {
    policies.GRUND_NICHT_AKTIV:
        "Das Postfach ist im Gateway nicht aktiviert — seine Post läuft unverändert durch.",
    policies.GRUND_INTERN_UNVERAENDERT:
        "Rein interne Post leitet das Gateway unverändert weiter (Einstellungen → Signatur: "
        "„Rein interne Mails auch signieren“ und „Interne Signatur auch ohne Add-in "
        "anhängen“ aus). Eine interne Signatur setzt dann nur das Verfassen-Add-in beim "
        "Schreiben.",
    policies.GRUND_POSTFACH_OHNE_SIGNATUR:
        "Das Postfach ist nur für S/MIME freigeschaltet (Haken „Signatur“ aus) — das "
        "Gateway hängt weder Signatur noch Banner noch Disclaimer an.",
    policies.GRUND_NOSIG: "#nosig im Betreff: keine Signatur.",
    policies.GRUND_KEINE_ANTWORT_SIGNATUR:
        "Keine Antwort-Signatur zugeordnet — in einer Kette, in der das Postfach schon "
        "signiert hat, hängt das Gateway keine weitere Signatur an.",
}


def quelle_text(teil: dict) -> str:
    """„Standard“, „Gruppe X“, „Postfach“ oder „Kampagne Y“."""
    if teil.get("kampagne"):
        return f"Kampagne {teil['kampagne']}"
    if teil.get("ebene") == policies.EBENE_GRUPPE:
        return f"Gruppe {teil.get('gruppe', '')}".strip()
    if teil.get("ebene") == policies.EBENE_POSTFACH:
        return "Postfach"
    return "Standard"


def _beschriftet(teil: dict) -> dict:
    t = dict(teil)
    t["quelle"] = quelle_text(teil)
    t.setdefault("kampagne", "")
    t["name"] = SLOT_NAME.get(teil["slot"], teil["slot"])
    return t


def _ueberschrift(t: dict) -> str:
    fest = ", festgeschrieben" if t.get("fest") and not t.get("kampagne") else ""
    return f"„{t['vorlage']}“ ({t['quelle']}{fest})"


async def zusammenstellen(email: str, fall: str = "neu", vorlage_override: str | None = None,
                          slot_override: str | None = None, bearbeiter: str | None = None,
                          sender_cfg: dict | None = None, per_nosig: bool = False,
                          jetzt=None) -> dict:
    """Richtlinienergebnis für `email` im Fall `fall` (policies.FAELLE), gerendert.

    `vorlage_override`/`slot_override`: die Vorlage, die gerade bearbeitet wird,
    und ihr Slot (`signatur` = der Signatur-Slot dieses Falls, sonst `banner`
    bzw. `disclaimer`). Ist sie hier wirksam, wird sie gezeigt (sie IST dann
    der Teil); sonst steht in `override` warum nicht, und ihr Rendering kommt
    gesondert mit — nie als Teil des Ergebnisses.

    `sender_cfg`: ein (ungespeicherter) Postfach-Eintrag statt des gespeicherten
    — für die Self-Service-Seite, deren Auswahl erst beim Speichern gilt.

    Liefert {fall, teile:[{slot, name, vorlage, quelle, ebene, gruppe, fest,
    kampagne, html, txt}], html, txt, signatur_unterdrueckt, grund, grund_text,
    zusatz, override_wirksam, override, hat_intern, error}.
    """
    entscheid = policies.entscheiden_fall(email, fall, per_nosig=per_nosig,
                                          sender_cfg=sender_cfg, jetzt=jetzt)
    error = None
    user_data = graph_client.UserData(mail=email)
    try:
        user_data = await graph_client.get_user(email)
    except Exception as exc:                                     # noqa: BLE001
        error = str(exc)
    # Wer schreibt: nur für die Signatur (wie im handler; Banner und Disclaimer
    # rendert er ohne `extra`).
    extra = None
    if bearbeiter and bearbeiter.strip().lower() != (email or "").strip().lower():
        try:
            extra = {"bearbeiter": await graph_client.get_user(bearbeiter)}
        except Exception as exc:                                 # noqa: BLE001
            extra = {"bearbeiter": graph_client.UserData(mail=bearbeiter)}
            error = error or str(exc)

    teile: list[dict] = []
    sig = entscheid["signatur"]
    if sig and not entscheid["signatur_unterdrueckt"]:
        html, txt = signature_engine.render(user_data, template_name=sig["vorlage"], extra=extra)
        teile.append(dict(_beschriftet(sig), html=html, txt=txt))
    for slot in ("banner", "disclaimer"):
        t = entscheid[slot]
        if t:
            html, txt = signature_engine.render(user_data, template_name=t["vorlage"])
            # Wie im handler: ein leer gerenderter Zusatz hängt nichts an.
            if html or txt.strip():
                teile.append(dict(_beschriftet(t), html=html, txt=txt))

    gesamt_html = "".join(t["html"] for t in teile)
    gesamt_txt = ""
    for t in teile:
        if t["slot"] in ("banner", "disclaimer"):
            if t["txt"].strip():
                gesamt_txt = signature_engine.text_anhaengen(gesamt_txt, t["txt"])
        else:
            gesamt_txt = t["txt"]

    ergebnis = {
        "fall": fall, "teile": teile, "html": gesamt_html, "txt": gesamt_txt,
        "signatur_unterdrueckt": entscheid["signatur_unterdrueckt"],
        "grund": entscheid["grund"], "grund_text": GRUND_TEXT.get(entscheid["grund"], ""),
        "zusatz": sorted(entscheid["zusatz"]),
        # Nur zur Anzeige (z. B. interne Signatur, die allein das Add-in setzt).
        "signatur_info": _beschriftet(sig) if sig else None,
        "hat_intern": bool(entscheid["vorlagen"].get("intern")),
        "override_wirksam": None, "override": None, "error": error,
    }
    if vorlage_override:
        ergebnis.update(_override(email, entscheid, teile, vorlage_override,
                                  slot_override or "signatur", user_data, extra))
    return ergebnis


def _override(email, entscheid, teile, vorlage, slot, user_data, extra) -> dict:
    """Ist die bearbeitete Vorlage in diesem Fall wirksam? Sonst: warum nicht."""
    if slot == "signatur":
        ziel = next((t for t in teile if t["slot"] in ("sig", "min", "intern")), None)
        slot_name = SLOT_NAME[policies._ART_SLOT.get(entscheid["art"], "sig")] \
            if entscheid["art"] else "Signatur"
    else:
        ziel = next((t for t in teile if t["slot"] == slot), None)
        slot_name = SLOT_NAME.get(slot, slot)
    if ziel is not None and ziel["vorlage"] == vorlage:
        return {"override_wirksam": True,
                "override": {"vorlage": vorlage, "slot": slot, "wirksam": True,
                             "hinweis": f"Wirksam als {ziel['name']} — {_ueberschrift(ziel)}."}}
    if ziel is not None and ziel.get("kampagne"):
        hinweis = (f"{email} bekommt diese Vorlage hier nicht — die laufende Kampagne "
                   f"„{ziel['kampagne']}“ ersetzt den Banner: wirksam ist „{ziel['vorlage']}“.")
    elif ziel is not None:
        hinweis = (f"{email} bekommt diese Vorlage hier nicht — wirksam ist "
                   f"{_ueberschrift(ziel)}.")
    elif entscheid["grund"] and slot == "signatur":
        hinweis = f"{email} bekommt hier keine {slot_name}. {GRUND_TEXT.get(entscheid['grund'], '')}"
    elif entscheid["grund"] in (policies.GRUND_NICHT_AKTIV, policies.GRUND_INTERN_UNVERAENDERT,
                                policies.GRUND_POSTFACH_OHNE_SIGNATUR):
        hinweis = f"{email} bekommt hier keinen {slot_name}. {GRUND_TEXT[entscheid['grund']]}"
    elif slot not in entscheid["zusatz"]:
        hinweis = (f"{email} bekommt hier keinen {slot_name} — in diesem Fall hängt das "
                   f"Gateway ihn nicht an (Einstellungen → Signatur: „Antworten in derselben "
                   f"Kette“ bzw. „Der Trigger entfernt“).")
    else:
        hinweis = f"{email} bekommt hier keinen {slot_name} — keiner zugeordnet."
    if slot == "signatur":
        html, txt = signature_engine.render(user_data, template_name=vorlage, extra=extra)
    else:
        html, txt = signature_engine.render(user_data, template_name=vorlage)
    return {"override_wirksam": False,
            "override": {"vorlage": vorlage, "slot": slot, "wirksam": False,
                         "hinweis": hinweis, "html": html, "txt": txt}}
