"""Fehlende Bundle-Vorlagen beim Start nach TEMPLATE_DIR kopieren.

Warum nötig: Zur Laufzeit überdeckt ein Bind-Mount das Image-Verzeichnis
`/app/templates` (docker-compose `./templates:/app/templates`; auf Azure
`/opt/exo-gateway/templates`). Die im Image mitgelieferten Vorlagen sind damit
unsichtbar — eine frische Installation hätte GAR KEINE Vorlage und lieferte
eine leere Signatur.

`app/seed_templates/` liegt NICHT unter dem Mount und bleibt erreichbar. Von
dort werden fehlende Vorlagen kopiert.

⚠️ Vorhandene Vorlagen werden NIE überschrieben — der Betreiber behält seine
Anpassungen. Einzige Ausnahme: eine LEERE (0-Byte) Standardvorlage
`signature.*`, wie sie manche Deploy-Wege anlegen, gilt als „fehlend" und wird
durch die Demo ersetzt (sonst bliebe die Signatur dauerhaft leer).
"""
from __future__ import annotations

import logging
import os
import shutil

import config

log = logging.getLogger(__name__)

SEED_DIR = os.path.join(os.path.dirname(__file__), "seed_templates")
_ENDUNGEN = (".html", ".txt", ".meta.json")


def seed_missing() -> list[str]:
    """Fehlende Vorlagen aus dem Seed nach `config.TEMPLATE_DIR` kopieren.

    Gibt die Namen der angelegten Vorlagen zurück (leer, wenn nichts zu tun war).
    """
    ziel = config.TEMPLATE_DIR
    if not os.path.isdir(SEED_DIR):
        return []
    try:
        os.makedirs(ziel, exist_ok=True)
    except OSError as exc:
        log.warning("Vorlagen-Seeding: Zielordner %s nicht anlegbar: %s", ziel, exc)
        return []

    namen = sorted({f[:-5] for f in os.listdir(SEED_DIR) if f.endswith(".html")})
    kopiert: list[str] = []
    for name in namen:
        zh = os.path.join(ziel, f"{name}.html")
        vorhanden = os.path.exists(zh)
        leer = vorhanden and os.path.getsize(zh) == 0
        if vorhanden and not leer:
            continue
        for endung in _ENDUNGEN:
            quelle = os.path.join(SEED_DIR, f"{name}{endung}")
            if os.path.exists(quelle):
                try:
                    shutil.copy2(quelle, os.path.join(ziel, f"{name}{endung}"))
                except OSError as exc:
                    log.warning("Vorlagen-Seeding: %s%s nicht kopierbar: %s",
                                name, endung, exc)
        kopiert.append(name)

    if kopiert:
        log.info("Vorlagen-Seeding: %d Vorlage(n) angelegt: %s",
                 len(kopiert), ", ".join(kopiert))
    return kopiert
