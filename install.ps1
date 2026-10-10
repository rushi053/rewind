# Rewind installer for Windows: installs the `rewind` CLI with uv, pipx or pip,
# then adds Rewind to the Start menu and the Desktop.
#
#   irm https://raw.githubusercontent.com/rushi053/rewind/main/install.ps1 | iex
#
# (Read it first; it is short.) Environment:
#   $env:REWIND_VERSION      install a specific version, e.g. 0.2.0
#   $env:REWIND_SOURCE       install from somewhere else, e.g. git+https://github.com/rushi053/rewind
#   $env:REWIND_NO_LAUNCHER  set to 1 to skip the shortcuts
#   $env:REWIND_AUTOSTART    set to 1 to start Rewind when you log in
$ErrorActionPreference = "Stop"

$pkg = "rewind-cli"
if ($env:REWIND_SOURCE) { $pkg = $env:REWIND_SOURCE }
elseif ($env:REWIND_VERSION) { $pkg = "rewind-cli==$($env:REWIND_VERSION)" }

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
