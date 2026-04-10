[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "lock-check", "lock")]
    [string]$Command = "init",
    [switch]$AllPackages
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$defaultPackageDirs = @(
    (Join-Path $repoRoot "libs\bog-agents"),
    (Join-Path $repoRoot "libs\cli")
)

function Assert-UvInstalled {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand) {
        $msg = "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and re-run this script."
        throw $msg
    }
}

function Get-PackageDirectories {
    param(
        [switch]$AllPackages
    )

    if (-not $AllPackages) {
        return $defaultPackageDirs
    }

    return Get-ChildItem -Path (Join-Path $repoRoot "libs") -Recurse -Filter Makefile -File |
        ForEach-Object { $_.Directory.FullName } |
        Sort-Object -Unique
}

function Invoke-UvInDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host "==> $WorkingDirectory"
    Write-Host ("uv {0}" -f ($Arguments -join " "))

    Push-Location $WorkingDirectory
    try {
        & uv @Arguments
        if ($LASTEXITCODE -ne 0) {
            $msg = "uv $($Arguments -join ' ') failed in $WorkingDirectory"
            throw $msg
        }
    }
    finally {
        Pop-Location
    }
}

Assert-UvInstalled

$packageDirs = @(Get-PackageDirectories -AllPackages:$AllPackages)
$scope = if ($AllPackages) { "all managed packages" } else { "the SDK and CLI packages" }

Write-Host ("Running '{0}' for {1}." -f $Command, $scope)

foreach ($packageDir in $packageDirs) {
    switch ($Command) {
        "init" {
            Invoke-UvInDirectory -WorkingDirectory $packageDir -Arguments @("sync", "--group", "test", "--locked")
        }
        "lock-check" {
            Invoke-UvInDirectory -WorkingDirectory $packageDir -Arguments @("lock", "--check", "--python", "3.12")
        }
        "lock" {
            Invoke-UvInDirectory -WorkingDirectory $packageDir -Arguments @("lock", "--python", "3.12")
        }
    }
}

Write-Host ""
Write-Host "Done."
