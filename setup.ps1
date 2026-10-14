<#
.SYNOPSIS
    Richtet das Projekt auf einem frischen Windows-Rechner ein.

.DESCRIPTION
    Prueft und installiert bei Bedarf Python (>= 3.11), Node.js (>= 20) und
    Google Chrome, legt die virtuelle Umgebung an, installiert die
    Abhaengigkeiten, erzeugt ".env" aus der Vorlage und baut die Oberflaeche.
    Am Ende wird die Anwendung gestartet.

    Das Skript ist mehrfach ausfuehrbar: Was schon da ist, wird nur gemeldet.

    Was es NICHT tut: Es aendert keine Systemeinstellungen dauerhaft und laedt
    keine Playwright-Browser herunter - die Website-Suche steuert das
    installierte Google Chrome.

    Hinweis zur Schreibweise: Diese Datei nutzt bewusst nur ASCII (ue/oe/ae/ss).
    PowerShell 5.1 liest Skripte ohne BOM als ANSI - Umlaute wuerden sonst im
    Fenster falsch erscheinen.

.PARAMETER Check
    Nur pruefen und berichten. Es wird nichts installiert oder geaendert.

.PARAMETER NoChrome
    Chrome nicht installieren, auch wenn er fehlt (nur melden).

.PARAMETER NoStart
    Am Ende nicht starten, nur einrichten.

.PARAMETER Rebuild
    Die Oberflaeche auch dann neu bauen, wenn sie schon vorhanden ist.

.EXAMPLE
    setup.bat -Check
    setup.bat
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoChrome,
    [switch]$NoStart,
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Mindestversionen (siehe README, Abschnitt 2)
$MinPythonMajor = 3
$MinPythonMinor = 11
$MinNodeMajor = 20
$WingetPython = @('Python.Python.3.13', 'Python.Python.3.12')
$WingetNode = 'OpenJS.NodeJS.LTS'
$WingetChrome = 'Google.Chrome'

function Write-Step { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok { param([string]$Text) Write-Host "    OK: $Text" -ForegroundColor Green }
function Write-Note { param([string]$Text) Write-Host "    Hinweis: $Text" -ForegroundColor Yellow }
function Write-Missing { param([string]$Text) Write-Host "    FEHLT: $Text" -ForegroundColor Red }

function Get-PythonInfo {
    <#
        Ermittelt Version und echten Pfad eines Python-Kandidaten.

        Bewusst ohne doppelte Anfuehrungszeichen im uebergebenen Code: PowerShell
        5.1 zerlegt Argumente fuer fremde Programme und verliert dabei
        eingebettete " - der Aufruf wuerde dann mit Syntaxfehler abbrechen.
    #>
    param([string]$Exe, [string[]]$Prefix = @())

    try {
        $versionText = (& $Exe @($Prefix + @('--version')) 2>&1 | Select-Object -First 1)
        if ($LASTEXITCODE -ne 0 -or -not $versionText) { return $null }
        if ($versionText -notmatch '(\d+)\.(\d+)') { return $null }
        $version = @([int]$Matches[1], [int]$Matches[2])

        $exePath = (& $Exe @($Prefix + @('-c', 'import sys;print(sys.executable)')) 2>$null | Select-Object -First 1)
        if (-not $exePath) { $exePath = $Exe }
        return [pscustomobject]@{
            Major  = $version[0]
            Minor  = $version[1]
            Exe    = $exePath.ToString().Trim()
            Prefix = $Prefix
        }
    }
    catch {
        return $null
    }
}

function Get-PythonCandidate {
    <# Sucht die beste vorhandene Python-Installation - auch eine frisch installierte. #>
    $tests = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $tests += , @('py', @('-3')) }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and $cmd.Source -notmatch 'WindowsApps') {
        $tests += , @($cmd.Source, @())
    }
    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "${env:ProgramFiles}", "${env:ProgramFiles(x86)}", 'C:\')) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        Get-ChildItem -Path $root -Filter 'python.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch 'WindowsApps' } |
            ForEach-Object { $tests += , @($_.FullName, @()) }
    }

    $found = @()
    foreach ($test in $tests) {
        $info = Get-PythonInfo -Exe $test[0] -Prefix $test[1]
        if ($info) { $found += $info }
    }
    if ($found.Count -eq 0) { return $null }

    $passend = $found |
        Where-Object { Test-PythonOk $_ } |
        Sort-Object Major, Minor -Descending
    if ($passend) { return $passend[0] }
    return ($found | Sort-Object Major, Minor -Descending)[0]
}

function Test-PythonOk {
    param($Info)
    if (-not $Info) { return $false }
    if ($Info.Major -gt $MinPythonMajor) { return $true }
    return $Info.Major -eq $MinPythonMajor -and $Info.Minor -ge $MinPythonMinor
}

function Get-NodeInfo {
    <# node.exe plus das zugehoerige npm.cmd - auch nach einer frischen Installation. #>
    $node = Get-Command node -ErrorAction SilentlyContinue
    $nodeExe = if ($node) { $node.Source } else { $null }
    if (-not $nodeExe -and (Test-Path "$env:ProgramFiles\nodejs\node.exe")) {
        $nodeExe = "$env:ProgramFiles\nodejs\node.exe"
    }
    if (-not $nodeExe) { return $null }

    $npmExe = Join-Path (Split-Path -Parent $nodeExe) 'npm.cmd'
    if (-not (Test-Path $npmExe)) {
        $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        $npmExe = if ($npm) { $npm.Source } else { $null }
    }

    $major = $null
    try {
        $versionText = (& $nodeExe -v) 2>$null
        if ($versionText -match '(\d+)') { $major = [int]$Matches[1] }
    }
    catch { $major = $null }

    return [pscustomobject]@{ Exe = $nodeExe; Npm = $npmExe; Major = $major }
}

function Get-ChromeExe {
    foreach ($path in @(
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
        )) {
        if ($path -and (Test-Path $path)) { return $path }
    }
    return $null
}

function Install-WingetPackage {
    <# Installiert ein Paket ueber winget. Ob es geklappt hat, entscheidet die erneute Pruefung. #>
    param([string[]]$Ids, [string]$Label, [switch]$UserScope)

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Note "winget ist nicht verfuegbar - $Label bitte von Hand installieren."
        return $false
    }
    foreach ($id in $Ids) {
        Write-Host "    installiere $Label ueber winget ($id) - Windows fragt ggf. nach Administratorrechten" -ForegroundColor Gray
        $arguments = @(
            'install', '--id', $id, '--exact', '--silent',
            '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'
        )
        if ($UserScope) { $arguments += @('--scope', 'user') }
        & winget @arguments | Out-Host
        return $true
    }
    return $false
}

function Show-ManualHints {
    param([string]$What)
    switch ($What) {
        'python' {
            Write-Note 'Python 3.11+ von https://www.python.org/downloads/ installieren und'
            Write-Note 'dabei "Add python.exe to PATH" anhaken - oder: winget install Python.Python.3.13'
        }
        'node' {
            Write-Note 'Node.js 20+ von https://nodejs.org/ installieren - oder: winget install OpenJS.NodeJS.LTS'
        }
        'chrome' {
            Write-Note 'Google Chrome von https://www.google.com/chrome/ installieren - oder: winget install Google.Chrome.'
            Write-Note 'Ohne Chrome gibt es keine Suche ueber eBay- oder Kleinanzeigen-Website.'
        }
    }
}

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
Write-Step 'Python pruefen'
$python = Get-PythonCandidate
if (Test-PythonOk $python) {
    Write-Ok "Python $($python.Major).$($python.Minor) ($($python.Exe))"
}
else {
    if ($python) { Write-Missing "Python $($python.Major).$($python.Minor) ist zu alt (noetig: $MinPythonMajor.$MinPythonMinor+)" }
    else { Write-Missing "Python $MinPythonMajor.$MinPythonMinor+" }

    if ($Check) {
        Show-ManualHints 'python'
    }
    else {
        Install-WingetPackage -Ids $WingetPython -Label 'Python' -UserScope | Out-Null
        $python = Get-PythonCandidate
        if (Test-PythonOk $python) {
            Write-Ok "Python $($python.Major).$($python.Minor) installiert ($($python.Exe))"
        }
        else {
            Show-ManualHints 'python'
            Write-Host "`nOhne Python $MinPythonMajor.$MinPythonMinor+ kann die Einrichtung nicht weiterlaufen." -ForegroundColor Red
            exit 1
        }
    }
}

# ---------------------------------------------------------------------------
# 2. Node.js (nur fuer die Oberflaeche)
# ---------------------------------------------------------------------------
Write-Step 'Node.js pruefen (nur fuer die Oberflaeche noetig)'
$node = Get-NodeInfo
if ($node -and $node.Major -ge $MinNodeMajor) {
    Write-Ok "Node.js $($node.Major) ($($node.Exe))"
}
else {
    if ($node) { Write-Missing "Node.js $($node.Major) ist zu alt (noetig: $MinNodeMajor+)" }
    else { Write-Missing "Node.js $MinNodeMajor+" }

    if ($Check) {
        Show-ManualHints 'node'
    }
    else {
        Install-WingetPackage -Ids @($WingetNode) -Label 'Node.js' | Out-Null
        $node = Get-NodeInfo
        if ($node -and $node.Major -ge $MinNodeMajor) {
            Write-Ok "Node.js $($node.Major) installiert ($($node.Exe))"
        }
        else {
            Show-ManualHints 'node'
            Write-Note 'Ohne Node.js wird die Oberflaeche nicht gebaut - die API laeuft trotzdem (http://127.0.0.1:8000/docs).'
            $node = $null
        }
    }
}

# ---------------------------------------------------------------------------
# 3. Google Chrome (fuer die Website-Suche)
# ---------------------------------------------------------------------------
Write-Step 'Google Chrome pruefen (fuer die Suche ueber eBay- und Kleinanzeigen-Website)'
$chrome = Get-ChromeExe
if ($chrome) {
    Write-Ok "Chrome gefunden ($chrome)"
}
else {
    Write-Missing 'Google Chrome'
    if ($Check -or $NoChrome) {
        Show-ManualHints 'chrome'
    }
    else {
        Install-WingetPackage -Ids @($WingetChrome) -Label 'Google Chrome' | Out-Null
        $chrome = Get-ChromeExe
        if ($chrome) { Write-Ok "Chrome installiert ($chrome)" } else { Show-ManualHints 'chrome' }
    }
}

if ($Check) {
    Write-Host "`nNur geprueft - es wurde nichts geaendert. Einrichtung starten: setup.bat" -ForegroundColor Cyan
    exit 0
}

# ---------------------------------------------------------------------------
# 4. Virtuelle Umgebung und Abhaengigkeiten
# ---------------------------------------------------------------------------
Write-Step 'Python-Umgebung einrichten'
$venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    Write-Ok '.venv ist schon vorhanden'
}
else {
    Write-Host '    lege .venv an ...' -ForegroundColor Gray
    & $python.Exe @($python.Prefix + @('-m', 'venv', '.venv'))
    if (-not (Test-Path $venvPy)) {
        Write-Host 'FEHLER: Die virtuelle Umgebung liess sich nicht anlegen.' -ForegroundColor Red
        exit 1
    }
    Write-Ok '.venv angelegt'
}

Write-Host '    installiere Abhaengigkeiten (requirements.txt) ...' -ForegroundColor Gray
& $venvPy -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host 'FEHLER: pip install ist fehlgeschlagen - Meldung oberhalb.' -ForegroundColor Red
    exit 1
}
Write-Ok 'Abhaengigkeiten installiert'

# ---------------------------------------------------------------------------
# 5. Konfiguration
# ---------------------------------------------------------------------------
Write-Step 'Konfiguration'
if (Test-Path '.env') {
    Write-Ok '.env ist vorhanden (wird nicht ueberschrieben)'
}
else {
    Copy-Item '.env.example' '.env'
    Write-Ok '.env aus .env.example erzeugt - eigene Werte bei Bedarf dort eintragen'
}

# ---------------------------------------------------------------------------
# 6. Oberflaeche bauen
# ---------------------------------------------------------------------------
Write-Step 'Oberflaeche bauen'
$distIndex = Join-Path $PSScriptRoot 'frontend\dist\index.html'
if (-not $node) {
    Write-Note 'Ohne Node.js uebersprungen - die API laeuft trotzdem unter http://127.0.0.1:8000/docs'
}
elseif ((Test-Path $distIndex) -and -not $Rebuild) {
    Write-Ok 'frontend/dist ist schon vorhanden (neu bauen: setup.bat -Rebuild)'
}
else {
    Push-Location (Join-Path $PSScriptRoot 'frontend')
    try {
        if (Test-Path 'package-lock.json') { & $node.Npm ci } else { & $node.Npm install }
        if ($LASTEXITCODE -ne 0) {
            Write-Note 'npm ci fehlgeschlagen - versuche npm install'
            & $node.Npm install
        }
        if ($LASTEXITCODE -ne 0) { throw 'npm install ist fehlgeschlagen.' }
        & $node.Npm run build
        if ($LASTEXITCODE -ne 0) { throw 'npm run build ist fehlgeschlagen.' }
        Write-Ok 'Oberflaeche gebaut (frontend/dist)'
    }
    catch {
        Write-Host "FEHLER: $($_.Exception.Message)" -ForegroundColor Red
        Write-Note 'Die API laeuft auch ohne Oberflaeche: http://127.0.0.1:8000/docs'
        Pop-Location
        exit 1
    }
    Pop-Location
}

# ---------------------------------------------------------------------------
# 7. Starten
# ---------------------------------------------------------------------------
Write-Step 'Fertig'
Write-Host '    Einstellungen und eigene Daten: siehe README, Abschnitt 2 und 4.' -ForegroundColor Gray
if ($NoStart) {
    Write-Host '    Starten mit: start.bat' -ForegroundColor Gray
    exit 0
}
Write-Host '    Starte die Anwendung (Fenster offen lassen, Beenden mit Strg+C) ...' -ForegroundColor Gray
& (Join-Path $PSScriptRoot 'start.bat')
