# Basisabbild AUF DEN DIGEST festgenagelt, nicht auf den Tag.
#
# `python:3.11-slim` bewegt sich: Derselbe Tag lieferte ueber die Monate
# verschiedene Python- und Debian-Staende. Damit bekaeme ein Betreiber, der in
# einem halben Jahr baut, etwas anderes als das, was hier geprueft wurde — und
# niemand haette das entschieden. Dieselbe Ueberlegung wie bei
# `requirements.lock`; siehe deren Kopf fuer den Vorfall, der dazu fuehrte.
#
# Dies ist der INDEX-Digest (arch-uebergreifend). Ein architekturspezifischer
# Digest wuerde den Build auf der jeweils anderen Plattform unmoeglich machen —
# das Gateway laeuft auf arm64 (Raspi) UND amd64 (Azure-VM).
#
# Stand: python 3.11.15-slim-trixie (Debian 13.6) — dieselbe Fassung, die am
# 10.08.2026 produktiv lief.
#
# AKTUALISIEREN: `docker buildx imagetools inspect python:3.11-slim` liefert
# den neuen Index-Digest. Danach bauen, testen, erst dann ausrollen.
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff AS base

WORKDIR /app

# ── System packages + certbot ─────────────────────────────────────────────────
#
# ⚠️ BEWUSST NICHT auf Fassungen festgenagelt — der einzige verbleibende
# bewegliche Teil des Abbilds.
#
# Zwei Gruende. Erstens laesst es sich nicht durchhalten: Debian entfernt alte
# Paketfassungen aus dem Spiegel, sobald eine neue da ist; ein `=1.2.3`-Pin
# laesst den Build ein paar Wochen spaeter mit „Version not found" scheitern —
# also genau dann, wenn ein Betreiber installiert. Zweitens ist es hier auch nicht
# gewollt: Diese Zeile ist der Weg, auf dem Sicherheitsaktualisierungen von
# OpenSSL und certbot ueberhaupt ins Abbild kommen.
#
# Der Preis ist Ehrlichkeit wert: Zwei Builds zu verschiedenen Zeitpunkten
# koennen sich in diesen Paketen unterscheiden. Alles andere im Abbild
# (Basis, Python-Pakete, PowerShell, Exchange-Modul) ist festgeschrieben.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl certbot libcap2-bin openssl wget ca-certificates \
        libssl-dev libicu-dev \
    && rm -rf /var/lib/apt/lists/* \
    && setcap cap_net_bind_service=+eip $(readlink -f /usr/local/bin/python3)

# ── PowerShell — install from GitHub release (arch-aware, no MS repo needed) ──
# Supports amd64 (x86_64) and arm64 (aarch64) which covers both dev and Pi prod.
# ⚠️ Der Tarball wird gegen die offizielle SHA256 geprueft (Lieferkette). Beim
# Anheben von PS_VERSION BEIDE Hashes aus
# https://github.com/PowerShell/PowerShell/releases/download/v<VERSION>/hashes.sha256
# mit uebernehmen — sonst schlaegt der Build fehl (genau das ist der Zweck).
RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    PS_VERSION="7.6.2"; \
    case "${ARCH}" in \
        amd64)   PS_ARCH="x64";   PS_SHA="6cbcfbf20e376aa62ffd91c973493c41a7a52ddfd5a5db3ff9bc12f0d0fe9292" ;; \
        arm64)   PS_ARCH="arm64"; PS_SHA="a8d4e386dfafda385d0604045eed03ce6f3a843d45fc8f0b9588b836ca17cdb8" ;; \
        *)       echo "Unsupported arch: ${ARCH}" && exit 1 ;; \
    esac; \
    PS_URL="https://github.com/PowerShell/PowerShell/releases/download/v${PS_VERSION}/powershell-${PS_VERSION}-linux-${PS_ARCH}.tar.gz"; \
    mkdir -p /opt/microsoft/powershell/7; \
    wget -q -O /tmp/pwsh.tar.gz "${PS_URL}"; \
    echo "${PS_SHA}  /tmp/pwsh.tar.gz" | sha256sum -c -; \
    tar -xz -C /opt/microsoft/powershell/7 -f /tmp/pwsh.tar.gz; \
    rm /tmp/pwsh.tar.gz; \
    chmod +x /opt/microsoft/powershell/7/pwsh; \
    ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh

# ── ExchangeOnlineManagement PowerShell module ────────────────────────────────
# ARCH-HINWEIS: Dieser Schritt FÜHRT pwsh aus. amd64 und arm64 bauen NATIV
# problemlos (x64 ist PowerShells Hauptplattform). Ein CROSS-Build via QEMU
# (z.B. `buildx --platform linux/amd64` auf einem ARM-Host) CRASHT hier mit
# Exit 134/SIGABRT — .NET läuft nicht zuverlässig unter QEMU-User-Emulation.
# → Multi-Arch-Images NUR nativ bauen (Hardware/Runner je Arch) oder in einer
#   CI-Matrix mit nativen amd64- + arm64-Runnern. Verifiziert 2026-07-12:
#   amd64-Layer bis hier (Base, Pakete, pwsh-x64-Binary) bauen emuliert sauber;
#   erst das AUSFÜHREN von pwsh kippt — reines Emulations-Artefakt, kein x64-Bug.
#
# FASSUNG FESTGENAGELT. Ohne `-RequiredVersion` holte `Install-Module` beim
# Bauen, was der Katalog gerade anbietet — dieses Modul steuert aber die
# gesamte Exchange-Verwaltung: Verteilerlisten, Transportregeln,
# Postfachabfragen. Aendert Microsoft dort das Verhalten eines Cmdlets, traefe
# es einen Betreiber, der spaeter baut, und nicht uns beim Testen. Von allen
# wandernden Bestandteilen war das der mit der groessten Hebelwirkung.
#
# 3.10.1 ist die Fassung, die am 10.08.2026 auf Raspi UND Azure-VM lief.
#
# AKTUALISIEREN: bewusst, danach `docker exec … pwsh -c 'Get-Module -ListAvailable
# ExchangeOnlineManagement'` gegenpruefen und die EXO-Verbindung testen.
RUN pwsh -NoProfile -NonInteractive -Command \
    "Set-PSRepository PSGallery -InstallationPolicy Trusted; \
     Install-Module ExchangeOnlineManagement -RequiredVersion 3.10.1 \
       -Force -AllowClobber -Scope AllUsers"

# ── Python dependencies ───────────────────────────────────────────────────────
#
# Installiert wird aus der LOCK-Datei, nicht aus requirements.txt.
#
# requirements.txt pinnt nur die 11 direkt benutzten Pakete; alles, was als
# deren Abhaengigkeit mitkommt, blieb frei und wanderte zwischen den Umgebungen
# (am 09.08.2026 gemessen: vier verschiedene Starlette-Fassungen bei
# identischer requirements.txt — Begruendung und Messwerte stehen im Kopf der
# Lock-Datei). Damit war der Baum, gegen den die CI prueft, nicht der Baum, der
# beim Betreiber laeuft.
#
# BEIDE Dateien werden kopiert, obwohl nur die Lock-Datei installiert wird:
# requirements.txt gehoert ins Abbild, damit im Container nachlesbar ist, was
# absichtlich ausgewaehlt wurde und was nur mitkam.
COPY app/requirements.txt app/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# ── App code ──────────────────────────────────────────────────────────────────
COPY app/ .
COPY legal/ /app/legal/
COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md

# Die Mountpunkte VOR dem chown anlegen: Sonst legt sie die folgende
# VOLUME-Anweisung als root:root an, und appuser (UID 1000) kann nicht in
# /app/data schreiben → PermissionError beim ersten Start (Crash-Loop).
RUN mkdir -p /app/data /app/certs /app/templates \
 && useradd -m appuser \
 && chown -R appuser /app
USER appuser

EXPOSE 25 80 8080

VOLUME ["/app/templates", "/app/certs", "/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -fk https://localhost:8080/health || curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
