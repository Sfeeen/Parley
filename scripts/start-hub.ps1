<#
.SYNOPSIS
    Start a new parley, hosting the Hub.

.DESCRIPTION
    Robust wrapper around `python -m parley init`. Finds a usable Python 3, makes
    the clone importable without an install step, and runs the Hub with the current
    directory (or -Workspace) as the synced workspace.

    Anything you pass that this script does not recognise is forwarded to
    `parley init` unchanged, so the full CLI is available.

.PARAMETER Name
    Human name for the parley, shown on the Deck.

.PARAMETER Workspace
    The synced folder. Defaults to the current directory.
    NOTE: this must NOT be the Parley clone -- everything in the workspace is
    replicated to every participant.

.PARAMETER Port
    Hub port. Default 7777.

.PARAMETER Bind
    Listen address. Default 0.0.0.0. Use 127.0.0.1 when fronting it with a tunnel.

.PARAMETER Public
    Tighten the enrolment policy for internet exposure.

.PARAMETER Seal
    Encrypt request and response bodies (for when TLS is unavailable).

.PARAMETER Approve
    New agents land in 'pending' until the host approves them.

.EXAMPLE
    .\scripts\start-hub.ps1 -Name "my-parley"

.EXAMPLE
    .\scripts\start-hub.ps1 -Name "my-parley" -Bind 127.0.0.1 -Public

.LINK
    docs/DEPLOY.md
#>

[CmdletBinding()]
param(
    [string]   $Name,
    [string]   $Workspace,
    [int]      $Port,
    [string]   $Bind,
    [switch]   $Public,
    [switch]   $Seal,
    [switch]   $Approve,
    [int]      $Words,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Passthrough
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$MinPython = [Version]'3.9'

function Find-Python {
    <#  Returns @{ Exe = <path or command>; Args = <string[]>; Version = <Version> }  #>
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

Make sure 'Add python.exe to PATH' is ticked in the installer, then open a NEW
PowerShell window.

Or point PARLEY_PYTHON at a specific interpreter:
  `$env:PARLEY_PYTHON = 'C:\Python312\python.exe'
"@ -ForegroundColor Red
    exit 1
}

# --- locate the repository, independently of the working directory ------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir '..')).Path

if (-not (Test-Path (Join-Path $RepoRoot 'parley'))) {
    Write-Host "parley: this does not look like a Parley clone: $RepoRoot\parley is missing" -ForegroundColor Red
    exit 1
}

# --- assemble the argument list ----------------------------------------------
$cliArgs = @('-m', 'parley', 'init')
if ($Name)              { $cliArgs += @('--name', $Name) }
if ($Workspace)         { $cliArgs += @('--workspace', $Workspace) }
if ($PSBoundParameters.ContainsKey('Port'))  { $cliArgs += @('--port', "$Port") }
if ($Bind)              { $cliArgs += @('--bind', $Bind) }
if ($Public)            { $cliArgs += '--public' }
if ($Seal)              { $cliArgs += '--seal' }
if ($Approve)           { $cliArgs += '--approve' }
if ($PSBoundParameters.ContainsKey('Words')) { $cliArgs += @('--words', "$Words") }
if ($Passthrough)       { $cliArgs += $Passthrough }

# --- warn about the workspace == clone mistake --------------------------------
$ws = if ($Workspace) { $Workspace } else { (Get-Location).Path }
if ((Test-Path (Join-Path $ws 'parley\hub')) -and (Test-Path (Join-Path $ws 'docs\SPEC.md'))) {
    Write-Host @"
parley: WARNING -- the workspace looks like the Parley clone itself.

  workspace: $ws

Everything in the workspace is replicated to every participant. Syncing Parley's
own source code is almost certainly not what you want.

Use a separate directory:
  New-Item -ItemType Directory -Force -Path `$HOME\work\parley-ws | Out-Null
  cd `$HOME\work\parley-ws

Continuing in 5 seconds; Ctrl-C to abort.
"@ -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}

# --- run ----------------------------------------------------------------------
$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH   = if ($oldPythonPath) { "$RepoRoot;$oldPythonPath" } else { $RepoRoot }
$env:PYTHONUNBUFFERED = '1'

try {
    $full = @($py.Args) + $cliArgs
    & $py.Exe @full
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPythonPath
}
