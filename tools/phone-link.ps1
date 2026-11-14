# Phone link helper.
#   .\tools\phone-link.ps1                 forward the phone's 127.0.0.1:<port> to the laptop's brain over adb
#   .\tools\phone-link.ps1 -Command connect|stop|reconnect|echo|unpair
#                                          drive a DEBUG build of the body app (no screen taps needed)
# The laptop firewall blocks inbound connections from the phone, so the app dials 127.0.0.1.
# `python -m brain serve` sets up the forward itself and re-runs it every minute.
# Pairing (giving the phone the brain's token) is `python -m brain pair`.
param(
    [int]$Port = 8765,
    [ValidateSet("forward", "connect", "stop", "reconnect", "echo", "unpair")] [string]$Command = "forward",
    [int]$Count = 1000,
    [int]$Bytes = 102400,
    [int]$UnreachableSeconds = 0
)

$adb = $null
foreach ($dir in @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT, "$env:LOCALAPPDATA\Android\Sdk")) {
    if ($dir -and (Test-Path "$dir\platform-tools\adb.exe")) { $adb = "$dir\platform-tools\adb.exe"; break }
}
if (-not $adb) { $adb = (Get-Command adb -ErrorAction SilentlyContinue).Source }
if (-not $adb) { Write-Error "adb not found; set ANDROID_HOME or install platform-tools"; exit 1 }

# The receiver exists only in debug builds and requires android.permission.DUMP (held by the adb shell,
# preinstalled privileged apps, or an app granted it with `adb shell pm grant`).
$receiver = @("shell", "am", "broadcast", "--allow-background-activity-starts",
              "-n", "com.jmd.embodied.body/.DebugCommandReceiver", "-a")
$prefix = "com.jmd.embodied.body.debug."
switch ($Command) {
    "forward" {
        & $adb reverse "tcp:$Port" "tcp:$Port"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $adb reverse --list
    }
    "connect" {
        $extra = @("--es", "host", "127.0.0.1", "--ei", "port", "$Port")
        if ($UnreachableSeconds -gt 0) { $extra += @("--ei", "unreachable_s", "$UnreachableSeconds") }
        & $adb @receiver "${prefix}CONNECT" @extra
    }
    "echo" { & $adb @receiver "${prefix}ECHO" --ei count $Count --ei bytes $Bytes }
    "stop" { & $adb @receiver "${prefix}STOP" }
    "reconnect" { & $adb @receiver "${prefix}RECONNECT" }
    "unpair" { & $adb @receiver "${prefix}UNPAIR" }
}
