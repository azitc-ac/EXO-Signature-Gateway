"""Signaturvorlagen: bearbeiten, ansehen, in Bausteine zerlegen.

Aus `app/webui/app.py` herausgelöst (21.08.2026). Reines Umsortieren — der
Inhalt ist unverändert; die Routen-Momentaufnahme in `tests/test_routes.py`
belegt, dass dieselbe Oberfläche herauskommt.

Warum gerade diese Gruppe zuerst: Sie ist thematisch geschlossen und die
einzige, die auch der **Signatur-Editor** benutzt (siehe `EDITOR_DARF` in
`tests/test_wachen.py`). Was hier steht, entscheidet mit darüber, was diese
Rolle kann — das gehört an einen Ort und nicht verteilt in eine
Dreitausend-Zeilen-Datei.

⚠️ Die Nachrichten an Postfachinhaber (`/api/usermails`) liegen bewusst hier:
Sie sind technisch dieselben Vorlagen und werden im selben Editor bearbeitet.
"""
from __future__ import annotations

import json as _json_mod
import re as _re
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import config
import settings_store
import signature_engine

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
)

router = APIRouter()


def _vorlagenverweise_umbenennen(alt: str, neu: str) -> dict:
    """Jeden gespeicherten Verweis auf eine Vorlage umschreiben.

    EINE Stelle, die alle Orte kennt — sonst vergisst die nächste Änderung
    einen davon, und ein Postfach zeigt still auf eine Vorlage, die es nicht
    mehr gibt. Der Signaturdienst fällt dann wortlos auf „default" zurück; der
    Betreiber merkt es erst an einer falschen Signatur.

    Orte (Stand 2026-08-03):
      MAILBOX_CONFIG[*]  -> "template", "min_template", "addin_templates" (Liste)
      TEMPLATE_POLICIES  -> "sig", "min", "addin"
      CUSTOM_POLICIES[*] -> "template"
    """
    geaendert: dict[str, int] = {}
    aenderungen: dict[str, object] = {}

    mc = settings_store.get("MAILBOX_CONFIG") or {}
    n_mc = 0
    for cfg in mc.values():
        if not isinstance(cfg, dict):
            continue
        for feld in ("template", "min_template"):
            if cfg.get(feld) == alt:
                cfg[feld] = neu
                n_mc += 1
        liste = cfg.get("addin_templates")
        if isinstance(liste, list) and alt in liste:
            cfg["addin_templates"] = [neu if x == alt else x for x in liste]
            n_mc += 1
    if n_mc:
        aenderungen["MAILBOX_CONFIG"] = mc
        geaendert["Postfächer"] = n_mc

    tp = settings_store.get("TEMPLATE_POLICIES") or {}
    n_tp = 0
    if isinstance(tp, dict):
        for feld in ("sig", "min", "addin"):
            if tp.get(feld) == alt:
                tp[feld] = neu
                n_tp += 1
    if n_tp:
        aenderungen["TEMPLATE_POLICIES"] = tp
        geaendert["Richtlinien"] = n_tp

    cp = settings_store.get("CUSTOM_POLICIES") or []
    n_cp = 0
    if isinstance(cp, list):
        for pol in cp:
            if isinstance(pol, dict) and pol.get("template") == alt:
                pol["template"] = neu
                n_cp += 1
    if n_cp:
        aenderungen["CUSTOM_POLICIES"] = cp
        geaendert["Eigene Richtlinien"] = n_cp

    if aenderungen:
        settings_store.update(aenderungen)
    return geaendert

def _usermail_liste() -> list[dict]:
    """Die bekannten Nachrichten an Postfachinhaber für die Auswahl im Editor."""
    import usermail
    return [{"key": k,
             "name": usermail.dateiname(k),
             "anzeige": v["anzeige"],
             "zweck": v["zweck"],
             "ist_standard": usermail.ist_standard(k)}
            for k, v in usermail.VORLAGEN.items()]

def _usermail_key(fname: str) -> str:
    """Schlüssel, falls die gerade geöffnete Vorlage eine Nutzer-Mail ist."""
    import usermail
    for k in usermail.VORLAGEN:
        if usermail.dateiname(k) == fname:
            return k
    return ""


@router.get("/api/templates")
async def api_get_templates(_=Depends(_check_auth)):
    """List available signature template names."""
    import signature_engine
    return {"templates": signature_engine.list_templates()}

@router.delete("/api/templates/{name}")
async def api_delete_template(name: str, _=Depends(_check_auth)):
    """Delete a named template (not 'default')."""
    if not name or name == "default":
        raise HTTPException(400, "Das 'default'-Template kann nicht gelöscht werden")
    html_path = Path(config.TEMPLATE_DIR) / f"{name}.html"
    txt_path = Path(config.TEMPLATE_DIR) / f"{name}.txt"
    meta_path = Path(config.TEMPLATE_DIR) / f"{name}.meta.json"
    deleted = []
    for p in (html_path, txt_path, meta_path):
        if p.exists():
            p.unlink()
            deleted.append(p.name)
    if not deleted:
        raise HTTPException(404, f"Template '{name}' nicht gefunden")
    import signature_engine
    signature_engine._reload_env()
    log.info("Template '%s' deleted", name)
    return {"ok": True, "deleted": deleted}

@router.post("/api/templates/{name}/rename")
async def api_rename_template(name: str, request: Request, _=Depends(_check_auth)):
    """Vorlage umbenennen — samt aller Verweise darauf.

    Ohne das Nachziehen der Verweise wäre Umbenennen gefährlicher als Löschen:
    Beim Löschen fällt der Fehler sofort auf, beim Umbenennen zeigt ein
    Postfach stillschweigend ins Leere.
    """
    import shutil
    daten = await request.json()
    ziel = _re.sub(r"[^a-zA-Z0-9_\-]", "", (daten.get("ziel") or "").strip()).strip("-_")
    if not ziel:
        raise HTTPException(400, "Ungültiger Name (Buchstaben, Ziffern, - und _).")
    if ziel == "default":
        raise HTTPException(400, "Der Name 'default' ist vergeben.")

    quelle_safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
    if quelle_safe == "default":
        raise HTTPException(400, "Die Standardvorlage lässt sich nicht umbenennen.")
    if quelle_safe == ziel:
        raise HTTPException(400, "Alter und neuer Name sind gleich.")

    verz = Path(config.TEMPLATE_DIR)
    if not (verz / f"{quelle_safe}.html").exists():
        raise HTTPException(404, f"Vorlage '{name}' nicht gefunden.")
    if (verz / f"{ziel}.html").exists():
        raise HTTPException(409, f"Es gibt bereits eine Vorlage '{ziel}'.")

    verschoben = []
    for endung in ("html", "txt", "meta.json"):
        q = verz / f"{quelle_safe}.{endung}"
        if q.exists():
            shutil.move(str(q), str(verz / f"{ziel}.{endung}"))
            verschoben.append(endung)

    verweise = _vorlagenverweise_umbenennen(quelle_safe, ziel)
    import signature_engine
    signature_engine._reload_env()
    log.info("Template '%s' -> '%s' umbenannt (Verweise: %s)", quelle_safe, ziel, verweise or "keine")

    teile = [f"{n} {was}" for was, n in verweise.items()]
    return JSONResponse({
        "ok": True, "name": ziel, "moved": verschoben, "verweise": verweise,
        "message": (f"Umbenannt in '{ziel}'."
                    + (f" Nachgezogen: {', '.join(teile)}." if teile else "")),
    })

@router.post("/api/templates/{name}/create")
async def api_create_template(name: str, _=Depends(_check_auth)):
    """Leere Vorlage anlegen, damit sie sofort in der Auswahl steht.

    Bisher fuehrte „+ Neue Vorlage" nur auf die Bearbeitungsseite; auf der
    Platte entstand nichts. Die Vorlage tauchte deshalb erst nach dem ersten
    Speichern in der Liste auf — wer zwischendurch wegnavigierte, fand seine
    Arbeit nicht wieder und legte sie ein zweites Mal an.
    """
    safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_")
    if not safe:
        raise HTTPException(400, "Ungültiger Name (Buchstaben, Ziffern, - und _).")
    if safe == "default":
        raise HTTPException(400, "Der Name 'default' ist vergeben.")
    verz = Path(config.TEMPLATE_DIR)
    if (verz / f"{safe}.html").exists():
        raise HTTPException(409, f"Es gibt bereits eine Vorlage '{safe}'.")

    (verz / f"{safe}.html").write_text("", encoding="utf-8")
    (verz / f"{safe}.txt").write_text("", encoding="utf-8")
    import signature_engine
    signature_engine._reload_env()
    log.info("Template '%s' angelegt (leer) von %s", safe, _)
    return JSONResponse({"ok": True, "name": safe})

@router.post("/api/templates/{name}/duplicate")
async def api_duplicate_template(name: str, request: Request, _=Depends(_check_auth)):
    """Vorlage kopieren — samt Blockliste, damit die Kopie im Baukasten bleibt.

    WARUM ES DAS BRAUCHT
    Den HTML-Quelltext einer Baukasten-Vorlage in eine neue zu kopieren ergibt
    eine Vorlage OHNE `.meta.json`. Sie funktioniert, lässt sich aber nur noch
    als Quelltext bearbeiten: das HTML ist das Erzeugnis, die Blockliste ist die
    Quelle. Eine Rückübersetzung gibt es nicht und wäre auch nicht verlässlich —
    aus fertigem HTML ließe sich nicht ablesen, welche Blöcke es einmal waren.

    Deshalb kopiert dieser Weg alle drei Dateien. Fehlt der Quelle die
    `.meta.json` (von Hand geschriebene Vorlage), wird das ausdrücklich
    gemeldet, statt stillschweigend eine nicht mehr baukastenfähige Kopie zu
    hinterlassen.
    """
    import shutil
    daten = await request.json()
    ziel_roh = (daten.get("ziel") or "").strip()
    ziel = _re.sub(r"[^a-zA-Z0-9_\-]", "", ziel_roh).strip("-_")
    if not ziel:
        raise HTTPException(400, "Bitte einen Namen für die Kopie angeben "
                                 "(Buchstaben, Ziffern, - und _).")
    if ziel == "default":
        raise HTTPException(400, "Der Name 'default' ist vergeben.")

    quelle_safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
    quelle = "signature" if quelle_safe == "default" else quelle_safe
    verz = Path(config.TEMPLATE_DIR)
    if not (verz / f"{quelle}.html").exists():
        raise HTTPException(404, f"Vorlage '{name}' nicht gefunden.")
    if (verz / f"{ziel}.html").exists():
        raise HTTPException(409, f"Es gibt bereits eine Vorlage '{ziel}'.")

    kopiert = []
    for endung in ("html", "txt", "meta.json"):
        q = verz / f"{quelle}.{endung}"
        if q.exists():
            shutil.copyfile(q, verz / f"{ziel}.{endung}")
            kopiert.append(endung)

    import signature_engine
    signature_engine._reload_env()
    baukasten = "meta.json" in kopiert
    log.info("Template '%s' dupliziert nach '%s' (Baukasten: %s)",
             quelle, ziel, baukasten)
    return JSONResponse({
        "ok": True, "name": ziel, "builder": baukasten, "copied": kopiert,
        "message": (f"Kopie '{ziel}' angelegt — im Baukasten bearbeitbar."
                    if baukasten else
                    f"Kopie '{ziel}' angelegt. Die Vorlage hat keine Baukasten-Daten "
                    f"und lässt sich nur als Quelltext bearbeiten."),
    })

@router.post("/api/templates/{name}/parse")
async def api_parse_template(name: str, request: Request, _=Depends(_check_auth)):
    """HTML in eine Blockliste zurücklesen — als VORSCHLAG, nichts wird gespeichert.

    Der Editor ruft das beim Wechsel auf den Baukasten, wenn eine Vorlage nur
    als Quelltext vorliegt. Erst ein anschliessendes Speichern macht die
    Umwandlung verbindlich; bis dahin bleibt die Vorlage unangetastet. So kann
    der Nutzer das Ergebnis in der Vorschau vergleichen und ablehnen.
    """
    import template_parser as _tp
    try:
        daten = await request.json()
    except Exception:
        daten = {}
    html_roh = daten.get("html")
    if html_roh is None:
        safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
        fname = "signature" if safe == "default" else safe
        pfad = Path(config.TEMPLATE_DIR) / f"{fname}.html"
        if not pfad.exists():
            raise HTTPException(404, f"Vorlage '{name}' nicht gefunden.")
        html_roh = pfad.read_text(encoding="utf-8")

    meta = _tp.parse_html(html_roh)
    hinweise = meta.pop("_hinweise", [])
    import template_builder as _tb
    # Die Vorschau kommt aus dem Vorschlag selbst, nicht aus der Quelle: nur so
    # sieht der Nutzer, was NACH dem Speichern herauskaeme.
    return JSONResponse({"ok": True, "meta": meta, "hinweise": hinweise,
                         "html": _tb.render_html(meta),
                         "blocks": len(meta.get("blocks") or [])})

@router.get("/api/templates/{name}/meta")
async def api_get_template_meta(name: str, _=Depends(_check_auth)):
    """Return the builder meta JSON for a template, or 404 if none exists."""
    safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
    fname = "signature" if safe == "default" else safe
    meta_path = Path(config.TEMPLATE_DIR) / f"{fname}.meta.json"
    if not meta_path.exists():
        # ⚠️ Für Nachrichten an Postfachinhaber ist „keine Datei" kein Fehler,
        # sondern der Normalfall: Solange niemand sie bearbeitet hat, gilt die
        # mitgelieferte Fassung. Ohne diesen Zweig zeigte der Editor eine LEERE
        # Vorlage — und Speichern hätte den Text gelöscht, den der Empfänger
        # braucht, um die CA-Mail von Phishing zu unterscheiden.
        schluessel = _usermail_key(fname)
        if schluessel:
            import usermail
            return JSONResponse(usermail.standard_meta(schluessel))
        raise HTTPException(404, "Kein Builder-Meta für diese Vorlage")
    import json as _json
    return JSONResponse(_json.loads(meta_path.read_text()))

@router.post("/api/templates/{name}/meta")
async def api_save_template_meta(name: str, request: Request, _=Depends(_check_auth)):
    """Save builder meta JSON, regenerate .html and .txt from it."""
    import json as _json
    import template_builder as _tb
    import signature_engine
    safe = _re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("-_") or "default"
    fname = "signature" if safe == "default" else safe
    try:
        meta = await request.json()
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")
    if not isinstance(meta, dict) or "blocks" not in meta:
        raise HTTPException(400, "Meta-JSON muss 'blocks' enthalten")
    meta.setdefault("version", 1)
    if not (meta.get("blocks") or []):
        # Eine leere Bausteinliste ergibt eine leere Vorlage — und jede damit
        # versandte Mail traegt gar keine Signatur mehr. Am 02.08.2026 ist
        # genau das passiert: Die Ruecklesung lieferte nichts, gespeichert
        # wurde trotzdem, und die Vorlage war weg.
        raise HTTPException(400, "Die Vorlage enthält keine Bausteine. "
                                 "Speichern würde die Signatur löschen.")

    # Ein Fehler beim Erzeugen darf NIE als nackter Serverfehler herauskommen:
    # Der Editor bekommt dann HTML statt JSON und meldet „Speichern
    # fehlgeschlagen: … is not valid JSON" — eine Meldung, aus der niemand auf
    # sein Eingabefeld schliessen kann. Genau so gemeldet am 06.08.2026, nachdem
    # in ein px-Feld „12pt" getippt worden war.
    try:
        html_content = _tb.render_html(meta)
        txt_content = _tb.render_txt(meta)
    except Exception as exc:
        log.error("Template '%s' liess sich nicht erzeugen: %s", safe, exc, exc_info=True)
        raise HTTPException(400, f"Die Vorlage liess sich nicht erzeugen: {exc}. "
                                 f"Bitte die Eingaben prüfen — nicht gespeichert.")

    # Erzeugt der Baukasten ein UNGUELTIGES Template, waere die Signatur beim
    # Versand leer — sichtbar wird das erst beim Empfaenger. Deshalb hier
    # pruefen und die Datei gar nicht erst schreiben.
    try:
        import jinja2
        jinja2.Environment().parse(html_content)
        jinja2.Environment().parse(txt_content)
    except Exception as exc:
        log.error("Template '%s' waere unbrauchbar geworden: %s", safe, exc)
        raise HTTPException(400, f"Die erzeugte Vorlage ist kein gültiges "
                                 f"Template ({exc}). Nicht gespeichert — die "
                                 f"bisherige Fassung bleibt erhalten.")

    # Sicherung der bisherigen Fassung, bevor sie ueberschrieben wird.
    alt = Path(config.TEMPLATE_DIR) / f"{fname}.html"
    if alt.exists():
        try:
            (Path(config.TEMPLATE_DIR) / f"{fname}.html.bak").write_text(
                alt.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as exc:
            log.warning("Sicherung von %s fehlgeschlagen: %s", fname, exc)
    # Entweder alle drei Dateien oder keine — und ueber tmp+replace().
    #
    # Vorher liefen hier drei nackte write_text() hintereinander. Zwei Fehler
    # steckten darin, beide am 07.08.2026 auf der Azure-VM aufgeschlagen:
    #
    # 1. Ein Fehlschlag beim zweiten Aufruf hinterliess die neue meta.json neben
    #    dem alten HTML. Baukasten und ausgelieferte Signatur waren dann
    #    verschieden, ohne dass es irgendwo auffiel.
    # 2. `write_text()` oeffnet die ZIELDATEI. Gehoert die einem anderen Nutzer
    #    (auf der VM schrieb der als root laufende Deploy die Vorlagen), scheitert
    #    das mit EACCES — obwohl das Verzeichnis dem Dienst gehoert. `replace()`
    #    braucht nur Schreibrecht am VERZEICHNIS und kommt damit durch.
    #
    # Der Fehler kam als nackter 500 heraus; der Editor bekam HTML statt JSON und
    # meldete „Unexpected token 'I', "Internal S"... is not valid JSON". Aus so
    # einer Meldung ist die Ursache nicht zu erraten.
    ziele = [
        (Path(config.TEMPLATE_DIR) / f"{fname}.meta.json",
         _json.dumps(meta, ensure_ascii=False, indent=2)),
        (Path(config.TEMPLATE_DIR) / f"{fname}.html", html_content),
        (Path(config.TEMPLATE_DIR) / f"{fname}.txt", txt_content),
    ]
    fertig: list[tuple[Path, Path]] = []
    try:
        for ziel, inhalt in ziele:
            tmp = ziel.parent / f"{ziel.name}.tmp"
            tmp.write_text(inhalt, encoding="utf-8")
            # Rechte auf der TEMP-Datei setzen, nicht auf dem Ziel: replace()
            # uebernimmt die der Quelldatei (dieselbe Falle wie in
            # settings_store._save(), dort mit 600 statt 644).
            tmp.chmod(0o644)
            fertig.append((tmp, ziel))
        for tmp, ziel in fertig:
            tmp.replace(ziel)
    except OSError as exc:
        for tmp, _ziel in fertig:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        log.error("Vorlage '%s' liess sich nicht schreiben: %s", safe, exc, exc_info=True)
        raise HTTPException(500, f"Die Vorlage liess sich nicht schreiben: {exc}. "
                                 f"Nicht gespeichert — die bisherige Fassung bleibt "
                                 f"erhalten. Meist sind es die Zugriffsrechte auf "
                                 f"dem Vorlagenverzeichnis.")
    signature_engine._reload_env()
    log.info("Template '%s' saved via builder by %s", safe, _)
    return {"ok": True, "html": html_content, "txt": txt_content}

@router.post("/api/usermails/{schluessel}/standard")
async def api_usermail_standard(schluessel: str, _=Depends(_check_auth)):
    """Die mitgelieferte Fassung wiederherstellen.

    Sie wird geschrieben wie eine bearbeitete Vorlage — dieselbe Datenstruktur,
    derselbe Weg. Deshalb ist die wiederhergestellte Fassung anschliessend ganz
    normal weiter bearbeitbar und nicht etwa schreibgeschützt.
    """
    import usermail
    import template_builder as _tb
    if not usermail.ist_bekannt(schluessel):
        raise HTTPException(404, "Unbekannte Nachricht")
    meta = usermail.standard_meta(schluessel)
    fname = usermail.dateiname(schluessel)
    verz = Path(config.TEMPLATE_DIR)
    (verz / f"{fname}.meta.json").write_text(
        _json_mod.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (verz / f"{fname}.html").write_text(_tb.render_html(meta), encoding="utf-8")
    (verz / f"{fname}.txt").write_text(_tb.render_txt(meta), encoding="utf-8")
    log.info("Nutzer-Mail %s auf die mitgelieferte Fassung zurückgesetzt", schluessel)
    return JSONResponse({"ok": True})

@router.get("/api/usermails")
async def api_usermails(_=Depends(_check_auth)):
    return JSONResponse({"usermails": _usermail_liste()})

@router.get("/template", response_class=HTMLResponse)
async def template_editor(request: Request, user: str = Depends(_check_auth)):
    import signature_engine as _sig_engine
    name = request.query_params.get("name") or "default"
    fname = "signature" if name == "default" else name
    html_path = Path(config.TEMPLATE_DIR) / f"{fname}.html"
    txt_path = Path(config.TEMPLATE_DIR) / f"{fname}.txt"
    meta_path = Path(config.TEMPLATE_DIR) / f"{fname}.meta.json"
    template_list = _sig_engine.list_templates()
    custom_vars = [cv["name"] for cv in (settings_store.get("CUSTOM_TEMPLATE_VARS") or []) if cv.get("name")]
    return templates.TemplateResponse(
        request=request, name="template_editor.html",
        context={
            "html_content": html_path.read_text() if html_path.exists() else "",
            "txt_content": txt_path.read_text() if txt_path.exists() else "",
            # Für Nachrichten an Postfachinhaber IMMER wahr: Ohne eigene
            # Datei liefert der Meta-Endpunkt die mitgelieferte Fassung, und
            # der Editor soll sie laden statt leer zu bleiben.
            "has_meta": meta_path.exists() or bool(_usermail_key(fname)),
            # Wurde der Quelltext NACH dem letzten Baukasten-Speichern
            # geaendert? Dann sind die Bausteine veraltet, und der Editor bietet
            # an, sie aus dem Quelltext neu zu lesen.
            #
            # Genau dafuer wurde die Ruecklesung gebaut: Handaenderungen am
            # HTML sollen in den Baukasten UEBERNOMMEN werden. Ohne diese
            # Pruefung griff sie nur bei Vorlagen ohne Bausteine — wer eine
            # Baukasten-Vorlage von Hand nachbesserte, sah beim naechsten
            # Oeffnen die alten Bausteine und verlor seine Arbeit beim
            # Speichern.
            "quelltext_neuer": (
                meta_path.exists() and html_path.exists()
                and html_path.stat().st_mtime > meta_path.stat().st_mtime + 1
            ),
            "active": "template",
            "saved": request.query_params.get("saved"),
            "current_template": name,
            "template_list": template_list,
            "custom_vars": custom_vars,
            "gateway_name": _gateway_name(),
            # Nachrichten an Postfachinhaber. Sie liegen im selben Verzeichnis
            # und werden im selben Baukasten bearbeitet, stehen aber in einer
            # EIGENEN Auswahl — in der Signaturliste hätten sie nichts zu
            # suchen, dort wäre eine Zuweisung ein Klick.
            "usermails": _usermail_liste(),
            "usermail_key": _usermail_key(fname),
        },
    )

@router.post("/template", response_class=HTMLResponse)
async def template_save(
    request: Request,
    html_content: str = Form(""),
    txt_content: str = Form(""),
    template_name: str = Form("default"),
    user: str = Depends(_check_auth),
):
    # Sanitise template_name: only allow alphanumeric, dash, underscore
    import re as _re2
    safe_name = _re2.sub(r"[^a-zA-Z0-9_\-]", "", template_name).strip("-_") or "default"
    if safe_name == "default":
        html_path = Path(config.TEMPLATE_DIR, "signature.html")
        txt_path = Path(config.TEMPLATE_DIR, "signature.txt")
    else:
        html_path = Path(config.TEMPLATE_DIR, f"{safe_name}.html")
        txt_path = Path(config.TEMPLATE_DIR, f"{safe_name}.txt")
    html_path.write_text(html_content)
    txt_path.write_text(txt_content)
    signature_engine._reload_env()
    log.info("Template '%s' saved by user %s", safe_name, user)
    return RedirectResponse(url=f"/template?name={safe_name}&saved=1", status_code=303)

@router.get("/preview", response_class=HTMLResponse)
async def preview(request: Request, email: str = "", user: str = Depends(_check_auth)):
    return templates.TemplateResponse(
        request=request, name="preview.html",
        context={"email": email, "active": "preview", "gateway_name": _gateway_name()},
    )

@router.get("/api/preview-data")
async def api_preview_data(
    email: str = "",
    template: str = "default",
    banner: str = "",
    disclaimer: str = "",
    explizit: bool = False,
    user: str = Depends(_check_auth),
):
    """Render a signature template for a given email address (Graph lookup).
    Also renders the configured banner and disclaimer (or explicit params) and
    returns them as `banner_html` / `disclaimer_html`.

    `explizit=1` bedeutet: Die drei Vorlagennamen sind VERBINDLICH, ein leerer
    Wert heisst „keine" und nicht „nimm die aus der Postfach-Konfiguration".

    Ohne dieses Kennzeichen liesse sich „ausdruecklich keiner" gar nicht
    ausdruecken — ein leerer Banner faellt sonst auf die Konfiguration zurueck.
    Genau das braucht aber die Vorschau-Seite, auf der Signatur, Banner und
    Disclaimer frei zusammengestellt werden. Die Live-Vorschau im Baukasten
    schickt das Kennzeichen NICHT: dort soll stehen, was das Postfach
    tatsaechlich bekaeme."""
    import graph_client as _gc
    import mailbox_match

    # ⚠️ Nachrichten an Postfachinhaber gehen einen ANDEREN Weg als Signaturen:
    # Sie kennen weder `user` noch `custom`, sondern `empfaenger` und `ca`.
    # Ohne diesen Zweig rendert die Vorschau sie mit dem Signatur-Kontext —
    # die Platzhalter sind dort unbekannt und werden LEER eingesetzt. Im Editor
    # stand dann „Für Ihre Adresse  wird ein Zertifikat…", und es sah aus, als
    # sei die Vorlage kaputt.
    schluessel = _usermail_key(template or "")
    if schluessel:
        import usermail
        ergebnis = usermail.rendern(
            schluessel,
            email or "vorname.nachname@example.org",
            (settings_store.get("CA_ANZEIGENAME") or "").strip() or "Ihrer Zertifizierungsstelle")
        betreff, rumpf = ergebnis if ergebnis else ("", "")
        return JSONResponse({"html": rumpf, "txt": "", "betreff": betreff,
                             "banner_html": "", "disclaimer_html": "", "error": None})

    user_data = _gc.UserData()
    error = None
    if email:
        try:
            user_data = await _gc.get_user(email)
        except Exception as exc:
            error = str(exc)
    if explizit and not template:
        sig_html, sig_txt = "", ""
    else:
        sig_html, sig_txt = signature_engine.render(user_data, template_name=template)
    # Resolve banner and disclaimer: explicit param > mailbox config
    if not explizit and email and (not banner or not disclaimer):
        _mc = settings_store.get("MAILBOX_CONFIG") or {}
        _cfg = mailbox_match.match_sender(_mc, email)
        if not banner:
            banner = _cfg.get("banner_template", "")
        if not disclaimer:
            disclaimer = _cfg.get("disclaimer_template", "")
    banner_html = ""
    if banner:
        banner_html, _ = signature_engine.render(user_data, template_name=banner)
    disclaimer_html = ""
    if disclaimer:
        disclaimer_html, _ = signature_engine.render(user_data, template_name=disclaimer)
    return JSONResponse({"html": sig_html, "txt": sig_txt, "error": error,
                         "banner_html": banner_html, "banner_template": banner,
                         "disclaimer_html": disclaimer_html, "disclaimer_template": disclaimer})
