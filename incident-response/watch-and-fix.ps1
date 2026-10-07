<#
Host-side incident watcher (Windows).

Polls incident-response/incidents for new REAL (non-drill) alerts saved by
the responder container, and dispatches each one to a genuine headless
OpenCode run (`opencode.cmd run`, no TTY) with the scoped prompt from
agent-prompt.md. The container itself cannot fix code (it has no repo
access); this host watcher closes the Q5/Q6 loop where the repo lives.

Usage:
  powershell -ExecutionPolicy Bypass -File incident-response\watch-and-fix.ps1 -Once
  powershell -ExecutionPolicy Bypass -File incident-response\watch-and-fix.ps1 -Loop -IntervalSeconds 60
#>
param(
    [switch]$Once,
    [switch]$Loop,
    [int]$IntervalSeconds = 60,
    [string]$Incident = ""
)

$ErrorActionPreference = "Stop"
$ResponderDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $ResponderDir
$IncidentsDir = Join-Path $ResponderDir "incidents"
$PromptTemplate = Join-Path $ResponderDir "agent-prompt.md"

function Get-PendingIncidents {
    Get-ChildItem -Path $IncidentsDir -Directory | Where-Object {
        $_.Name -ne ".gitkeep" -and
        -not (Test-Path (Join-Path $_.FullName "host_agent_result.md")) -and
        (Test-Path (Join-Path $_.FullName "alert.json"))
    } | ForEach-Object {
        $dir = $_
        $alert = Get-Content (Join-Path $dir.FullName "alert.json") -Raw | ConvertFrom-Json
        $alertName = [string]$alert.labels.alertname
        $testFlag = [string]$alert.labels.test
        $isDrill = ($testFlag -eq "true") -or ($alertName -eq "ResponderTest")
        $wanted = ([string]$Incident -eq "") -or ($dir.Name -eq [string]$Incident)
        if ((-not $isDrill) -and $wanted) { $dir }
    }
}

function Invoke-IncidentAgent($IncidentDir) {
    $prompt = (Get-Content $PromptTemplate -Raw) -replace "\{\{INCIDENT_DIR\}\}", $IncidentDir.FullName
    $promptFile = Join-Path $IncidentDir.FullName "host_agent_prompt.md"
    $prompt | Set-Content -Path $promptFile -NoNewline
    $alert = Get-Content (Join-Path $IncidentDir.FullName "alert.json") -Raw | ConvertFrom-Json
    $alertName = $alert.labels.alertname
    $dispatch = [ordered]@{
        incident   = $IncidentDir.Name
        cmd       = "opencode.cmd run (headless, --dir repo root, prompt + alert attachments)"
        startedAt = (Get-Date).ToUniversalTime().ToString("o")
    }
    $dispatch | ConvertTo-Json | Set-Content (Join-Path $IncidentDir.FullName "host_agent_dispatch.json")
    Write-Host "Dispatching headless agent for $($IncidentDir.Name) ..."
    Push-Location $RepoRoot
    $prevErr = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $promptFile = Join-Path $IncidentDir.FullName "host_agent_prompt.md"
        $alertFile = Join-Path $IncidentDir.FullName "alert.json"
        $contextFile = Join-Path $IncidentDir.FullName "context.json"
        $out = & opencode.cmd run "Investigate this Order Tracker incident per the attached prompt file." -f $promptFile -f $alertFile -f $contextFile 2>&1
        $code = $LASTEXITCODE
        $out | Out-File (Join-Path $IncidentDir.FullName "host_agent_stdout.log") -Append
        if ($code -ne 0) {
            Write-Host "Headless run exited with code $code; see host_agent_stdout.log"
        }
    }
    finally {
        $ErrorActionPreference = $prevErr
        Pop-Location
    }
    Write-Host "Headless run finished for $($IncidentDir.Name). See host_agent_result.md."
}

do {
    $pending = @(Get-PendingIncidents)
    if ($pending.Count -eq 0) { Write-Host "No pending incidents." }
    foreach ($pendingItem in $pending) {
        if ($null -eq $pendingItem) { Write-Host "WARNING: null entry skipped"; continue }
        Invoke-IncidentAgent $pendingItem
    }
    if ($Loop -and -not $Once) { Start-Sleep -Seconds $IntervalSeconds }
} while ($Loop -and -not $Once)
