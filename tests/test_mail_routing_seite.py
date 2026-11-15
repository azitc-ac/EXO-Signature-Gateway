"""Einstellungen → Mail-Routing: die Einrichtung ist immer erreichbar.

ANLASS (v1.12.18): Domänen-Routing, Direktversand und Hybrid-Koexistenz standen
als Karten auf der SMTP-Relay-Seite. Die erschien nur bei aktivem Relay, die
Karten nur mit dem Anzeige-Schalter DOMAIN_ROUTING_ENABLED (bzw. COEX_ENABLED)
unter Erweitert. Wer den Direktversand für etwas anderes als das Relay brauchte,
kam an die Einrichtung nicht heran. Funktion (Hauptmenü) und Einrichtung
(Einstellungen) sind jetzt getrennt.
"""
import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
VORLAGEN = WURZEL / "app" / "webui" / "templates"
sys.path.insert(0, str(WURZEL / "app"))


def _karten_koepfe(text):
    """Das öffnende <div> jeder Karte (die Zeile nach `<!-- ── Name ─`)."""
    return re.findall(r"^<!-- ── ([^─]+?) ─.*?\n(<div[^>]*>)", text, re.M)


def test_routing_karten_sind_nie_ausgeblendet():
    """Keine Karte hängt an einem Schalter — sonst wäre die Einrichtung wieder
    nur über einen Umweg erreichbar."""
    text = (VORLAGEN / "settings_routing.html").read_text("utf-8")
    koepfe = _karten_koepfe(text)
    assert [k.split(" (")[0].strip() for k, _ in koepfe] == [
        "Zielserver", "Regeln", "Selbst zustellen", "Lokaler Exchange", "Verlauf"]
    for name, div in koepfe:
        assert "display:none" not in div and "{%" not in div, (name, div)
    assert "DOMAIN_ROUTING_ENABLED" not in text and "COEX_ENABLED" not in text


def test_reiter_haengt_nicht_am_relay():
    """Der Reiter steht in der Einstellungsleiste ohne Bedingung."""
    nav = (VORLAGEN / "_nav_einstellungen.html").read_text("utf-8")
    zeile = next(z for z in nav.splitlines() if 'href="/settings/routing"' in z)
    assert "Mail-Routing" in zeile
    # Bis zum Link selbst — ein {% if %} kann auch in derselben Zeile davor stehen.
    vorher = nav[:nav.index('<li><a href="/settings/routing"')]
    offen = len(re.findall(r"{%\s*if\b", vorher)) - len(re.findall(r"{%\s*endif\b", vorher))
    assert offen == 0, "Der Reiter Mail-Routing steht in einem {% if %}-Block"


def test_hauptmenue_markiert_einstellungen():
    base = (VORLAGEN / "base.html").read_text("utf-8")
    zeile = next(z for z in base.splitlines() if 'href="/settings" class=' in z)
    assert "'settings-routing'" in zeile


def test_anzeige_schalter_sind_entfallen():
    import settings_store
    for schluessel in ("DOMAIN_ROUTING_ENABLED", "COEX_ENABLED"):
        assert schluessel in settings_store.OBSOLETE_KEYS, schluessel
        assert schluessel not in settings_store.DEFAULTS, schluessel
    erweitert = (VORLAGEN / "advanced.html").read_text("utf-8")
    assert "domain-routing-toggle" not in erweitert and "coex-toggle" not in erweitert


def test_direktversand_schnittstelle_im_eigenen_modul():
    """Pfade unverändert, Modul getrennt vom Relay."""
    from webui.routen import mail_routing, relay
    pfade = {r.path for r in mail_routing.router.routes}
    assert {"/settings/routing", "/api/direkt/einstellungen", "/api/direkt/port25-test",
            "/api/direkt/dkim", "/api/direkt/dkim/loeschen",
            "/api/direkt/dns-pruefung"} <= pfade
    assert not any(r.path.startswith("/api/direkt/") for r in relay.router.routes)
