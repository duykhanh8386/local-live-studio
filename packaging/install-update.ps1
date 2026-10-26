param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$updateSettings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$taskSource = [IO.Path]::GetFullPath($updateSettings.source)
$taskTarget = [IO.Path]::GetFullPath($updateSettings.target)
$taskStage = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Config))
$taskFolder = [IO.Path]::GetDirectoryName($taskTarget)
$taskBackup = $taskTarget + '.previous'
$taskIncoming = Join-Path $taskFolder ('update-' + [guid]::NewGuid().ToString('N') + '.exe')
$taskLog = Join-Path $taskStage 'install.log'
$taskSwapped = $false
try {
    if ([IO.Path]::GetDirectoryName($taskSource) -ne $taskStage -or $taskSource -eq $taskTarget) { throw 'Invalid update path.' }
    if ($updateSettings.sha256 -notmatch '^[0-9a-f]{64}$' -or $updateSettings.commit -notmatch '^[0-9a-f]{40}$') { throw 'Invalid update metadata.' }
    if ((Get-FileHash -LiteralPath $taskSource -Algorithm SHA256).Hash.ToLowerInvariant() -ne $updateSettings.sha256) { throw 'Update SHA-256 mismatch.' }
    $previous = Get-Process -Id $updateSettings.pid -ErrorAction SilentlyContinue
    if ($previous -and -not $previous.WaitForExit(120000)) { throw 'The application did not close. No files replaced.' }
    # A PyInstaller one-file bootloader can outlive its Python child briefly.
    $bootloader = Get-Process -Id $updateSettings.parent_pid -ErrorAction SilentlyContinue
    if ($bootloader -and $bootloader.Path -eq $taskTarget -and -not $bootloader.WaitForExit(30000)) { throw 'The EXE bootloader did not close.' }
    Copy-Item -LiteralPath $taskSource -Destination $taskIncoming
    Copy-Item -LiteralPath $taskTarget -Destination $taskBackup -Force
    Move-Item -LiteralPath $taskIncoming -Destination $taskTarget -Force
    $taskSwapped = $true
    $env:LIVE_DATA_DIR = $updateSettings.data_dir
    $taskArguments = @('--port', [string][int]$updateSettings.port, '--max-streams', [string][int]$updateSettings.max_streams, '--no-browser')
    $newProcess = Start-Process -FilePath $taskTarget -WorkingDirectory $taskFolder -ArgumentList $taskArguments -WindowStyle Hidden -PassThru
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(90)
    $taskReady = $false
    while ([DateTime]::UtcNow -lt $taskDeadline -and -not $newProcess.HasExited) {
        try {
            $client = New-Object System.Net.WebClient
            $client.Proxy = $null
            $result = $client.DownloadString('http://127.0.0.1:' + [string][int]$updateSettings.port + '/api/health') | ConvertFrom-Json
            $client.Dispose()
            if ($result.service -eq 'local-live-studio' -and $result.commit -eq $updateSettings.commit) { $taskReady = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
        $newProcess.Refresh()
    }
    if (-not $taskReady) { throw 'The new version did not become ready. Previous EXE is kept as .previous.' }
    'Update completed: ' + $updateSettings.commit | Set-Content -LiteralPath $taskLog -Encoding UTF8
} catch {
    ('Update failed: ' + $_.Exception.Message + '. Backup: ' + $taskBackup) | Set-Content -LiteralPath $taskLog -Encoding UTF8
    # Leave the backup and persistent data untouched for recovery. Never kill
    # another process or try to replace a newly running executable blindly.
    exit 1
} finally {
    if (Test-Path -LiteralPath $taskIncoming -PathType Leaf) { Remove-Item -LiteralPath $taskIncoming }
}
