"""Let's Encrypt via certbot (HTTP-01, webroot) — Ausstellen UND Erneuern aus EINER Quelle.

⚠️ HINTERGRUND (der Grund, warum es dieses Modul gibt): Ausstellung (früher inline
in `main.py`) und Erneuerung (früher inline in `scheduler.py`) waren getrennte
Code-Wege und drifteten auseinander. Die Ausstellung gab certbot ausdrücklich
`--config-dir/--work-dir/--logs-dir` UNTER `DATA_DIR` (vom Container-Benutzer
schreibbar); der Renew nahm dagegen die **Default-Verzeichnisse**
(`/etc/letsencrypt`, `/var/log/letsencrypt` — root-eigen). Folge: `certbot renew`
fand die Renewal-Konfiguration nie (rc≠0), erneuerte also nichts und kopierte das
Zertifikat auch nicht an den Listener-Pfad. Ergebnis im Betrieb: „läuft bald ab,
bitte manuell erneuern", obwohl Auto-Renew eingeschaltet war.

Diese eine Quelle schließt die Drift strukturell aus: `ausstellen()` und
`erneuern()` nutzen **dieselben** Verzeichnisse und **dieselbe** Übernahme ans
Listener-Zertifikat (`uebernehmen()`).

KEIN root nötig — alle Pfade liegen unter `DATA_DIR` (UID des Containers). Deshalb
funktionierte die Ausstellung als `appuser` und deshalb funktioniert jetzt auch
der Renew als `appuser`.

Der manuelle **DNS-01**-Weg (`tls_acme_dns.py`, für Betreiber ohne offenen Port 80)
bleibt bewusst getrennt: ohne DNS-API ist dort keine automatische Erneuerung
möglich; die Oberfläche weist darauf hin. Ob ein Zertifikat über DIESEN certbot-Weg
verwaltet wird, sagt `verwaltet()`.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import config

log = logging.getLogger(__name__)

CERT_NAME = "gateway"
_TIMEOUT_ISSUE = 120
# ⚠️ Großzügig: certbot ist nach dem Wegfall der Zufallsverzögerung (s.u.) in
# Sekunden fertig, aber ein langsamer ACME-/Netz-Moment soll nicht ins Timeout
# laufen. Der eigentliche Fallstrick war NICHT die Dauer der Erneuerung, sondern
# die Zufallsverzögerung davor — siehe --no-random-sleep-on-renew in erneuern().
_TIMEOUT_RENEW = 300


def _dirs() -> dict[str, Path]:
    d = Path(config.DATA_DIR)
    return {
        "webroot": d / "acme-webroot",
        "config": d / "le-config",
        "work": d / "le-work",
        "logs": d / "le-logs",
    }


def _dir_flags() -> list[str]:
    """Die drei Verzeichnis-Flags — GEMEINSAM für Ausstellen und Erneuern.
    Genau hier lag der Bug: der Renew ließ sie weg und traf die Default-Pfade."""
    dd = _dirs()
    return ["--config-dir", str(dd["config"]),
            "--work-dir", str(dd["work"]),
            "--logs-dir", str(dd["logs"])]


def _live() -> Path:
    return _dirs()["config"] / "live" / CERT_NAME


def verwaltet() -> bool:
    """True, wenn eine certbot-Renewal-Konfiguration für uns existiert (dann ist
    HTTP-01/certbot der aktive Weg, nicht DNS-01)."""
    return (_dirs()["config"] / "renewal" / f"{CERT_NAME}.conf").exists()


def _expiry(cert_path: Path) -> datetime | None:
    if not cert_path.exists():
        return None
    try:
        from cryptography import x509
        from smime_store import _get_expiry
        return _get_expiry(x509.load_pem_x509_certificate(cert_path.read_bytes()))
    except Exception as exc:                          # noqa: BLE001
        log.warning("le_certbot: Zertifikat nicht lesbar (%s): %s", cert_path, exc)
        return None


def uebernehmen() -> str:
    """certbot-Live-Zertifikat an den Listener-Pfad (SMTP_TLS_CERT/KEY) kopieren,
    Schlüssel 600. Gibt das Ablaufdatum (dd.mm.YYYY) zurück. Wirft OSError.

    ⚠️ Der Listener liest die KOPIE unter SMTP_TLS_CERT, nicht das certbot-Live-
    Verzeichnis. Ohne diese Übernahme bliebe ein erneuertes Zertifikat unbenutzt —
    genau das fehlte dem alten Renew-Pfad."""
    live = _live()
    cert_dest = Path(config.SMTP_TLS_CERT)
    key_dest = Path(config.SMTP_TLS_KEY)
    cert_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live / "fullchain.pem", cert_dest)
    shutil.copy2(live / "privkey.pem", key_dest)
    key_dest.chmod(0o600)
    exp = _expiry(cert_dest)
    return exp.strftime("%d.%m.%Y") if exp else "?"


def ausstellen(hostname: str, email: str) -> tuple[bool, str]:
    """`certbot certonly --webroot`. Rückgabe (True, Ablaufdatum) bei Erfolg,
    sonst (False, Fehlertext)."""
    dd = _dirs()
    for d in dd.values():
        d.mkdir(parents=True, exist_ok=True)
    cmd = (["certbot", "certonly", "--webroot", "-w", str(dd["webroot"]),
            "-d", hostname, "--cert-name", CERT_NAME,
            "--email", email, "--agree-tos", "--non-interactive"] + _dir_flags())
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_ISSUE)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "certbot error").strip()
    try:
        return True, uebernehmen()
    except OSError as exc:
        return False, f"certbot OK, aber Kopieren fehlgeschlagen: {exc}"


def erneuern(force: bool = False) -> tuple[str, str]:
    """`certbot renew` mit DENSELBEN Verzeichnissen wie `ausstellen()`, danach
    Übernahme ans Listener-Zertifikat.

    Rückgabe (status, info):
      ``renewed``     info = neues Ablaufdatum (Live-Zert war neuer → übernommen)
      ``unchanged``   info = ''  (certbot lief, aber nichts erneuert)
      ``error``       info = Fehlertext
      ``not_managed`` info = ''  (keine Renewal-Konfig — z. B. DNS-01-Zert)

    Kopiert NUR bei tatsächlicher Erneuerung. Der Aufrufer startet danach neu,
    damit der Listener das neue Zertifikat lädt (es wird beim Start gelesen)."""
    if not verwaltet():
        return "not_managed", ""
    served_before = _expiry(Path(config.SMTP_TLS_CERT))
    # ⚠️ --no-random-sleep-on-renew ist ZWINGEND: certbot legt im
    # non-interactive-Modus sonst eine Zufallsverzögerung von bis zu ~8 Minuten
    # VOR die Erneuerung (Lastverteilung für breite Cron-Jobs). Wir planen die
    # Erneuerung selbst (einmal täglich im Scheduler), brauchen die Streuung nicht
    # — und die Verzögerung fraß sonst das Subprozess-Timeout auf, bevor certbot
    # überhaupt die Challenge begann (live gemessen: „random delay of 172s" bei
    # 180s Timeout → status=error/timeout, Zert nie erneuert).
    cmd = (["certbot", "renew", "--cert-name", CERT_NAME,
            "--non-interactive", "--no-random-sleep-on-renew", "--quiet"] + _dir_flags())
    if force:
        cmd.append("--force-renewal")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_RENEW)
    except FileNotFoundError:
        return "error", "certbot nicht gefunden"
    except Exception as exc:                          # noqa: BLE001
        return "error", str(exc)
    if res.returncode != 0:
        return "error", (res.stderr or res.stdout or f"rc={res.returncode}").strip()
    live_exp = _expiry(_live() / "fullchain.pem")
    if live_exp and (served_before is None or live_exp > served_before):
        try:
            return "renewed", uebernehmen()
        except OSError as exc:
            return "error", f"erneuert, aber Kopieren fehlgeschlagen: {exc}"
    return "unchanged", ""
