# Mitgliederpflege der Verteilerlisten des Gateways — EINE Fassung fuer
# update_mailbox_dg.ps1 und setup_rule_split.ps1 (per Dot-Sourcing).
#
# WARUM EIN SAMMELAUFRUF (seit v1.12.67)
# Bis dahin lief je Mitglied ein eigenes Add-/Remove-DistributionGroupMember.
# Werden hunderte Postfaecher auf einmal aktiviert (etwa alle Hybrid-Nutzer),
# sind das hunderte Aufrufe hintereinander — bei rund einer Sekunde je Aufruf
# (Annahme, Groessenordnung wie Get-App: 0,8-1,3 s gemessen) laenger als die
# Zeitgrenze des Laufs. Ab SammelAb Aenderungen setzt deshalb EIN
# Update-DistributionGroupMember die ganze Soll-Liste. Laut Microsoft ersetzt
# das Cmdlet alle Mitglieder, hat eine eingebaute Rueckfrage (-Confirm:$false
# noetig) und nimmt hoechstens 10000 Eintraege (learn.microsoft.com,
# Update-DistributionGroupMember). Das Ergebnis ist dasselbe wie beim
# Einzelweg, der ebenfalls auf die Soll-Liste abgleicht.
#
# Scheitert der Sammelaufruf (ob er dann teilweise wirkte, dokumentiert
# Microsoft nicht), liest die Funktion die Liste neu und gleicht einzeln ab,
# wie zuvor — mit Fehlermeldung je Mitglied. Kleine Aenderungen (der Alltag:
# ein, zwei Postfaecher) gehen wie bisher einzeln.
#
# Nie mit leerer Soll-Liste sammeln: "alle ersetzen durch nichts" waere eine
# Leerung, und die bleibt dem Einzelweg vorbehalten (dort sichtbar je Mitglied).
# Das Gate der Transportregel beruehrt diese Funktion nicht.
#
# -UmgehtManagerPruefung (seit v1.12.71, fuer setup_app_access_policy.ps1):
# reicht -BypassSecurityGroupManagerCheck an Update-/Add-/Remove- weiter. Die
# App-Scope-Gruppe ist eine mail-enabled Security-Gruppe, deren ManagedBy die
# App-only-Identitaet nicht enthaelt; laut Microsoft braucht, wer nicht in
# ManagedBy steht, diesen Schalter (alle drei Cmdlets kennen ihn,
# learn.microsoft.com). Ohne den Schalter bleibt alles wie bisher.

function Get-DgIst([string]$Identity) {
    $ist = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($o in @(Get-DistributionGroupMember -Identity $Identity -ResultSize Unlimited -ErrorAction Stop)) {
        $a = [string]$o.PrimarySmtpAddress
        if ($a) { [void]$ist.Add($a.ToLower()) }
    }
    return ,$ist
}

function Sync-DgMitglieder {
    param(
        [Parameter(Mandatory)][string]$Identity,
        [string[]]$Soll = @(),
        [int]$SammelAb = 20,
        [switch]$UmgehtManagerPruefung
    )
    # ⚠️ Eigener Name: PowerShell unterscheidet bei Variablen nicht nach Gross/Klein —
    # `$soll` WAERE der Parameter `$Soll` und ueberschriebe ihn vor der Schleife.
    $sollMenge = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($m in $Soll) {
        if ($m -and $m.Trim()) { [void]$sollMenge.Add($m.Trim().ToLower()) }
    }
    $ist = Get-DgIst $Identity
    $hinzu = @($sollMenge | Where-Object { -not $ist.Contains($_) })
    $weg   = @($ist  | Where-Object { -not $sollMenge.Contains($_) })
    $sammel = 'nein'
    $umgehen = @{}
    if ($UmgehtManagerPruefung) { $umgehen['BypassSecurityGroupManagerCheck'] = $true }

    if (($hinzu.Count + $weg.Count) -ge $SammelAb -and $sollMenge.Count -gt 0 -and $sollMenge.Count -le 10000) {
        try {
            Update-DistributionGroupMember -Identity $Identity -Members @($sollMenge) @umgehen -Confirm:$false -ErrorAction Stop
            $sammel = 'ja'
            Write-Host "[OK] Sammelaufruf: $($sollMenge.Count) Mitglieder gesetzt (+$($hinzu.Count) / -$($weg.Count))" -ForegroundColor Green
            $hinzu = @(); $weg = @()
        } catch {
            $sammel = 'gescheitert'
            Write-Host "[WARN] Sammelaufruf gescheitert, einzeln weiter: $($_.Exception.Message)" -ForegroundColor Yellow
            $ist = Get-DgIst $Identity
            $hinzu = @($sollMenge | Where-Object { -not $ist.Contains($_) })
            $weg   = @($ist  | Where-Object { -not $sollMenge.Contains($_) })
        }
    }

    $neu = 0; $raus = 0; $fehler = 0
    foreach ($m in $hinzu) {
        try {
            Add-DistributionGroupMember -Identity $Identity -Member $m @umgehen -ErrorAction Stop
            Write-Host "[OK] Added: $m" -ForegroundColor Green
            $neu++
        } catch {
            if ($_.Exception.Message -like '*already a member*') {
                Write-Host "[OK] Bereits Mitglied: $m" -ForegroundColor Green
            } else {
                Write-Host "[ERROR] Failed to add $m : $_" -ForegroundColor Red
                $fehler++
            }
        }
    }
    foreach ($m in $weg) {
        try {
            Remove-DistributionGroupMember -Identity $Identity -Member $m @umgehen -Confirm:$false -ErrorAction Stop
            Write-Host "[OK] Removed: $m" -ForegroundColor Green
            $raus++
        } catch {
            Write-Host "[ERROR] Failed to remove $m : $_" -ForegroundColor Red
            $fehler++
        }
    }
    # Zusammenfassung mit Bezugsgroesse — eine Zeile, auch wenn nichts zu tun war.
    Write-Host "[DG-SYNC] '$Identity': Soll $($sollMenge.Count), einzeln hinzugefuegt $neu, einzeln entfernt $raus, Fehler $fehler, Sammelaufruf $sammel"
    return [pscustomobject]@{ Soll = $sollMenge.Count; Hinzu = $neu; Weg = $raus; Fehler = $fehler; Sammel = $sammel }
}
