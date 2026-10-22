#!/bin/sh
# Postfix des Direktversands: Konfiguration vom Gateway übernehmen, Postfix im
# Vordergrund laufen lassen, Zustand für die Oberfläche schreiben.
set -u

GW=/gw                       # = data/mta des Gateways
LOG=/var/log/postfix.log
UID_GW=1000                  # Gateway-Prozess (appuser) muss die Statusdatei lesen

mkdir -p "$GW"
# Legt Docker das Verzeichnis beim ersten Start selbst an, gehört es root — das
# Gateway (appuser) könnte dann seine Konfiguration nicht hineinschreiben.
chown "$UID_GW:$UID_GW" "$GW" && chmod 700 "$GW"

anwenden() {
    # Immer vom Auslieferungsstand aus: Eine Einstellung, die das Gateway nicht
    # mehr schreibt, soll auch nicht aus einem früheren Lauf stehen bleiben.
    cp /etc/postfix/main.cf.orig /etc/postfix/main.cf
    # Grundstellung, falls das Gateway (noch) nichts geschrieben hat: nichts
    # hinauslassen. Das Gateway schreibt beim Start und bei jeder Änderung.
    postconf -e "default_transport = error:5.3.0 Direktversand ist ausgeschaltet" \
                "maillog_file = $LOG" "inet_protocols = ipv4" "mydestination =" \
                "smtpd_relay_restrictions = permit_mynetworks, reject" \
                "mynetworks_style = subnet"
    if [ -f "$GW/postconf.cf" ]; then
        while IFS= read -r zeile || [ -n "$zeile" ]; do
            case "$zeile" in ''|'#'*) continue ;; esac
            postconf -e "$zeile"
        done < "$GW/postconf.cf"
    fi
    if [ -f "$GW/sasl_passwd" ]; then
        install -m 600 "$GW/sasl_passwd" /etc/postfix/sasl_passwd
    else
        : > /etc/postfix/sasl_passwd; chmod 600 /etc/postfix/sasl_passwd
    fi
    postfix check 2>&1 | head -5
}

fingerabdruck() {
    cat "$GW/postconf.cf" "$GW/sasl_passwd" 2>/dev/null | md5sum | cut -d' ' -f1
}

status_schreiben() {
    tmp="$GW/.status.json.tmp"
    if [ -s "$LOG" ]; then
        gesendet=$(grep -c ' status=sent' "$LOG")
        zurueck=$(grep -c ' status=deferred' "$LOG")
        abgewiesen=$(grep -c ' status=bounced' "$LOG")
        fehler=$(grep -E ' status=(bounced|deferred)' "$LOG" | tail -n 10 | jq -R . | jq -s .)
    else
        gesendet=0; zurueck=0; abgewiesen=0; fehler='[]'
    fi
    warteschlange=$(postqueue -j 2>/dev/null | wc -l)
    aufgeschoben=$(postqueue -j 2>/dev/null | grep -c '"queue_name": *"deferred"')
    jq -n --arg stand "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
          --argjson gesendet "$gesendet" --argjson zurueckgestellt "$zurueck" \
          --argjson abgewiesen "$abgewiesen" --argjson warteschlange "$warteschlange" \
          --argjson aufgeschoben "$aufgeschoben" --argjson letzte_fehler "$fehler" \
          --arg version "$(postconf -h mail_version)" \
          '{stand:$stand, postfix:$version, heute:{gesendet:$gesendet,
            zurueckgestellt:$zurueckgestellt, abgewiesen:$abgewiesen},
            warteschlange:$warteschlange, aufgeschoben:$aufgeschoben,
            letzte_fehler:$letzte_fehler}' > "$tmp" \
      && chown "$UID_GW:$UID_GW" "$tmp" && chmod 600 "$tmp" && mv "$tmp" "$GW/status.json"
}

anwenden
stand=$(fingerabdruck)
tag=$(date +%Y%m%d)
touch "$LOG"
tail -n 0 -F "$LOG" &          # Zustellprotokoll auch in `docker logs`
postfix start-fg &
PF=$!

zaehler=0
while kill -0 "$PF" 2>/dev/null; do
    sleep 5
    neu=$(fingerabdruck)
    if [ "$neu" != "$stand" ]; then
        echo "Konfiguration vom Gateway geändert — übernehme"
        anwenden && postfix reload
        stand=$neu
    fi
    # Protokoll je Tag: Die Zähler der Statusdatei gelten damit für „heute“.
    if [ "$(date +%Y%m%d)" != "$tag" ]; then
        postfix logrotate >/dev/null 2>&1
        ls -1t /var/log/postfix.log.* 2>/dev/null | tail -n +8 | xargs -r rm -f
        tag=$(date +%Y%m%d)
    fi
    zaehler=$((zaehler + 1))
    if [ $((zaehler % 6)) -eq 1 ]; then status_schreiben; fi
done
echo "Postfix beendet" >&2
exit 1
