"""Zertifikate für viele Postfächer auf einmal — Vorschau.

Einzeln bestellen ist bei zwanzig Postfächern mühsam und bei hundert nicht mehr
machbar. Was diesen Weg bisher blockierte, war nicht die Oberfläche, sondern die
Bestätigung durch jeden einzelnen Postfachinhaber; seit die das Gateway
übernehmen kann (`ca_bestaetigung`), ist ein Sammellauf überhaupt sinnvoll.

DIESES MODUL BESTELLT NICHTS. Es beantwortet ausschliesslich die Frage „was
würde passieren?" — und zwar bevor Geld fliesst. Das ist kein Beiwerk:

* **Kosten.** Die Deckung wird sonst je Bestellung geprüft, und jede Lücke löst
  eine eigene Nachladung aus — bei hundert Bestellungen hundertmal die
  Grundgebühr des Zahlungsdienstes. Der Gesamtbedarf gehört VOR den Lauf.
* **Kontingent.** Ein Monatslimit, das mitten im Lauf greift, hinterlässt einen
  halb erledigten Stand. Besser vorher wissen, wie weit es reicht.
* **Voraussetzungen.** Fehlt die Zustimmung zu den Bedingungen oder ist der
  Bezug gesperrt, scheitern alle Bestellungen gleichermassen — das einmal zu
  sagen ist brauchbarer als hundertmal.
"""
import logging

log = logging.getLogger(__name__)

# Zustände je Postfach. „bereit" ist der einzige, der bestellt würde.
BEREIT = "bereit"
HAT_ZERTIFIKAT = "hat_zertifikat"
KEIN_SMIME = "kein_smime"


def _gueltige_zertifikate(email: str) -> int:
    """Anzahl brauchbarer Zertifikate — abgelaufene und defekte zählen nicht."""
    import smime_store
    from datetime import datetime
    heute = datetime.now()
    n = 0
    for c in smime_store.list_user_certs(email):
        if c.get("error"):
            continue
        try:
            if datetime.strptime(c["expiry"], "%d.%m.%Y") > heute:
                n += 1
        except Exception:
            continue
    return n


def postfaecher() -> list[dict]:
    """Alle Postfächer mit ihrem Zustand — Grundlage jeder Auswahl."""
    import settings_store

    cfg = settings_store.get("MAILBOX_CONFIG") or {}
    aus = []
    for _guid, v in cfg.items():
        if not isinstance(v, dict):
            continue
        adresse = (v.get("primary") or "").strip().lower()
        if not adresse:
            continue
        anzahl = _gueltige_zertifikate(adresse)
        if not v.get("smime"):
            zustand = KEIN_SMIME
        elif anzahl:
            zustand = HAT_ZERTIFIKAT
        else:
            zustand = BEREIT
        aus.append({
            "email": adresse,
            "name": v.get("display_name") or "",
            "zustand": zustand,
            "zertifikate": anzahl,
        })
    return sorted(aus, key=lambda p: p["email"])


async def vorschau(provider_id: str, adressen: list[str]) -> dict:
    """Was ein Sammellauf kosten und bewirken würde. Bestellt nichts.

    `adressen` ist die Auswahl des Betreibers; alles Weitere wird hier geprüft,
    nicht dort. Eine Oberfläche, die selbst entscheidet, wer „dran" ist, würde
    beim nächsten Umbau eine andere Antwort geben als der Server.
    """
    import hub_catalog, hub_client, config

    auswahl = [a.strip().lower() for a in (adressen or []) if a and a.strip()]
    bekannt = {p["email"]: p for p in postfaecher()}
    rechte = await hub_client.cert_eligibility()

    # ⚠️ Katalog auffrischen, BEVOR der Preis gelesen wird. Ein frisch
    # gestarteter Prozess hat ihn noch nicht — und ein nicht gefundener Anbieter
    # fiele sonst still auf den Vorgabepreis zurück. Eine Kostenvorschau, die im
    # Zweifel eine falsche Zahl nennt, ist schlimmer als gar keine: Sie wird
    # geglaubt.
    try:
        await hub_catalog.refresh()
    except Exception as exc:
        log.warning("Anbieterkatalog nicht auffrischbar: %s", exc)

    prov = hub_catalog.get(provider_id)
    if prov is None:
        return {"ok": False,
                "hindernisse": [f"Anbieter {provider_id!r} ist im Katalog nicht "
                                f"vorhanden — ohne ihn lassen sich die Kosten "
                                f"nicht bestimmen."],
                "postfaecher": [], "bestellbar": 0, "kosten_cents": 0}

    roh = prov.get("price_cents")
    # Wie im Hub: 0 heisst kostenlos, fehlend heisst „Vorgabepreis".
    netto = int(roh) if roh is not None else int(rechte.get("cert_price_cents") or 0)
    ust = int(rechte.get("vat_percent", 19) or 0)
    brutto = (netto * (100 + ust) + 50) // 100

    zeilen = []
    for adresse in auswahl:
        p = bekannt.get(adresse)
        if not p:
            zeilen.append({"email": adresse, "zustand": "unbekannt",
                           "hinweis": "Postfach ist hier nicht eingerichtet."})
            continue
        zeilen.append({**p, "hinweis": {
            HAT_ZERTIFIKAT: "Hat bereits ein gültiges Zertifikat.",
            KEIN_SMIME: "S/MIME ist für dieses Postfach nicht eingeschaltet.",
        }.get(p["zustand"], "")})

    bestellbar = [z for z in zeilen if z.get("zustand") == BEREIT]

    # Kontingent: 0 heisst unbegrenzt.
    limit = int(rechte.get("monthly_limit") or 0)
    genutzt = int(rechte.get("used_this_month") or 0)
    frei = max(0, limit - genutzt) if limit else None
    gekappt = len(bestellbar) - frei if (frei is not None and len(bestellbar) > frei) else 0

    tatsaechlich = len(bestellbar) - gekappt
    kosten = tatsaechlich * brutto
    guthaben = int(rechte.get("balance_cents") or 0)
    rechnungskunde = rechte.get("billing_mode") == "invoice"

    hindernisse = []
    if not rechte.get("ok"):
        hindernisse.append(rechte.get("reason") or rechte.get("error")
                           or "Zertifikatsbezug derzeit nicht möglich.")
    if gekappt:
        hindernisse.append(
            f"Monatskontingent reicht für {frei} von {len(bestellbar)} Bestellungen "
            f"({genutzt} von {limit} bereits genutzt).")
    if not rechnungskunde and kosten > guthaben:
        fehlt = kosten - guthaben
        hindernisse.append(
            f"Guthaben deckt den Lauf nicht: {guthaben/100:.2f} € vorhanden, "
            f"{kosten/100:.2f} € nötig — es fehlen {fehlt/100:.2f} €.")

    return {
        "ok": True,
        "anbieter": {"id": provider_id, "label": prov.get("label") or provider_id,
                     "netto_cents": netto, "brutto_cents": brutto,
                     "vat_percent": ust},
        "postfaecher": zeilen,
        "bestellbar": tatsaechlich,
        "uebersprungen": len(zeilen) - tatsaechlich,
        "kosten_cents": kosten,
        "guthaben_cents": guthaben,
        "fehlbetrag_cents": max(0, kosten - guthaben) if not rechnungskunde else 0,
        "kontingent_frei": frei,
        "hindernisse": hindernisse,
    }
