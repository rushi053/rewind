# Rewind installer for Windows: installs the `rewind` CLI with uv, pipx or pip,
# then adds Rewind to the Start menu and the Desktop.
#
# Supported install (rewind-cli is not on PyPI yet):
#   pipx install git+https://github.com/rushi053/rewind
#
# This script does the same install, then adds shortcuts. Read it before
# piping it into PowerShell.
#
# Environment:
#   $env:REWIND_VERSION      install a tag, e.g. 0.2.0 (git tag v0.2.0; none exist yet)
#   $env:REWIND_SOURCE       override the package, e.g. rewind-cli once it is published
#   $env:REWIND_NO_LAUNCHER  set to 1 to skip the shortcuts
#   $env:REWIND_AUTOSTART    set to 1 to start Rewind when you log in
$ErrorActionPreference = "Stop"

# rewind-cli is not on PyPI yet. Install from git so this script cannot 404.
# After the owner publishes, $env:REWIND_SOURCE = "rewind-cli" selects the package.
$pkg = "git+https://github.com/rushi053/rewind"
if ($env:REWIND_SOURCE) { $pkg = $env:REWIND_SOURCE }
elseif ($env:REWIND_VERSION) {
    $ver = $env:REWIND_VERSION -replace '^v', ''
    $pkg = "git+https://github.com/rushi053/rewind@v$ver"
}

function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

if (Have "uv") {
    Write-Host "Installing $pkg with uv..."
    uv tool install --force $pkg
} elseif (Have "pipx") {
    Write-Host "Installing $pkg with pipx..."
    pipx install --force $pkg
} else {
    $py = $null
    foreach ($candidate in @("py", "python", "python3")) {
        if (Have $candidate) {
            & $candidate -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $py = $candidate; break }
        }
    }
    if (-not $py) { throw "Python 3.9+ is required (or install uv: https://docs.astral.sh/uv/)" }
    Write-Host "Installing $pkg with $py -m pip --user (consider uv or pipx for isolated installs)..."
    & $py -m pip install --user --upgrade $pkg
}

$rewind = (Get-Command rewind -ErrorAction SilentlyContinue).Source
if (-not $rewind) {
    $guess = Join-Path $env:USERPROFILE ".local\bin\rewind.exe"
    if (Test-Path $guess) { $rewind = $guess }
}
if (-not $rewind) {
    Write-Host ""
    Write-Host "Installed, but 'rewind' is not on your PATH yet."
    Write-Host "Run 'uv tool update-shell' or 'pipx ensurepath', then open a new terminal."
    exit 0
}

Write-Host ""
Write-Host "Installed: $(& $rewind --version)"
if (-not $env:REWIND_NO_LAUNCHER) {
    # writes %LOCALAPPDATA%\Rewind\Rewind.vbs and creates Desktop + Start menu shortcuts
    & $rewind app install-launcher
}
if ($env:REWIND_AUTOSTART) { & $rewind app --autostart on }
Write-Host ""
Write-Host "Open Rewind from the Start menu, or run:  rewind app"
