"""Vorschlagsliste im Vorlagen-Editor: Klammern setzt sie selbst.

Ein gewählter Vorschlag wird zu `{{ name }}`, außer der Cursor steht schon in
einem offenen `{{ … }}` oder `{% … %}` — dann bleibt es beim nackten Namen.
Geprüft wird der Abschnitt `// <klammern>` aus template_editor.html, in node
ausgeführt (die Browser-Tests laufen in der CI nicht).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

VORLAGE = Path(__file__).resolve().parents[1] / "app/webui/templates/template_editor.html"
A, Z = "{" + "{", "}" + "}"

FAELLE = [
    ("Abteilung: ", f"{A} custom_u.abt {Z}"),               # freier Text → Klammern
    ("", f"{A} custom_u.abt {Z}"),                          # Feldanfang
    (f"{A} ", "custom_u.abt"),                              # nach „{{ “
    (f"{A}", "custom_u.abt"),                               # direkt nach „{{“
    ("{% if ", "custom_u.abt"),                             # in einer Bedingung
    (f"{A} user.mail {Z} · ", f"{A} custom_u.abt {Z}"),      # nach geschlossenem Ausdruck
    ("{% if x %}", f"{A} custom_u.abt {Z}"),                # nach geschlossener Bedingung
    (f"{A} a {Z} {A} user.x | default(", "custom_u.abt"),    # zweiter, offener Ausdruck
    (f'<a href="', f"{A} custom_u.abt {Z}"),                # Attribut ohne Ausdruck
]


def _abschnitt() -> str:
    m = re.search(r"// <klammern>.*?\n(.*?)\s*// </klammern>",
                  VORLAGE.read_text(encoding="utf-8"), re.S)
    assert m, "Abschnitt // <klammern> fehlt in template_editor.html"
    return m.group(1)


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_klammern_nur_ausserhalb_eines_ausdrucks():
    skript = (_abschnitt() + "\n"
              f"console.log(JSON.stringify({json.dumps([v for v, _ in FAELLE])}"
              ".map(v => einsatzText(v, 'custom_u.abt'))));")
    aus = subprocess.run(["node", "-e", skript], capture_output=True, text=True, timeout=30)
    assert aus.returncode == 0, aus.stderr
    assert json.loads(aus.stdout) == [e for _, e in FAELLE]


def test_einfuegen_nutzt_die_entscheidung():
    """Die Entscheidung muss am Einfügen hängen, und der Cursor steht danach
    HINTER dem eingesetzten Text (np = start + Länge des Eingesetzten)."""
    q = VORLAGE.read_text(encoding="utf-8")
    assert "const f = einsatzText(ziel.value.slice(0, start), treffer[i]);" in q
    assert "const np = start + f.length;" in q


def test_vorschlaege_kennen_beide_namensraeume():
    q = VORLAGE.read_text(encoding="utf-8")
    assert ".concat(CUSTOM_VARS.map(c => 'custom_u.' + c))" in q
    assert ".concat(CUSTOM_VARS.map(c => 'custom_b.' + c))" in q



@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_lange_schreibweise_wird_als_eigener_praefix_erkannt():
    """„user.custom.“ darf nicht als „custom.“ erkannt werden — sonst entstünde
    „user.custom_u.x“. Führt den Abschnitt // <praefix> in node aus."""
    m = re.search(r"// <praefix>.*?\n(.*?)\s*// </praefix>", VORLAGE.read_text(encoding="utf-8"), re.S)
    assert m, "Abschnitt // <praefix> fehlt"
    faelle = ["Hallo user.", "x bearbeiter.custom.a", "user.custom.", "custom.a", "{{ custom_b."]
    skript = ("const USER_FIELDS=['displayName','mail'], CUSTOM_VARS=['abt'], OOF_FIELDS=[];\n"
              + m.group(1) + "\n"
              f"console.log(JSON.stringify({json.dumps(faelle)}.map(t => {{ const r = t.match(PRAEFIX_RE);"
              " return r ? r[1] : null; })));")
    aus = subprocess.run(["node", "-e", skript], capture_output=True, text=True, timeout=30)
    assert aus.returncode == 0, aus.stderr
    assert json.loads(aus.stdout) == ["user", "bearbeiter.custom", "user.custom", "custom", "custom_b"]
