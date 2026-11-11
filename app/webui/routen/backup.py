"""Routen der Sicherung — Herunterladen, Ansehen, Wiederherstellen.

Viertes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

Nach S/MIME der einfache Fall: vier Endpunkte, ein zusammenhängender Block,
keine geteilten Hilfsfunktionen. Die eigentliche Arbeit liegt ohnehin nicht
hier, sondern in `backup_manager` — diese Routen nehmen die Datei entgegen,
prüfen die Auswahl und reichen weiter.

⚠️ ALLE HÄNGEN AN `_require_admin`, und das ist hier keine Formalie:
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


# Verschlüsselte Backups: Das Passwort gehört nicht in eine URL (Protokolle,
# Verlauf). Deshalb erst POST mit Passwort → fertige Datei liegt kurz unter
# einer Einmal-Kennung, dann GET mit dieser Kennung (der gemeinsame
# Download-Baustein `dateiHerunterladen` kann nur GET).
_ABRUFE: dict[str, tuple[float, bytes, str]] = {}
_ABRUF_TTL_S = 300


@router.post("/api/backup/erstellen")
async def api_backup_erstellen(request: Request, user: str = Depends(_require_admin)):
    """Verschlüsseltes Backup erstellen; liefert eine Einmal-Kennung zum Abholen."""
    import secrets
    import time
    import backup_manager as _bm
    try:
        daten = await request.json()
    except Exception:
        daten = {}
    passwort = str(daten.get("passwort") or "")
    if len(passwort) < _bm.MIN_PASSWORT:
        raise HTTPException(400, f"Passwort zu kurz (mindestens {_bm.MIN_PASSWORT} Zeichen).")
    inhalt, filename = await asyncio.get_event_loop().run_in_executor(
        None, _bm.create_backup, passwort)
    jetzt = time.monotonic()
    for k in [k for k, (t, _, _) in _ABRUFE.items() if jetzt - t > _ABRUF_TTL_S]:
        _ABRUFE.pop(k, None)
    abruf = secrets.token_urlsafe(24)
    _ABRUFE[abruf] = (jetzt, inhalt, filename)
    return JSONResponse({"ok": True, "abruf": abruf, "name": filename})


@router.get("/api/backup/download")
async def api_backup_download(abruf: str = "", user: str = Depends(_require_admin)):
    """Backup herunterladen — ohne `abruf` ein neues, unverschlüsseltes ZIP;
    mit `abruf` die zuvor verschlüsselt erstellte Datei (genau einmal)."""
    import time
    import backup_manager as _bm
    from fastapi.responses import Response as _Resp
    if abruf:
        eintrag = _ABRUFE.pop(abruf, None)
        if eintrag is None or time.monotonic() - eintrag[0] > _ABRUF_TTL_S:
            raise HTTPException(404, "Abruf abgelaufen — Backup bitte neu erstellen.")
        _, inhalt, filename = eintrag
        art = "application/octet-stream"
    else:
        inhalt, filename = await asyncio.get_event_loop().run_in_executor(
            None, _bm.create_backup)
        art = "application/zip"
    return _Resp(
        content=inhalt,
        media_type=art,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _oeffnen(data: bytes, passwort: str) -> bytes | dict:
    """Hochgeladene Datei entschlüsseln — oder die Fehlerantwort liefern."""
    import backup_manager as _bm
    try:
        return _bm.oeffnen(data, passwort)
    except _bm.PasswortFehler as exc:
        return {"ok": False, "error": str(exc), "passwort_noetig": True,
                "gruppen": [], "restored_files": 0, "warnings": []}


@router.post("/api/backup/inspect")
async def api_backup_inspect(
    file: UploadFile = File(...),
    passwort: str = Form(""),
    _user: str = Depends(_require_admin),
):
    """Inhalt eines Backups anzeigen, OHNE etwas zu schreiben.

    Grundlage für die Auswahl beim Wiederherstellen. Bewusst ein eigener
    Endpunkt statt eines Vorschau-Schalters am Wiederherstellen: Ein Aufruf,
    der nichts verändert, soll auch nicht so heissen wie einer, der es tut.
    """
    import backup_manager as _bm
    data = await asyncio.get_event_loop().run_in_executor(
        None, _oeffnen, await file.read(), passwort)
    if isinstance(data, dict):
        return JSONResponse(data)
    ergebnis = await asyncio.get_event_loop().run_in_executor(
        None, _bm.inspect_backup, data
    )
    return JSONResponse(ergebnis)


@router.post("/api/backup/restore")
async def api_backup_restore(
    file: UploadFile = File(...),
    auswahl: str = Form(""),
    passwort: str = Form(""),
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
    data = await asyncio.get_event_loop().run_in_executor(
        None, _oeffnen, await file.read(), passwort)
    if isinstance(data, dict):
        return JSONResponse(data)

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
