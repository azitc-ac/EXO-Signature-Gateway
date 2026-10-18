"""Jeder Changelog-Eintrag sagt dem Betreiber, was nach dem Update zu tun ist.

ANLASS (18.10.2026): Das Verfassen-Add-in (v1.9.164) und die stille
Tab-Anmeldung (v1.9.170) brauchten Einträge an der Anmelde-App in Entra. Sie
wurden auf der VM von Hand angelegt, und der Changelog sagte bestehenden
Installationen nicht, dass sie das ebenfalls tun müssen. Ein Betreiber erfuhr
davon erst, wenn etwas nicht ging.

Die Passage ist deshalb Pflicht, auch wenn die Antwort „nichts“ lautet. Ein
ausdrückliches „nichts“ ist eine geprüfte Aussage; eine fehlende Passage lässt
offen, ob jemand darüber nachgedacht hat. Ältere Einträge bleiben, wie sie
sind; sie beschreiben den Stand ihres Tages.
"""
import re
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
AB_VERSION = (1, 9, 173)
_KOPF = re.compile(r"^## v(\d+)\.(\d+)\.(\d+)\b.*$", re.M)
# „**Zu tun:**“ als Absatz, oder eine Überschrift „### Zu tun“ / „### Was du tun musst“.
_PASSAGE = re.compile(r"^(\*\*Zu tun:\*\*|### (Zu tun|Was du tun musst))", re.M)


def _eintraege():
    text = CHANGELOG.read_text("utf-8")
    koepfe = list(_KOPF.finditer(text))
    for i, m in enumerate(koepfe):
        ende = koepfe[i + 1].start() if i + 1 < len(koepfe) else len(text)
        yield tuple(int(x) for x in m.groups()), m.group(0), text[m.end():ende]


def test_jeder_neue_eintrag_hat_eine_zu_tun_passage():
    fehlt = [kopf for ver, kopf, rumpf in _eintraege()
             if ver >= AB_VERSION and not _PASSAGE.search(rumpf)]
    assert not fehlt, (
        "Ohne „**Zu tun:**“-Passage:\n  " + "\n  ".join(fehlt) + "\n\n"
        "Sag dem Betreiber, was nach dem Update zu tun ist, und nenne dabei den "
        "Weg in der Oberfläche. Zum Beispiel: erneut in der Einrichtung "
        "anmelden (Main- oder Bootstrap-App), ein Manifest neu hochladen, einer "
        "Berechtigung zustimmen. Ist nichts zu tun, schreib „nichts“.")


def test_die_pruefung_greift_ueberhaupt():
    """Ohne einen einzigen geprüften Eintrag wäre der Test oben still grün."""
    assert any(ver >= AB_VERSION for ver, _k, _r in _eintraege())
