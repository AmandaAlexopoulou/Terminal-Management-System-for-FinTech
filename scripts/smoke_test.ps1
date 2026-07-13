<#
.SYNOPSIS
    Smoke test for the whole TMS API - covers Feature A, B, C, D and all
    3 bonus items (CSV report, cache HIT/MISS, cron container) in one run.

.DESCRIPTION
    Prerequisite: the docker compose stack must already be up
    (docker compose up --build) before running this script.

    Safe to re-run multiple times: each run creates a NEW terminal via
    from-template (never reuses the same tid), so it never collides with
    terminals created by a previous run.

    NOTE: this file is intentionally written in plain ASCII (no Greek/
    accented characters). Windows PowerShell 5.1 reads .ps1 files without
    a UTF-8 BOM using the system codepage, which corrupts non-ASCII
    characters and breaks parsing entirely - ASCII-only avoids that
    problem regardless of PowerShell version or console codepage.

.EXAMPLE
    .\scripts\smoke_test.ps1
#>

$ErrorActionPreference = "Stop"
$base = "http://localhost:5000"
$failCount = 0

function Test-Step {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
    Write-Host "`n--- $Name ---" -ForegroundColor Cyan
    try {
        & $Action
        Write-Host "OK: $Name" -ForegroundColor Green
    } catch {
        Write-Host "FAIL: $Name -> $($_.Exception.Message)" -ForegroundColor Red
        $script:failCount++
    }
}

function Assert-HttpError {
    <#
        Runs $Action and asserts it throws an HTTP error with EXACTLY
        $ExpectedStatus. We read the real $_.Exception.Response.StatusCode
        instead of searching for the number inside the exception message
        text - the same property path works on both Windows PowerShell 5.1
        (System.Net.WebException) and PowerShell 7+ (HttpResponseException),
        while the exact message text differs between versions (as seen in
        an earlier real session, where the message was just the JSON body,
        with no "409" anywhere in it).
    #>
    param(
        [scriptblock]$Action,
        [int]$ExpectedStatus
    )
    try {
        & $Action | Out-Null
        throw "expected HTTP $ExpectedStatus but the call succeeded (2xx)"
    } catch {
        $actual = $null
        try { $actual = [int]$_.Exception.Response.StatusCode } catch { }

        if ($null -eq $actual) {
            # Could not read a status code (e.g. a network problem, not an
            # HTTP error) - rethrow the original exception.
            throw
        }
        if ($actual -ne $ExpectedStatus) {
            throw "expected HTTP $ExpectedStatus but got HTTP $actual"
        }
        Write-Host "  correctly returned HTTP $ExpectedStatus"
    }
}

# ============================================================================
# Technical requirement - /health
# ============================================================================
Test-Step "Health check" {
    $r = Invoke-RestMethod -Uri "$base/health"
    if ($r.status -ne "ok") { throw "status = $($r.status) (expected 'ok')" }
    Write-Host "  database=$($r.components.database) redis=$($r.components.redis)"
}

# ============================================================================
# Feature A - Terminals
# ============================================================================
Test-Step "A1: GET /terminals" {
    $r = Invoke-RestMethod -Uri "$base/terminals"
    Write-Host "  terminals: $($r.Count)"
}

Test-Step "A1: GET /terminals?enabled=true" {
    $r = Invoke-RestMethod -Uri "$base/terminals?enabled=true"
    Write-Host "  enabled terminals: $($r.Count)"
}

Test-Step "A1: invalid 'enabled' value -> 400" {
    Assert-HttpError -ExpectedStatus 400 -Action {
        Invoke-RestMethod -Uri "$base/terminals?enabled=maybe"
    }
}

Test-Step "A2: GET /terminals/<tid> - unknown tid -> 404" {
    Assert-HttpError -ExpectedStatus 404 -Action {
        Invoke-RestMethod -Uri "$base/terminals/DOESNOTEXIST"
    }
}

Test-Step "A3: GET /terminals/flagged" {
    $r = Invoke-RestMethod -Uri "$base/terminals/flagged"
    Write-Host "  flagged terminals: $($r.Count)"
}

# ============================================================================
# Feature B - Templates (+ create a new terminal used by the next steps)
# ============================================================================
Test-Step "B1: GET /templates" {
    $r = Invoke-RestMethod -Uri "$base/templates"
    if ($r.Count -lt 1) { throw "no templates found" }
    Write-Host "  templates: $($r.Count)"
    $script:firstTemplateId = $r[0].template_id
}

Test-Step "B2: GET /templates/<id>" {
    $r = Invoke-RestMethod -Uri "$base/templates/$firstTemplateId"
    Write-Host "  template #$firstTemplateId -> $($r.hardware_model)"
}

Test-Step "B2: GET /templates/<id> - unknown id -> 404" {
    Assert-HttpError -ExpectedStatus 404 -Action {
        Invoke-RestMethod -Uri "$base/templates/999999"
    }
}

Test-Step "B3: POST /terminals/from-template" {
    $body = @{ template_id = $firstTemplateId; mid = "MID000101" } | ConvertTo-Json
    $r = Invoke-RestMethod -Uri "$base/terminals/from-template" -Method Post -ContentType "application/json" -Body $body
    $script:newTid = $r.tid
    Write-Host "  created terminal: $newTid"
}

Test-Step "B3: POST /terminals/from-template - unknown mid -> 404" {
    Assert-HttpError -ExpectedStatus 404 -Action {
        $body = @{ template_id = $firstTemplateId; mid = "MID000999" } | ConvertTo-Json
        Invoke-RestMethod -Uri "$base/terminals/from-template" -Method Post -ContentType "application/json" -Body $body
    }
}

# ============================================================================
# Feature A4/A5 - using the terminal we just created
# ============================================================================
Test-Step "A4: POST /terminals/<tid>/flag" {
    $body = @{ scenario_number = "7" } | ConvertTo-Json
    $r = Invoke-RestMethod -Uri "$base/terminals/$newTid/flag" -Method Post -ContentType "application/json" -Body $body
    if ($r.scenario_number -ne "7") { throw "unexpected scenario_number: $($r.scenario_number)" }
}

Test-Step "A4: POST /terminals/<tid>/flag - missing scenario_number -> 400" {
    Assert-HttpError -ExpectedStatus 400 -Action {
        Invoke-RestMethod -Uri "$base/terminals/$newTid/flag" -Method Post -ContentType "application/json" -Body "{}"
    }
}

Test-Step "A4: POST /terminals/<tid>/unflag" {
    $r = Invoke-RestMethod -Uri "$base/terminals/$newTid/unflag" -Method Post
    if ($r.scenario_number -ne "0") { throw "unexpected scenario_number: $($r.scenario_number)" }
}

Test-Step "A5: POST /terminals/<tid>/decommission" {
    $r = Invoke-RestMethod -Uri "$base/terminals/$newTid/decommission" -Method Post
    Write-Host "  queued_on=$($r.queued_on) delete_after=$($r.delete_after)"
}

Test-Step "A5: second decommission -> 409" {
    Assert-HttpError -ExpectedStatus 409 -Action {
        Invoke-RestMethod -Uri "$base/terminals/$newTid/decommission" -Method Post
    }
}

Test-Step "A5: GET /terminals/decommissioned" {
    $r = Invoke-RestMethod -Uri "$base/terminals/decommissioned"
    $found = $r | Where-Object { $_.tid -eq $newTid }
    if (-not $found) { throw "$newTid was not found in the decommission queue" }
    Write-Host "  $newTid found in queue, days_remaining=$($found.days_remaining)"
}

# ============================================================================
# Feature D - Statistics
# ============================================================================
Test-Step "D1: GET /statistics/by-hardware" {
    $r = Invoke-RestMethod -Uri "$base/statistics/by-hardware"
    Write-Host "  $($r.data.Count) hardware model(s)"
}

Test-Step "D2: GET /statistics/by-state" {
    $r = Invoke-RestMethod -Uri "$base/statistics/by-state"
    Write-Host "  active=$($r.active) inactive=$($r.inactive) total=$($r.total)"
    if ($r.active + $r.inactive -ne $r.total) { throw "active+inactive != total" }
}

Test-Step "D3: GET /statistics/by-hardware-family" {
    $r = Invoke-RestMethod -Uri "$base/statistics/by-hardware-family"
    Write-Host "  $($r.data.Count) hardware family(ies)"
}

Test-Step "D4: GET /statistics/idle-distribution" {
    $r = Invoke-RestMethod -Uri "$base/statistics/idle-distribution"
    Write-Host "  $($r.data.Count) bucket(s)"
}

# ============================================================================
# Bonus - CSV report
# ============================================================================
Test-Step "Bonus: GET /reports/terminals-basic (CSV download)" {
    $outFile = Join-Path $env:TEMP "terminals_basic.csv"
    Invoke-WebRequest -Uri "$base/reports/terminals-basic" -OutFile $outFile -UseBasicParsing
    $size = (Get-Item $outFile).Length
    if ($size -le 0) { throw "downloaded CSV is empty" }
    Write-Host "  saved: $outFile ($size bytes)"
}

# ============================================================================
# Bonus - Feature C cache HIT/MISS (checks the container logs)
# ============================================================================
Test-Step "Bonus: Feature C cache HIT (via tms-api logs)" {
    Invoke-RestMethod -Uri "$base/terminals" | Out-Null   # first call -> MISS
    Start-Sleep -Seconds 1
    Invoke-RestMethod -Uri "$base/terminals" | Out-Null   # second call -> HIT (TTL 30s)
    Start-Sleep -Seconds 1
    $logs = docker compose logs --tail=100 tms-api 2>$null
    $hits = $logs | Select-String "Cache HIT"
    if (-not $hits) { throw "no 'Cache HIT' found in recent logs - check manually with 'docker compose logs tms-api'" }
    Write-Host "  found $(@($hits).Count) 'Cache HIT' line(s) in recent logs"
}

# ============================================================================
# Bonus - cron container (just confirms the container is up)
# ============================================================================
Test-Step "Bonus: tms-cron container is up" {
    $psOutput = docker compose ps tms-cron 2>$null
    if (-not ($psOutput -match "tms-cron")) {
        throw "tms-cron container not found - run 'docker compose up --build'"
    }
    Write-Host "  tms-cron container: OK (see 'docker compose logs tms-cron' for cleanup logs)"
}

# ============================================================================
# Summary
# ============================================================================
Write-Host "`n===================================" -ForegroundColor Cyan
if ($failCount -eq 0) {
    Write-Host "ALL TESTS PASSED" -ForegroundColor Green
} else {
    Write-Host "$failCount test(s) FAILED - see FAIL lines above" -ForegroundColor Red
}
Write-Host "===================================" -ForegroundColor Cyan
