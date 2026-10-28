[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$WorkspaceRoot)
$ErrorActionPreference='Stop'
if(-not [IO.Path]::IsPathRooted($WorkspaceRoot)){throw 'WorkspaceRoot must be absolute.'}
$ipPhysicalRoot=[IO.Path]::GetFullPath($WorkspaceRoot)
$ipSourceRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if($ipSourceRoot -ne $ipPhysicalRoot -and -not $ipSourceRoot.StartsWith($ipPhysicalRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Source must remain inside the requested workspace.'}
$env:INSIGHTPILOT_WORKSPACE_ROOT=$ipPhysicalRoot
$env:INSIGHTPILOT_SOURCE_ROOT=$ipSourceRoot
$env:UV_CACHE_DIR=Join-Path $ipPhysicalRoot '.cache/uv'
$env:UV_PYTHON_INSTALL_DIR=Join-Path $ipPhysicalRoot '.python'
$env:UV_PROJECT_ENVIRONMENT=Join-Path $ipSourceRoot '.venv'
$env:UV_PYTHON_PREFERENCE='only-managed'
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:TEMP=Join-Path $ipPhysicalRoot '.cache/insightpilot-tmp'
$env:TMP=$env:TEMP
foreach($ipDirectory in @($env:TEMP,$env:UV_CACHE_DIR,(Join-Path $ipPhysicalRoot '.cache/insightpilot-verification'),(Join-Path $ipPhysicalRoot '.data/insightpilot'))){New-Item -ItemType Directory -Path $ipDirectory -Force|Out-Null}
$ipPrivateEnv=Join-Path $ipPhysicalRoot '.env.insightpilot'
if(Test-Path -LiteralPath $ipPrivateEnv){
 foreach($ipLine in [IO.File]::ReadAllLines($ipPrivateEnv)){
  if($ipLine -match '^\s*(INSIGHTPILOT_[A-Z0-9_]+)\s*=\s*(.*?)\s*$'){
   $ipName=$Matches[1];$ipValue=$Matches[2]
   if($ipValue.Length -ge 2 -and (($ipValue.StartsWith('"') -and $ipValue.EndsWith('"')) -or ($ipValue.StartsWith("'") -and $ipValue.EndsWith("'")))){$ipValue=$ipValue.Substring(1,$ipValue.Length-2)}
   [Environment]::SetEnvironmentVariable($ipName,$ipValue,'Process')
  }
 }
}
$env:PATH=(Join-Path $ipPhysicalRoot '.tools/uv')+';'+$env:PATH

$env:LANGGRAPH_STRICT_MSGPACK = 'true'
