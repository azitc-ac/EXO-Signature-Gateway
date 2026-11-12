#!/bin/bash
# Checkt im Repository den Stand eines Update-Kanals aus — EINE Regel für
# Erstinstallation und Update:
#   - update-watcher.sh (Update-Knopf in der Oberfläche)
#   - deploy/azure/cloud-init.yaml (Azure-Vorlage, Marketplace, azure-vm-setup.ps1)
#
# Aufruf: stand-auschecken.sh <kanal> [<version>]
#   release  letztes Release-Tag (v*, nach Versionsnummer sortiert);
#            mit <version> genau dieses Tag (Rollback / gezielte Wahl)
#   nightly  Zweig `nightly` (täglich 23:59 Berlin der letzte grüne main-Stand,
#            .github/workflows/nightly.yml)
#   main     Entwicklungsstand
#
# Bis v1.11.62 klonte die Erstinstallation main und blieb dort, obwohl neue
# Installationen auf dem Kanal Stabil (release) stehen: Ihr erster Stand war
# keiner, den ein Release freigegeben hatte. Steht die Regel hier im Repository,
# lässt sie sich ändern, ohne das Marketplace-Paket neu zertifizieren zu lassen.
set -euo pipefail

KANAL="${1:-}"
ZIEL="${2:-}"
REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"

if [ "$KANAL" = "release" ]; then
  # --force: Ein Tag, das auf dem Server neu gesetzt wurde (etwa nach einer
  # Bereinigung der Historie), wird übernommen. Ohne --force weist git es mit
  # „would clobber existing tag“ ab, behält die alte Fassung und endet mit 1.
  # Scheitert der Abruf (Netz), gelten die vorhandenen Tags — so verhielt sich
  # der Watcher schon, als die Regel noch in ihm stand.
  git fetch --tags --force 2>&1 || echo "WARNUNG: git fetch --tags fehlgeschlagen — vorhandene Tags gelten"
  if [ -n "$ZIEL" ]; then
    TAG="v${ZIEL#v}"
    if ! git rev-parse "$TAG" >/dev/null 2>&1; then
      echo "FEHLER: Tag $TAG nicht gefunden (git fetch --tags ausgeführt)"
      exit 1
    fi
    git reset --hard "$TAG" 2>&1
    echo "→ Release $TAG ausgecheckt (gezielt gewählt — ggf. Rollback)"
  else
    LATEST=$(git tag -l 'v*' | sort -V | tail -1)
    if [ -z "$LATEST" ]; then echo "FEHLER: Keine Release-Tags gefunden"; exit 1; fi
    git reset --hard "$LATEST" 2>&1
    echo "→ Release $LATEST ausgecheckt"
  fi
elif [ "$KANAL" = "nightly" ]; then
  git fetch origin nightly 2>&1 || { echo "FEHLER: Zweig nightly nicht gefunden"; exit 1; }
  git reset --hard origin/nightly 2>&1
  echo "→ Nightly-Stand ausgecheckt"
elif [ "$KANAL" = "main" ]; then
  git fetch origin main 2>&1 && git reset --hard origin/main 2>&1
else
  # Unbekannter Kanal: nicht still auf main ausweichen — das wäre der
  # experimentellste Stand, den niemand gewählt hat.
  echo "FEHLER: Unbekannter Kanal '$KANAL'"
  exit 1
fi
