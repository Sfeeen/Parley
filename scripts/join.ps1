<#
.SYNOPSIS
    Join a parley and start the local daemon.

.DESCRIPTION
    Two phases:
      1. `parley join`  -- enrol, write <workspace>\.parley\credentials.json
      2. `parley run`   -- the daemon: sync + heartbeat + PSR freshness + pigeonhole

    Finds a usable Python 3 and makes the clone importable without an install step.

.PARAMETER Hub
    Hub URL, e.g. http://192.168.1.20:7777 or https://your-tunnel.example.com

.PARAMETER Discover
    Find the Hub by UDP broadcast on the LAN instead of giving a URL. Does not work
    across subnets, and is disabled when the Hub was started with --public.

.PARAMETER Invite
    The watchword. Quote it. Case, spacing and punctuation are normalised, so
    "Copper Otter Climbs the Quiet Hill" is the same as copper-otter-climbs-the-quiet-hill.

.PARAMETER Name
    What other participants see. Short and distinct.

.PARAMETER Kind
    What sort of agent you are: claude-code, cursor, generic, human, ...

.PARAMETER Workspace
    The synced folder. Defaults to the current directory.

.PARAMETER Seal
    Required only if the Hub was started with --seal.

.PARAMETER Local
    Skip enrolment; just start the daemon. Use when you are already enrolled --
    for example, you are the host.

.PARAMETER NoRun
    Enrol but do not start the daemon.

.EXAMPLE
    .\scripts\join.ps1 -Hub http://192.168.1.20:7777 -Invite "copper-otter-climbs-the-quiet-hill" -Name Bram

.EXAMPLE
    .\scripts\join.ps1 -Discover -Invite "copper-otter-climbs-the-quiet-hill" -Name Bram

.LINK
    docs/QUICKSTART.md
#>

[CmdletBinding()]
param(
    [string]   $Hub,
    [switch]   $Discover,
    [string]   $Invite,
    [string]   $Name,
    [string]   $Kind,
    [string]   $Workspace,
    [switch]   $Seal,
    [switch]   $Local,
    [switch]   $NoRun,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Passthrough
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$MinPython = [Version]'3.9'

function Find-Python {
    $candidates = @()
    if ($env:PARLEY_PYTHON) {
        $candidates += @{ Exe = $env:PARLEY_PYTHON; Args = @() }
    }
    $candidates += @{ Exe = 'py';      Args = @('-3') }
    $candidates += @{ Exe = 'python3'; Args = @() }
    $candidates += @{ Exe = 'python';  Args = @() }

    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $probe = @($c.Args) + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')
            $out = & $c.Exe @probe 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            $v = [Version]($out.Trim())
            if ($v -ge $MinPython) {
                return @{ Exe = $c.Exe; Args = $c.Args; Version = $v }
            }
        } catch {
            continue
        }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host @"
parley: no usable Python found.

Parley needs Python $MinPython or newer. It has no other dependencies -- there is
nothing to pip install.

Tried: `$env:PARLEY_PYTHON, py -3, python3, python.

Install Python and try again:
  winget install Python.Python.3.12
  or https://www.python.org/downloads/windows/

Tick 'Add python.exe to PATH' in the installer, then open a NEW PowerShell window.

Or point PARLEY_PYTHON at a specific interpreter:
  `$env:PARLEY_PYTHON = 'C:\Python312\python.exe'
"@ -ForegroundColor Red
    exit 1
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir '..')).Path

if (-not (Test-Path (Join-Path $RepoRoot 'parley'))) {
    Write-Host "parley: this does not look like a Parley clone: $RepoRoot\parley is missing" -ForegroundColor Red
    exit 1
}

# --- workspace ----------------------------------------------------------------
$ws = if ($Workspace) { $Workspace } else { (Get-Location).Path }
if (-not (Test-Path $ws)) {
    New-Item -ItemType Directory -Force -Path $ws | Out-Null
}
$ws = (Resolve-Path $ws).Path

if ((Test-Path (Join-Path $ws 'parley\hub')) -and (Test-Path (Join-Path $ws 'docs\SPEC.md'))) {
    Write-Host @"
parley: WARNING -- the workspace looks like the Parley clone itself.

  workspace: $ws

Everything in the workspace is replicated to every participant. Use a separate
directory for the work you are actually collaborating on.

Continuing in 5 seconds; Ctrl-C to abort.
"@ -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}

$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH       = if ($oldPythonPath) { "$RepoRoot;$oldPythonPath" } else { $RepoRoot }
$env:PYTHONUNBUFFERED = '1'

try {
    # --- phase 1: enrol -------------------------------------------------------
    if (-not $Local) {
        if (-not $Invite) {
            Write-Host @"
parley: nothing to join.

You need an invite (the watchword) and a way to reach the Hub:

  .\scripts\join.ps1 -Hub http://192.168.1.20:7777 ``
      -Invite "copper-otter-climbs-the-quiet-hill" -Name Bram

On the same LAN you can skip the URL and let the client find the Hub:

  .\scripts\join.ps1 -Discover -Invite "copper-otter-climbs-the-quiet-hill" -Name Bram

If you are already enrolled (for example you are the host), start the daemon only:

  .\scripts\join.ps1 -Local
"@ -ForegroundColor Red
            exit 2
        }

        if (-not $Hub -and -not $Discover) {
            Write-Host "parley: give either -Hub <url> or -Discover." -ForegroundColor Red
            exit 2
        }

        $joinArgs = @('-m', 'parley', 'join')
        if ($Hub)       { $joinArgs += @('--hub', $Hub) }
        if ($Discover)  { $joinArgs += '--discover' }
        $joinArgs += @('--invite', $Invite)
        if ($Name)      { $joinArgs += @('--name', $Name) }
        if ($Kind)      { $joinArgs += @('--kind', $Kind) }
        $joinArgs += @('--workspace', $ws)
        if ($Seal)      { $joinArgs += '--seal' }
        if ($Passthrough) { $joinArgs += $Passthrough }

        Write-Host 'parley: enrolling...' -ForegroundColor Cyan
        $full = @($py.Args) + $joinArgs
        & $py.Exe @full
        $rc = $LASTEXITCODE

        if ($rc -ne 0) {
            switch ($rc) {
                3 { Write-Host 'parley: enrolment failed (auth). Check the watchword.' -ForegroundColor Red }
                4 { Write-Host 'parley: cannot reach the Hub. Check the URL and the firewall; see docs/TROUBLESHOOTING.md section 1.' -ForegroundColor Red }
                5 { Write-Host 'parley: FINGERPRINT MISMATCH. You reached a different Hub than the one you were invited to. Stop and tell a human.' -ForegroundColor Red }
            }
            exit $rc
        }

        Write-Host ''
        Write-Host 'parley: compare the three-word fingerprint above with what the host read out.' -ForegroundColor Yellow
        Write-Host 'parley: if it does not match, stop now.' -ForegroundColor Yellow
        Write-Host ''
    }

    # --- phase 2: the daemon --------------------------------------------------
    if ($NoRun) {
        Write-Host 'parley: enrolled. Start the daemon when you are ready:'
        Write-Host "  cd $ws"
        Write-Host "  `$env:PYTHONPATH = '$RepoRoot'; $($py.Exe) $($py.Args) -m parley run"
        exit 0
    }

    # The daemon re-emits the PSR written to .parley\me.json, which is how an agent
    # satisfies the freshness contract without a timer in its own loop (SPEC 6.1, 10).
    $runArgs = @('-m', 'parley', 'run',
                 '--workspace', $ws,
                 '--psr-from', (Join-Path $ws '.parley\me.json'))

    Write-Host 'parley: starting the daemon (Ctrl-C to leave the parley cleanly).' -ForegroundColor Cyan
    $full = @($py.Args) + $runArgs
    & $py.Exe @full
    exit $LASTEXITCODE

} finally {
    $env:PYTHONPATH = $oldPythonPath
}
