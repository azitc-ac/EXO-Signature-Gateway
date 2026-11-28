"""Regelprobe (regel_probe.py): Greift die Transportregel für ein Postfach?

Invarianten:
  * Verworfen wird NUR eine Mail mit einer offenen, vom Gateway ausgestellten,
    unverbrauchten Kennung, deren Absender und einziger Empfänger das Postfach
    ist, für das sie ausgestellt wurde. Alles andere läuft normal weiter —
    sonst könnte jeder durch Setzen des Kopfs Post verschwinden lassen.
  * Die Kennung im Betreff genügt (falls Exchange den x-Kopf verliert).
  * Erkennung nur auf dem Exchange-Weg, nicht für Relay-Geräte.
  * Kommt die Probe nicht am Gateway an, wird sie im Posteingang gesucht und
    gelöscht; im 3-h-Fenster nach der Aktivierung heißt das „wartet“ (info),
    danach „greift nicht“ (error).
  * Hybrid-Nutzer: keine Mail, Grund gespeichert. Nicht aktive: abgelehnt.
  * Nichts läuft ohne Knopf: Nur die Route startet Proben.
"""
from __future__ import annotations

import ast
import asyncio
import email
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import handler
import regel_probe as rp

WURZEL = Path(__file__).resolve().parent.parent
ADR = "max.mustermann@firma.at"
TENANT = "aaaaaaaa-1111-2222-3333-444444444444"


@pytest.fixture(autouse=True)
def sauber(monkeypatch):
    rp._offen.clear()
    rp._laufend.clear()
    rp._lauf.update({"gesamt": 0, "fertig": 0, "ergebnisse": {}, "fehler": {}})
    monkeypatch.setattr(rp, "TAKT_S", 0.01)
    monkeypatch.setattr(rp, "SUCHE_TAKT_S", 0.02)
    monkeypatch.setattr(rp, "ANKUNFT_FRIST_S", 0.05)
    monkeypatch.setattr(rp, "GESAMT_FRIST_S", 0.3)
    monkeypatch.setattr(rp, "SAMMEL_ABSTAND_S", 0.0)
    yield
    rp._offen.clear()
    rp._laufend.clear()


def _kennung(adresse=ADR):
    pid, geheim = rp.ausstellen(adresse)
    return pid, geheim, f"{pid}.{geheim}"


# ── erkennen(): Token-Sicherheit ─────────────────────────────────────────────

def test_gueltige_kennung_wird_erkannt_und_verbraucht():
    pid, _g, k = _kennung()
    erg = rp.erkennen(k, "egal", ADR, [ADR])
    assert erg and erg["adresse"] == ADR and erg["probe_id"] == pid
    assert rp.angekommen(pid) is not None
    # Wiederverwendung: zweite Mail mit derselben Kennung wird NICHT verworfen.
    assert rp.erkennen(k, "egal", ADR, [ADR]) is None


@pytest.mark.parametrize("fall", ["fremd", "geheim_falsch", "anderes_postfach",
                                  "zweiter_empfaenger", "anderer_absender", "abgelaufen",
                                  "geschlossen"])
def test_ungueltige_kennung_wird_ignoriert(fall, monkeypatch):
    pid, geheim, k = _kennung()
    absender, empfaenger, kopf = ADR, [ADR], k
    if fall == "fremd":
        kopf = "0123456789ab." + "A" * 32
    elif fall == "geheim_falsch":
        kopf = f"{pid}." + ("B" * 32 if geheim != "B" * 32 else "C" * 32)
    elif fall == "anderes_postfach":
        absender, empfaenger = "erika@firma.at", ["erika@firma.at"]
    elif fall == "zweiter_empfaenger":
        empfaenger = [ADR, "kunde@extern.de"]
    elif fall == "anderer_absender":
        absender = "angreifer@extern.de"
    elif fall == "abgelaufen":
        rp._offen[pid]["ablauf"] = 0
    elif fall == "geschlossen":
        rp.schliessen(pid)
    assert rp.erkennen(kopf, "", absender, empfaenger) is None
    if fall not in ("abgelaufen", "geschlossen"):
        # Ein Fehlversuch verbraucht die echte Kennung nicht.
        assert rp.erkennen(k, "", ADR, [ADR]) is not None


def test_kennung_im_betreff_genuegt():
    _pid, _g, k = _kennung()
    assert rp.erkennen("", f"AW: {rp.BETREFF} {k}", ADR, [ADR]) is not None


def test_normale_post_kostet_nichts():
    assert rp.erkennen("", "Angebot", ADR, [ADR]) is None
    assert rp.erkennen("", f"{rp.BETREFF} ohne Kennung", ADR, [ADR]) is None


# ── Handler: Platz der Erkennung ─────────────────────────────────────────────

class _Sitzung:
    peer = ("40.92.10.10", 5000)
    ssl = None


class _Umschlag:
    def __init__(self, content, rcpt=None):
        self.mail_from = ADR
        self.rcpt_tos = rcpt or [ADR]
        self.content = content


@pytest.fixture
def anlage(monkeypatch):
    werte = {
        "REINJECT_MODE": "smtp", "SMTP_RELAY_ENABLED": False, "RELAY_TENANT_CHECK": True,
        "MAILBOX_CONFIG": {ADR: {"sig": True, "template": "Sig", "use_policy": False}},
        "SMIME_SIGNING_ENABLED": False, "SKIP_SIG_IN_THREAD": True,
        "NOSIG_TRIGGER": "#nosig", "NODIGSIG_TRIGGER": "#nodigsig",
        "SIGN_INTERNAL_ONLY_MAIL": True, "TENANT_ID": TENANT,
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: True)
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)
    zugestellt, protokoll = [], []
    import reinject
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: zugestellt.append(a) or True)
    import mail_audit
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: protokoll.append(kw["action"]))
    import graph_client
    async def _user(e):
        return graph_client.UserData(mail=e)
    monkeypatch.setattr(graph_client, "get_user", _user)
    import signature_engine
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, **kw: ("<p>[Sig]</p>", "[Sig]"))
    import sig_thread
    monkeypatch.setattr(sig_thread, "kennt", lambda s, b: False)
    monkeypatch.setattr(sig_thread, "merken", lambda s, m: None)
    return {"zugestellt": zugestellt, "protokoll": protokoll}


def _mail(kopf="", betreff="Test"):
    zeilen = [f"From: {ADR}", f"To: {ADR}", f"Subject: {betreff}",
              "Message-ID: <p1@firma.at>", f"X-MS-Exchange-CrossTenant-Id: {TENANT}"]
    if kopf:
        zeilen.append(f"{rp.KOPF}: {kopf}")
    zeilen += ["MIME-Version: 1.0", "Content-Type: text/html; charset=utf-8", "",
               "<html><body><p>Text</p></body></html>", ""]
    return "\r\n".join(zeilen).encode()


def _durch(sitzung=None, **kw):
    return asyncio.run(handler.SignatureHandler().handle_DATA(
        None, sitzung or _Sitzung(), _Umschlag(_mail(**kw))))


def test_handler_verwirft_gueltige_probe(anlage):
    _pid, _g, k = _kennung()
    assert _durch(kopf=k, betreff=f"{rp.BETREFF} {k}") == "250 OK"
    assert anlage["zugestellt"] == [], "Probe darf nicht zugestellt werden"
    assert anlage["protokoll"] == ["regel_probe"]


def test_handler_verwirft_probe_ohne_kopf_nur_mit_betreff(anlage):
    _pid, _g, k = _kennung()
    assert _durch(betreff=f"{rp.BETREFF} {k}") == "250 OK"
    assert anlage["zugestellt"] == []


def test_handler_verarbeitet_gefaelschte_probe_normal(anlage):
    _kennung()                                  # eine offene Probe existiert
    assert _durch(kopf="0123456789ab." + "A" * 32) == "250 OK"
    assert len(anlage["zugestellt"]) == 1, "gefälschte Kennung → normale Zustellung"
    msg = email.message_from_bytes(next(a for a in anlage["zugestellt"][0]
                                        if isinstance(a, (bytes, bytearray))))
    html = next(t.get_payload(decode=True) for t in msg.walk()
                if t.get_content_type() == "text/html")
    assert b"[Sig]" in html, "normal signiert"
    assert anlage["protokoll"] == ["signed"]


def test_handler_wiederverwendete_kennung_wird_zugestellt(anlage):
    _pid, _g, k = _kennung()
    _durch(kopf=k)
    _durch(kopf=k)
    assert len(anlage["zugestellt"]) == 1


def test_handler_relay_geraet_kann_nicht_verwerfen(anlage, monkeypatch):
    import smtp_relay
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle", lambda ip: True)
    monkeypatch.setattr(smtp_relay, "pruefe", lambda s, r, ip: (True, "", ""))
    import relay_hosts, relay_stats
    monkeypatch.setattr(relay_hosts, "merke_zustellung", lambda *a, **kw: None)
    monkeypatch.setattr(relay_stats, "merke", lambda *a, **kw: None)
    _pid, _g, k = _kennung()
    _durch(kopf=k)
    assert len(anlage["zugestellt"]) == 1, "Relay-Post ist nie eine Probe"


def test_handler_erkennung_vor_jedem_zustellweg():
    """Die Erkennung steht nach dem Tenant-Gate und vor Harvest, ACME,
    Auto-Submitted, Kalender und S/MIME — sonst ginge die Probe zugestellt."""
    src = (WURZEL / "app/handler.py").read_text(encoding="utf-8")
    probe = src.index("regel_probe.erkennen(")
    assert src.index('"tenant_fremd"') < probe
    assert src.count("regel_probe.erkennen(") == 1
    for spaeter in ('settings_store.get("SMIME_HARVEST_RCPT")', 'decoded_subject.startswith("ACME: ")',
                    '(msg.get("Auto-Submitted")', "if _has_calendar_part(msg):",
                    "smime_decrypt.decrypt(", "# ── Loop detection",
                    # erster Rückweg in handle_DATA (die Tenant-Weiterleitung davor
                    # gilt nur FREMDEN Tenants und nutzt _relay_to_target)
                    "reinject.send(sender"):
        assert src.count(spaeter) >= 1, f"Anker {spaeter!r} fehlt — Prüfer veraltet"
        start = src.index("async def handle_DATA")
        assert probe < src.index(spaeter, start), f"{spaeter} steht vor der Regelprobe"


# ── Ablauf ───────────────────────────────────────────────────────────────────

@pytest.fixture
def graph(monkeypatch):
    """Graph ersetzt: Senden, Posteingang, Löschen."""
    zustand = {"gesendet": [], "posteingang": None, "geloescht": [], "senden_fehler": "",
               "loeschen_fehler": "", "ankunft": False, "wartet": False}

    async def senden(adresse, pid, geheim):
        zustand["gesendet"].append(adresse)
        if not zustand["senden_fehler"] and zustand["ankunft"]:
            rp.erkennen(f"{pid}.{geheim}", "", adresse, [adresse])
        return zustand["senden_fehler"]

    async def suchen(adresse, pid, ab):
        return zustand["posteingang"]

    async def loeschen(adresse, nid):
        zustand["geloescht"].append(nid)
        return zustand["loeschen_fehler"]

    monkeypatch.setattr(rp, "_senden", senden)
    monkeypatch.setattr(rp, "_im_posteingang", suchen)
    monkeypatch.setattr(rp, "_loeschen", loeschen)
    monkeypatch.setattr(rp, "_regel_wartet", lambda cfg: zustand["wartet"])
    monkeypatch.setattr(rp, "_cfg", lambda a: {"sig": True})
    return zustand


def test_durchlauf_greift(graph):
    graph["ankunft"] = True
    erg = asyncio.run(rp._durchlauf(ADR))
    assert erg["ergebnis"] == "greift" and erg["status"] == "ok"
    assert graph["geloescht"] == [] and rp._offen == {}, "Kennung geschlossen"


def test_durchlauf_greift_nicht_sucht_und_loescht(graph):
    graph["posteingang"] = {"id": "AAMk1", "ueber_gateway": False}
    erg = asyncio.run(rp._durchlauf(ADR))
    assert erg["ergebnis"] == "greift_nicht" and erg["status"] == "error"
    assert graph["geloescht"] == ["AAMk1"] and "gelöscht" in erg["detail"]
    assert rp._offen == {}


def test_durchlauf_im_wartefenster_ist_hinweis(graph):
    graph["posteingang"] = {"id": "AAMk1", "ueber_gateway": False}
    graph["wartet"] = True
    erg = asyncio.run(rp._durchlauf(ADR))
    assert erg["ergebnis"] == "wartet" and erg["status"] == "info"


def test_durchlauf_loeschen_gescheitert_wird_gemeldet(graph):
    graph["posteingang"] = {"id": "AAMk1", "ueber_gateway": False}
    graph["loeschen_fehler"] = "HTTP 403"
    erg = asyncio.run(rp._durchlauf(ADR))
    assert "liegt im Posteingang" in erg["detail"]


def test_durchlauf_kopie_ueber_gateway_ist_kein_beleg(graph):
    graph["posteingang"] = {"id": "AAMk1", "ueber_gateway": True}
    erg = asyncio.run(rp._durchlauf(ADR))
    assert erg["ergebnis"] == "unklar" and erg["status"] == "warn"


def test_durchlauf_nichts_gefunden_ist_unklar(graph):
    erg = asyncio.run(rp._durchlauf(ADR))
    assert erg["ergebnis"] == "unklar" and graph["geloescht"] == []


def test_durchlauf_senden_gescheitert(graph):
    graph["senden_fehler"] = "sendMail HTTP 403: denied"
    erg = asyncio.run(rp._durchlauf(ADR))
    assert erg["ergebnis"] == "fehler" and "403" in erg["detail"] and rp._offen == {}


# ── pruefen(): Hybrid, nicht aktiv, Speichern ────────────────────────────────

@pytest.fixture
def speicher(monkeypatch):
    store = {"MAILBOX_HEALTH": {}, "MAILBOX_CONFIG": {ADR: {"sig": True}}}
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: store.update(p))
    return store


def test_hybrid_sendet_nichts_und_nennt_grund(speicher, graph, monkeypatch):
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda a: True)
    rp.anmelden([ADR])
    erg = asyncio.run(rp.pruefen(ADR))
    assert erg["ergebnis"] == "nicht_moeglich" and graph["gesendet"] == []
    assert speicher["MAILBOX_HEALTH"][ADR]["checks"]["regel_greift"]["status"] == "skip"
    assert ADR not in rp._laufend


def test_nicht_aktiv_wird_abgelehnt(speicher, graph, monkeypatch):
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda a: False)
    speicher["MAILBOX_CONFIG"] = {ADR: {"sig": False, "smime": False}}
    monkeypatch.setattr(rp, "_cfg", lambda a: {"sig": False, "smime": False})
    erg = asyncio.run(rp.pruefen(ADR))
    assert erg["ergebnis"] == "abgelehnt" and graph["gesendet"] == []
    assert speicher["MAILBOX_HEALTH"] == {}


def test_ergebnis_landet_im_gesundheitsstatus(speicher, graph, monkeypatch):
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda a: False)
    speicher["MAILBOX_HEALTH"] = {ADR: {"overall": "ok", "checks": {
        "dg_member": {"status": "ok", "detail": "", "checked_at": ""}}}}
    graph["posteingang"] = {"id": "x", "ueber_gateway": False}
    asyncio.run(rp.pruefen(ADR))
    h = speicher["MAILBOX_HEALTH"][ADR]
    assert h["checks"]["regel_greift"]["status"] == "error"
    assert "dg_member" in h["checks"], "übrige Prüfungen bleiben"
    assert h["overall"] == "error"


def test_zweite_probe_fuer_dasselbe_postfach_wird_abgewiesen():
    assert rp.anmelden([ADR]) == ([ADR], [])
    assert rp.anmelden([ADR, "erika@firma.at"]) == (["erika@firma.at"], [ADR])


def test_sammeln_drosselt_und_sammelt_fehler(monkeypatch):
    gleichzeitig, hoechst = [0], [0]

    async def pruefen(a):
        gleichzeitig[0] += 1
        hoechst[0] = max(hoechst[0], gleichzeitig[0])
        await asyncio.sleep(0.02)
        gleichzeitig[0] -= 1
        if a.startswith("kaputt"):
            raise RuntimeError("Graph weg")
        return {"ergebnis": "greift"}
    monkeypatch.setattr(rp, "pruefen", pruefen)
    adressen = [f"p{i}@firma.at" for i in range(8)] + ["kaputt@firma.at"]
    asyncio.run(rp.sammeln(adressen))
    s = rp.stand()
    assert hoechst[0] <= rp.SAMMEL_PARALLEL, hoechst
    assert s["fertig"] == s["gesamt"] == 9
    assert s["ergebnisse"] == {"greift": 8, "fehler": 1}
    assert "kaputt@firma.at" in s["fehler"]


# ── Gesundheitsprüfung übernimmt, setzt aber nicht selbst ────────────────────

def _probe(ergebnis="greift", vor=timedelta(0)):
    t = (datetime.now(timezone.utc) - vor).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ergebnis": ergebnis, "status": "ok", "detail": "d", "checked_at": t}


def test_gesundheitspruefung_uebernimmt_aktuelles_ergebnis(speicher):
    import health_check
    speicher["MAILBOX_HEALTH"] = {ADR: {"regel_probe": _probe()}}
    health_check._ergebnis_speichern(ADR, {"exo_mailbox": {"status": "ok"}})
    h = speicher["MAILBOX_HEALTH"][ADR]
    assert h["checks"]["regel_greift"]["status"] == "ok" and h["regel_probe"]


def test_veraltetes_ergebnis_faellt_weg(speicher):
    import health_check
    jetzt = datetime.now(timezone.utc)
    # Probe VOR der (erneuten) Aktivierung → sagt nichts über die neue Mitgliedschaft
    speicher["MAILBOX_CONFIG"] = {ADR: {"sig": True, "aktiviert_seit": jetzt.isoformat()}}
    speicher["MAILBOX_HEALTH"] = {ADR: {"regel_probe": _probe(vor=timedelta(hours=1))}}
    health_check._ergebnis_speichern(ADR, {})
    assert "regel_greift" not in speicher["MAILBOX_HEALTH"][ADR]["checks"]
    # Postfach deaktiviert
    speicher["MAILBOX_CONFIG"] = {ADR: {"sig": False}}
    speicher["MAILBOX_HEALTH"] = {ADR: {"regel_probe": _probe()}}
    health_check._ergebnis_speichern(ADR, {})
    assert "regel_greift" not in speicher["MAILBOX_HEALTH"][ADR]["checks"]


def test_belegte_regel_nimmt_wartemarke(monkeypatch):
    import exo_mailboxes
    from webui.routen import mailboxes as mbroute
    frisch = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    store = {"USER_BOOKINGS": {},
             "MAILBOX_CONFIG": {ADR: {"sig": True, "aktiviert_seit": frisch},
                                "erika@firma.at": {"sig": True, "aktiviert_seit": frisch}},
             "MAILBOX_HEALTH": {ADR: {"regel_probe": _probe()}}}
    monkeypatch.setattr(mbroute.settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(exo_mailboxes, "hybrid_eingeschaltet", lambda: False)
    monkeypatch.setattr(exo_mailboxes, "hybrid_aktiv", lambda: False)
    monkeypatch.setattr(mbroute._pol, "standards", lambda *_a: {})
    monkeypatch.setattr(exo_mailboxes, "mit_hybrid", lambda *_a: [
        {"primary": ADR, "display_name": "Max", "type": "UserMailbox"}])
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=False))
    z = {m["email"]: m for m in r["mailboxes"]}
    assert z[ADR]["regel_wartet"] is False, "Probe hat belegt, dass die Regel greift"
    assert z["erika@firma.at"]["regel_wartet"] is True


def test_nur_die_route_startet_proben():
    """Proben starten nur der Knopf (Route) und — seit v1.12.188 — der Takt für
    vorbereitete, noch nicht belegte Postfächer (scheduler._vorbereitung_pruefen,
    verarbeitung.offen). Nie die Gesundheitsprüfung."""
    aufrufer = []
    for datei in (WURZEL / "app").rglob("*.py"):
        if datei.name == "regel_probe.py":
            continue
        baum = ast.parse(datei.read_text(encoding="utf-8"))
        for knoten in ast.walk(baum):
            if (isinstance(knoten, ast.Attribute) and knoten.attr in ("starten", "sammeln", "pruefen")
                    and isinstance(knoten.value, ast.Name) and knoten.value.id == "regel_probe"):
                aufrufer.append(f"{datei.relative_to(WURZEL)}:{knoten.attr}")
    assert sorted(aufrufer) == ["app/scheduler.py:sammeln",
                                "app/webui/routen/mailboxes.py:starten"], aufrufer


# ── Graph-Aufrufe (httpx ersetzt) ────────────────────────────────────────────

def test_graph_aufrufe(monkeypatch):
    import httpx
    anfragen = []

    def antwort(req: httpx.Request):
        anfragen.append((req.method, req.url.path, req))
        if req.method == "POST":
            return httpx.Response(202)
        if req.method == "DELETE":
            return httpx.Response(204)
        if req.url.path.endswith("/messages") :
            return httpx.Response(200, json={"value": [
                {"id": "fremd", "subject": "Angebot"},
                {"id": "AAMk1", "subject": f"{rp.BETREFF} 0123456789ab.{'A' * 32}"}]})
        return httpx.Response(200, json={"internetMessageHeaders": [
            {"name": "X-MS-Exchange-CrossTenant-Id", "value": TENANT}]})

    echt = httpx.AsyncClient
    monkeypatch.setattr(rp.httpx, "AsyncClient",
                        lambda **kw: echt(transport=httpx.MockTransport(antwort), **kw))

    async def token():
        return "t"
    monkeypatch.setattr(rp, "_token", token)

    assert asyncio.run(rp._senden(ADR, "0123456789ab", "A" * 32)) == ""
    import json
    rumpf = json.loads(anfragen[0][2].content)
    assert rumpf["saveToSentItems"] is False
    koepfe = {h["name"] for h in rumpf["message"]["internetMessageHeaders"]}
    assert koepfe == {rp.KOPF}, "kein Auto-Submitted, kein X-Sig-Applied"
    assert rumpf["message"]["toRecipients"][0]["emailAddress"]["address"] == ADR

    fund = asyncio.run(rp._im_posteingang(ADR, "0123456789ab", datetime.now(timezone.utc)))
    assert fund == {"id": "AAMk1", "ueber_gateway": False}
    assert asyncio.run(rp._loeschen(ADR, "AAMk1")) == ""
    assert anfragen[-1][0] == "DELETE" and anfragen[-1][1].endswith("/messages/AAMk1")
