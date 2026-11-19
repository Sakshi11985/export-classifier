# Windows PowerShell version of scripts/set_llm.sh - switch the reasoning backend in .env and restart api + worker.
#
#   powershell -ExecutionPolicy Bypass -File scripts\set_llm.ps1 offline   # default: deterministic policy, no key
#   powershell -ExecutionPolicy Bypass -File scripts\set_llm.ps1 stub      # load test + circuit-breaker demo ONLY
#   powershell -ExecutionPolicy Bypass -File scripts\set_llm.ps1 real      # copies REAL_LLM_* into LLM_*
#   add -NoRestart to only edit .env
#
# If this script is blocked or misbehaves, do the same by hand: edit .env in Notepad (the keys are listed in
# README section 7), then run:  docker compose up -d --no-deps --force-recreate --scale worker=3 api worker
param(
    [Parameter(Mandatory = $true)][ValidateSet('offline', 'stub', 'real')][string]$Mode,
    [switch]$NoRestart
)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }

$envPath = Join-Path (Get-Location) '.env'
$lines = New-Object System.Collections.Generic.List[string]
foreach ($l in [System.IO.File]::ReadAllLines($envPath)) { $lines.Add($l.TrimEnd("`r")) }

function Get-Kv([string]$k) {
    $v = ''
    foreach ($l in $lines) { if ($l.StartsWith("$k=")) { $v = $l.Substring($k.Length + 1) } }
    return $v
}
function Set-Kv([string]$k, [string]$v) {
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].StartsWith("$k=")) { $lines[$i] = "$k=$v"; return }
    }
    $lines.Add("$k=$v")
}

switch ($Mode) {
    'offline' { Set-Kv 'LLM_PROVIDER' 'offline' }
    'stub' {
        Set-Kv 'LLM_PROVIDER' 'openai_compat'; Set-Kv 'LLM_BASE_URL' 'http://llm-stub:8080/v1'; Set-Kv 'LLM_API_KEY' ''
        Set-Kv 'LLM_MODEL_CLASSIFIER' 'stub-model'; Set-Kv 'LLM_MODEL_VERIFIER' 'stub-model'
        Set-Kv 'LLM_FALLBACK_BASE_URL' ''; Set-Kv 'LLM_FALLBACK_API_KEY' ''; Set-Kv 'LLM_FALLBACK_MODEL' ''
    }
    'real' {
        $url = Get-Kv 'REAL_LLM_BASE_URL'; $key = Get-Kv 'REAL_LLM_API_KEY'; $model = Get-Kv 'REAL_LLM_MODEL'
        if (-not $url -or -not $key -or -not $model) {
            Write-Error 'Fill REAL_LLM_BASE_URL, REAL_LLM_API_KEY and REAL_LLM_MODEL in .env first (README section 7).'
        }
        $verifier = Get-Kv 'REAL_LLM_VERIFIER_MODEL'; if (-not $verifier) { $verifier = $model }
        Set-Kv 'LLM_PROVIDER' 'openai_compat'; Set-Kv 'LLM_BASE_URL' $url; Set-Kv 'LLM_API_KEY' $key
        Set-Kv 'LLM_MODEL_CLASSIFIER' $model; Set-Kv 'LLM_MODEL_VERIFIER' $verifier
        Set-Kv 'LLM_FALLBACK_BASE_URL' (Get-Kv 'REAL_LLM_FALLBACK_BASE_URL')
        Set-Kv 'LLM_FALLBACK_API_KEY' (Get-Kv 'REAL_LLM_FALLBACK_API_KEY')
        Set-Kv 'LLM_FALLBACK_MODEL' (Get-Kv 'REAL_LLM_FALLBACK_MODEL')
    }
}

# write back with LF line endings and no byte-order mark (what docker compose and the Linux containers expect)
[System.IO.File]::WriteAllText($envPath, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("active backend: LLM_PROVIDER={0}  base_url={1}  model={2}" -f (Get-Kv 'LLM_PROVIDER'), (Get-Kv 'LLM_BASE_URL'), (Get-Kv 'LLM_MODEL_CLASSIFIER'))

if (-not $NoRestart) {
    $n = @(docker compose ps -q worker).Where({ $_ }).Count
    if ($n -lt 1) { $n = 1 }
    docker compose up -d --no-deps --force-recreate --scale "worker=$n" api worker
    Write-Host 'restarted api + worker. Check:  docker compose exec api python scripts/demo.py health'
}
