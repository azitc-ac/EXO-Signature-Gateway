"""Selbst-Update über Trigger-Dateien — EINE Umsetzung für Gateway UND Hub.

DIESE DATEI MUSS IN BEIDEN ANWENDUNGEN INHALTSGLEICH SEIN.
tools/driftcheck.py vergleicht die SHA-256.

WARUM DAS EXISTIERT
-------------------
`updater.py` existierte zweimal, zu 77 % identisch, mit identischer
Funktionsliste — und war auseinandergelaufen. Konkret fehlte dem Gateway die
Behandlung **privater Repositorys**, die der Hub am 2026-07-26 bekam:

  * Der Hub liest die Fernversion vorrangig aus `data/.remote-version`, einer
    Datei, die das Watcher-Skript per `git fetch` befüllt — die GitHub-API
    liefert für private Repos 404.
  * Der Hub fängt `HTTPError 404` ab und erklärt die Lage, statt einen rohen
    Fehler zu zeigen. Beim Gateway landete derselbe Fall im generischen
    Exception-Zweig als „Not Found" — genau die Meldung, über die sich der
    Betreiber beim Hub beschwert hatte.

Gemessen: `azitc-ac/EXO-Signature-Gateway` ist öffentlich (VERSION per HTTP 200
abrufbar), `azitc-ac/sig-provider` privat (404). Das Gateway brauchte den
Rückfall also heute nicht — aber sobald das Gateway-Repo privat wird (bei einem
kommerziellen Produkt naheliegend), bräche dort dieselbe Meldung ohne Rückfall.
Eine Umsetzung für beide beseitigt diese Asymmetrie dauerhaft.

ABGRENZUNG
----------
Die HOST-Seite (`update-watcher.sh`, systemd-Unit) bleibt bewusst je Anwendung
getrennt: verschiedene Dienstnamen, Pfade und Compose-Projekte. Hier steckt nur
die Container-Seite.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import config

log = logging.getLogger(__name__)

# Wie lange die Oberfläche pollt, bevor sie den Watcher für abwesend erklärt
WATCHER_TIMEOUT_S = 60
# Heartbeat älter als das → Watcher gilt als tot.
# 300 s deckt einen vollständigen `docker compose build`-Zyklus ab.
HEARTBEAT_MAX_AGE_S = 300

# Obergrenze für angezeigte Changelog-Einträge
MAX_CHANGELOG_ENTRIES = 25


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except Exception:
        return (0,)


def _version_number(v: str) -> int:
    """Version als eine Zahl, für die Distanzberechnung beim Changelog."""
    try:
        parts = (list(int(x) for x in v.lstrip("v").split(".")) + [0, 0, 0])[:3]
    except Exception:
        return 0
    return parts[0] * 1_000_000 + parts[1] * 1_000 + parts[2]


class Updater:
    """Container-Seite des Selbst-Updates für eine Anwendung.

    Alles Anwendungsspezifische steckt in den Konstruktor-Argumenten. Wer eine
    dritte Anwendung anbindet, legt eine weitere Instanz an — und nicht eine
    weitere Kopie dieser Datei.
    """

    def __init__(self, repo: str, user_agent: str, data_dir: str = ""):
        self.repo = repo
        self.user_agent = user_agent
        # Leer = Vorgabe aus der Konfiguration. NICHT `config.DATA_DIR` direkt
        # als Vorgabewert: der wuerde beim Import der Klasse eingefroren, und
        # ein Test, der `config.DATA_DIR` umbiegt, schriebe weiter ins echte
        # Verzeichnis.
        self.data = Path(data_dir or config.DATA_DIR)
        self.trigger = self.data / ".update-trigger"
        self.status_file = self.data / ".update-status"
        self.heartbeat = self.data / ".update-heartbeat"
        self.restart_trigger = self.data / ".restart-trigger"
        # Vom Watcher per `git fetch` befüllt. Primärquelle, weil sie auch für
        # private Repositorys funktioniert (GitHub-API liefert dort 404).
        self.remote_version_file = self.data / ".remote-version"

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _get(self, url: str, timeout: int = 10, accept: str | None = None) -> str:
        kopf = {"User-Agent": self.user_agent}
        if accept:
            kopf["Accept"] = accept
        req = urllib.request.Request(url, headers=kopf)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()

    def _repo_datei(self, pfad: str) -> str:
        """Inhalt einer Datei des main-Branch — über die API, NICHT über raw.

        `raw.githubusercontent.com` liefert über ein Auslieferungsnetz mit
        `max-age=300` aus. Direkt nach einer Veröffentlichung meldet die
        Prüfung dort bis zu fünf Minuten lang die VORIGE Fassung; "Bereits
        aktuell" ist dann schlicht falsch.

        Am selben Fall gemessen (raw lieferte 1.7.113 bei `source-age: 129`,
        im Repository stand 1.7.114) — drei naheliegende Auswege halfen NICHT:

            wechselnder Abfrageparameter  -> 1.7.113   (nicht im Schlüssel)
            Cache-Control: no-cache       -> 1.7.113   (ignoriert)
            Pragma: no-cache              -> 1.7.113   (ignoriert)
            API /contents                 -> 1.7.114   ✓

        Die API hält nur 60s vor, wird für den Release-Kanal ohnehin benutzt,
        und ihr Kontingent (60/Stunde ohne Anmeldung) reicht für eine von Hand
        ausgelöste Prüfung. Grenze des Endpunkts sind 1 MB je Datei — der
        Changelog liegt bei rund 0,4 MB.
        """
        return self._get(
            f"https://api.github.com/repos/{self.repo}/contents/{pfad}?ref=main",
            accept="application/vnd.github.raw")

    # ── Versionsermittlung ──────────────────────────────────────────────────
    def read_remote_version(self) -> str | None:
        """Fernversion aus der Watcher-Datei. None, wenn nicht vorhanden/leer."""
        try:
            v = self.remote_version_file.read_text().strip()
            return v or None
        except OSError:
            return None

    def fetch_changelog_entries(self, from_version: str, to_version: str) -> list:
        """Changelog-Einträge zwischen zwei Versionen.

        DRIFT-IMMUN: Es werden die obersten K Einträge genommen, K = Abstand der
        Versionsnummern. Die Nummern im Changelog driften von der VERSION-Datei
        ab, ein Abgleich auf exakte Überschriften würde also Einträge verlieren.
        """
        try:
            text = self._repo_datei("CHANGELOG.md")
        except Exception:
            return []
        entries: list[dict] = []
        header, body = "", []
        for line in text.splitlines():
            if line.startswith("## v"):
                if header:
                    entries.append({"header": header, "body": "\n".join(body).strip()})
                header, body = line, []
            elif header:
                body.append(line)
        if header:
            entries.append({"header": header, "body": "\n".join(body).strip()})
        steps = max(1, _version_number(to_version) - _version_number(from_version))
        return entries[:min(steps, len(entries), MAX_CHANGELOG_ENTRIES)]

    def _result(self, channel: str, current: str, latest: str | None,
                url: str = "", note: str = "",
                available: bool | None = ...) -> dict:      # type: ignore[assignment]
        """Ergebnis für die Oberfläche.

        ⚠️ `available` unterscheidet DREI Zustände, und die Oberfläche wertet
        alle drei aus (`backup.html`: `=== true` / `=== null` / sonst):
          True  → Update anbieten
          None  → Version NICHT ERMITTELBAR: Hinweis zeigen und die
                  Installations-Schaltfläche TROTZDEM sichtbar lassen
          False → aktuell bzw. nichts zu installieren → Schaltfläche verbergen

        None und False sind deshalb NICHT austauschbar. Würde man den Fall
        „privates Repository, Watcher hat noch keine Fernversion geschrieben" auf
        False abbilden, verschwände die Schaltfläche — der Betreiber könnte auf
        genau der Maschine kein Update mehr starten. Wer hier etwas ändert,
        prüfe zuerst die drei Zweige in `backup.html`.
        """
        if available is ...:
            available = (_version_tuple(latest) > _version_tuple(current)) if latest else None
        out = {
            "ok": True, "channel": channel, "current": current,
            "latest": latest, "available": available, "url": url,
            "changelog_entries": (self.fetch_changelog_entries(current, latest)
                                  if available is True and latest else []),
        }
        if note:
            out["note"] = note
        return out

    def check_update(self, channel: str, current_version: str) -> dict:
        """Neuere Version verfügbar?

        channel "main"    → VERSION-Datei des main-Branch
        channel "release" → jüngstes GitHub-Release (Tag)

        Rangfolge im main-Kanal: erst die lokale `.remote-version` (funktioniert
        auch für private Repos, kein API-Aufruf), dann die GitHub-API.
        """
        if channel != "release":
            local = self.read_remote_version()
            if local:
                return self._result(channel, current_version, local)

        try:
            if channel == "release":
                data = json.loads(self._get(
                    f"https://api.github.com/repos/{self.repo}/releases/latest"))
                if "tag_name" not in data:
                    # available=False (nicht None): es GIBT nichts zu
                    # installieren, also soll die Schaltfläche verborgen bleiben.
                    return self._result(channel, current_version, None,
                                        note="Noch kein Release veröffentlicht",
                                        available=False)
                return self._result(channel, current_version,
                                    data["tag_name"].lstrip("v"),
                                    url=data.get("html_url", ""))
            latest = self._repo_datei("VERSION").strip()
            return self._result(channel, current_version, latest)

        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Privates Repo und der Watcher hat noch keine .remote-version
                # geschrieben. KEIN Fehler: das Update lässt sich trotzdem
                # anstoßen, nur die Vorabanzeige der Zielversion fehlt.
                return self._result(
                    channel, current_version, None,
                    note="Versionsprüfung nicht verfügbar (privates Repository, "
                         "Watcher hat noch keine Fernversion geschrieben). Das "
                         "Update kann trotzdem gestartet werden.")
            return {"ok": False, "error": f"GitHub: HTTP {e.code} {e.reason}",
                    "channel": channel}
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"GitHub nicht erreichbar: {e.reason}",
                    "channel": channel}
        except Exception as e:
            return {"ok": False, "error": str(e), "channel": channel}

    def list_release_tags(self, limit: int = 30) -> list:
        """Veröffentlichte GitHub-Releases (Tag, Datum, URL), neueste zuerst.
        Für die Auswahl einer Zielversion (vor oder zurück) in der Oberfläche."""
        try:
            data = json.loads(self._get(
                f"https://api.github.com/repos/{self.repo}/releases?per_page={limit}"))
        except Exception:
            return []
        return [
            {"version": rel["tag_name"].lstrip("v"),
             "published_at": rel.get("published_at", ""),
             "url": rel.get("html_url", ""),
             "name": rel.get("name") or rel["tag_name"]}
            for rel in data
            if rel.get("tag_name") and not rel.get("draft")
        ]

    # ── Zustand und Auslöser ────────────────────────────────────────────────
    def get_status(self) -> dict:
        try:
            return json.loads(self.status_file.read_text())
        except Exception:
            return {"state": "idle"}

    def clear_status(self) -> None:
        try:
            self.status_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _write_trigger(self, path: Path, payload: dict) -> dict:
        # 644 ist hier RICHTIG und kein Versehen: die Datei wird vom
        # HOST-Watcher gelesen, der als anderer Benutzer läuft. Sie enthält
        # keine Geheimnisse (Auslöser, Zeitstempel, Zielversion) und geht
        # deshalb absichtlich NICHT über secure_io.
        try:
            path.write_text(json.dumps(payload))
            path.chmod(0o644)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def request_update(self, requested_by: str, current_version: str,
                       channel: str = "main",
                       target_version: str | None = None) -> dict:
        """Trigger-Datei schreiben, damit der Host-Watcher das Update ausführt.

        target_version: bestimmtes Release-Tag (nur Release-Kanal). None = jüngste.
        Eine ältere Zielversion als die laufende ist die Rückrollung.
        """
        if self.get_status().get("state") == "running":
            return {"ok": False, "error": "Update läuft bereits"}
        payload = {
            "requested_by": requested_by,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "current_version": current_version,
            "channel": channel,
        }
        if target_version:
            payload["target_version"] = target_version
        return self._write_trigger(self.trigger, payload)

    def request_container_restart(self, requested_by: str) -> dict:
        """Neustart-Auslöser → Watcher führt `docker compose restart` aus."""
        if self.get_status().get("state") == "running":
            return {"ok": False, "error": "Update läuft bereits — bitte warten"}
        return self._write_trigger(self.restart_trigger, {
            "requested_by": requested_by,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })

    def watcher_ok(self) -> bool:
        """True, wenn der Host-Watcher kürzlich einen Heartbeat geschrieben hat."""
        try:
            age = datetime.now(timezone.utc).timestamp() - self.heartbeat.stat().st_mtime
            return age < HEARTBEAT_MAX_AGE_S
        except Exception:
            return False
