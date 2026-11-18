# Install the "bloom" Agent Skill (reads Bloom meetings) for the AI agents on this PC.
#   ~/.agents/skills : Codex, Gemini CLI, Cursor, GitHub Copilot, OpenCode
#   ~/.claude/skills : Claude Code (only if ~/.claude exists)
# Run again to update.  irm https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/skills/install.ps1 | iex
& {
    $ErrorActionPreference = 'Stop'
    $repo = if ($env:BLOOM_REPO) { $env:BLOOM_REPO } else { 'lightblue888/bloom-meeting' }
    $base = if ($env:BLOOM_SKILL_BASE) { $env:BLOOM_SKILL_BASE } else { "https://raw.githubusercontent.com/$repo/main/skills/bloom" }
    $files = @('SKILL.md', 'scripts/watch.py', 'scripts/history.py')
    $roots = @(Join-Path $HOME '.agents\skills')
    if (Test-Path (Join-Path $HOME '.claude')) { $roots += (Join-Path $HOME '.claude\skills') }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('bloom-skill-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force (Join-Path $tmp 'scripts') | Out-Null
    try {
        foreach ($f in $files) {
            Invoke-WebRequest -UseBasicParsing "$base/$f" -OutFile (Join-Path $tmp $f)
        }
        foreach ($r in $roots) {
            $d = Join-Path $r 'bloom'
            New-Item -ItemType Directory -Force (Join-Path $d 'scripts') | Out-Null
            foreach ($f in $files) { Copy-Item (Join-Path $tmp $f) (Join-Path $d $f) -Force }
            Write-Host "bloom -> $d"
        }
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}
