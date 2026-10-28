[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$WorkspaceRoot,
      [string]$Node='node', [string]$Npm='npm', [string]$BrowserExecutable,
      [switch]$BuildImages)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'env.ps1') -WorkspaceRoot $WorkspaceRoot
$ipUv=Join-Path $WorkspaceRoot '.tools/uv/uv.exe'
if(-not(Test-Path -LiteralPath $ipUv)){$ipUv=(Get-Command uv -ErrorAction Stop).Source}
function Invoke-Checked {param([string]$Program,[string[]]$Arguments)
 & $Program @Arguments
 if($LASTEXITCODE -ne 0){throw "Verification command failed: $Program"}
}
Push-Location -LiteralPath $env:INSIGHTPILOT_SOURCE_ROOT
try {
 Invoke-Checked $ipUv @('sync','--all-groups','--frozen')
 Invoke-Checked $ipUv @('run','ruff','check','src','tests','scripts','evals')
 Invoke-Checked $ipUv @('run','ruff','format','--check','src','tests','scripts','evals')
 Invoke-Checked $ipUv @('run','pytest','-q','--tb=short')
 $env:npm_config_cache=Join-Path $WorkspaceRoot '.cache/npm'
 Invoke-Checked $Npm @('--prefix','frontend','ci')
 Invoke-Checked $Npm @('--prefix','frontend','run','test','--','--run')
 Invoke-Checked $Npm @('--prefix','frontend','run','typecheck')
 Invoke-Checked $Npm @('--prefix','frontend','run','build')
 if(-not $BrowserExecutable){$BrowserExecutable='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'}
 if(-not(Test-Path -LiteralPath $BrowserExecutable)){throw 'Provide an existing browser executable'}
 $env:IP_BROWSER_NODE=(Get-Command $Node -ErrorAction Stop).Source
 $env:IP_BROWSER_PLAYWRIGHT=Join-Path $env:INSIGHTPILOT_SOURCE_ROOT 'frontend/node_modules/playwright/index.mjs'
 $env:IP_BROWSER_EXECUTABLE=$BrowserExecutable
 Invoke-Checked $ipUv @('run','pytest','tests/browser/check_workbench.py','-q','--tb=short')
 if($BuildImages){
  $ipDocker=Join-Path $WorkspaceRoot '.tools/docker/resources/bin/docker.exe'
  if(-not(Test-Path -LiteralPath $ipDocker)){$ipDocker=(Get-Command docker -ErrorAction Stop).Source}
  Invoke-Checked $ipDocker @('build','-t','insightpilot-app:local','.')
  Invoke-Checked $ipDocker @('build','-f','frontend/Dockerfile','-t','insightpilot-web:local','.')
 }
} finally {Pop-Location}
