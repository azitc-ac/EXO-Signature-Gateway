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
# Bezugswege des Katalogs tragen dieses Präfix (ca_backends.get_backend).
HUB_PRAEFIX = "hub:"

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


def _nachladebetrag(fehlt: int, rechte: dict) -> int:
    """Was die automatische Aufladung ziehen würde — dieselbe Rechnung wie dort.

    Bewusst nachgebildet und nicht geschätzt: Ein Betrag, den wir ankündigen und
    der dann abweicht, ist schlimmer als gar keine Ankündigung.
    """
    schritt = max(int(rechte.get("auto_topup_schritt_cents") or 0),
                  int(rechte.get("min_topup_cents") or 0))
    if schritt <= 0 or fehlt <= 0:
        return 0
    mal = -(-fehlt // schritt)                   # aufrunden
    hoechst = int(rechte.get("max_topup_cents") or 0)
    betrag = schritt * mal
    return min(betrag, hoechst) if hoechst else betrag


def hindernisse_blockieren(rechte: dict, fehlt: int, automatik: bool) -> bool:
    """Steht dem Start etwas ENTGEGEN? Ein angekündigter Nachladebetrag nicht.

    Die Unterscheidung ist der Kern: „Es werden 25 € eingezogen" ist eine
    Ankündigung, die bestätigt werden will — kein Hindernis. „Es fehlen 25 €"
    ohne Automatik ist eines.
    """
    if not rechte.get("ok"):
        return True
    return bool(fehlt) and not automatik


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

    # ⚠️ Die Oberfläche schickt den BEZUGSWEG ("hub:certum_test"), der Katalog
    # führt die ANBIETER ("certum_test"). Beides sah lange gleich aus und ist es
    # nicht. Ohne Präfix landet ca_backends.get_backend() zudem auf seinem
    # stillen Rückfall `assisted_manual` — ein Sammellauf hätte dann lautlos den
    # falschen Bezugsweg genommen. Deshalb wird hier ausdrücklich verlangt, was
    # gemeint ist, statt es zu erraten.
    if not provider_id.startswith(HUB_PRAEFIX):
        return {"ok": False,
                "hindernisse": ["Für eine Sammelbestellung wird eine über den "
                                "Anbieterkatalog bezogene Zertifizierungsstelle "
                                "benötigt."],
                "postfaecher": [], "bestellbar": 0, "kosten_cents": 0}

    prov = hub_catalog.get(provider_id[len(HUB_PRAEFIX):])
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

    # Was die automatische Aufladung ziehen würde. ⚠️ Bei einer Massenoperation
    # ist eine unangekündigte Abbuchung der teuerste Überraschungsmoment — der
    # Betrag gehört VOR den Start, nicht auf den Kontoauszug.
    fehlt = max(0, kosten - guthaben) if not rechnungskunde else 0
    automatik = bool(rechte.get("auto_topup_aktiv"))
    nachladung = _nachladebetrag(fehlt, rechte) if (fehlt and automatik) else 0

    hindernisse = []
    # ⚠️ „Nicht startbereit" ohne Grund ist eine Sackgasse: Der Betreiber sieht
    # einen abgelehnten Start und keinen Hinweis, was zu tun wäre. Der Fall „gar
    # nichts zu bestellen" erzeugte bisher kein Hindernis, weil er kein Fehler
    # im engeren Sinn ist — für den, der davorsteht, aber schon.
    if not tatsaechlich:
        if not zeilen:
            hindernisse.append("Keine Postfächer ausgewählt.")
        elif gekappt:
            hindernisse.append(
                f"Monatskontingent erschöpft — {genutzt} von {limit} bereits genutzt.")
        else:
            hindernisse.append(
                "Für keines der ausgewählten Postfächer ist eine Bestellung nötig: "
                "sie haben bereits ein gültiges Zertifikat oder sind nicht für "
                "S/MIME eingeschaltet.")
    if not rechte.get("ok"):
        hindernisse.append(rechte.get("reason") or rechte.get("error")
                           or "Zertifikatsbezug derzeit nicht möglich.")
    if gekappt:
        hindernisse.append(
            f"Monatskontingent reicht für {frei} von {len(bestellbar)} Bestellungen "
            f"({genutzt} von {limit} bereits genutzt).")
    if fehlt and automatik:
        hindernisse.append(
            f"Guthaben deckt den Lauf nicht ({guthaben/100:.2f} € von "
            f"{kosten/100:.2f} €). Die automatische Aufladung zieht dafür "
            f"{nachladung/100:.2f} € von Ihrem Zahlungsmittel ein.")
    elif fehlt:
        hindernisse.append(
            f"Guthaben deckt den Lauf nicht: {guthaben/100:.2f} € vorhanden, "
            f"{kosten/100:.2f} € nötig — es fehlen {fehlt/100:.2f} €. "
            f"Bitte vor dem Start aufladen.")

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
        "fehlbetrag_cents": fehlt,
        "automatik_aktiv": automatik,
        "nachladung_cents": nachladung,
        # Startbereit heisst: gedeckt, oder die Automatik schliesst die Lücke.
        "startbereit": bool(tatsaechlich) and not hindernisse_blockieren(
            rechte, fehlt, automatik),
        "kontingent_frei": frei,
        "hindernisse": hindernisse,
    }


# ── Der Lauf ─────────────────────────────────────────────────────────────────
# Ein Sammellauf ist langlebig: Hundert Bestellungen dauern Minuten und
# überleben keine Browser-Anfrage. Der Zustand liegt deshalb im Modul, wird
# fortlaufend geschrieben und ist über eine eigene Adresse abfragbar.
#
# ⚠️ NUR EIN LAUF GLEICHZEITIG. Zwei parallele Läufe würden dasselbe Guthaben
# verplanen und dieselben Postfächer doppelt bestellen — beides fällt erst auf,
# wenn das Geld weg ist.

LAEUFT = "laeuft"
PAUSIERT = "pausiert"          # Guthaben erschöpft, wartet auf Entscheidung
FERTIG = "fertig"
ABGEBROCHEN = "abgebrochen"

_lauf: dict | None = None


def lauf_zustand() -> dict | None:
    """Aktueller Lauf oder None. Kopie — der Aufrufer soll nichts verändern."""
    return dict(_lauf) if _lauf else None


def lauf_abbrechen() -> bool:
    """Bittet den Lauf, nach der laufenden Bestellung aufzuhören.

    Kein hartes Abbrechen: Eine Bestellung, die bereits bei der
    Zertifizierungsstelle liegt, lässt sich nicht zurücknehmen — sie muss
    zu Ende geführt und verbucht werden, sonst entsteht genau der unbezahlte
    Vorgang, den `store.unbezahlte_bestellungen()` im Hub aufspürt.
    """
    if _lauf and _lauf["status"] in (LAEUFT, PAUSIERT):
        _lauf["abbruch_gewuenscht"] = True
        return True
    return False


async def lauf_starten(provider_id: str, adressen: list[str], actor: str = "") -> dict:
    """Startet einen Sammellauf im Hintergrund. Liefert den Anfangszustand."""
    global _lauf
    import asyncio

    if _lauf and _lauf["status"] in (LAEUFT, PAUSIERT):
        return {"ok": False, "error": "Es läuft bereits ein Sammelvorgang."}

    # ⚠️ Deckung VOR dem Start prüfen, nicht erst bei der ersten Bestellung.
    # Ohne das startet ein Lauf über hundert Postfächer, bestellt fünf und bleibt
    # dann stehen — mit fünf Bestellungen, die niemand geplant hat. Die Vorschau
    # sagt dasselbe, aber zwischen ihr und dem Start kann Zeit vergehen.
    pruefung = await vorschau(provider_id, adressen)
    if not pruefung.get("ok"):
        return {"ok": False, "error": " ".join(pruefung.get("hindernisse") or
                                               ["Vorschau nicht möglich."])}
    if not pruefung.get("startbereit"):
        return {"ok": False, "error": " ".join(pruefung.get("hindernisse") or
                                               ["Der Lauf ist nicht startbereit."]),
                "fehlbetrag_cents": pruefung.get("fehlbetrag_cents", 0)}

    offen = [a.strip().lower() for a in (adressen or []) if a and a.strip()]
    _lauf = {
        "status": LAEUFT, "anbieter": provider_id, "gestartet_von": actor,
        "offen": offen, "erledigt": [], "gesamt": len(offen),
        "fehlbetrag_cents": 0, "abbruch_gewuenscht": False, "meldung": "",
    }
    asyncio.create_task(_arbeiten())
    return {"ok": True, **lauf_zustand()}


async def lauf_fortsetzen() -> dict:
    """Nach dem Aufladen weitermachen — ohne die bereits erledigten zu wiederholen."""
    global _lauf
    import asyncio
    if not _lauf or _lauf["status"] != PAUSIERT:
        return {"ok": False, "error": "Kein angehaltener Sammelvorgang."}
    _lauf["status"] = LAEUFT
    _lauf["meldung"] = ""
    _lauf["fehlbetrag_cents"] = 0
    asyncio.create_task(_arbeiten())
    return {"ok": True, **lauf_zustand()}


async def _arbeiten() -> None:
    """Arbeitet die offene Liste ab. Läuft im Hintergrund, wirft nie."""
    import hub_client

    while _lauf and _lauf["status"] == LAEUFT and _lauf["offen"]:
        if _lauf.get("abbruch_gewuenscht"):
            _lauf["status"] = ABGEBROCHEN
            _lauf["meldung"] = "Auf Wunsch beendet."
            return

        adresse = _lauf["offen"][0]
        try:
            ergebnis = await _eine_bestellung(adresse, _lauf["anbieter"])
        except Exception as exc:                      # nie den ganzen Lauf reissen lassen
            log.error("Sammellauf: %s unerwartet gescheitert: %s", adresse, exc)
            ergebnis = {"email": adresse, "ok": False, "grund": str(exc)[:200]}

        # ⚠️ Bei Guthabenmangel NICHT als erledigt vermerken: Die Adresse bleibt
        # vorn in der Liste und wird nach dem Aufladen als Erstes bestellt.
        if ergebnis.get("grund_kurz") == "guthaben":
            _lauf["status"] = PAUSIERT
            _lauf["fehlbetrag_cents"] = int(ergebnis.get("fehlbetrag_cents") or 0)
            _lauf["meldung"] = ergebnis.get("grund") or "Guthaben erschöpft."
            return

        _lauf["offen"].pop(0)
        _lauf["erledigt"].append(ergebnis)

    if _lauf and _lauf["status"] == LAEUFT:
        _lauf["status"] = FERTIG
        gut = sum(1 for e in _lauf["erledigt"] if e.get("ok"))
        _lauf["meldung"] = f"{gut} von {_lauf['gesamt']} bestellt."


async def _eine_bestellung(adresse: str, provider_id: str) -> dict:
    """Eine einzelne Bestellung über denselben Weg wie die Einzelbestellung."""
    import ca_backends, settings_store

    cfg = dict((settings_store.get("CA_USER_CONFIG") or {}).get(adresse) or {})
    backend = ca_backends.get_backend(provider_id)
    # ⚠️ get_backend() liefert bei unbekanntem Namen NICHT None, sondern
    # `assisted_manual` — ein stiller Rückfall, der hier eine Bestellung über den
    # falschen Bezugsweg erzeugt hätte. Deshalb wird gegengeprüft, was man
    # bekommen hat, statt nur auf None zu prüfen.
    if not backend or backend.get_name() != provider_id:
        return {"email": adresse, "ok": False,
                "grund": f"Bezugsweg {provider_id} unbekannt — Lauf abgebrochen, "
                         f"damit nichts über einen anderen Weg bestellt wird."}
    try:
        await backend.initiate_renewal(adresse, cfg)
        return {"email": adresse, "ok": True, "grund": ""}
    except Exception as exc:
        text = str(exc)
        # Guthabenmangel ist der einzige Fehler, der den ganzen Lauf betrifft —
        # alle weiteren Bestellungen scheitern genauso. Deshalb gesondert.
        fehlbetrag = getattr(exc, "fehlbetrag_cents", 0)
        if "uthaben" in text or fehlbetrag:
            return {"email": adresse, "ok": False, "grund": text[:200],
                    "grund_kurz": "guthaben", "fehlbetrag_cents": fehlbetrag}
        return {"email": adresse, "ok": False, "grund": text[:200]}
