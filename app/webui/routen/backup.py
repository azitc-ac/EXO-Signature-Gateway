"""Routen der Sicherung — Herunterladen, Ansehen, Wiederherstellen.

Viertes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

Nach S/MIME der einfache Fall: vier Endpunkte, ein zusammenhängender Block,
keine geteilten Hilfsfunktionen. Die eigentliche Arbeit liegt ohnehin nicht
hier, sondern in `backup_manager` — diese Routen nehmen die Datei entgegen,
prüfen die Auswahl und reichen weiter.

⚠️ ALLE VIER HÄNGEN AN `_require_admin`, und das ist hier keine Formalie:
`/api/backup/download` gibt den gesamten Datenbestand heraus — Einstellungen
mit dem Anmeldegeheimnis, private Schlüssel, Postfachkonfiguration.
`/api/backup/restore` schreibt ihn zurück. Eine Route, die beim Verschieben
ihre Wache verliert, öffnet also beides.

Geprüft wird das von `tests/test_wachen.py`, und zwar für JEDE Route der
Anwendung. Diese Datei war der Anlass dafür: Beim Herauslösen liess sich
`Depends(_require_admin)` von `/api/backup/download` entfernen, ohne dass einer
der damals 500 Tests etwas merkte — `tests/test_seiten.py` prüft die Anmeldung
nur an zwei Stichproben, und die trafen ausgerechnet die angefasste Route nicht.

Die Arbeitsteilung zwischen `inspect` und `restore` ist Absicht und soll so
bleiben: Ein Aufruf, der nichts verändert, heisst nicht wie einer, der es tut.
"""
from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse

import config
import settings_store

from webui.deps import templates, _gateway_name, _require_admin

router = APIRouter()


@router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="backup.html",
        # `s` braucht diese Seite selbst nicht — wohl aber die gemeinsame
        # Reiterleiste (`_nav_einstellungen.html`), die daran abliest, ob der
        # Relay-Reiter zu zeigen ist. Fehlte der Wert, verschwände der Reiter
        # ausgerechnet hier, und die Seite sähe wie eine Sackgasse aus.
        context={"active": "backup", "gateway_name": _gateway_name(),
                 "s": settings_store.public_view(),
                 "version": config.VERSION},
    )


@router.get("/api/backup/download")
async def api_backup_download(user: str = Depends(_require_admin)):
    """Vollständiges Backup als ZIP herunterladen."""
    import backup_manager as _bm
    import asyncio as _aio
    from fastapi.responses import Response as _Resp
    zip_bytes, filename = await _aio.get_event_loop().run_in_executor(
        None, _bm.create_backup
    )
    return _Resp(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/backup/inspect")
async def api_backup_inspect(
    file: UploadFile = File(...),
    _user: str = Depends(_require_admin),
):
    """Inhalt eines Backups anzeigen, OHNE etwas zu schreiben.

    Grundlage für die Auswahl beim Wiederherstellen. Bewusst ein eigener
    Endpunkt statt eines Vorschau-Schalters am Wiederherstellen: Ein Aufruf,
    der nichts verändert, soll auch nicht so heissen wie einer, der es tut.
    """
    import backup_manager as _bm
    data = await file.read()
    ergebnis = await asyncio.get_event_loop().run_in_executor(
        None, _bm.inspect_backup, data
    )
    return JSONResponse(ergebnis)


@router.post("/api/backup/restore")
async def api_backup_restore(
    file: UploadFile = File(...),
    auswahl: str = Form(""),
    _user: str = Depends(_require_admin),
):
    """Backup-ZIP hochladen und wiederherstellen.

    `auswahl` ist eine JSON-Liste von Dateinamen aus dem ZIP. Fehlt sie, wird
    ALLES wiederhergestellt — so verhält sich der Endpunkt wie vor der
    Auswahlmöglichkeit, und ein alter Aufrufer (oder die Ersteinrichtung)
    stellt nicht versehentlich nichts wieder her.
    """
    import backup_manager as _bm
    import json as _json
    data = await file.read()

    gewaehlt = None
    if auswahl.strip():
        try:
            gewaehlt = _json.loads(auswahl)
        except Exception:
            raise HTTPException(400, "Auswahl ist kein gültiges JSON")
        if not isinstance(gewaehlt, list):
            raise HTTPException(400, "Auswahl muss eine Liste von Dateinamen sein")

    result = await asyncio.get_event_loop().run_in_executor(
        None, _bm.restore_backup, data, gewaehlt
    )
    return JSONResponse(result)
