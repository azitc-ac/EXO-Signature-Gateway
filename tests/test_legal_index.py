"""legal/index.json bleibt an CURRENT_DOCUMENTS gebunden.

Der Hub liest `index.json` zur Laufzeit und leitet daraus ab, welche Rechtstexte
er unter sighub.zarenko.net/legal/… öffentlich ausliefert. Die Datei ist erzeugt,
nicht gepflegt — erzeugt von `tools/legal-sync-check.py` aus der Registry in
`app/legal_consent.py`.

Zwei Wege, auf denen das still auseinanderläuft:

1. **Registry geändert, index.json vergessen.** Eine neue Dokumentversion trägt
   den Versionsstand im Dateinamen. Bleibt index.json stehen, liefert der Hub
   weiter die alte Fassung aus — öffentlich, unter genau der Adresse, auf die
   Verträge und die Zahlungsseite verweisen. Das Gateway verlangt derweil
   Zustimmung zur neuen. Zwei Fassungen, die identisch sein sollen.

2. **Registry nennt eine Datei, die es nicht gibt.** Dann liefert der Hub für
   dieses Dokument eine Fehlerseite — und zwar erst, wenn jemand sie aufruft.

Der Test prüft ausserdem, dass das Skript die Registry richtig ausliest: Es holt
sie aus dem Syntaxbaum (kein Import, weil `legal_consent` `config` nachzöge).
Hier steht der echte Import daneben, also lässt sich beides vergleichen.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _skript():
    pfad = ROOT / "tools" / "legal-sync-check.py"
    spec = importlib.util.spec_from_file_location("legal_sync_check", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def test_ast_auslesen_trifft_die_echte_registry():
    """Gegenprobe: Syntaxbaum gegen echten Import."""
    import legal_consent
    assert _skript().registry() == legal_consent.CURRENT_DOCUMENTS


def test_index_ist_auf_dem_stand_der_registry():
    skript = _skript()
    soll = skript.index_bauen(skript.registry())
    ist = (ROOT / "legal" / "index.json").read_text(encoding="utf-8")
    assert ist == soll, (
        "legal/index.json weicht von CURRENT_DOCUMENTS ab — der Hub würde eine "
        "andere Fassung ausliefern als das Gateway verlangt. "
        "Erzeugen mit: python3 tools/legal-sync-check.py --fix"
    )


def test_jede_genannte_datei_existiert():
    import legal_consent
    fehlend = [
        rel
        for d in legal_consent.CURRENT_DOCUMENTS.values()
        for rel in (d["path_de"], d["path_en"])
        if not (ROOT / "legal" / rel).is_file()
    ]
    assert not fehlend, f"in CURRENT_DOCUMENTS genannt, aber nicht vorhanden: {fehlend}"


def test_dateiname_traegt_die_angegebene_version():
    """`hub-terms` v2.3 muss auf …-v2.3.md zeigen. Beim Anheben der Version wird
    gern die Zahl geändert und der Dateiname stehen gelassen — dann bestätigt
    der Nutzer „Fassung 2.4" und liest 2.3."""
    import legal_consent
    falsch = [
        (kennung, d["version"], rel)
        for kennung, d in legal_consent.CURRENT_DOCUMENTS.items()
        for rel in (d["path_de"], d["path_en"])
        if f"-v{d['version']}.md" not in rel
    ]
    assert not falsch, f"Version und Dateiname passen nicht zusammen: {falsch}"


@pytest.mark.parametrize("kennung", ["hub-terms", "product-privacy"])
def test_kernpflichtdokumente_bleiben_in_der_registry(kennung):
    """Die Adressen dieser beiden stehen in laufenden Verträgen bzw. sind nach
    Art. 13 DSGVO geschuldet. Wer sie entfernt, macht eine verlinkte Adresse tot."""
    import legal_consent
    assert kennung in legal_consent.CURRENT_DOCUMENTS
