import os
import logging
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

import config
from graph_client import UserData

log = logging.getLogger(__name__)

# ── Vorlagen-Arten (kind) ─────────────────────────────────────────────────────
# Jede Vorlage hat GENAU EINE Art. `signatur` ist die Vorgabe und zugleich der
# Rückfall für Dateien ohne Meta (siehe vorlagen_art()). Die Zuweisungslisten der
# Oberfläche zeigen je Verwendungszweck nur die passende Art — so lässt sich ein
# Banner nicht als Signatur zuweisen und umgekehrt.
ARTEN: tuple[str, ...] = ("signatur", "banner", "disclaimer", "oof", "usermail")

# Zuordnung Zuweisungs-Slot → Vorlagen-Art. Ein Slot ist die ROLLE, in der eine
# Vorlage verwendet wird. `min` (Antwort-Minimalsignatur) und `addin`
# (Add-in-Auswahl) sind Signaturen im engeren Sinn und ziehen deshalb aus der Art
# `signatur`; nur `banner`/`disclaimer` haben eigene Arten. Grundlage sowohl für
# die typgefilterten Dropdowns als auch für die Auto-Typisierung des Bestands.
SLOT_ART: dict[str, str] = {
    "sig": "signatur",
    "min": "signatur",
    "addin": "signatur",
    "banner": "banner",
    "disclaimer": "disclaimer",
    "oof": "oof",          # zentrale Abwesenheitsnotiz (Out-of-Office)
}


def ist_bekannte_art(art: str) -> bool:
    """Ist `art` eine gültige Vorlagen-Art (für die Validierung beim Speichern)?"""
    return art in ARTEN


_env: Environment | None = None


def _get_env() -> Environment:
    """Vorlagen laufen in der Sandbox.

    Eine Signaturvorlage ist Jinja2-Quelltext, der aus der Oberfläche stammt:
    der Freitext-Block reicht HTML absichtlich roh durch, und Größen-, Farb-
    und URL-Angaben landen unverändert im erzeugten Quelltext. Alles davon
    wird beim Rendern ausgewertet — in einer gewöhnlichen Umgebung genügt
    damit ein Ausdruck wie `{{ x.__class__.__mro__[1].__subclasses__() }}`,
    um an Python-Interna und darüber an die Zugangsdaten des Containers zu
    gelangen. Vorlagen darf auch die Editor-Rolle speichern.

    Die Sandbox unterbindet genau diesen Zugriff (SecurityError), lässt
    `{{ user.x }}`, Filter und `{% if %}` aber unverändert. Gegengeprüft:
    alle mitgelieferten Vorlagen rendern zeichengleich wie zuvor.
    """
    global _env
    if _env is None:
        _env = SandboxedEnvironment(
            loader=FileSystemLoader(config.TEMPLATE_DIR),
            autoescape=select_autoescape(["html"]),
        )
    return _env


def _reload_env() -> Environment:
    global _env
    _env = None
    return _get_env()


def _resolve_template_names(template_name: str | None) -> tuple[str, str]:
    """Return (html_filename, txt_filename) for the given template name."""
    if not template_name or template_name == "default":
        return "signature.html", "signature.txt"
    return f"{template_name}.html", f"{template_name}.txt"


def render(user: UserData, template_name: str | None = None) -> tuple[str, str]:
    env = _get_env()
    ctx = {
        "user": user,
        "custom": user.custom,
    }

    html_file, txt_file = _resolve_template_names(template_name)

    # HTML template — fall back to signature.html if named template not found
    try:
        html = env.get_template(html_file).render(**ctx)
    except TemplateNotFound:
        if html_file != "signature.html":
            log.warning("Template %s not found, falling back to signature.html", html_file)
            try:
                html = env.get_template("signature.html").render(**ctx)
            except TemplateNotFound:
                html = ""
            except Exception as exc:
                log.error("Error rendering HTML signature fallback: %s", exc)
                html = ""
        else:
            log.warning("signature.html not found, using empty HTML signature")
            html = ""
    except Exception as exc:
        log.error("Error rendering HTML signature: %s", exc)
        html = ""

    # Plaintext template — fall back to signature.txt if named template not found
    try:
        txt = env.get_template(txt_file).render(**ctx)
    except TemplateNotFound:
        if txt_file != "signature.txt":
            log.warning("Template %s not found, falling back to signature.txt", txt_file)
            try:
                txt = env.get_template("signature.txt").render(**ctx)
            except TemplateNotFound:
                txt = ""
            except Exception as exc:
                log.error("Error rendering plaintext signature fallback: %s", exc)
                txt = ""
        else:
            log.warning("signature.txt not found, using empty plaintext signature")
            txt = ""
    except Exception as exc:
        log.error("Error rendering plaintext signature: %s", exc)
        txt = ""

    return html, txt


def vorlagen_art(name: str) -> str:
    """Art einer Vorlage: eine aus `ARTEN` (`signatur`/`banner`/`disclaimer`/`usermail`).

    Massgeblich ist `kind` in der Meta-Datei, nicht der Dateiname. Vorlagen ohne
    Meta — von Hand abgelegte HTML-Dateien — gelten als Signatur; so waren sie
    immer gemeint, bevor es die Unterscheidung gab. Ein unbekanntes `kind` fällt
    ebenfalls auf `signatur` zurück (defensiv: lieber sichtbar in der
    Signaturliste als in gar keiner).
    """
    import json
    import os
    pfad = os.path.join(config.TEMPLATE_DIR, f"{name}.meta.json")
    try:
        with open(pfad, encoding="utf-8") as f:
            art = json.load(f).get("kind") or "signatur"
        return art if art in ARTEN else "signatur"
    except Exception:
        return "signatur"


def textfassung_fehlt(name: str) -> bool:
    """Gibt es zu dieser Vorlage KEINE Nur-Text-Fassung?

    ANLASS (24.08.2026): Auf der Produktions-VM lag `Blog-Banner-Orange.html`
    ohne zugehörige `.txt`. `render()` fällt dann auf `signature.txt` zurück und
    protokolliert eine Warnung — im Nur-Text-Teil trug die Nachricht also die
    Standardsignatur statt der zugewiesenen, und gesehen hat das niemand: Der
    Bearbeiter sieht im Editor nur ein leeres Feld, und ein leeres Feld sieht
    aus wie eine Entscheidung.

    ⚠️ Das ist KEIN Fehler, sondern eine Auskunft. Wer die Textfassung bewusst
    weglässt, bekommt bewusst den Rückfall — er soll ihn nur kennen.
    """
    import os
    datei = "signature" if name in ("", "default") else name
    return not os.path.exists(os.path.join(config.TEMPLATE_DIR, f"{datei}.txt"))


def list_templates(art: str = "signatur") -> list[str]:
    """Vorlagennamen einer Art, alphabetisch (Signaturen immer mit 'default').

    ⚠️ Die Vorgabe ist `"signatur"`, und das ist wichtig: Alle bestehenden
    Aufrufer sind Zuweisungslisten für Postfächer und Richtlinien. Wären dort
    Nachrichten an die Belegschaft mit aufgeführt, liesse sich eine als Signatur
    zuweisen — ein Klick, und jede ausgehende Mail trüge den Text „Bitte
    bestätigen Sie Ihr Zertifikat".
    """
    import os
    names: set[str] = set()
    try:
        for fname in os.listdir(config.TEMPLATE_DIR):
            if fname.endswith(".html") and fname != "signature.html":
                name = fname[:-5]
                if vorlagen_art(name) == art:
                    names.add(name)
    except OSError:
        pass
    if art != "signatur":
        return sorted(names, key=lambda n: (n.lower(), n))
    names.add("default")
    # Durchgehend alphabetisch, ohne Ruecksicht auf Gross-/Kleinschreibung.
    #
    # `sorted()` allein ordnet nach Zeichenwerten, dort stehen alle
    # Grossbuchstaben vor allen kleinen: "Minimal" kam vor
    # "default-without-greeting", und man sucht seine Vorlage an zwei Stellen.
    #
    # "default" wird MITSORTIERT, nicht vorangestellt. Vorangestellt standen
    # "default" und "default-without-greeting" durch mehrere fremde Namen
    # getrennt — zwei offensichtlich zusammengehoerige Eintraege an
    # unzusammenhaengenden Stellen. Eine Sonderstellung, die man beim Suchen
    # mitdenken muss, ist keine Hilfe.
    return sorted(names, key=lambda n: (n.lower(), n))


def templates_nach_art() -> dict[str, list[str]]:
    """Vorlagennamen je Zuweisungs-Art — Grundlage der typgefilterten Dropdowns.

    Liefert einen Eintrag pro tatsächlich zugewiesener Art (die Werte aus
    `SLOT_ART`, also `signatur`/`banner`/`disclaimer`; `oof` sobald es dazukommt).
    Die Signaturliste trägt wie in `list_templates()` immer `default`. Arten ohne
    Vorlage stehen mit leerer Liste drin, damit die Oberfläche keinen fehlenden
    Schlüssel behandeln muss.

    ⚠️ `usermail` fehlt bewusst: Nachrichten an Postfachinhaber sind keine
    zuweisbare Vorlage — genau das trennt `SLOT_ART` von `ARTEN`.
    """
    arten: list[str] = []
    for art in SLOT_ART.values():
        if art not in arten:
            arten.append(art)
    return {art: list_templates(art) for art in arten}
