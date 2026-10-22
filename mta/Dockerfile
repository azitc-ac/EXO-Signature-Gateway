# Postfix für den Direktversand des Gateways (eigener Container).
#
# Der Container ist auf JEDER Installation vorhanden, aber untätig, solange der
# Direktversand in der Oberfläche aus ist: Dann steht `default_transport` auf
# `error`, und er lauscht ohnehin nur im internen Docker-Netz (kein Port nach
# außen). So kommt er mit dem normalen Update ins Compose, ohne Handgriff in
# `.env`.
#
# Konfiguriert wird er NICHT hier, sondern vom Gateway: Es schreibt
# `data/mta/postconf.cf` (Zeilen „schlüssel = wert“ für `postconf -e`) und ggf.
# `data/mta/sasl_passwd`; `entrypoint.sh` übernimmt Änderungen binnen Sekunden.
#
# Basis auf den INDEX-Digest festgenagelt (arm64 UND amd64), wie im Gateway.
# AKTUALISIEREN: `docker buildx imagetools inspect alpine:3.22`.
FROM alpine:3.22@sha256:822614ea121e1d40a33655081af10ef85f0aa2ab9e61f649ceaf0c651822433c

# Paketfassungen bewusst frei (Sicherheitsaktualisierungen, siehe Gateway-Dockerfile).
# cyrus-sasl(-login): SMTP AUTH am Smarthost; jq: Statusdatei als sauberes JSON.
RUN apk add --no-cache postfix cyrus-sasl cyrus-sasl-login ca-certificates jq \
    && cp /etc/postfix/main.cf /etc/postfix/main.cf.orig

COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 25
ENTRYPOINT ["/entrypoint.sh"]
