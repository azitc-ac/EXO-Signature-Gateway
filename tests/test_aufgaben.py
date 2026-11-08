"""Sammelpunkt „Offene Aufgaben“ oben rechts, dringende als Banner (v1.10.82).

Was nach einem Update zu tun war, stand bis dahin nur im Changelog. Geprüft:
1. jede Aufgabe ist eine Prüfung der Abnahme (eine Soll-Funktion, kein Nachbau),
2. die Prüfungen gehen nicht ins Netz — sie laufen bei jedem Seitenaufbau,
3. jedes „Zu tun“ im Changelog ab v1.10.80 hat eine Aufgabe (oder eine
   begründete Ausnahme) — und jede Aufgabe mit `seit` ein „Zu tun“,
4. vor der Einrichtung bleibt der Sammelpunkt leer,
5. Kopfleiste und Banner zeigen die Aufgaben, maskiert, mit Link.
"""
import re
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import abnahme
import aufgaben
from webui import deps

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
AB_VERSION = (1, 10, 80)

# „Zu tun“ jenseits von „nichts“, das keine Aufgabe tragen KANN — mit Grund.
OHNE_AUFGABE: dict[str, str] = {
    "1.10.84": "einmalige Durchsicht der Admin-Liste — ob ein Eintrag dorthin "
               "gehört, kann keine Prüfung feststellen; eine Aufgabe bliebe für immer offen",
}

_KOPF = re.compile(r"^## v(\d+\.\d+\.\d+)\b.*$", re.M)
_ZU_TUN = re.compile(r"^\*\*Zu tun:\*\*\s*(.*)$", re.M)


def _zu_tun_je_fassung() -> dict[str, str]:
    text = CHANGELOG.read_text("utf-8")
    koepfe = list(_KOPF.finditer(text))
    aus = {}
    for i, m in enumerate(koepfe):
        ver = m.group(1)
        if tuple(int(x) for x in ver.split(".")) < AB_VERSION:
            continue
        ende = koepfe[i + 1].start() if i + 1 < len(koepfe) else len(text)
        z = _ZU_TUN.search(text, m.end(), ende)
        aus[ver] = z.group(1).strip() if z else ""
    return aus


def _ist_nichts(satz: str) -> bool:
    return satz.lower().startswith("nichts")


def test_jede_aufgabe_ist_eine_abnahmepruefung():
    fremd = [a.kennung for a in aufgaben.AUFGABEN if a.pruefung not in abnahme.PRUEFUNGEN]
    assert not fremd, f"Aufgabe ohne Abnahme-Prüfung (zweite Soll-Funktion?): {fremd}"
    assert len({a.kennung for a in aufgaben.AUFGABEN}) == len(aufgaben.AUFGABEN)


def test_pruefungen_gehen_nicht_ins_netz(monkeypatch):
    versuche = []

    def kein_netz(*a, **kw):
        versuche.append(a)
        raise OSError("kein Netz im Seitenaufbau")
    monkeypatch.setattr(socket.socket, "connect", kein_netz)
    monkeypatch.setattr(socket, "create_connection", kein_netz)
    monkeypatch.setattr(socket, "getaddrinfo", kein_netz)
    for a in aufgaben.AUFGABEN:
        try:
            a.pruefung()
        except Exception:                         # noqa: BLE001 — zählt nur der Netzversuch
            pass
    assert not versuche, "Aufgaben-Prüfung mit Netzabfrage — gehört nur in die Abnahme"


_NETZ = re.compile(r"urllib|socket\.|httpx|requests\.|subprocess|asyncio|_acquire_token|"
                   r"app_token|regel_lesen|_exo_rollen|_oeffentliche_ip|_dns_adressen|"
                   r"_self_fetch")


def _quelltext_mit_helfern(fn, gesehen=None) -> str:
    """Quelltext der Prüfung samt der abnahme-Helfer, die sie ruft."""
    import inspect
    gesehen = gesehen if gesehen is not None else set()
    if fn.__name__ in gesehen:
        return ""
    gesehen.add(fn.__name__)
    quelle = inspect.getsource(fn)
    for name in set(re.findall(r"\b(_\w+)\(", quelle)):
        helfer = getattr(abnahme, name, None)
        if callable(helfer) and getattr(helfer, "__module__", "") == "abnahme" \
                and name != "_punkt":
            quelle += _quelltext_mit_helfern(helfer, gesehen)
    return quelle


def test_pruefungen_rufen_kein_netz_auf():
    """Statisch, weil Laufzeit allein nicht reicht: Eine Netzprüfung bricht im
    Testlauf oft vorher ab (keine Außenadresse eingestellt) und sähe harmlos aus."""
    treffer = {a.kennung: _NETZ.findall(_quelltext_mit_helfern(a.pruefung))
               for a in aufgaben.AUFGABEN}
    assert not any(treffer.values()), f"Netzabfrage in Aufgaben-Prüfung: {treffer}"


def test_jedes_zu_tun_hat_eine_aufgabe():
    zu_tun = _zu_tun_je_fassung()
    assert zu_tun, "keine geprüfte Fassung — der Test wäre still grün"
    mit_aufgabe = {a.seit for a in aufgaben.AUFGABEN if a.seit}
    fehlt = [v for v, satz in zu_tun.items()
             if not _ist_nichts(satz) and v not in mit_aufgabe and v not in OHNE_AUFGABE]
    assert not fehlt, (
        f"„Zu tun“ ohne Aufgabe in aufgaben.AUFGABEN: {fehlt}. Sonst steht es "
        "wieder nur im Changelog. Kann es keine Prüfung geben, mit Grund in "
        "OHNE_AUFGABE eintragen.")


def test_jede_aufgabe_mit_seit_hat_ein_zu_tun():
    zu_tun = _zu_tun_je_fassung()
    falsch = [(a.kennung, a.seit) for a in aufgaben.AUFGABEN
              if a.seit and (a.seit not in zu_tun or _ist_nichts(zu_tun[a.seit]))]
    assert not falsch, f"`seit` zeigt auf keine Fassung mit „Zu tun“: {falsch}"


def _eingerichtet(monkeypatch, wert):
    monkeypatch.setattr(aufgaben, "_eingerichtet", lambda: wert)
    aufgaben.vergessen()


def test_vor_der_einrichtung_leer(monkeypatch):
    _eingerichtet(monkeypatch, False)
    assert aufgaben.offene() == []


def test_offen_und_gescheitert_erscheinen_erledigt_nicht(monkeypatch):
    _eingerichtet(monkeypatch, True)

    def kaputt():
        raise RuntimeError("Datei weg")
    monkeypatch.setattr(aufgaben, "AUFGABEN", (
        aufgaben.Aufgabe("ok", lambda: abnahme._punkt("A", abnahme.OK), "/a"),
        aufgaben.Aufgabe("offen", lambda: abnahme._punkt("B", abnahme.OFFEN, "fehlt"), "/b"),
        aufgaben.Aufgabe("dringend", lambda: abnahme._punkt("C", abnahme.OFFEN, "x"), "/c",
                         dringend=True),
        aufgaben.Aufgabe("kaputt", kaputt, "/d"),
    ))
    liste = aufgaben.offene()
    assert [a["kennung"] for a in liste] == ["dringend", "offen", "kaputt"]
    assert "Datei weg" in liste[2]["befund"]
    aufgaben.vergessen()


def test_stempel_folgt_dem_befund():
    a = aufgaben._stempel({"befund": "2 Schlüssel im Klartext"})
    assert a == aufgaben._stempel({"befund": "2 Schlüssel im Klartext"})
    assert a != aufgaben._stempel({"befund": "3 Schlüssel im Klartext"})


def _seite(monkeypatch, liste, rolle="admin"):
    monkeypatch.setattr(aufgaben, "offene", lambda: liste)
    req = SimpleNamespace(state=SimpleNamespace(user_role=rolle), url=SimpleNamespace(path="/"))
    return deps.templates.env.get_template("base.html").render(request=req, active="")


_EINE = {"kennung": "smime-schluessel", "titel": "S/MIME-Schlüssel", "befund": "2 <b>roh</b>",
         "zu_tun": "Passwort setzen", "link": "/settings/smime#schluesselablage",
         "dringend": True, "seit": "1.10.82", "stempel": "abc"}


def test_kopfleiste_und_banner(monkeypatch):
    html = _seite(monkeypatch, [_EINE, {**_EINE, "kennung": "b", "dringend": False}])
    assert 'aria-label="Offene Aufgaben: 2"' in html
    assert html.count('class="aufgabe-banner"') == 1          # nur die dringende
    assert 'data-aufgabe="smime-schluessel:abc" hidden' in html
    assert '/settings/smime#schluesselablage' in html
    assert "<b>roh</b>" not in html and "&lt;b&gt;roh" in html
    assert "seit v1.10.82" in html


def test_ohne_aufgaben_kein_banner_aber_erreichbar(monkeypatch):
    html = _seite(monkeypatch, [])
    assert 'id="aufgaben-btn"' in html and 'class="aufgabe-banner"' not in html
    assert "Nichts offen" in html


def test_bearbeiter_sieht_keinen_sammelpunkt(monkeypatch):
    html = _seite(monkeypatch, [_EINE], rolle="editor")
    assert 'id="aufgaben-btn"' not in html and 'class="aufgabe-banner"' not in html


@pytest.mark.parametrize("link", sorted({a.link for a in aufgaben.AUFGABEN}))
def test_links_zeigen_auf_vorhandene_anker(link):
    pfad, _, anker = link.partition("#")
    if not anker:
        return
    vorlagen = Path(__file__).resolve().parent.parent / "app/webui/templates"
    datei = {"/setup": "setup.html", "/settings": "settings.html",
             "/settings/smime": "settings_smime.html"}[pfad]
    assert f'id="{anker}"' in (vorlagen / datei).read_text("utf-8")
