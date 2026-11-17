# Bloom installer for Windows 10/11 (x64). One line in PowerShell:
#
#   irm https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/install.ps1 | iex
#
# What it does (nothing else; run it again to update):
#   1. uv (Python manager) and ffmpeg, if missing (uv from astral.sh, ffmpeg with winget)
#   2. the meeting backend (this repository) into %LOCALAPPDATA%\bloom-meeting, with its own Python environment
#   3. the desktop app (*setup.exe from the latest GitHub release), installed for this user only
#   4. ~/.blurt/config.json pointed at the backend; config.json created from config.example.json (kept on updates)
# Your own Claude account does the thinking: Claude Code must be installed and signed in (`claude`).
#
# Options (environment variables, since `irm | iex` takes no arguments):
#   BLOOM_DIR      where the backend goes            (default %LOCALAPPDATA%\bloom-meeting)
#   BLOOM_REF      branch or tag to install          (default main)
#   BLOOM_SOURCE   a local .zip of the repository    (instead of downloading; for testing)
#   BLOOM_NO_APP   1 = skip the desktop app          (backend only)
#   GITHUB_TOKEN   a token, if the repository is private
#
# This file is plain ASCII on purpose: Windows PowerShell 5.1 reads a script file without a BOM in the system code
# page, and non-ASCII text there can swallow the next line. Chinese messages are written as \uXXXX and decoded at run
# time (Z).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = 'lightblue888/bloom-meeting'
$Dir = if ($env:BLOOM_DIR) { $env:BLOOM_DIR } else { Join-Path $env:LOCALAPPDATA 'bloom-meeting' }
$Ref = if ($env:BLOOM_REF) { $env:BLOOM_REF } else { 'main' }
$App = Join-Path $Dir 'app'
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ('bloom-install-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

function Z([string]$s) { [regex]::Unescape($s) }
function Say([string]$zh, [string]$en) { Write-Host ('  ' + (Z $zh)) -ForegroundColor Cyan; if ($en) { Write-Host ('  ' + $en) -ForegroundColor DarkGray } }
function Ok([string]$t) { Write-Host ('  [ok] ' + $t) -ForegroundColor Green }
function Warn([string]$t) { Write-Host ('  [!] ' + $t) -ForegroundColor Yellow }
function Headers([string]$accept) {
    $h = @{ 'User-Agent' = 'bloom-installer' }
    if ($accept) { $h['Accept'] = $accept }
    if ($env:GITHUB_TOKEN) { $h['Authorization'] = 'Bearer ' + $env:GITHUB_TOKEN }
    return $h
}
# native programs: their stderr must not stop the script (PowerShell 5.1 turns it into an error under 'Stop');
# success is judged by the exit code instead
function Native([scriptblock]$b) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $b 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $old }
}
function Refresh-Path {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine'); $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($m, $u, (Join-Path $env:USERPROFILE '.local\bin'), $env:Path | Where-Object { $_ }) -join ';'
}
# write JSON as UTF-8 without a BOM (Set-Content -Encoding UTF8 in 5.1 adds one)
function Write-Json($obj, [string]$path) {
    [IO.File]::WriteAllText($path, ($obj | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding $false))
}

Write-Host ''
Write-Host '  Bloom' -ForegroundColor White
Say '\u7ec4\u4f1a\u52a9\u624b\uff1a\u4f1a\u4e0a\u8bf4\u5b9a\u7684\u6539\u6cd5\uff0c\u76f4\u63a5\u6539\u8fdb\u8bba\u6587\u3002' 'A meeting assistant that turns spoken decisions into live paper revisions.'
Write-Host ''

if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) { throw 'Bloom needs 64-bit Windows 10/11.' }

# ---------------------------------------------------------------- 1. uv and ffmpeg
Refresh-Path
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say '\u5b89\u88c5 uv\uff08Python \u7ba1\u7406\u5de5\u5177\uff09...' 'Installing uv...'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    Refresh-Path
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv did not install. See https://docs.astral.sh/uv/' }
}
Ok ('uv ' + ((Native { uv --version }) -replace '^uv\s+', ''))
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say '\u5b89\u88c5 ffmpeg\uff08\u5f55\u4f1a\u8bae\u7a97\u53e3\u7528\uff09...' 'Installing ffmpeg (records the meeting window)...'
        Native { winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements } | Out-Null
        Refresh-Path
    }
}
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Ok 'ffmpeg' } else { Warn 'ffmpeg is missing (needed to record the meeting window): winget install Gyan.FFmpeg' }

# ---------------------------------------------------------------- 2. the backend
$zip = Join-Path $Tmp 'src.zip'
if ($env:BLOOM_SOURCE) {
    Copy-Item -LiteralPath $env:BLOOM_SOURCE -Destination $zip
} else {
    Say ('\u4e0b\u8f7d Bloom\uff08' + $Ref + '\uff09...') "Downloading Bloom ($Ref)..."
    Invoke-WebRequest -Uri "https://api.github.com/repos/$Repo/zipball/$Ref" -Headers (Headers '') -OutFile $zip -UseBasicParsing
}
$unz = Join-Path $Tmp 'src'
Expand-Archive -LiteralPath $zip -DestinationPath $unz -Force
$top = Get-ChildItem -LiteralPath $unz -Directory | Select-Object -First 1
$srcRoot = if ((Test-Path (Join-Path $unz 'pyproject.toml'))) { $unz } else { $top.FullName }
New-Item -ItemType Directory -Force -Path $App | Out-Null
# update in place: your config.json and the Python environment stay
Native { robocopy $srcRoot $App /MIR /XD .venv /XF config.json /NFL /NDL /NJH /NJS /NP } | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying the files failed (robocopy $LASTEXITCODE)" }
Ok "Bloom -> $App"

Say '\u51c6\u5907 Python \u73af\u5883\uff08\u7b2c\u4e00\u6b21\u8981\u4e0b\u8f7d\uff0c\u51e0\u5206\u949f\uff09...' 'Setting up Python (first time takes a few minutes)...'
$venv = Join-Path $App '.venv'
if ((Test-Path $venv) -and -not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Remove-Item -Recurse -Force $venv            # a broken environment (interrupted before): make a fresh one
}
Push-Location $App
try {
    $out = Native { uv sync --python 3.14 --quiet }
    if ($LASTEXITCODE -ne 0) { $out | Select-Object -Last 15 | ForEach-Object { Write-Host "    $_" }; throw 'uv sync failed' }
} finally { Pop-Location }
$py = Join-Path $App '.venv\Scripts\python.exe'
Ok 'Python 3.14 + dependencies'

$cfg = Join-Path $App 'config.json'
if (-not (Test-Path $cfg)) {
    $c = [IO.File]::ReadAllText((Join-Path $App 'config.example.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
    $c.meeting.me = $env:USERNAME
    Write-Json $c $cfg
    Ok "config.json (host name: $env:USERNAME, edit it any time)"
}

# ---------------------------------------------------------------- 3. the desktop app
if ($env:BLOOM_NO_APP -ne '1') {
    Say '\u4e0b\u8f7d\u684c\u9762\u7aef...' 'Downloading the desktop app...'
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers (Headers 'application/vnd.github+json')
    $asset = $rel.assets | Where-Object { $_.name -like '*setup.exe' } | Select-Object -First 1
    if (-not $asset) { throw "no *setup.exe in release $($rel.tag_name)" }
    $exe = Join-Path $Tmp $asset.name
    Invoke-WebRequest -Uri $asset.url -Headers (Headers 'application/octet-stream') -OutFile $exe -UseBasicParsing
    Say ('\u5b89\u88c5\u684c\u9762\u7aef ' + $rel.tag_name + '...') "Installing the desktop app $($rel.tag_name)..."
    $p = Start-Process -FilePath $exe -ArgumentList '/S' -PassThru -Wait
    if ($p.ExitCode -ne 0) { throw "the desktop installer exited with $($p.ExitCode)" }
    Ok "Bloom $($rel.tag_name)"
}

# ---------------------------------------------------------------- 4. point the app at the backend
$bdir = Join-Path $env:USERPROFILE '.blurt'
New-Item -ItemType Directory -Force -Path $bdir | Out-Null
$bcfg = Join-Path $bdir 'config.json'
$j = if (Test-Path $bcfg) { [IO.File]::ReadAllText($bcfg, [Text.Encoding]::UTF8).TrimStart([char]0xFEFF) | ConvertFrom-Json } else { New-Object PSObject }
if (-not $j.PSObject.Properties['zuhui']) { $j | Add-Member -NotePropertyName zuhui -NotePropertyValue (New-Object PSObject) }
$j.zuhui | Add-Member -NotePropertyName project_dir -NotePropertyValue $App -Force
$j.zuhui | Add-Member -NotePropertyName python -NotePropertyValue $py -Force
Write-Json $j $bcfg
Ok 'desktop app -> backend'

# a quick check: the backend imports
Push-Location $App
try { $chk = Native { & $py -X utf8 -c "import zuhui.app, zuhui.view; print('backend ok')" } } finally { Pop-Location }
if ($LASTEXITCODE -eq 0) { Ok 'backend imports' } else { $chk | Select-Object -Last 6 | ForEach-Object { Write-Host "    $_" }; throw 'the backend does not import (see above)' }

# ---------------------------------------------------------------- Claude
Refresh-Path
$claude = Get-Command claude -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
Write-Host ''
if ($claude) {
    Ok 'Claude Code'
    Say '\u5982\u679c\u8fd8\u6ca1\u767b\u5f55\uff1a\u5728\u7ec8\u7aef\u91cc\u8fd0\u884c claude \u767b\u5f55\u4e00\u6b21\uff1b\u6216\u8005\u5728 Bloom \u7684 \u8bbe\u7f6e \u2192 \u901a\u7528 \u2192 \u6a21\u578b\u6765\u6e90 \u91cc\u586b API key\uff08DeepSeek\u3001Kimi \u7b49\uff09\u3002' 'If you have not signed in: run `claude` once, or put an API key (DeepSeek, Kimi, ...) in Bloom: Settings > General > Model source.'
} else {
    Warn 'Claude Code is missing: Bloom runs its models through it (sign in with Claude, or use an API key from Settings > Model source).'
    Say '\u5b89\u88c5\uff1airm https://claude.ai/install.ps1 | iex   \u7136\u540e\u8fd0\u884c claude \u767b\u5f55\uff0c\u6216\u5728 Bloom \u8bbe\u7f6e\u91cc\u586b API key' 'Install: irm https://claude.ai/install.ps1 | iex   then run `claude` to sign in, or put an API key in Bloom settings'
}
Write-Host ''
Say '\u88c5\u597d\u4e86\u3002\u5f00\u59cb\u83dc\u5355\u91cc\u6253\u5f00 Bloom\uff0c\u628a\u8bba\u6587\u62d6\u8fdb\u53bb\uff0c\u70b9\u300c\u5f00\u4f1a\u300d\u3002' 'Done. Open Bloom from the Start menu, drop a paper in, and start a meeting.'
Write-Host ''
