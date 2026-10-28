[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$WorkspaceRoot)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'env.ps1') -WorkspaceRoot $WorkspaceRoot
$ipDocker=Join-Path $WorkspaceRoot '.tools/docker/resources/bin/docker.exe'
if(-not(Test-Path -LiteralPath $ipDocker)){$ipDocker=(Get-Command docker -ErrorAction Stop).Source}
$ipUv=Join-Path $WorkspaceRoot '.tools/uv/uv.exe'
if(-not(Test-Path -LiteralPath $ipUv)){$ipUv=(Get-Command uv -ErrorAction Stop).Source}
$ipCompose=@('compose','--project-directory',$env:INSIGHTPILOT_SOURCE_ROOT,'--env-file',(Join-Path $WorkspaceRoot '.env.insightpilot'),'-f',(Join-Path $env:INSIGHTPILOT_SOURCE_ROOT 'compose.yaml'))
Push-Location -LiteralPath $env:INSIGHTPILOT_SOURCE_ROOT
try {
 & $ipDocker @ipCompose up -d --wait postgres redis
 if($LASTEXITCODE -ne 0){throw 'Database startup failed'}
 & $ipUv run --frozen python -m scripts.manage --workspace-root $WorkspaceRoot --create-accounts
 if($LASTEXITCODE -ne 0){throw 'Schema initialization failed'}
 & $ipDocker @ipCompose --profile api --profile worker --profile mcp --profile web up -d --build --wait --wait-timeout 120
 if($LASTEXITCODE -ne 0){throw 'Service startup failed'}
} finally {Pop-Location}
