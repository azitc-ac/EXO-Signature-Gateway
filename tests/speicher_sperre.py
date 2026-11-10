"""Sperre gegen Testläufe auf einem ausgelasteten Rechner.

ANLASS (2026-11-10): Auf dem Raspi laufen neben dem Gateway weitere Projekte.
Ein voller Lauf (13 min, Chromium) füllte den Swap, ein anderer Container fiel
aus. Danach liefen zwei Teilläufe bei VOLLEM Swap, obwohl die Notiz „vorher
free -m prüfen“ gerade geschrieben war — und wieder scheiterte dort ein Upload.
Eine Notiz hat das nicht verhindert; deshalb prüft pytest selbst beim Start.

Bewusst übergehen: PYTEST_SPEICHER_EGAL=1.
"""
MIN_VERFUEGBAR_MB = 1024
MIN_SWAP_FREI_ANTEIL = 0.25


def meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo → {Schlüssel: kB}."""
    werte = {}
    for zeile in text.splitlines():
        name, _, rest = zeile.partition(":")
        teile = rest.split()
        if teile and teile[0].isdigit():
            werte[name.strip()] = int(teile[0])
    return werte


def einwand(text: str) -> str | None:
    """Grund, nicht zu starten — oder None. Ohne Angaben kein Einwand."""
    w = meminfo(text)
    gruende = []
    if "MemAvailable" in w and w["MemAvailable"] < MIN_VERFUEGBAR_MB * 1024:
        gruende.append(f"nur {w['MemAvailable'] // 1024} MB Arbeitsspeicher verfügbar "
                       f"(Grenze {MIN_VERFUEGBAR_MB} MB)")
    gesamt, frei = w.get("SwapTotal", 0), w.get("SwapFree", 0)
    if gesamt and frei < gesamt * MIN_SWAP_FREI_ANTEIL:
        gruende.append(f"Swap fast voll ({frei // 1024} von {gesamt // 1024} MB frei)")
    if not gruende:
        return None
    return ("Testlauf abgebrochen: " + "; ".join(gruende) + ". Andere Dienste auf diesem "
            "Rechner würden ausgebremst. Prüfen über die CI (push, dann gh run list) oder "
            "eine andere Sitzung; bewusst übergehen mit PYTEST_SPEICHER_EGAL=1.")
