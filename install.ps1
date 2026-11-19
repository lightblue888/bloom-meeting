# Bloom installer for Windows 10/11 (x64). One line in PowerShell:
#
#   irm https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/install.ps1 | iex
#
# What it does (nothing else; run it again to update):
#   1. uv (Python manager) and ffmpeg, if missing (uv from astral.sh; ffmpeg with winget, else Gyan's build from GitHub
#      into ~/.blurt/bin) - an ffmpeg that can record a window (gfxcapture) and has ffplay
#   2. the meeting backend (this repository) into %LOCALAPPDATA%\bloom-meeting, with its own Python environment
#      (with the relay and voice extras; pandoc comes with it, as pypandoc-binary)
#   3. the desktop app (*setup.exe from the latest GitHub release), installed for this user only
#   4. ~/.blurt/config.json pointed at the backend, with the live transcript's meeting settings (record.live_fast);
#      config.json created from config.example.json (kept on updates)
#   5. what a meeting needs on this PC, so that the first launch downloads nothing: the speech model Fun-ASR-Nano
#      (~800 MB, fetched by the app's own transcribe.py, so the same files in the same place), the turn-taking model
#      smart-turn v3.2 (8 MB), and the Python packages of the recorder and the transcriber
# The models: the user picks a provider and pastes an API key in the app (Settings > General > Model source).
#
# Options (environment variables, since `irm | iex` takes no arguments):
#   BLOOM_DIR      where the backend goes            (default %LOCALAPPDATA%\bloom-meeting)
#   BLOOM_REF      branch or tag to install          (default main)
#   BLOOM_SOURCE   a local .zip of the repository    (instead of downloading; for testing)
#   BLOOM_NO_APP   1 = skip the desktop app          (backend only)
#   BLOOM_REPO     owner/name of the repository      (default lightblue888/bloom-meeting; a fork)
#   BLOOM_API      the GitHub API base               (default https://api.github.com; a mirror)
#   GITHUB_TOKEN   a token, if the repository is private
#
# Everything runs inside one script block: nothing it sets (error handling, variables, functions) stays in your
# PowerShell window, and its temporary folder goes away even when a step fails.
#
# This file is plain ASCII on purpose: Windows PowerShell 5.1 reads a script file without a BOM in the system code
# page, and non-ASCII text there can swallow the next line. Chinese messages are written as \uXXXX and decoded at run
# time (Z).

& {
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = if ($env:BLOOM_REPO) { $env:BLOOM_REPO } else { 'lightblue888/bloom-meeting' }
$Api = if ($env:BLOOM_API) { $env:BLOOM_API.TrimEnd('/') } else { 'https://api.github.com' }
$Dir = if ($env:BLOOM_DIR) { $env:BLOOM_DIR } else { Join-Path $env:LOCALAPPDATA 'bloom-meeting' }
$Ref = if ($env:BLOOM_REF) { $env:BLOOM_REF } else { 'main' }
$App = Join-Path $Dir 'app'
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ('bloom-install-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
# the desktop app's home (its config, the speech models, and the tools it puts in bin): $BLURT_HOME, else ~/.blurt
$BlurtHome = if ($env:BLURT_HOME) { $env:BLURT_HOME } else { Join-Path $env:USERPROFILE '.blurt' }
$BlurtBin = Join-Path $BlurtHome 'bin'

# Gyan's essentials build (ffmpeg, ffplay, ffprobe; gfxcapture since FFmpeg 8), for PCs without winget
$FFmpegVer = '9.0.2'
$FFmpegUrls = @("https://github.com/GyanD/codexffmpeg/releases/download/$FFmpegVer/ffmpeg-$FFmpegVer-essentials_build.zip",
                'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip')
# Pipecat smart-turn v3.2 (BSD-2), the file zuhui/turn.py loads. huggingface.co does not load from mainland China
# without a proxy: hf-mirror.com next (the same file, checked against the SHA-256 below)
$SmartTurnUrls = @('https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx',
                   'https://hf-mirror.com/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx')
$SmartTurnSize = 8679182
$SmartTurnSha = '2BB026316B14A660486A75B1733CD3FBAB8C2FD0314DC9AF7BE49F8CCA967E4F'
# the live transcript's model (Blurt's asr_models.json id) and Bloom's settings for it in ~/.blurt/config.json
$AsrModel = 'fun-asr-nano-onnx'
$Hotwords = if ($env:ZUHUI_HOME) { (Join-Path $env:ZUHUI_HOME 'lexicon\hotwords.json') } else { '~/.zuhui/lexicon/hotwords.json' }
$LiveFast = [ordered]@{ max_gap = 0.5; close_margin = 0.1; max_chunk = 10; preview = 2.5; speculate = $true; vocab_file = $Hotwords }

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
function Read-Json([string]$path) {
    $o = if (Test-Path -LiteralPath $path) { [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8).TrimStart([char]0xFEFF) | ConvertFrom-Json } else { $null }
    if ($o) { return $o } else { return New-Object PSObject }
}
# $o.<name> as an object: made when it is missing (or not an object), kept when it is there
function Get-Obj($o, [string]$name) {
    $p = $o.PSObject.Properties[$name]
    if (-not $p -or -not ($p.Value -is [System.Management.Automation.PSCustomObject])) {
        $o | Add-Member -NotePropertyName $name -NotePropertyValue (New-Object PSObject) -Force
    }
    return $o.$name
}
# one console line that counts up: "    412.3 / 802.7 MB   51%"
function Show-Bar([double]$done, [double]$total) {
    $s = if ($total -gt 0) { '    {0,8:N1} / {1:N1} MB  {2,3}%' -f ($done / 1MB), ($total / 1MB), [int](100 * $done / $total) } else { '    {0,8:N1} MB' -f ($done / 1MB) }
    Write-Host -NoNewline ("`r" + $s + '    ') -ForegroundColor DarkGray
}
# a download with progress; what an interrupted run left (<dest>.part) is continued. The first URL that works wins;
# with $sha256 the file must match it before it takes its name. -> $true / $false
function Get-File([string[]]$urls, [string]$dest, [string]$sha256) {
    $part = $dest + '.part'
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    foreach ($u in $urls) {
        try {
            $have = if (Test-Path -LiteralPath $part) { (Get-Item -LiteralPath $part).Length } else { 0 }
            $req = [Net.HttpWebRequest]::Create($u)
            $req.UserAgent = 'bloom-installer'
            $req.Timeout = 30000
            $req.ReadWriteTimeout = 60000
            if ($have -gt 0) { $req.AddRange([long]$have) }
            $res = $null
            try { $res = $req.GetResponse() } catch {
                $e = $_.Exception
                while ($e -and -not ($e -is [Net.WebException])) { $e = $e.InnerException }
                # 416: nothing past what is there, the .part is the whole file
                if (-not ($have -gt 0 -and $e -and $e.Response -and [int]$e.Response.StatusCode -eq 416)) { throw }
            }
            if ($res) {
                $mode = if ($have -gt 0 -and [int]$res.StatusCode -eq 206) { 'Append' } else { $have = 0; 'Create' }
                $total = if ($res.ContentLength -ge 0) { $have + $res.ContentLength } else { 0 }
                $in = $res.GetResponseStream()
                $out = New-Object IO.FileStream($part, [IO.FileMode]$mode, [IO.FileAccess]::Write)
                $done = $have
                try {
                    $buf = New-Object byte[] 262144
                    $sw = [Diagnostics.Stopwatch]::StartNew()
                    while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                        $out.Write($buf, 0, $n)
                        $done += $n
                        if ($sw.ElapsedMilliseconds -ge 500) { Show-Bar $done $total; $sw.Restart() }
                    }
                    Show-Bar $done $total
                    Write-Host ''
                } finally { $out.Dispose(); $in.Dispose(); $res.Close() }
                if ($total -gt 0 -and $done -lt $total) { throw "the download stopped at $done of $total bytes" }
            }
            if ($sha256 -and (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash -ne $sha256) {
                Remove-Item -LiteralPath $part -Force
                throw 'the file came down damaged (checksum)'
            }
            Move-Item -LiteralPath $part -Destination $dest -Force
            return $true
        } catch {
            Write-Host ''
            Warn ($u + ': ' + $_.Exception.Message)
        }
    }
    return $false
}
# a native command whose stderr reports "  N/M MB" (Blurt's progress_line): the numbers count up on one line, the
# other lines are kept and returned (the caller shows the last few when it failed). Exit code in $LASTEXITCODE.
function Run-Progress([scriptblock]$b) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $keep = New-Object Collections.Generic.List[string]
    $bar = $false
    try {
        & $b 2>&1 | ForEach-Object {
            $s = "$_"
            if ($s -match '^\s*([\d.]+)/([\d.]+) MB\s*$') {
                Show-Bar ([double]$Matches[1] * 1MB) ([double]$Matches[2] * 1MB)
                $bar = $true
            } elseif ($s -match '^unpacking ') {
                if ($bar) { Write-Host ''; $bar = $false }
                Say '\u89e3\u538b\uff08\u7ea6 2 \u5206\u949f\uff0c\u8fd9\u6bb5\u6ca1\u6709\u8fdb\u5ea6\u6761\uff09...' 'Unpacking (about 2 minutes, no progress shown)...'
            } elseif ($s.Trim()) { $keep.Add($s) }
        }
    } finally {
        $ErrorActionPreference = $old
        if ($bar) { Write-Host '' }
    }
    return , $keep.ToArray()
}
# the ffmpeg Bloom will use: the first on PATH, else the one in ~/.blurt/bin (where the app and the backend look next)
function Find-FFmpeg {
    $c = Get-Command ffmpeg -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Path }
    $b = Join-Path $BlurtBin 'ffmpeg.exe'
    if (Test-Path -LiteralPath $b) { return $b }
    return $null
}
# good enough for a meeting: records a window (gfxcapture; older builds record it black) and has an ffplay
function Test-FFmpeg([string]$ff) {
    if (-not $ff -or -not (Test-Path -LiteralPath $ff)) { return $false }
    if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $ff) 'ffplay.exe')) -and -not (Get-Command ffplay -ErrorAction SilentlyContinue)) { return $false }
    $h = Native { & $ff -hide_banner -h filter=gfxcapture }
    return [bool](($h -join "`n") -match 'Filter gfxcapture')
}
# ffmpeg.exe, ffplay.exe and ffprobe.exe out of Gyan's zip into $dir
function Expand-FFmpeg([string]$zip, [string]$dir) {
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $z = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($e in $z.Entries) {
            if ($e.FullName -match '[\\/]bin[\\/](ffmpeg|ffplay|ffprobe)\.exe$') {
                $dst = Join-Path $dir $e.Name
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dst + '.part', $true)
                Move-Item -LiteralPath ($dst + '.part') -Destination $dst -Force
            }
        }
    } finally { $z.Dispose() }
}

try {
Write-Host ''
Write-Host '  Bloom' -ForegroundColor White
Say '\u7ec4\u4f1a\u52a9\u624b\uff1a\u4f1a\u4e0a\u8bf4\u5b9a\u7684\u6539\u6cd5\uff0c\u76f4\u63a5\u6539\u8fdb\u8bba\u6587\u3002' 'A meeting assistant that turns spoken decisions into live paper revisions.'
Write-Host ''

if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) { throw 'Bloom needs 64-bit Windows 10/11.' }

# ---------------------------------------------------------------- 1. uv and ffmpeg
Refresh-Path
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say '\u5b89\u88c5 uv\uff08Python \u7ba1\u7406\u5de5\u5177\uff09...' 'Installing uv...'
    # in a child PowerShell with the policy lifted for that one process: astral's script refuses to run under the default
    # policy of a new PC (Restricted) and its exit would close this window
    $out = Native { powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' }
    if ($LASTEXITCODE -ne 0) { $out | Select-Object -Last 8 | ForEach-Object { Write-Host "    $_" } }
    Refresh-Path
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv did not install. See https://docs.astral.sh/uv/' }
}
Ok ('uv ' + ((Native { uv --version }) -replace '^uv\s+', ''))
$ff = Find-FFmpeg
if (-not (Test-FFmpeg $ff) -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    # missing, or too old to record a window: winget installs (or upgrades) Gyan's build
    Say '\u5b89\u88c5 ffmpeg\uff08\u5f55\u4f1a\u8bae\u7a97\u53e3\u7528\uff09...' 'Installing ffmpeg (records the meeting window)...'
    Native { winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements } | Out-Null
    Refresh-Path
    $ff = Find-FFmpeg
}
if (-not (Test-FFmpeg $ff)) {
    # no winget (or it did not help): Gyan's build into ~/.blurt/bin. The desktop app then skips its own fallback
    # (ffmpeg 6.1, which has neither ffplay nor gfxcapture)
    Say '\u4e0b\u8f7d ffmpeg\uff08\u5f55\u4f1a\u8bae\u7a97\u53e3\u7528\uff09...' "Downloading ffmpeg $FFmpegVer (records the meeting window)..."
    $fzip = Join-Path ([IO.Path]::GetTempPath()) "bloom-ffmpeg-$FFmpegVer.zip"
    if (Get-File $FFmpegUrls $fzip '') {
        try { Expand-FFmpeg $fzip $BlurtBin; Remove-Item -LiteralPath $fzip -Force } catch { Warn ('unpacking ffmpeg failed: ' + $_.Exception.Message) }
    }
    $mine = Join-Path $BlurtBin 'ffmpeg.exe'
    if ((Test-FFmpeg $mine) -and $ff -and $ff -ne $mine) {
        Warn "an older ffmpeg comes first on PATH ($ff) and cannot record a window: remove it, or put $BlurtBin before it"
    }
    if (-not $ff -or (Test-FFmpeg $mine)) { $ff = $mine }
}
if (Test-FFmpeg $ff) { Ok "ffmpeg ($ff)" } else { Warn 'ffmpeg is missing or cannot record a window (needs FFmpeg 8+ with ffplay): winget install Gyan.FFmpeg' }

# ---------------------------------------------------------------- 2. the backend
$zip = Join-Path $Tmp 'src.zip'
if ($env:BLOOM_SOURCE) {
    Copy-Item -LiteralPath $env:BLOOM_SOURCE -Destination $zip
} else {
    Say ('\u4e0b\u8f7d Bloom\uff08' + $Ref + '\uff09...') "Downloading Bloom ($Ref)..."
    Invoke-WebRequest -Uri "$Api/repos/$Repo/zipball/$Ref" -Headers (Headers '') -OutFile $zip -UseBasicParsing
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
    # relay: people joining from a browser; voice: edge-tts. Both are part of a normal install. With uv.lock: exactly
    # the versions Bloom was tested with (a new release of a library can't break a fresh install)
    $lockArgs = if (Test-Path -LiteralPath (Join-Path $App 'uv.lock')) { @('--frozen', '--inexact') } else { @('--inexact') }
    $out = Native { uv sync --python 3.14 @lockArgs --quiet --extra relay --extra voice }
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
    $rel = Invoke-RestMethod -Uri "$Api/repos/$Repo/releases/latest" -Headers (Headers 'application/vnd.github+json')
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
New-Item -ItemType Directory -Force -Path $BlurtHome | Out-Null
$bcfg = Join-Path $BlurtHome 'config.json'
$j = Read-Json $bcfg
$z = Get-Obj $j 'zuhui'
$z | Add-Member -NotePropertyName project_dir -NotePropertyValue $App -Force
$z | Add-Member -NotePropertyName python -NotePropertyValue $py -Force
# the live transcript in a meeting: short chunks cut at short pauses, and the paper's terms as hotwords (the backend
# writes them, zuhui/lexicon.py). Settings already there stay; only a missing hotword file is added to them
$rec = Get-Obj $j 'record'
if (-not $rec.PSObject.Properties['live_fast']) {
    $rec | Add-Member -NotePropertyName live_fast -NotePropertyValue ([PSCustomObject]$LiveFast)
} elseif ($rec.live_fast -is [System.Management.Automation.PSCustomObject] -and -not $rec.live_fast.PSObject.Properties['vocab_file']) {
    $rec.live_fast | Add-Member -NotePropertyName vocab_file -NotePropertyValue $Hotwords
}
Write-Json $j $bcfg
Ok 'desktop app -> backend'

# a quick check: the backend imports
Push-Location $App
try { $chk = Native { & $py -X utf8 -c "import zuhui.app, zuhui.view; print('backend ok')" } } finally { Pop-Location }
if ($LASTEXITCODE -eq 0) { Ok 'backend imports' } else { $chk | Select-Object -Last 6 | ForEach-Object { Write-Host "    $_" }; throw 'the backend does not import (see above)' }

# ---------------------------------------------------------------- 5. what a meeting needs on this PC
$models = Join-Path $BlurtHome 'models'
New-Item -ItemType Directory -Force -Path $models | Out-Null

# smart-turn: has the speaker finished? (zuhui/turn.py; without it only the pause and the word list decide)
$st = Join-Path $models 'smart-turn-v3.2-cpu.onnx'
if (-not ((Test-Path -LiteralPath $st) -and (Get-Item -LiteralPath $st).Length -eq $SmartTurnSize)) {
    Say '\u4e0b\u8f7d\u5224\u505c\u6a21\u578b smart-turn\uff088 MB\uff09...' 'Downloading the turn-taking model smart-turn (8 MB)...'
    [void](Get-File $SmartTurnUrls $st $SmartTurnSha)
}
if ((Test-Path -LiteralPath $st) -and (Get-Item -LiteralPath $st).Length -eq $SmartTurnSize) { Ok 'smart-turn v3.2' } else { Warn 'smart-turn did not download (run this again; meetings still work, turn-taking is slower)' }

# the recorder's and the transcriber's packages (the app runs these scripts with uv; installed now, nothing to fetch in
# the first meeting), then the speech model, fetched by the app's own transcribe.py: same URL, same files, same folder
$scripts = Join-Path $App 'desktop\skills\blurt\scripts'
if (Test-Path -LiteralPath (Join-Path $scripts 'transcribe.py')) {
    Say '\u51c6\u5907\u5f55\u97f3\u548c\u8f6c\u5199\u7528\u7684 Python \u5305...' 'Setting up the recorder and the transcriber (Python packages)...'
    foreach ($s in 'record.py', 'live.py') {
        $out = Native { uv sync --quiet --script (Join-Path $scripts $s) }
        if ($LASTEXITCODE -ne 0) { $out | Select-Object -Last 5 | ForEach-Object { Write-Host "    $_" }; Warn "the packages of $s did not install (the app fetches them when it needs them)" }
    }
    Say '\u4e0b\u8f7d\u8bed\u97f3\u8bc6\u522b\u6a21\u578b Fun-ASR-Nano\uff08\u7ea6 800 MB\uff0c\u53ea\u4e0b\u4e00\u6b21\uff09...' 'Downloading the speech model Fun-ASR-Nano (about 800 MB, once)...'
    $tail = Run-Progress { uv run --quiet --script (Join-Path $scripts 'transcribe.py') setup --model-id $AsrModel }
    if ($LASTEXITCODE -eq 0) {
        # the app's own first-run download (record.dictation.first_download) is then done
        $j = Read-Json $bcfg
        $d = Get-Obj (Get-Obj $j 'record') 'dictation'
        if (-not $d.PSObject.Properties['first_download']) {
            $d | Add-Member -NotePropertyName first_download -NotePropertyValue ((Get-Date).ToUniversalTime().ToString('o'))
            Write-Json $j $bcfg
        }
        Ok 'Fun-ASR-Nano'
    } else {
        $tail | Select-Object -Last 6 | ForEach-Object { Write-Host "    $_" }
        Warn 'the speech model did not download: run this again (it continues where it stopped), or the app fetches it at first launch'
    }
} else {
    Warn "no transcribe.py in ${scripts}: the app downloads the speech model at first launch"
}

# ---------------------------------------------------------------- done
Refresh-Path
Write-Host ''
if (Get-Process -Name bloom, blurt -ErrorAction SilentlyContinue) {
    # an update while Bloom runs: the running one still has the old backend
    Say 'Bloom \u6b63\u5f00\u7740\uff1a\u4ece\u901a\u77e5\u533a\u9000\u51fa\u540e\u91cd\u65b0\u6253\u5f00\uff0c\u66f4\u65b0\u624d\u751f\u6548\u3002' 'Bloom is running: quit it from the notification area and open it again to use the update.'
    Write-Host ''
}
Say '\u7b2c\u4e00\u6b21\u6253\u5f00 Bloom \u65f6\uff0c\u5728 \u8bbe\u7f6e \u2192 \u901a\u7528 \u2192 \u6a21\u578b\u6765\u6e90 \u91cc\u9009\u670d\u52a1\u5546\u3001\u586b API key\u3002' 'First time: open Bloom, then Settings > General > Model source, pick a provider and paste an API key.'
Write-Host ''
Say '\u88c5\u597d\u4e86\u3002\u5f00\u59cb\u83dc\u5355\u91cc\u6253\u5f00 Bloom\uff0c\u628a\u8bba\u6587\u62d6\u8fdb\u53bb\uff0c\u70b9\u300c\u5f00\u4f1a\u300d\u3002' 'Done. Open Bloom from the Start menu, drop a paper in, and start a meeting.'
Write-Host ''
} finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
}
