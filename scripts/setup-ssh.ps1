#Requires -Version 5.1
<## Configure an existing LOCAL account. Run -DryRun first; see docs/ssh-setup.md. ##>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$UserName,
    [Parameter(Mandatory=$true)][string]$PublicKeyFile,
    [Parameter(Mandatory=$true)][string[]]$AllowFrom,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

function Read-Ed25519PublicKey([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 8192) {
        throw 'PublicKeyFile must be a regular file, not a link, of at most 8192 bytes.'
    }
    $content = [IO.File]::ReadAllText($item.FullName, [Text.UTF8Encoding]::new($false, $true))
    $lines = @($content -split '\r?\n' | Where-Object { $_.Trim().Length -gt 0 })
    if ($lines.Count -ne 1 -or $lines[0] -notmatch '^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [^\x00-\x1f\x7f]*)?$') {
        throw 'Provide exactly one ssh-ed25519 public key, with no authorized_keys options or private key.'
    }
    $blob = $Matches[1]
    try { $bytes = [Convert]::FromBase64String($blob) } catch { throw 'Invalid public-key base64.' }
    $prefix = [byte[]](0,0,0,11,115,115,104,45,101,100,50,53,53,49,57,0,0,0,32)
    if ($bytes.Length -ne 51 -or [Convert]::ToBase64String($bytes) -cne $blob) { throw 'Invalid Ed25519 key packet.' }
    for ($i=0; $i -lt $prefix.Length; $i++) {
        if ($bytes[$i] -ne $prefix[$i]) { throw 'Invalid Ed25519 key packet.' }
    }
    return $lines[0]
}

function ConvertTo-SourceNetwork([string]$Value) {
    if ($Value -notmatch '^([0-9A-Fa-f:.]+)(?:/(\d{1,3}))?$') { throw "Invalid source IP/CIDR: $Value" }
    $addressText = $Matches[1]; $prefixText = [string]$Matches[2]
    if ($addressText -notmatch ':' -and $addressText -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw 'IPv4 must use dotted-quad notation.' }
    $ip = $null
    if (-not [Net.IPAddress]::TryParse($addressText, [ref]$ip)) { throw "Invalid source IP: $addressText" }
    $bytes = $ip.GetAddressBytes(); $bits = $bytes.Length * 8
    $prefixLength = $bits
    if ($prefixText -ne '') { $prefixLength = [int]$prefixText }
    if ($prefixLength -lt 1 -or $prefixLength -gt $bits -or $ip.IsIPv6Multicast -or $ip.IsIPv4MappedToIPv6 -or
        ($bits -eq 32 -and $bytes[0] -ge 224) -or $ip.Equals([Net.IPAddress]::Any) -or $ip.Equals([Net.IPAddress]::IPv6Any)) {
        throw 'Use a specific unicast IP/CIDR; Any, unspecified, multicast and /0 are not accepted.'
    }
    for ($i=0; $i -lt $bytes.Length; $i++) {
        $remaining = $prefixLength - 8*$i
        if ($remaining -le 0) { $bytes[$i] = 0 }
        elseif ($remaining -lt 8) { $bytes[$i] = $bytes[$i] -band (256 - [math]::Pow(2, 8-$remaining)) }
    }
    $network = [Net.IPAddress]::new($bytes).ToString()
    return "$network/$prefixLength"
}

function Assert-NoReparsePath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $pathItem = Get-Item -LiteralPath $current -Force
            if (($pathItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $pathItem.LinkType -eq 'HardLink') {
                throw "Refusing a symlink/junction/reparse path: $current"
            }
        }
        $parent = [IO.Directory]::GetParent($current)
        if (-not $parent) { break }
        $current = $parent.FullName
    }
}

function Set-KeyAcl([string]$Path, [bool]$Directory, [string]$OwnerSid, [string[]]$AllowedSids) {
    if ($Directory) { $acl = [Security.AccessControl.DirectorySecurity]::new() }
    else { $acl = [Security.AccessControl.FileSecurity]::new() }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sidText in $AllowedSids) {
        $sid = [Security.Principal.SecurityIdentifier]::new($sidText)
        if ($Directory) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        } else { $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow') }
        $acl.AddAccessRule($rule)
    }
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new($OwnerSid))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Read-EffectiveConfig([string]$Sshd, [string]$Config, [string]$User, [string]$Address) {
    $lines = @(& $Sshd -T -f $Config -C "user=$($User.ToLowerInvariant()),host=$env:COMPUTERNAME,addr=$Address" 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "sshd configuration inspection failed: $($lines -join ' ')" }
    return $lines
}

function Assert-KeyLocation([object[]]$Lines, [bool]$AdminAccount) {
    $entry = @($Lines | Where-Object { "$_" -match '^authorizedkeysfile ' })
    if ($entry.Count -ne 1) { throw 'Could not determine AuthorizedKeysFile.' }
    $paths = ("$($entry[0])" -replace '^authorizedkeysfile ', '').Trim() -split '\s+'
    $expected = '.ssh/authorized_keys'
    if ($AdminAccount) { $expected = '__PROGRAMDATA__/ssh/administrators_authorized_keys' }
    if (-not @($paths | Where-Object { $_.Replace('\','/').Trim('"') -ieq $expected }).Count) {
        throw "Custom AuthorizedKeysFile is in use. This script only supports the standard layout ($expected); no configuration is overwritten."
    }
    if (@($Lines | Where-Object { "$_" -eq 'pubkeyauthentication no' }).Count) { throw 'Public-key login is disabled by existing policy; review it manually.' }
}

# All input checks precede installation or file/firewall changes.
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell 5.1 or PowerShell 7 on Windows.' }
if ($UserName -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$') { throw 'Use an existing local account name (letters, digits, underscore, dot, hyphen); domain/Entra accounts are not supported.' }
$keyLine = Read-Ed25519PublicKey $PublicKeyFile
$sources = @($AllowFrom | ForEach-Object { ConvertTo-SourceNetwork $_ } | Sort-Object -Unique)
if ($sources.Count -eq 0) { throw 'At least one source IP/CIDR is required.' }
$account = Get-LocalUser -Name $UserName
if (-not $account.Enabled) { throw 'The target local account is disabled.' }
$accountSid = $account.SID.Value
$adminSid = 'S-1-5-32-544'; $systemSid = 'S-1-5-18'
$adminMembers = @(Get-LocalGroupMember -SID ([Security.Principal.SecurityIdentifier]::new($adminSid)))
$isAdminAccount = @($adminMembers | Where-Object { $_.SID.Value -eq $accountSid }).Count -gt 0
$sshDirectory = Join-Path $env:ProgramData 'ssh'
$config = Join-Path $sshDirectory 'sshd_config'
$openSshDirectory = Join-Path $env:WINDIR 'System32\OpenSSH'
$sshd = Join-Path $openSshDirectory 'sshd.exe'
$keygen = Join-Path $openSshDirectory 'ssh-keygen.exe'
$keyDirectory = $sshDirectory
$keysPath = Join-Path $keyDirectory 'administrators_authorized_keys'
if (-not $isAdminAccount) {
    $profile = Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$accountSid" -Name ProfileImagePath
    $profilePath = [Environment]::ExpandEnvironmentVariables($profile.ProfileImagePath)
    if (-not (Test-Path -LiteralPath $profilePath -PathType Container)) { throw 'Log in locally once to create the target account profile first.' }
    $keyDirectory = Join-Path $profilePath '.ssh'
    $keysPath = Join-Path $keyDirectory 'authorized_keys'
}
Assert-NoReparsePath $keysPath
Assert-NoReparsePath $config
$serviceBefore = Get-CimInstance Win32_Service -Filter "Name='sshd'"
if ($serviceBefore -and $serviceBefore.PathName.Trim('"') -ine $sshd) {
    throw 'This script supports the official default Windows OpenSSH service only; custom service binaries/arguments are preserved for manual setup.'
}
$ports = @(22)
if ((Test-Path -LiteralPath $sshd) -and (Test-Path -LiteralPath $config)) {
    foreach ($source in $sources) {
        $effective = @(Read-EffectiveConfig $sshd $config $UserName ($source.Split('/')[0]))
        Assert-KeyLocation $effective $isAdminAccount
    }
    $ports = @($effective | Where-Object { "$_" -match '^port \d+$' } | ForEach-Object { [int]("$_" -split ' ')[1] } | Sort-Object -Unique)
    if ($ports.Count -eq 0) { throw 'No SSH ports were reported.' }
}
Write-Host "Target: $env:COMPUTERNAME ; local user: $UserName ; administrator group: $isAdminAccount"
Write-Host "Authorized file: $keysPath"
Write-Host "Source networks: $($sources -join ', ') ; configured ports: $($ports -join ', ')"
Write-Host 'Plan: official Windows capability if missing, append key if absent, restrict key ACL, add scoped firewall rule, validate sshd, start only if stopped.'
Write-Host 'Existing SSH configuration, authentication methods, custom ports and existing firewall rules are preserved. Other allow rules may still allow other sources.'
if ($DryRun) { Write-Host 'DRY RUN: no changes made. Ports for a missing server will be checked after installation.'; return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this script in an elevated administrator PowerShell window.' }

$defaultFirewallExisted = [bool](Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)
$capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if (-not $capability) { throw 'OpenSSH Server capability unavailable. Supported: Windows 10 1809+/11 and Server 2019+.' }
if ($capability.State -ne 'Installed') {
    $installed = Add-WindowsCapability -Online -Name $capability.Name
    # A newly generated default allow rule must not leave unrestricted access.
    if (-not $defaultFirewallExisted) {
        Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule | Out-Null
    }
    if ($installed.RestartNeeded) { throw 'Windows requests a restart. This script does not restart the machine; schedule it safely and rerun afterward.' }
}
if (-not (Test-Path -LiteralPath $sshd) -or -not (Test-Path -LiteralPath $keygen)) { throw 'Official OpenSSH binaries are unavailable after installation.' }
if (-not (Test-Path -LiteralPath $sshDirectory)) { New-Item -ItemType Directory -Path $sshDirectory | Out-Null }
if (-not (Test-Path -LiteralPath $config)) {
    Copy-Item -LiteralPath (Join-Path $openSshDirectory 'sshd_config_default') -Destination $config
    Write-Host "Created the vendor default configuration: $config"
}
& $keygen -A
if ($LASTEXITCODE -ne 0) { throw 'Host-key generation failed.' }
& $sshd -t -f $config
if ($LASTEXITCODE -ne 0) { throw 'sshd configuration validation failed; no service start/restart attempted.' }
foreach ($source in $sources) {
    $effective = @(Read-EffectiveConfig $sshd $config $UserName ($source.Split('/')[0]))
    Assert-KeyLocation $effective $isAdminAccount
}
$ports = @($effective | Where-Object { "$_" -match '^port \d+$' } | ForEach-Object { [int]("$_" -split ' ')[1] } | Sort-Object -Unique)
if ($ports.Count -eq 0) { throw 'No SSH ports were reported.' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
Assert-NoReparsePath $keysPath
if (-not (Test-Path -LiteralPath $keyDirectory)) { New-Item -ItemType Directory -Path $keyDirectory | Out-Null }
if (-not $isAdminAccount) {
    $directoryAclBackup = "$keyDirectory.labmon-acl-$stamp.txt"
    [IO.File]::WriteAllText($directoryAclBackup, (Get-Acl -LiteralPath $keyDirectory).Sddl)
    Set-KeyAcl $keyDirectory $true $accountSid @($accountSid,$systemSid,$adminSid)
    Write-Host "Previous directory ACL: $directoryAclBackup"
}
$existing = ''
if (Test-Path -LiteralPath $keysPath) {
    $fileInfo = Get-Item -LiteralPath $keysPath -Force
    if ($fileInfo.PSIsContainer) { throw 'Authorized key target is a directory.' }
    $existingBytes = [IO.File]::ReadAllBytes($keysPath)
    if ($existingBytes.Length -gt 1 -and (($existingBytes[0] -eq 255 -and $existingBytes[1] -eq 254) -or ($existingBytes[0] -eq 254 -and $existingBytes[1] -eq 255))) {
        throw 'Existing authorized_keys is UTF-16. Review its encoding manually; no key will be appended.'
    }
    $existing = [Text.UTF8Encoding]::new($false, $true).GetString($existingBytes)
    $backup = "$keysPath.labmon-before-$stamp"
    Copy-Item -LiteralPath $keysPath -Destination $backup
    [IO.File]::WriteAllText("$backup.acl.txt", (Get-Acl -LiteralPath $keysPath).Sddl)
    Write-Host "Previous authorized keys and ACL: $backup"
}
$blob = ($keyLine -split ' ')[1]
if ($existing -notmatch ('(?m)(?:^|[ \t])ssh-ed25519[ \t]+' + [regex]::Escape($blob) + '(?:[ \t\r\n]|$)')) {
    $separator = ''
    if ($existing.Length -gt 0 -and -not $existing.EndsWith("`n")) { $separator = "`r`n" }
    [IO.File]::AppendAllText($keysPath, $separator + $keyLine + "`r`n", [Text.UTF8Encoding]::new($false))
    Write-Host 'Added one public key; existing keys were preserved.'
} else { Write-Host 'The same public key already exists; its existing options were preserved.' }
if ($isAdminAccount) { Set-KeyAcl $keysPath $false $adminSid @($adminSid,$systemSid) }
else { Set-KeyAcl $keysPath $false $accountSid @($accountSid,$adminSid,$systemSid) }

$hash = [Security.Cryptography.SHA256]::Create()
try { $digest = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes("$accountSid|$($sources -join ',')|$($ports -join ',')"))).Replace('-','').Substring(0,16) }
finally { $hash.Dispose() }
$ruleName = "LabMonitor-SSH-$digest"
if (-not (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name $ruleName -DisplayName "Lab Monitor SSH ($UserName)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $ports -RemoteAddress $sources -Profile Any | Out-Null
    Write-Host "Added firewall rule: $ruleName"
} else { Write-Host "Firewall rule already exists; unchanged: $ruleName" }
Write-Host "Previous sshd state/start mode: $($serviceBefore.State) / $($serviceBefore.StartMode)"
Set-Service -Name sshd -StartupType Automatic
if ((Get-Service -Name sshd).Status -ne 'Running') { Start-Service -Name sshd }
# Only authorized_keys changed: running sshd rereads it for each login; no restart is necessary.
Get-Service -Name sshd | Select-Object Name,Status,StartType | Format-Table -AutoSize
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -eq (Get-CimInstance Win32_Service -Filter "Name='sshd'").ProcessId })
Write-Host "Configured ports: $($ports -join ', ') ; actual listening ports: $(($listeners.LocalPort | Sort-Object -Unique) -join ', ')"
if ($listeners.Count -eq 0) { throw 'sshd started but no listener was observed. Setup is not verified; inspect the OpenSSH event log.' }
Write-Host 'Host Ed25519 fingerprint (compare on your client before trusting this host):'
$fingerprintFound = $false
foreach ($hostKeyEntry in @($effective | Where-Object { "$_" -match '^hostkey ' })) {
    $hostPublicKey = (("$hostKeyEntry" -replace '^hostkey ', '') -replace '__PROGRAMDATA__', $env:ProgramData) + '.pub'
    if (Test-Path -LiteralPath $hostPublicKey -PathType Leaf) {
        $fingerprint = @(& $keygen -l -E sha256 -f $hostPublicKey)
        if ($LASTEXITCODE -eq 0 -and ($fingerprint -join ' ') -match '\(ED25519\)\s*$') {
            $fingerprint | ForEach-Object { Write-Host $_ }
            $fingerprintFound = $true
        }
    }
}
if (-not $fingerprintFound) { Write-Warning 'No public Ed25519 host key from the active configuration could be inspected. Verify your configured host-key fingerprint manually.' }
Write-Host "Connection example: ssh -p $($ports[0]) $UserName@<server-address>"
Write-Host 'Listening is not a verified client login or Internet reachability. Test a new login before closing existing sessions.'
