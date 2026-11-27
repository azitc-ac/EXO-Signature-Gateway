"""Gleichlauf des DNS-Provider-Katalogs mit dem Relay.

`app/dns_provider/` ist im Relay entstanden („Relay zuerst, Gateway
nachziehen“) und wird UNVERÄNDERT übernommen — kein Fork. Wer ihn hier ändern
müsste, ändert ihn im Relay und übernimmt den neuen Stand.

Invarianten:

1. Inhalt == Manifest: Jede Datei hat die SHA-256 aus `KATALOG_VERSION`
   (dieselbe Prüfung wie ``python -m dns_provider pruefen``). Fängt jede
   Änderung an einer Datei hier.
2. Manifest == Relay-Stand: Fassung und Prüfsummen gleichen dem zuletzt
   übernommenen Relay-Stand (`RELAY_STAND` unten, Tag v0.2.58-beta). Wer den
   Katalog hier verändert UND das Manifest neu schreibt, scheitert hier.
   Dasselbe für die Beilagen `FASSUNGEN` und `LIESMICH.md` (`RELAY_BEILAGEN`).
3. Relay-Stand == Relay heute: Das Manifest auf `main` des (öffentlichen)
   Relay-Repositorys wird abgerufen und verglichen. Hat das Relay den Katalog
   weiterentwickelt, wird dieser Test rot, bis das Gateway nachzieht — dann
   Dateien übernehmen und `RELAY_STAND` neu setzen. In der CI ist ein
   Netzfehler ein Fehler, lokal ohne Netz wird übersprungen.
3b. Register `FASSUNGEN` (Gegenstück zu den Relay-Tests in
   tests/test_dns01_provider.py, Ablauf in app/dns_provider/LIESMICH.md):
   die laufende Fassung steht als letzte mit dem Fingerabdruck ihres Manifests
   darin, Fassungen steigen, und jede je hier committete Fassung trägt
   denselben Fingerabdruck wie heute im Register — ein Inhalt taucht nie unter
   einer schon vergebenen Fassung auf.
4. Keine Gateway-Kopplung im Katalog: nur Standardbibliothek, httpx und
   relative Importe.
5. Apache-2.0: SPDX-Kopf und Herkunft in jeder Datei, Hinweis in
   THIRD_PARTY_NOTICES.md, Lizenztext liegt bei und geht mit ins Image.
"""
import ast
import hashlib
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
KATALOG = WURZEL / "app" / "dns_provider"
RELAY_ROH = "https://raw.githubusercontent.com/azitc-ac/exo-smtp-relay/main/app/dns_provider/"
RELAY_MANIFEST_URL = RELAY_ROH + "KATALOG_VERSION"

# Zuletzt übernommener Relay-Stand (Tag v0.2.58-beta, Commit 0dc7b05; die *.py
# und das Manifest unverändert seit v0.2.57-beta).
RELAY_STAND = ("1.0.0", {
    "__init__.py": "d933d4db5741834fffbbe2ae4069932175230be8d6d64917251cd59c3e3f0f17",
    "__main__.py": "c21902fd9c243e0cd565791fc17822ba0a1367b508085fe37914b58120bfc2f9",
    "azure.py": "4f5aeef6dc3ee9130c086cfe35e2d439c83476047591ff20d5a39b432c418b96",
    "basis.py": "9407d688ae02803039f98704d80b31db490e8e73e705399ef398d2c29cabc4f7",
    "dnsabfrage.py": "e02722c515d92d3cfbd2469a32d3d4f179e126576c49096690e3ee9125812103",
    "ionos.py": "8ad24da07c2cfbae4c26b2899d05f76c6721b4311fccdb5fa9ed3fd293d20be5",
})
# Beilagen ohne Eintrag im Manifest (das deckt nur *.py) — SHA-256 der Datei.
RELAY_BEILAGEN = {
    "FASSUNGEN": "7c0b2b798533bcce91d13af72ef751f830e94013a12da949ddbca358df8ccacb",
    "LIESMICH.md": "ceb03bd2a4766572702757ab76cc48997650804bd0cac1d8e613ce12b4c15caa",
}


def _manifest_parsen(text: str) -> tuple[str, dict[str, str]]:
    """Wie dns_provider.manifest_lesen — hier nachgebaut, damit auch das
    abgerufene Relay-Manifest geprüft werden kann."""
    fassung, summen = "", {}
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#"):
            continue
        if zeile.startswith("version "):
            fassung = zeile.split(None, 1)[1]
            continue
        summe, datei = zeile.split(None, 1)
        summen[datei.strip()] = summe
    return fassung, summen


def _abweichung(soll: dict[str, str], ist: dict[str, str]) -> list[str]:
    return sorted(set(soll) ^ set(ist) | {d for d in soll if d in ist and soll[d] != ist[d]})


def test_inhalt_passt_zum_manifest():
    import dns_provider
    fassung, soll = dns_provider.manifest_lesen()
    abw = _abweichung(soll, dns_provider.manifest_berechnen())
    assert not abw, (f"Katalog {fassung}: Datei(en) {abw} weichen vom Manifest ab. Der Katalog "
                     "wird nicht im Gateway geändert — Änderung im Relay machen und übernehmen.")


def test_manifest_gleich_dem_uebernommenen_relay_stand():
    import dns_provider
    fassung, summen = _manifest_parsen((KATALOG / "KATALOG_VERSION").read_text(encoding="utf-8"))
    assert fassung == RELAY_STAND[0], f"Fassung {fassung} ≠ Relay-Stand {RELAY_STAND[0]}"
    assert not _abweichung(RELAY_STAND[1], summen), _abweichung(RELAY_STAND[1], summen)
    assert dns_provider.__version__ == RELAY_STAND[0]


def test_beilagen_gleich_dem_uebernommenen_relay_stand():
    ist = {n: hashlib.sha256((KATALOG / n).read_bytes()).hexdigest() for n in RELAY_BEILAGEN}
    abw = _abweichung(RELAY_BEILAGEN, ist)
    assert not abw, (f"{abw} weicht vom Relay-Stand ab — die Beilagen werden wie der Katalog "
                     "unverändert aus dem Relay übernommen.")


def _abrufen(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:     # noqa: S310 (feste https-Adresse)
            return r.read()
    except (urllib.error.URLError, OSError) as exc:
        if os.environ.get("CI"):
            pytest.fail(f"{url} nicht abrufbar ({exc}) — in der CI muss der Abgleich laufen.")
        pytest.skip(f"{url} nicht abrufbar (ohne Netz?): {exc}")


def test_relay_hat_denselben_stand():
    fassung, summen = _manifest_parsen(_abrufen(RELAY_MANIFEST_URL).decode("utf-8"))
    assert (fassung, summen) == RELAY_STAND, (
        f"Das Relay hat den DNS-Katalog auf Fassung {fassung} weiterentwickelt "
        f"(Abweichung: {_abweichung(RELAY_STAND[1], summen) or 'Fassung'}). Gateway nachziehen: "
        "app/dns_provider/ aus dem Relay übernehmen und RELAY_STAND in diesem Test neu setzen.")
    ist = {n: hashlib.sha256(_abrufen(RELAY_ROH + n)).hexdigest() for n in RELAY_BEILAGEN}
    assert not _abweichung(RELAY_BEILAGEN, ist), (
        f"Das Relay hat {_abweichung(RELAY_BEILAGEN, ist)} geändert — übernehmen und "
        "RELAY_BEILAGEN neu setzen.")


# ── Register FASSUNGEN (Gegenstück zu den gleichnamigen Relay-Tests) ─────────

def _fingerabdruck(summen: dict[str, str]) -> str:
    return hashlib.sha256("".join(f"{s}  {d}\n" for d, s in sorted(summen.items())).encode()).hexdigest()


def _fassung_tupel(fassung: str) -> tuple[int, int, int]:
    teile = fassung.split(".")
    assert len(teile) == 3 and all(t.isdigit() for t in teile), f"Fassung {fassung!r} ist kein X.Y.Z"
    return tuple(int(t) for t in teile)


def _register() -> list[tuple[str, str]]:
    zeilen = [z.split() for z in (KATALOG / "FASSUNGEN").read_text("utf-8").splitlines()
              if z.strip() and not z.lstrip().startswith("#")]
    assert all(len(z) == 2 and len(z[1]) == 64 for z in zeilen), "FASSUNGEN: Zeile ist nicht »<fassung>  <sha256>«"
    return [(z[0], z[1]) for z in zeilen]


def test_katalog_fassung_im_register():
    """Manifest geändert ohne Versionssprung → rot. Die laufende Fassung muss die
    letzte im Register sein und genau diesen Inhalt tragen; Fassungen steigen."""
    import dns_provider
    fassung, soll = dns_provider.manifest_lesen()
    register = _register()
    fassungen = [f for f, _ in register]
    assert len(set(fassungen)) == len(fassungen), "FASSUNGEN: eine Fassung doppelt"
    tupel = [_fassung_tupel(f) for f in fassungen]
    assert tupel == sorted(tupel), "FASSUNGEN: Fassungen nicht aufsteigend"
    fp = _fingerabdruck(soll)
    eintrag = dict(register).get(fassung)
    assert eintrag is not None and fassungen[-1] == fassung, (
        f"Fassung {fassung} fehlt am Ende von app/dns_provider/FASSUNGEN — die Fassung "
        "entsteht im Relay (Ablauf: app/dns_provider/LIESMICH.md), hier nur übernehmen.")
    assert eintrag == fp, (
        f"Inhalt geändert, Fassung {fassung} aber nicht angehoben — der Katalog wird im Relay "
        "geändert und hochgezählt, nicht im Gateway.")


def test_katalog_fassungen_wie_in_der_historie():
    """Jede je hier committete Fassung (KATALOG_VERSION in der Git-Historie) steht mit
    DEMSELBEN Fingerabdruck im Register. Ohne Git übersprungen."""
    if not (WURZEL / ".git").exists():
        pytest.skip("keine Git-Historie")
    rel = "app/dns_provider/KATALOG_VERSION"
    try:
        commits = subprocess.run(["git", "-C", str(WURZEL), "log", "--format=%H", "--", rel],
                                 capture_output=True, text=True, check=True).stdout.split()
    except FileNotFoundError:
        pytest.skip("git nicht installiert")
    assert commits, f"{rel} nie committet?"
    register = dict(_register())
    for c in commits:
        inhalt = subprocess.run(["git", "-C", str(WURZEL), "show", f"{c}:{rel}"],
                                capture_output=True, text=True)
        if inhalt.returncode:
            continue                                    # Commit, der die Datei löschte
        fassung, summen = _manifest_parsen(inhalt.stdout)
        assert register.get(fassung) == _fingerabdruck(summen), (
            f"Commit {c[:7]}: Fassung {fassung} trug damals anderen Inhalt als FASSUNGEN heute sagt — "
            f"eine vergebene Fassung wird nie umgeschrieben, sondern hochgezählt")


def _importe(pfad: Path) -> list[tuple[str, int]]:
    """(Modul, Ebene) je Import — Ebene > 0 ist relativ."""
    aus = []
    for k in ast.walk(ast.parse(pfad.read_text(encoding="utf-8"))):
        if isinstance(k, ast.Import):
            aus += [(a.name.split(".")[0], 0) for a in k.names]
        elif isinstance(k, ast.ImportFrom):
            aus.append(((k.module or "").split(".")[0], k.level))
    return aus


def test_katalog_ohne_gateway_kopplung():
    erlaubt = set(sys.stdlib_module_names) | {"httpx", "__future__"}
    for pfad in sorted(KATALOG.glob("*.py")):
        fremd = [m for m, ebene in _importe(pfad) if ebene == 0 and m not in erlaubt]
        assert not fremd, f"{pfad.name} importiert {fremd} — der Katalog kennt das Gateway nicht."


def test_apache_kopf_und_hinweise():
    for pfad in sorted(KATALOG.glob("*.py")):
        kopf = pfad.read_text(encoding="utf-8").splitlines()[:3]
        assert kopf[0] == "# SPDX-License-Identifier: Apache-2.0", pfad.name
        assert "Herkunft: EXO SMTP Relay" in kopf[2], pfad.name
    hinweis = (WURZEL / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "app/dns_provider/" in hinweis and "Apache License, Version 2.0" in hinweis
    lizenz = (WURZEL / "LICENSES" / "Apache-2.0.txt").read_text(encoding="utf-8")
    assert "Apache License" in lizenz and "Version 2.0, January 2004" in lizenz
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in lizenz
    docker = (WURZEL / "Dockerfile").read_text(encoding="utf-8")
    assert "THIRD_PARTY_NOTICES.md" in docker and "COPY LICENSES/" in docker, \
        "Lizenz und Hinweis gehören mit ins Image (Weitergabe)"
