<#
.SYNOPSIS
    Install bog-agents-cli on Windows without the usual first-run traps.

.DESCRIPTION
    Picks the best available installer — `uv tool` (isolated, manages its own
    Python), then `pipx`, then `pip --user` — and, when there is no Python on
    the machine at all, installs uv and lets it fetch one. Warns when `python`
    or `pwsh` resolve to the Microsoft Store "App execution alias" (a zero-byte
    stub that fails with WinError 5), refreshes PATH so `bog-agents` works in
    this session, and finishes with `bog-agents --doctor`.

    ROADMAP #61. Idempotent: re-running upgrades to the requested version.

.PARAMETER Version
    Exact bog-agents-cli version (default: latest on PyPI).

.PARAMETER Extras
    Provider extras, e.g. "anthropic", "openai", "bedrock" or "all-providers".

.PARAMETER Method
    auto | uv | pipx | pip. Default auto (uv, then pipx, then pip --user).

.PARAMETER NoDoctor
    Skip the `bog-agents --doctor` check at the end.

.EXAMPLE
    irm https://raw.githubusercontent.com/bogware/bog-agents/main/install.ps1 | iex

.EXAMPLE
    .\install.ps1 -Extras anthropic -Method uv
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Extras = "",
    [ValidateSet("auto", "uv", "pipx", "pip")]
    [string]$Method = "auto",
    [switch]$NoDoctor
)

$ErrorActionPreference = "Stop"
$MinPython = [version]"3.11"

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn([string]$Message) { Write-Host "!!  $Message" -ForegroundColor Yellow }

function Get-CommandPath([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { return $null }
    return $cmd.Source
}

function Test-StoreAlias([string]$PathValue) {
    # The Microsoft Store registers zero-byte execution aliases under
    # %LOCALAPPDATA%\Microsoft\WindowsApps; spawning one fails with WinError 5.
    if (-not $PathValue) { return $false }
    if ($PathValue -notlike "*\WindowsApps\*") { return $false }
    $item = Get-Item $PathValue -ErrorAction SilentlyContinue
    return ($null -eq $item) -or ($item.Length -eq 0)
}

function Get-RealPython {
    # Prefer the py launcher, then python/python3 that are not Store aliases.
    foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
        $exe = Get-CommandPath $candidate[0]
        if (-not $exe) { continue }
        if (Test-StoreAlias $exe) {
            Write-Warn "$($candidate[0]) resolves to the Microsoft Store alias ($exe); ignoring it. Turn it off under Settings > Apps > App execution aliases if you install Python yourself."
            continue
        }
        try {
            $args = @($candidate[1..($candidate.Length - 1)]) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
            $out = & $exe @args 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $ver = [version]$out.Trim()
                if ($ver -ge $MinPython) { return @{ Exe = $exe; Args = $candidate[1..($candidate.Length - 1)]; Version = $ver } }
                Write-Warn "$exe is Python $ver; bog-agents needs $MinPython or newer."
            }
        } catch { }
    }
    return $null
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($machine, $user, $env:Path -join ";")
}

function Add-UserPath([string]$Dir) {
    if (-not (Test-Path $Dir)) { return }
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($user -split ";") -notcontains $Dir) {
        [Environment]::SetEnvironmentVariable("Path", (($user, $Dir) -join ";").Trim(";"), "User")
        Write-Step "Added $Dir to your user PATH (new terminals will see it)."
    }
    if (($env:Path -split ";") -notcontains $Dir) { $env:Path = "$Dir;$env:Path" }
}

$spec = "bog-agents-cli"
if ($Extras) { $spec = "bog-agents-cli[$Extras]" }
if ($Version) { $spec = "$spec==$Version" }

Write-Step "Installing $spec"

$python = Get-RealPython
$uv = Get-CommandPath "uv"
$pipx = Get-CommandPath "pipx"
if (Test-StoreAlias $uv) { $uv = $null }

$chosen = $Method
if ($chosen -eq "auto") {
    if ($uv) { $chosen = "uv" }
    elseif ($pipx) { $chosen = "pipx" }
    elseif ($python) { $chosen = "pip" }
    else { $chosen = "uv" }  # nothing usable: uv can bring its own Python
}

switch ($chosen) {
    "uv" {
        if (-not $uv) {
            Write-Step "uv not found; installing it (https://astral.sh/uv)"
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
            Refresh-Path
            $uv = Get-CommandPath "uv"
            if (-not $uv) { throw "uv did not land on PATH; open a new terminal and re-run, or pass -Method pip" }
        }
        $uvArgs = @("tool", "install", "--force")
        if (-not $python) { $uvArgs += @("--python", "3.12") }
        $uvArgs += $spec
        & $uv @uvArgs
        if ($LASTEXITCODE -ne 0) { throw "uv tool install failed (exit $LASTEXITCODE)" }
        & $uv tool update-shell 2>$null | Out-Null
        Add-UserPath (Join-Path $env:USERPROFILE ".local\bin")
    }
    "pipx" {
        if (-not $pipx) {
            if (-not $python) { throw "pipx and Python are both missing; use -Method uv" }
            Write-Step "pipx not found; installing it for the current user"
            & $python.Exe @($python.Args + @("-m", "pip", "install", "--user", "pipx"))
            & $python.Exe @($python.Args + @("-m", "pipx", "ensurepath"))
            Refresh-Path
            $pipx = Get-CommandPath "pipx"
            if (-not $pipx) { $pipx = "$($python.Exe)"; $pipxPrefix = @($python.Args + @("-m", "pipx")) } else { $pipxPrefix = @() }
        } else { $pipxPrefix = @() }
        & $pipx @($pipxPrefix + @("install", "--force", $spec))
        if ($LASTEXITCODE -ne 0) { throw "pipx install failed (exit $LASTEXITCODE)" }
        Add-UserPath (Join-Path $env:USERPROFILE ".local\bin")
    }
    "pip" {
        if (-not $python) { throw "No Python $MinPython+ found. Install one (winget install Python.Python.3.12) or use -Method uv" }
        & $python.Exe @($python.Args + @("-m", "pip", "install", "--user", "--upgrade", $spec))
        if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
        $scripts = & $python.Exe @($python.Args + @("-c", "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"))
        if ($scripts) { Add-UserPath $scripts.Trim() }
    }
}

Refresh-Path
$bog = Get-CommandPath "bog-agents"
if (-not $bog) {
    Write-Warn "bog-agents is installed but not on PATH in this session; open a new terminal."
    exit 0
}
Write-Step ("Installed: " + (& $bog --version 2>&1 | Select-Object -First 1))

$pwsh = Get-CommandPath "pwsh"
if (Test-StoreAlias $pwsh) {
    Write-Warn "pwsh resolves to the Microsoft Store alias; the opt-in powershell tool will use Windows PowerShell 5.1 until PowerShell 7 is installed (winget install Microsoft.PowerShell)."
}

if (-not $NoDoctor) {
    Write-Step "bog-agents --doctor"
    & $bog --doctor
}
Write-Host ""
Write-Host "Next: set a provider key (e.g. `$env:ANTHROPIC_API_KEY) and run `bog-agents`." -ForegroundColor Green
