"""Routen-Bestandsaufnahme — Sicherheitsnetz für das Aufteilen von app.py.

WOFÜR
-----
`app/webui/app.py` hat 4945 Zeilen und keinerlei Testabdeckung. Wer die Datei
aufteilt, kann eine Route verlieren, doppelt registrieren oder ihre Methoden
verändern, ohne dass es auffällt — der Fehler zeigt sich erst, wenn ein Nutzer
auf eine Schaltfläche drückt und nichts passiert.

Diese Datei hält deshalb die vollständige Routentabelle fest. Sie prüft KEIN
Verhalten (das können Unittests der einzelnen Funktionen besser), sondern nur:
*es ist danach dieselbe Oberfläche wie davor*. Genau die Frage, die bei einem
reinen Umsortieren zählt.

WENN DIESER TEST FEHLSCHLÄGT
----------------------------
Entweder wurde beim Umbau etwas verloren — dann ist es ein Fehler. Oder eine
Route wurde absichtlich hinzugefügt oder entfernt — dann die Momentaufnahme
bewusst neu erzeugen:

    python3 tests/test_routes.py --snapshot

Nie blind neu erzeugen: der Sinn ist, dass die Änderung durch die Hand geht.
"""
import json
import sys
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).parent / "routes_snapshot.json"


def _alle_route_objekte():
    """Routen der Anwendung UND der eingebundenen Routenmodule.

    ⚠️ `app.routes` allein genügt nicht mehr.

    Bis FastAPI 0.11x kopierte `include_router()` die Routen in `app.routes`.
    Ab 0.139 hängt es stattdessen einen Stellvertreter (`_IncludedRouter`) ein,
    dessen `path` `None` ist — die Routen dahinter werden zur Laufzeit korrekt
    bedient, tauchen in `app.routes` aber nicht mehr auf.

    Genau das ist am 09.08.2026 passiert: Nach dem Herauslösen des ersten
    Routenmoduls meldete die CI acht verlorene Adressen, während lokal alles
    grün war. Der Unterschied war die FastAPI-Fassung — `requirements.txt`
    pinnt 0.139.0, auf dem Entwicklungsrechner lag 0.115.6. Die Anwendung war
    in Ordnung; blind war die Prüfung.

    Deshalb wird NICHT in die Innereien von `_IncludedRouter` gegriffen (privat,
    ändert sich wieder), sondern über `app.ROUTENMODULE` — dieselbe Liste, aus
    der `app.py` die Router einbindet. Eine Quelle für beides: Wer ein Modul
    hinzufügt, ohne es einzutragen, bindet es auch nicht ein, und dann fallen
    die übrigen Prüfungen darüber.

    Doppelte werden nur an der Naht entfernt (Modulroute, die die Anwendung
    ohnehin führt) — die Duplikatprüfung weiter unten bleibt dadurch scharf.
    """
    from webui.app import app, ROUTENMODULE
    objekte = list(app.routes)
    bekannt = {(getattr(r, "path", None),
                tuple(sorted(getattr(r, "methods", None) or [])))
               for r in objekte}
    for modul in ROUTENMODULE:
        for r in modul.router.routes:
            schluessel = (getattr(r, "path", None),
                          tuple(sorted(getattr(r, "methods", None) or [])))
            if schluessel not in bekannt:
                objekte.append(r)
                bekannt.add(schluessel)
    return objekte


def aktuelle_routen() -> list[dict]:
    """Alle Routen als vergleichbare, sortierte Liste."""
    routen = []
    for r in _alle_route_objekte():
        pfad = getattr(r, "path", None)
        if pfad is None:
            continue
        methoden = sorted(getattr(r, "methods", None) or [])
        # HEAD wird von Starlette automatisch zu GET ergaenzt und sagt nichts
        # ueber die Anwendung aus.
        methoden = [m for m in methoden if m != "HEAD"]
        routen.append({
            "pfad": pfad,
            "methoden": methoden,
            "name": getattr(r, "name", "") or "",
        })
    return sorted(routen, key=lambda r: (r["pfad"], ",".join(r["methoden"])))


def test_momentaufnahme_existiert():
    assert SNAPSHOT.is_file(), (
        "Routen-Momentaufnahme fehlt — mit "
        "`python3 tests/test_routes.py --snapshot` erzeugen")


def test_routentabelle_unveraendert():
    erwartet = json.loads(SNAPSHOT.read_text())
    ist = aktuelle_routen()

    e_pfade = {(r["pfad"], ",".join(r["methoden"])) for r in erwartet}
    i_pfade = {(r["pfad"], ",".join(r["methoden"])) for r in ist}

    verloren = sorted(e_pfade - i_pfade)
    neu = sorted(i_pfade - e_pfade)

    meldung = []
    if verloren:
        meldung.append("VERLOREN (waren da, sind weg):\n  "
                       + "\n  ".join(f"{m} {p}" for p, m in verloren))
    if neu:
        meldung.append("NEU (sind da, waren nicht):\n  "
                       + "\n  ".join(f"{m} {p}" for p, m in neu))
    assert not meldung, (
        "\n\n" + "\n\n".join(meldung)
        + "\n\nWar das Absicht? Dann die Momentaufnahme bewusst neu erzeugen:"
          "\n  python3 tests/test_routes.py --snapshot\n")


def test_keine_doppelten_routen():
    """Zwei Registrierungen desselben Pfads mit derselben Methode: die zweite
    ist tot. Beim Verschieben von Endpunkten zwischen Dateien leicht passiert."""
    gesehen: dict[tuple, str] = {}
    doppelt = []
    for r in aktuelle_routen():
        for m in r["methoden"]:
            schluessel = (r["pfad"], m)
            if schluessel in gesehen:
                doppelt.append(f"{m} {r['pfad']}  ({gesehen[schluessel]} / {r['name']})")
            gesehen[schluessel] = r["name"]
    assert not doppelt, "doppelt registrierte Routen:\n  " + "\n  ".join(doppelt)


def test_alle_api_routen_haben_einen_namen():
    """Namenlose Endpunkte sind ein Zeichen dafür, dass ein Dekorator beim
    Verschieben verloren ging."""
    ohne = [r["pfad"] for r in aktuelle_routen()
            if r["pfad"].startswith("/api/") and not r["name"]]
    assert not ohne, f"Routen ohne Namen: {ohne}"


def test_aufzaehlung_sieht_routen_aus_modulen():
    """Die Aufzählung muss Router-Routen erfassen, nicht nur `app.routes`.

    ANLASS (09.08.2026): Ab FastAPI 0.139 kopiert `include_router()` die Routen
    nicht mehr nach `app.routes`, sondern hängt einen Stellvertreter ein. Die
    Anwendung bedient sie weiterhin korrekt — die Momentaufnahme sah sie aber
    nicht und meldete acht verlorene Adressen. Lokal blieb es grün, weil dort
    eine ältere FastAPI-Fassung lag als die in `requirements.txt` gepinnte.

    Ohne diese Prüfung wäre der umgekehrte Fall unsichtbar: eine Aufzählung,
    die Modulrouten übersieht, meldet bei jedem weiteren herausgelösten Modul
    „verloren" — oder schlimmer, nach einem blind neu erzeugten Abbild gar
    nichts mehr.
    """
    from webui.app import ROUTENMODULE
    assert ROUTENMODULE, "keine Routenmodule eingebunden — Liste leer?"
    aufgezaehlt = {r["pfad"] for r in aktuelle_routen()}
    for modul in ROUTENMODULE:
        for r in modul.router.routes:
            assert r.path in aufgezaehlt, (
                f"{r.path} aus {modul.__name__} fehlt in der Aufzählung — "
                f"die Momentaufnahme ist blind für ausgelagerte Routen")


def test_module_sind_auch_wirklich_eingebunden():
    """Gegenprobe zur Aufzählung: die Adressen müssen ERREICHBAR sein.

    Eine Liste, die stimmt, während die Route nicht eingebunden ist, wäre die
    schlimmere Fassung des Fehlers — die Prüfung wäre grün und die Oberfläche
    kaputt.

    ⚠️ NICHT über den Antwortcode geprüft. Die erste Fassung tat das und fiel
    sofort über `/portal/logo`: Die Route wirft selbst 404, wenn kein Logo
    hinterlegt ist, und zwar ohne Meldungstext — von „Adresse gibt es nicht"
    ist das im Antwortkörper nicht zu unterscheiden. Eine Ausnahmeliste hätte
    das kaschiert und beim nächsten solchen Fall wieder gefehlt.

    `url_path_for()` fragt stattdessen die Namenstabelle des Routers. Sie ist
    öffentliche Starlette-API und sieht auch die Routen hinter dem
    Stellvertreter, den FastAPI ab 0.139 für eingebundene Router einhängt —
    im Gegensatz zu `app.routes`.
    """
    import re as _re
    from starlette.routing import NoMatchFound
    from webui.app import app, ROUTENMODULE

    for modul in ROUTENMODULE:
        for r in modul.router.routes:
            name = getattr(r, "name", "") or ""
            assert name, f"{r.path} aus {modul.__name__} hat keinen Namen"
            # Pfadparameter mitgeben, sonst meldet url_path_for auch bei einer
            # vorhandenen Route „kein Treffer" — `/addin/icon/{size_str}` liess
            # die Pruefung zunaechst genau daran fehlschlagen. „1" passt auch
            # fuer die int-Umwandlung.
            params = {p.split(":")[0]: "1"
                      for p in _re.findall(r"\{([^}]+)\}", r.path)}
            try:
                app.url_path_for(name, **params)
            except NoMatchFound:
                pytest.fail(f"{r.path} ({name}) aus {modul.__name__} ist nicht "
                            f"eingebunden — der Router fehlt in ROUTENMODULE "
                            f"oder wird nicht included")


def test_keine_pruefung_verdrahtet_app_py_fest():
    """Wer den Quelltext der Oberfläche liest, muss ihn VOLLSTÄNDIG lesen.

    Eine Prüfung, die `app/webui/app.py` fest anspricht, verliert lautlos ihre
    Wirkung, sobald die geprüfte Gruppe in ein Routenmodul zieht: Sie liest
    weiter eine Datei, in der das Gesuchte nicht mehr steht. Am 11.08.2026 ist
    das `driftcheck` passiert (Einstellungen → `routen/settings.py`), und bei
    den Zahlweg-Gates in `test_legal_consent.py` stand es beim Hub-Modul
    erneut an.

    Der einzige zulässige Weg ist `hilfen.webui_quelltext()` bzw.
    `driftcheck.webui_quellen()`. Die beiden Dateien, die diesen Weg
    BEREITSTELLEN, dürfen den Pfad naturgemäss nennen.
    """
    import ast as _ast
    wurzel = Path(__file__).resolve().parent.parent
    erlaubt = {wurzel / "tests" / "hilfen.py", wurzel / "tools" / "driftcheck.py"}

    # Gesucht ist genau EINE Form: den Quelltext dieser einen Datei LESEN.
    #
    # Nicht gemeint ist, `app.py` überhaupt zu benennen. `test_importrichtung.py`
    # etwa bildet `WEBUI / "app.py"`, um die Importe dieser Datei zu prüfen —
    # die Regel „app.py entnimmt nichts aus Routenmodulen" ist auf sie gemünzt
    # und wandert nicht mit. Ein Muster über Zeilen oder blosse Pfadliterale
    # traf beides gleichermassen und dazu jeden Erklärtext, der die Datei nennt.
    treffer = []
    for datei in sorted((wurzel / "tests").glob("*.py")) + sorted((wurzel / "tools").glob("*.py")):
        if datei in erlaubt:
            continue
        for knoten in _ast.walk(_ast.parse(datei.read_text(encoding="utf-8"))):
            if not (isinstance(knoten, _ast.Call)
                    and isinstance(knoten.func, _ast.Attribute)
                    and knoten.func.attr in ("read_text", "read_bytes")):
                continue
            quelle = _ast.unparse(knoten.func.value)
            if "app.py" in quelle and "webui" in quelle:
                treffer.append(f"{datei.relative_to(wurzel)}:{knoten.lineno}: "
                               f"{quelle}.{knoten.func.attr}(…)")

    assert not treffer, (
        "Diese Stellen lesen nur `app/webui/app.py` und werden blind, sobald "
        "die geprüfte Gruppe in ein Routenmodul zieht:\n  " + "\n  ".join(treffer)
        + "\n\nStattdessen `hilfen.webui_quelltext()` (Tests) bzw. "
          "`driftcheck.webui_quellen()` (Prüfskripte) benutzen.")


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
        routen = aktuelle_routen()
        SNAPSHOT.write_text(json.dumps(routen, indent=2, ensure_ascii=False) + "\n")
        print(f"Momentaufnahme geschrieben: {len(routen)} Routen → {SNAPSHOT}")
    else:
        print(__doc__)
