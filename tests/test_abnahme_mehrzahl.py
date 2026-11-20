"""Befunde der Abnahme/Aufgaben nennen echte Einzahl/Mehrzahl — nicht
„6 Postfach/Postfächer“ oder „Vorlage(n)“ (Klicktest 20.11.2026, Sammelpunkt)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import abnahme  # noqa: E402

QUELLE = (Path(__file__).resolve().parent.parent / "app" / "abnahme.py").read_text(encoding="utf-8")


def test_zahl_waehlt_einzahl_und_mehrzahl():
    assert abnahme._zahl(1, "Postfach", "Postfächer") == "1 Postfach"
    assert abnahme._zahl(6, "Postfach", "Postfächer") == "6 Postfächer"
    assert abnahme._zahl(0, "Postfach", "Postfächer") == "0 Postfächer"


def test_keine_klammer_mehrzahl_in_befundtexten():
    """Nur Code, keine Kommentare/Docstrings: Zeilen mit einem String, der
    „Wort(n)“, „Wort(en)“, „Wort(e)“, „Wort(s)“ oder „X/Xer“ enthält."""
    treffer = []
    for nr, zeile in enumerate(QUELLE.splitlines(), 1):
        code = zeile.split("#", 1)[0]
        if '"""' in zeile:                     # Docstring von _zahl nennt das Beispiel
            continue
        if re.search(r"[\"'][^\"']*[A-Za-zäöüß]\((?:n|en|e|s)\)[^\"']*[\"']", code) \
                or "Postfach/Postfächer" in code:
            treffer.append(f"{nr}: {zeile.strip()}")
    assert not treffer, "Klammer-Mehrzahl statt _zahl():\n" + "\n".join(treffer)


def test_erneuerung_unentschieden_sagt_postfaecher(monkeypatch):
    import ca_backends
    import settings_store
    cfg = {f"p{i}@firma.at": {"backend": "castle_acme"} for i in range(6)}
    monkeypatch.setattr(settings_store, "get", lambda k, v=None: cfg if k == "CA_USER_CONFIG" else v)
    p = abnahme._erneuerung_unentschieden()
    assert p["befund"].startswith("6 Postfächer ohne Entscheidung"), p["befund"]
    monkeypatch.setattr(settings_store, "get",
                        lambda k, v=None: {"p0@firma.at": {"backend": "castle_acme"}} if k == "CA_USER_CONFIG" else v)
    assert abnahme._erneuerung_unentschieden()["befund"].startswith("1 Postfach ohne Entscheidung")
    assert ca_backends  # Import genügt: get_backend wird in der Prüfung gerufen
