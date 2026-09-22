[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [ValidateRange(2, 60)][int]$IntervalSeconds = 10,
    [string]$StopPath = '',
    [string]$TextfilePath = '',
    [switch]$Once
)
# Compatible with Windows PowerShell 5.1. Read-only telemetry; never stop jobs.
$ErrorActionPreference = 'Stop'
$destination = [IO.Path]::GetFullPath($OutputPath)
$parentDirectory = [IO.Path]::GetDirectoryName($destination)
if (-not [IO.Directory]::Exists($parentDirectory)) { [IO.Directory]::CreateDirectory($parentDirectory) | Out-Null }
$utf8 = New-Object System.Text.UTF8Encoding($false)
$invariantCulture = [Globalization.CultureInfo]::InvariantCulture

function Write-AtomicUtf8([string]$Path, [string]$Content) {
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $directory = [IO.Path]::GetDirectoryName($resolvedPath)
    if (-not [IO.Directory]::Exists($directory)) { [void][IO.Directory]::CreateDirectory($directory) }
    $temporaryPath = $resolvedPath + '.tmp.' + $PID
    [IO.File]::WriteAllText($temporaryPath, $Content, $utf8)
    if ([IO.File]::Exists($resolvedPath)) { [IO.File]::Replace($temporaryPath, $resolvedPath, [NullString]::Value) }
    else { [IO.File]::Move($temporaryPath, $resolvedPath) }
}

function Convert-PrometheusLabel([object]$Value) {
    return ([string]$Value).Replace('\', '\\').Replace('"', '\"').Replace("`n", '\n').Replace("`r", '')
}

function Convert-MetricNumber([object]$Value) {
    $parsed = 0.0
    if ($null -ne $Value -and [double]::TryParse(([string]$Value).Trim(), [Globalization.NumberStyles]::Float, $invariantCulture, [ref]$parsed)) {
        if (-not [double]::IsNaN($parsed) -and -not [double]::IsInfinity($parsed) -and $parsed -ge 0) { return $parsed }
    }
    return $null
}

function Get-GpuSnapshot {
    $command = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue
    $commandPath = if ($null -ne $command) { $command.Source } else { $null }
    if (-not $commandPath) {
        # Older NVIDIA drivers install NVSMI outside the service's PATH.
        foreach ($candidate in @("$env:SystemRoot\System32\nvidia-smi.exe", "${env:ProgramFiles}\NVIDIA Corporation\NVSMI\nvidia-smi.exe")) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $commandPath = $candidate; break }
        }
    }
    if (-not $commandPath) { return @{ gpus = @(); warning = 'nvidia-smi unavailable; GPU telemetry unknown.' } }
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $commandPath
    $startInfo.Arguments = '--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,driver_model.current --format=csv,noheader,nounits'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $gpuProcess = New-Object Diagnostics.Process
    $gpuProcess.StartInfo = $startInfo
    try {
        [void]$gpuProcess.Start()
        $outTask = $gpuProcess.StandardOutput.ReadToEndAsync()
        $errTask = $gpuProcess.StandardError.ReadToEndAsync()
        if (-not $gpuProcess.WaitForExit(5000)) {
            # Only this owned, newly spawned read-only nvidia-smi helper can be killed.
            $gpuProcess.Kill()
            [void]$gpuProcess.WaitForExit(1000)
            return @{ gpus = @(); warning = 'nvidia-smi timed out; GPU telemetry unknown.' }
        }
        if ($gpuProcess.ExitCode -ne 0) { return @{ gpus = @(); warning = 'nvidia-smi query failed; GPU telemetry unknown.' } }
        $rows = @($outTask.Result -split '\r?\n' | Where-Object { $_.Trim() } | ConvertFrom-Csv -Header 'id','name','utilization','used','total','temperature','power','driver_model')
        $items = @()
        foreach ($row in $rows) {
            $used = Convert-MetricNumber $row.used
            $total = Convert-MetricNumber $row.total
            $items += [ordered]@{
                id = $row.id.Trim(); name = $row.name.Trim()
                utilization_pct = (Convert-MetricNumber $row.utilization)
                memory_used_bytes = $(if ($null -ne $used) { $used * 1MB } else { $null })
                memory_total_bytes = $(if ($null -ne $total) { $total * 1MB } else { $null })
                temperature_c = (Convert-MetricNumber $row.temperature)
                power_w = (Convert-MetricNumber $row.power)
                driver_model = $row.driver_model.Trim()
            }
        }
        return @{ gpus = $items; warning = $null }
    } finally { $gpuProcess.Dispose() }
}

do {
    if ($StopPath -and (Test-Path -LiteralPath $StopPath)) { break }
    $iterationStart = [DateTime]::UtcNow
    $warnings = @()
    $cpuInfo = @{ model = $null; logical_processors = $null }
    try {
        $cpuRows = @(Get-CimInstance -ClassName Win32_Processor -Property Name,NumberOfLogicalProcessors)
        $cpuInfo = @{
            model = (($cpuRows | Select-Object -ExpandProperty Name -Unique) -join ' / ')
            logical_processors = [int](($cpuRows | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum)
        }
    } catch { $warnings += 'CPU hardware inventory unavailable.' }

    $ssh = @{ tcp_connections = $null; connections = @(); observed_at = $null; count_basis = 'established_tcp' }
    try {
        $allTcp = @(Get-NetTCPConnection)
        $daemonIds = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='sshd.exe'" -Property ProcessId | Select-Object -ExpandProperty ProcessId)
        $ports = @($allTcp | Where-Object { $_.State -eq 'Listen' -and $daemonIds -contains $_.OwningProcess } | Select-Object -ExpandProperty LocalPort -Unique)
        $connections = @($allTcp | Where-Object { $_.State -eq 'Established' -and $ports -contains $_.LocalPort } | ForEach-Object {
            $createdAt = $null
            if ($_.CreationTime -and $_.CreationTime -gt [DateTime]'2000-01-01') { $createdAt = $_.CreationTime.ToUniversalTime().ToString('o') }
            [ordered]@{ remote_address = $_.RemoteAddress; remote_port = [int]$_.RemotePort; local_port = [int]$_.LocalPort; created_at = $createdAt }
        })
        $ssh = @{ tcp_connections = $connections.Count; connections = $connections; observed_at = [DateTime]::UtcNow.ToString('o'); count_basis = 'established_tcp'; listening_ports = $ports }
    } catch { $warnings += 'SSH TCP inventory unavailable; count is unknown.' }

    $overrides = @()
    $identities = @()
    try {
        # Inspect command lines locally only to exclude middle services. Never persist them.
        foreach ($processItem in @(Get-CimInstance -ClassName Win32_Process -Property Name,ProcessId,CreationDate,CommandLine)) {
            $created = $null
            if ($processItem.CreationDate) { $created = ([DateTimeOffset]$processItem.CreationDate.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0 }
            if ($processItem.ProcessId -gt 0 -and $null -ne $created) {
                $identities += @{ pid = [int]$processItem.ProcessId; name = $processItem.Name; start_time = $created }
            }
            if ($processItem.CommandLine -match '(?i)(ansys[-_]?fluent[-_]?mcp|mcp[-_]server|labmon\s+--config|labmon\.server)') {
                $overrides += @{ pid = [int]$processItem.ProcessId; start_time = $created; software = 'system'; role = 'service' }
            }
        }
    } catch { $warnings += 'Service process classification unavailable.' }

    try { $gpuResult = Get-GpuSnapshot }
    catch { $gpuResult = @{ gpus = @(); warning = 'GPU query unavailable; telemetry unknown.' } }
    if ($gpuResult.warning) { $warnings += $gpuResult.warning }
    if (@($gpuResult.gpus | Where-Object { $_.driver_model -match 'WDDM' }).Count -gt 0) {
        $warnings += 'WDDM per-process GPU memory is unavailable; whole-card GPU data is reported.'
    }
    $snapshot = [ordered]@{
        observed_at = [DateTime]::UtcNow.ToString('o'); hostname = $env:COMPUTERNAME
        cpu = $cpuInfo; ssh = $ssh; gpus = @($gpuResult.gpus)
        process_overrides = @($overrides); process_identities = @($identities); warnings = @($warnings)
    }
    try {
        Write-AtomicUtf8 -Path $destination -Content ($snapshot | ConvertTo-Json -Depth 8 -Compress)
    } catch {
        # Keep the previous complete sample; consumers reject it after 30 seconds.
        Write-Warning 'Telemetry snapshot write failed.'
    }
    if ($TextfilePath) {
        try {
            $epoch = ([DateTimeOffset][DateTime]::UtcNow).ToUnixTimeMilliseconds() / 1000.0
            $lines = @('# TYPE labmon_sidecar_timestamp_seconds gauge', ('labmon_sidecar_timestamp_seconds ' + $epoch.ToString('R', $invariantCulture)))
            if ($null -ne $ssh.tcp_connections) {
                $lines += '# TYPE labmon_ssh_tcp_connections gauge'
                $lines += 'labmon_ssh_tcp_connections ' + $ssh.tcp_connections
            }
            $gpuMetricMap = [ordered]@{
                utilization_pct = 'labmon_gpu_utilization_percent'
                memory_used_bytes = 'labmon_gpu_memory_used_bytes'
                memory_total_bytes = 'labmon_gpu_memory_total_bytes'
                temperature_c = 'labmon_gpu_temperature_celsius'
                power_w = 'labmon_gpu_power_watts'
            }
            foreach ($field in $gpuMetricMap.Keys) {
                $metricName = $gpuMetricMap[$field]
                $lines += '# TYPE ' + $metricName + ' gauge'
                foreach ($card in @($gpuResult.gpus)) {
                    $metricValue = Convert-MetricNumber $card[$field]
                    if ($null -ne $metricValue) {
                        $labelText = '{id="' + (Convert-PrometheusLabel $card.id) + '",name="' + (Convert-PrometheusLabel $card.name) + '"}'
                        $lines += $metricName + $labelText + ' ' + $metricValue.ToString('R', $invariantCulture)
                    }
                }
            }
            Write-AtomicUtf8 -Path $TextfilePath -Content (($lines -join "`n") + "`n")
        } catch { Write-Warning 'Prometheus textfile write failed.' }
    }
    if ($Once) { break }
    $remaining = $IntervalSeconds - ([DateTime]::UtcNow - $iterationStart).TotalSeconds
    while ($remaining -gt 0) {
        if ($StopPath -and (Test-Path -LiteralPath $StopPath)) { break }
        Start-Sleep -Milliseconds ([int][Math]::Min(500, $remaining * 1000))
        $remaining = $IntervalSeconds - ([DateTime]::UtcNow - $iterationStart).TotalSeconds
    }
} while (-not $Once)
