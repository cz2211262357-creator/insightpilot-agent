[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$WorkspaceRoot)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'env.ps1') -WorkspaceRoot $WorkspaceRoot
$ipToolLock=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'toolchain.lock.json') -Raw|ConvertFrom-Json
$ipUv=Join-Path $env:INSIGHTPILOT_WORKSPACE_ROOT '.tools/uv/uv.exe'
if(-not(Test-Path -LiteralPath $ipUv)){
 New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($ipUv)) -Force|Out-Null
 $ipArchive=Join-Path $env:INSIGHTPILOT_WORKSPACE_ROOT '.cache/insightpilot-tmp/uv-win.whl'
 Invoke-WebRequest -Uri $ipToolLock.uv.url -OutFile $ipArchive
 if((Get-FileHash -LiteralPath $ipArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ipToolLock.uv.archive_sha256){throw 'uv archive checksum mismatch.'}
 Add-Type -AssemblyName System.IO.Compression.FileSystem
 $ipZip=[IO.Compression.ZipFile]::OpenRead($ipArchive)
 try{
  $ipEntry=@($ipZip.Entries|Where-Object {$_.FullName.EndsWith('/uv.exe')})
  if($ipEntry.Count -ne 1){throw 'uv executable archive entry is ambiguous.'}
  [IO.Compression.ZipFileExtensions]::ExtractToFile($ipEntry[0],$ipUv,$false)
 }finally{$ipZip.Dispose()}
}
if((Get-FileHash -LiteralPath $ipUv -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ipToolLock.uv.binary_sha256){throw 'uv executable checksum mismatch.'}
& $ipUv python install $ipToolLock.python --no-bin --no-registry
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
& $ipUv sync --project $env:INSIGHTPILOT_SOURCE_ROOT --all-groups --frozen
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
& $ipUv run --project $env:INSIGHTPILOT_SOURCE_ROOT --frozen python (Join-Path $PSScriptRoot 'bootstrap.py') --workspace-root $env:INSIGHTPILOT_WORKSPACE_ROOT
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
. (Join-Path $PSScriptRoot 'env.ps1') -WorkspaceRoot $WorkspaceRoot
