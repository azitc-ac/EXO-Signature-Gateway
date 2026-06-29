FROM python:3.11-slim AS base

WORKDIR /app

# ── System packages + certbot ─────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl certbot libcap2-bin openssl wget ca-certificates \
        libssl-dev libicu-dev \
    && rm -rf /var/lib/apt/lists/* \
    && setcap cap_net_bind_service=+eip $(readlink -f /usr/local/bin/python3)

# ── PowerShell — install from GitHub release (arch-aware, no MS repo needed) ──
# Supports amd64 (x86_64) and arm64 (aarch64) which covers both dev and Pi prod.
RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    PS_VERSION="7.6.2"; \
    case "${ARCH}" in \
        amd64)   PS_ARCH="x64"    ;; \
        arm64)   PS_ARCH="arm64"  ;; \
        *)       echo "Unsupported arch: ${ARCH}" && exit 1 ;; \
    esac; \
    PS_URL="https://github.com/PowerShell/PowerShell/releases/download/v${PS_VERSION}/powershell-${PS_VERSION}-linux-${PS_ARCH}.tar.gz"; \
    mkdir -p /opt/microsoft/powershell/7; \
    wget -q -O /tmp/pwsh.tar.gz "${PS_URL}"; \
    tar -xz -C /opt/microsoft/powershell/7 -f /tmp/pwsh.tar.gz; \
    rm /tmp/pwsh.tar.gz; \
    chmod +x /opt/microsoft/powershell/7/pwsh; \
    ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh

# ── ExchangeOnlineManagement PowerShell module ────────────────────────────────
RUN pwsh -NoProfile -NonInteractive -Command \
    "Set-PSRepository PSGallery -InstallationPolicy Trusted; \
     Install-Module ExchangeOnlineManagement -Force -AllowClobber -Scope AllUsers"

# ── Python dependencies ───────────────────────────────────────────────────────
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App code ──────────────────────────────────────────────────────────────────
COPY app/ .
COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md

RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 25 80 8080

VOLUME ["/app/templates", "/app/certs", "/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -fk https://localhost:8080/health || curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
