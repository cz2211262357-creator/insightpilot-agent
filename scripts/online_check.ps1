[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$WorkspaceRoot)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'env.ps1') -WorkspaceRoot $WorkspaceRoot
$ipUv=Join-Path $WorkspaceRoot '.tools/uv/uv.exe'
if(-not(Test-Path -LiteralPath $ipUv)){$ipUv=(Get-Command uv -ErrorAction Stop).Source}
Push-Location -LiteralPath $env:INSIGHTPILOT_SOURCE_ROOT
try {
 & $ipUv run --frozen python -m scripts.online_check --workspace-root $WorkspaceRoot --report (Join-Path $WorkspaceRoot '.cache/insightpilot-verification/online-checks.json')
 if($LASTEXITCODE -ne 0){throw 'Online acceptance failed; inspect sanitized result'}
} finally {Pop-Location}
