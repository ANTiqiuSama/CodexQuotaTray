# Receives only Claude Code's documented status-line JSON; saves numeric fields.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $session = [string]$payload.session_id
    if ($session -notmatch '^[a-zA-Z0-9_-]{1,100}$') { exit 0 }
    $folder = Join-Path $PSScriptRoot 'claude-status'
    [System.IO.Directory]::CreateDirectory($folder) | Out-Null
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    $destination = Join-Path $folder ($session + '.json')
    $previous = $null
    if (Test-Path -LiteralPath $destination) {
        try { $previous = [System.IO.File]::ReadAllText($destination) | ConvertFrom-Json } catch { $previous = $null }
    }
    $limits = @{}
    $live = @{}
    foreach ($name in @('five_hour', 'seven_day')) {
        $window = $payload.rate_limits.$name
        if ($null -ne $window -and $null -ne $window.used_percentage) {
            $used = [double]$window.used_percentage
            if ($used -ge 0 -and $used -le 100) {
                $limits[$name] = @{used_percentage=$used; resets_at=$window.resets_at; observed_at=$now}
                $live[$name] = $true
                continue
            }
        }
        # Claude Code omits a window once its reading passes the reset; keep
        # the last reading so the tray sees the rollover instead of a gap.
        $old = if ($null -ne $previous) { $previous.rate_limits.$name } else { $null }
        if ($null -ne $old -and $null -ne $old.used_percentage) {
            $seen = if ($null -ne $old.observed_at) { [double]$old.observed_at } else { [double]$previous.updated_at }
            $limits[$name] = @{used_percentage=[double]$old.used_percentage; resets_at=$old.resets_at; observed_at=$seen}
        }
    }
    $record = @{
        schema=2; session_id=$session
        updated_at=$now
        rate_limits=$limits
    }
    $temporary = $destination + '.' + $PID + '.tmp'
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json -Depth 5 -Compress), $utf8)
    Move-Item -LiteralPath $temporary -Destination $destination -Force
    $parts = @('Claude Code')
    if ($live.ContainsKey('five_hour')) { $parts += ('5h left {0:0}%' -f (100 - $limits.five_hour.used_percentage)) }
    if ($live.ContainsKey('seven_day')) { $parts += ('7d left {0:0}%' -f (100 - $limits.seven_day.used_percentage)) }
    [Console]::WriteLine(($parts -join ' | '))
} catch {
    # Do not spill stdin, terminal content or credentials to stdout/stderr.
    exit 0
}
