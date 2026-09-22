[CmdletBinding()]
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [ValidateSet('hub','agent')][string]$Role = 'hub',
    [Parameter(Mandatory=$true)][string]$Address,
    [Parameter(Mandatory=$true)][string]$HostId,
    [string]$HostName,
    [string]$HubAddress,
    [string[]]$WebAllowedFrom = @('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'),
    [switch]$Offline,
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($Root -notmatch '^[A-Za-z]:\\.+') { throw 'Use an absolute project directory, not a drive root.' }
if ($HostId -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$') { throw 'Invalid HostId.' }
$listenIp=$null
if (-not [Net.IPAddress]::TryParse($Address,[ref]$listenIp) -or $listenIp.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Address must be this server IPv4 address.' }
if (-not (Get-NetIPAddress -IPAddress $Address -ErrorAction SilentlyContinue)) { throw 'Address is not assigned to this server.' }
if ($Role -eq 'agent' -and -not $HubAddress) { throw 'Agent installation requires -HubAddress.' }
if ($HubAddress) {
    $hubIp=$null
    if (-not [Net.IPAddress]::TryParse($HubAddress,[ref]$hubIp) -or $hubIp.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $HubAddress -eq '0.0.0.0') { throw 'HubAddress must be a specific Hub IPv4 address.' }
}
foreach ($source in $WebAllowedFrom) {
    $parts=$source.Split('/')
    $sourceIp=$null
    if ($parts.Count -gt 2 -or -not [Net.IPAddress]::TryParse($parts[0],[ref]$sourceIp) -or $sourceIp.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'WebAllowedFrom must contain IPv4 addresses or CIDRs.' }
    if ($parts.Count -eq 2 -and ($parts[1] -notmatch '^\d{1,2}$' -or [int]$parts[1] -lt 1 -or [int]$parts[1] -gt 32)) { throw 'Use explicit IPv4 source ranges with prefixes 1-32.' }
}
if ($Role -eq 'hub' -and $WebAllowedFrom.Count -eq 0) { throw 'Hub installation requires at least one explicit WebAllowedFrom source.' }
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run from an elevated PowerShell.' }
# Refuse identity/address migrations before downloading dependencies or writing runtime files.
$oldAgentPath=Join-Path $Root 'config\agent.json'
$oldHubPath=Join-Path $Root 'config\hub.json'
if (Test-Path -LiteralPath $oldAgentPath) {
    $oldAgent=Get-Content -LiteralPath $oldAgentPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $expectedListen=if ($Role -eq 'hub') { '127.0.0.1' } else { $Address }
    if ($oldAgent.mode -ne 'agent' -or $oldAgent.host_id -ne $HostId -or $oldAgent.listen_host -ne $expectedListen) { throw 'Existing Agent identity/address differs; migrate configuration explicitly.' }
}
if (Test-Path -LiteralPath $oldHubPath) {
    $oldHub=Get-Content -LiteralPath $oldHubPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Role -ne 'hub' -or $oldHub.mode -ne 'hub' -or $oldHub.listen_host -ne $Address) { throw 'Existing Hub role/address differs; migrate configuration explicitly.' }
    $localServer=@($oldHub.servers | Where-Object { $_.id -eq $HostId })
    if ($localServer.Count -ne 1) { throw 'Existing Hub local server identity differs; migrate configuration explicitly.' }
}
$downloads = Join-Path $Root 'downloads'
$runtime = Join-Path $Root 'runtime'
foreach($privateDir in @('config','data')) {
    $privatePath=Join-Path $Root $privateDir
    New-Item -ItemType Directory -Path $privatePath -Force | Out-Null
    & icacls.exe $privatePath /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to restrict configuration directory.' }
}
if (-not $Offline) { & (Join-Path $PSScriptRoot 'download-dependencies.ps1') -Root $Root -Role $Role }
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
function Verify-Hash([string]$Name,[string]$Expected) {
    $actual = (Get-FileHash -LiteralPath (Join-Path $downloads $Name) -Algorithm SHA256).Hash
    if ($actual -ne $Expected) { throw "SHA256 mismatch: $Name" }
}
Verify-Hash 'windows_exporter-0.31.8-amd64.exe' '03bb0fe80b8ad0b4e39606b96c4c5cc56b1f766760011ea2b00111157c6ef077'
Verify-Hash 'python-3.13.15-embed-amd64.zip' 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf'
Verify-Hash 'WinSW-x64.exe' '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
if ($Role -eq 'hub') {
    Verify-Hash 'prometheus-3.13.3.windows-amd64.zip' 'acae8f2d71baab1acac7064a5af29c40ccb9843c1e7e32baa3d5c476ae7978c9'
    Verify-Hash 'grafana_13.2.2_windows_amd64.tar.gz' '3372d0577aa271b7d724fba7e402bc90f49653f86827900fea4a12ad67b46da5'
}
if (-not (Test-Path -LiteralPath (Join-Path $runtime 'python\python.exe'))) {
    Expand-Archive -LiteralPath (Join-Path $downloads 'python-3.13.15-embed-amd64.zip') -DestinationPath (Join-Path $runtime 'python')
}
New-Item -ItemType Directory -Path (Join-Path $runtime 'exporter') -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $runtime 'exporter\windows_exporter.exe'))) {
    Copy-Item -LiteralPath (Join-Path $downloads 'windows_exporter-0.31.8-amd64.exe') -Destination (Join-Path $runtime 'exporter\windows_exporter.exe')
}
if ($Role -eq 'hub') {
    if (-not (Test-Path -LiteralPath (Join-Path $runtime 'prometheus\prometheus.exe'))) {
        $stage = Join-Path $runtime 'prometheus-extract'
        Expand-Archive -LiteralPath (Join-Path $downloads 'prometheus-3.13.3.windows-amd64.zip') -DestinationPath $stage -Force
        New-Item -ItemType Directory -Path (Join-Path $runtime 'prometheus') -Force | Out-Null
        Get-ChildItem -LiteralPath (Join-Path $stage 'prometheus-3.13.3.windows-amd64') | Copy-Item -Destination (Join-Path $runtime 'prometheus') -Recurse -Force
    }
    if (-not (Test-Path -LiteralPath (Join-Path $runtime 'grafana\bin\grafana.exe'))) {
        $stage = Join-Path $runtime 'grafana-extract'
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        & tar.exe -xzf (Join-Path $downloads 'grafana_13.2.2_windows_amd64.tar.gz') -C $stage
        if ($LASTEXITCODE -ne 0) { throw 'Grafana extraction failed' }
        $grafanaDir = Get-ChildItem -LiteralPath $stage -Directory | Where-Object {Test-Path -LiteralPath (Join-Path $_.FullName 'bin\grafana.exe')} | Select-Object -First 1
        if (-not $grafanaDir) { throw 'Grafana archive layout not recognized' }
        New-Item -ItemType Directory -Path (Join-Path $runtime 'grafana') -Force | Out-Null
        Get-ChildItem -LiteralPath $grafanaDir.FullName | Copy-Item -Destination (Join-Path $runtime 'grafana') -Recurse -Force
    }
}
$python = Join-Path $runtime 'python\python.exe'
$renderArgs=@((Join-Path $Root 'scripts\render-config.py'),'--root',$Root,'--role',$Role,'--address',$Address,'--host-id',$HostId)
if ($HostName) { $renderArgs+=@('--host-name',$HostName) }
if ($HubAddress) { $renderArgs+=@('--hub-address',$HubAddress) }
& $python @renderArgs
if ($LASTEXITCODE -ne 0) { throw 'Configuration failed' }
# Keep generated secrets, registration databases and raw collector state private to local admins/SYSTEM.
& icacls.exe (Join-Path $Root 'config') /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
& icacls.exe (Join-Path $Root 'data') /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($PrepareOnly) { Write-Output 'Prepared runtime and configuration; services not started.'; exit 0 }
$serviceNames = Get-Content -LiteralPath (Join-Path $Root 'config\service-list.json') -Raw | ConvertFrom-Json
foreach ($serviceName in $serviceNames) {
    $wrapper = Join-Path $Root ('services\'+$serviceName+'.exe')
    if (-not (Get-Service -Name $serviceName -ErrorAction SilentlyContinue)) {
        & $wrapper install
        if ($LASTEXITCODE -ne 0) { throw "Service installation failed: $serviceName" }
    }
    if ((Get-Service -Name $serviceName).Status -ne 'Running') { Start-Service -Name $serviceName }
}
if ($Role -eq 'hub') {
    if (-not (Get-NetFirewallRule -Name 'LabMonWebLAN' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'LabMonWebLAN' -DisplayName 'Lab Monitor LAN web 8766' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8766 -LocalAddress $Address -RemoteAddress $WebAllowedFrom -Profile Any | Out-Null
    }
} else {
    if (-not (Get-NetFirewallRule -Name 'LabMonAgentHub' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'LabMonAgentHub' -DisplayName 'Lab Monitor agent from hub' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8767 -LocalAddress $Address -RemoteAddress $HubAddress -Profile Any | Out-Null
    }
}
Get-Service -Name $serviceNames | Select-Object Name,Status,StartType
