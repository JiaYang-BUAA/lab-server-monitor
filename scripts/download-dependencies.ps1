[CmdletBinding()]
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [ValidateSet('hub','agent')][string]$Role = 'agent'
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\assets\windows-dependencies.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$downloads=Join-Path $Root 'downloads'
New-Item -ItemType Directory -Path $downloads -Force | Out-Null
foreach($item in $manifest | Where-Object { $_.roles -contains $Role }) {
    $target=Join-Path $downloads $item.file
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $item.sha256) { throw "Existing dependency checksum mismatch: $($item.file)" }
        Write-Output "Verified cached dependency: $($item.file)"
        continue
    }
    $partial=$target+'.partial'
    Write-Output "Downloading official dependency: $($item.file)"
    Invoke-WebRequest -UseBasicParsing -Uri $item.url -OutFile $partial
    if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $item.sha256) { throw "Downloaded dependency checksum mismatch: $($item.file)" }
    Move-Item -LiteralPath $partial -Destination $target
}
