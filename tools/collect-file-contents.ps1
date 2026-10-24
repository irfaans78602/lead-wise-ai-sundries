<#
.SYNOPSIS
    Collects files by extension from the sundries repo tree and writes
    one output file per routed destination.

.DESCRIPTION
    The sundries repo is a small Flask backend. Its layout is flat:

        ask_ai.py           /api/ask-ai blueprint (CORS, signature, RAG)
        cli.py              ingest / serve entrypoint
        db.py               master + tenant connection pools
        env.py              .env loader
        gemini.py           embed_query / embed_document / generate_answer
        ingest_rag.py       JSON/HTML/PDF/URL ingestion pipeline
        main.py             Flask app factory
        security.py         HMAC-SHA256 signing
        sign_ask_ai.py      emit a signed <script> tag
        secrets/README.md   signpost (real secrets live in the loader repo)
        requirements.txt
        pyproject.toml      (if added)
        .env                (gitignored — MASTER_DATABASE_URL, LINK_SIGNING_KEYS)
        .env.example
        README.md
        .gitignore

    Output routing (all under $OutputRoot):

      bases\        dotfiles.txt                  .gitignore and other dotfiles
      bases\        env-real.txt                  .env (holds real credentials)
      bases\        env-example.txt               .env.example
      bases\        md.txt, txt.txt, …            routed base extensions
      bases\        python.txt                    *.py outside src/ or tests/ (rare)

      others\py.txt                               every *.py at the repo root
      others\tests\py.txt                         tests/**/*.py (if you add tests)
      others\ps1\ps1.txt                          *.ps1

      others\common\html_json\json.txt            any *.json (unlikely)
      others\common\html_json\html.txt            any *.html (unlikely)

      others\yml.txt                              *.yml
      others\toml.txt                             *.toml

    Every entry in every output file starts with its full source path on its
    own line, so mixed-content buckets remain unambiguous.

    Excluded folders (at any depth):
      node_modules, __pycache__, .pytest_cache, .mypy_cache, .venv,
      venv, build, dist, htmlcov, logs, .git

    Excluded extensions: log

    Excluded files: tree.txt itself, and anything under $OutputRoot.

.PARAMETER Extension
    Optional. A single extension to collect, without the leading dot.

.EXAMPLE
    .\collect-file-contents.ps1
    .\collect-file-contents.ps1 -Extension py
#>

param(
    [Parameter(Mandatory = $false)]
    [ValidateSet(
        'js', 'mjs', 'cjs', 'ts',
        'json', 'map',
        'html', 'css',
        'svg', 'png', 'woff2',
        'md', 'txt', 'xml',
        'ps1', 'py',
        'yml', 'toml',
        'gitignore'
    )]
    [string]$Extension
)

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
$SearchPath = (Get-Location).Path
$OutputRoot = 'C:\Irfaan\leadwiseai\lead-wise-ai-extensions'
$BasesDir   = Join-Path -Path $OutputRoot -ChildPath 'bases'
$OthersDir  = Join-Path -Path $OutputRoot -ChildPath 'others'

$BasesExtensions = @(
    'md', 'txt', 'xml', 'toml'
    # 'py' is routed by Get-Destination (root vs tests vs other).
)

$ExcludedFolders = @(
    'node_modules',
    '__pycache__',
    '.pytest_cache',
    '.mypy_cache',
    '.venv',
    'venv',
    'build',
    'dist',
    'htmlcov',
    'logs',
    '.git'
)

$ExcludedExtensions = @('log')

$AllowedExtensions = @(
    'js', 'mjs', 'cjs', 'ts',
    'json', 'map',
    'html', 'css',
    'svg', 'png', 'woff2',
    'md', 'txt', 'xml',
    'ps1', 'py',
    'yml', 'toml',
    'gitignore'
)

$NoExtensionAllowList = @()

# Real .env file (holds MASTER_DATABASE_URL, LINK_SIGNING_KEYS).
# Do not share the collected output.
$RealEnvNames = @('.env')
$EnvExamplePattern = '.env.*.example|\.env\.example'

# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------

function Test-IsExcludedFolder {
    param([string]$Path)
    $segments = $Path -split '[\\/]'
    foreach ($segment in $segments) {
        foreach ($folder in $ExcludedFolders) {
            if ($segment -ieq $folder) { return $true }
        }
    }
    return $false
}

function Test-IsDotFile {
    param([System.IO.FileInfo]$File)
    return $File.Name.StartsWith('.')
}

function Get-FileExtension {
    param([System.IO.FileInfo]$File)
    $name = $File.Name

    if ($name.StartsWith('.')) {
        $rest = $name.Substring(1)
        if ($rest.Length -eq 0) { return '__noext__' }
        if ($rest.IndexOf('.') -ge 0) {
            return $File.Extension.TrimStart('.').ToLower()
        }
        return $rest.ToLower()
    }

    if ($name.IndexOf('.') -lt 0) { return '__noext__' }
    return $File.Extension.TrimStart('.').ToLower()
}

function Test-IsAllowedNoExtensionFile {
    param([System.IO.FileInfo]$File)
    foreach ($name in $NoExtensionAllowList) {
        if ($File.Name -ieq $name) { return $true }
    }
    return $false
}

function Get-RelativePath {
    param(
        [string]$FullPath,
        [string]$RootPath
    )
    $root = $RootPath.TrimEnd('\','/')
    $rel  = $FullPath.Substring($root.Length).TrimStart('\','/')
    return ($rel -replace '\\','/')
}

function Read-FileContent {
    param([System.IO.FileInfo]$File)
    try {
        return [System.IO.File]::ReadAllText($File.FullName, [System.Text.Encoding]::UTF8)
    }
    catch {
        Write-Warning "    Failed to read: $($File.FullName) -- $($_.Exception.Message)"
        return "[ERROR reading file: $($_.Exception.Message)]"
    }
}

# ------------------------------------------------------------------
# Routing
# ------------------------------------------------------------------

function Get-Destination {
    param(
        [System.IO.FileInfo]$File,
        [string]$RootPath
    )

    $ext = Get-FileExtension -File $File
    $rel = Get-RelativePath -FullPath $File.FullName -RootPath $RootPath
    $relLower = $rel.ToLower()
    $nameLower = $File.Name.ToLower()

    # --- Real .env (credentials) -> bases\env-real.txt ----------------------
    foreach ($envName in $RealEnvNames) {
        if ($nameLower -eq $envName.ToLower()) {
            return @{ DestDir = $BasesDir; BaseName = 'env-real' }
        }
    }

    # --- .env.example -> bases\env-example.txt ------------------------------
    if ($nameLower -match $EnvExamplePattern) {
        return @{ DestDir = $BasesDir; BaseName = 'env-example' }
    }

    # --- Other dotfiles -> bases\dotfiles.txt -------------------------------
    if (Test-IsDotFile -File $File) {
        return @{ DestDir = $BasesDir; BaseName = 'dotfiles' }
    }

    # --- Bases (flat file per extension) ------------------------------------
    if ($ext -eq '__noext__' -or $BasesExtensions -contains $ext) {
        return @{
            DestDir  = $BasesDir
            BaseName = if ($ext -eq '__noext__') { 'no-extension' } else { $ext -replace '\.','-' }
        }
    }

    # --- PS1 ----------------------------------------------------------------
    if ($ext -eq 'ps1') {
        return @{
            DestDir  = Join-Path -Path $OthersDir -ChildPath 'ps1'
            BaseName = 'ps1'
        }
    }

    # --- PY: root flat files vs tests/ --------------------------------------
    if ($ext -eq 'py') {
        # tests\**\*.py -> others\tests\py.txt
        if ($relLower -match '(^|/)tests/') {
            return @{
                DestDir  = Join-Path $OthersDir 'tests'
                BaseName = 'py'
            }
        }
        # Root-level *.py -> others\py.txt
        if ($relLower -notmatch '/') {
            return @{
                DestDir  = $OthersDir
                BaseName = 'py'
            }
        }
        # Fallback: *.py in a subfolder that isn't tests/ (e.g. future scripts/).
        # Put them with the root-level py bucket — the file path header in the
        # output makes their subfolder obvious.
        return @{
            DestDir  = $OthersDir
            BaseName = 'py'
        }
    }

    # --- JSON / HTML: kept for completeness (unlikely in sundries) ----------
    if ($ext -eq 'json') {
        return @{
            DestDir  = Join-Path (Join-Path $OthersDir 'common') 'html_json'
            BaseName = 'json'
        }
    }
    if ($ext -eq 'html') {
        return @{
            DestDir  = Join-Path (Join-Path $OthersDir 'common') 'html_json'
            BaseName = 'html'
        }
    }

    # --- yml / toml / map / ts … --------------------------------------------
    return @{
        DestDir  = $OthersDir
        BaseName = $ext -replace '\.','-'
    }
}

# ------------------------------------------------------------------
# Writers
# ------------------------------------------------------------------

function Write-FlatGroup {
    param(
        [System.IO.FileInfo[]]$Files,
        [string]$Label,
        [string]$BaseName,
        [string]$DestDir,
        [System.Collections.Generic.List[string]]$Summary
    )

    $fileList = @($Files)
    $outFile  = Join-Path -Path $DestDir -ChildPath "$BaseName.txt"

    if (-not (Test-Path -Path $DestDir)) {
        New-Item -Path $DestDir -ItemType Directory -Force | Out-Null
    }

    Write-Host ("Writing {0,4} file(s) -> {1}" -f $fileList.Count, $outFile)

    if (Test-Path -Path $outFile) { Remove-Item -Path $outFile -Force }

    $sb = [System.Text.StringBuilder]::new()
    foreach ($file in $fileList) {
        [void]$sb.AppendLine($file.FullName)
        $content = Read-FileContent -File $file
        if ($null -ne $content) { [void]$sb.AppendLine($content) }
        [void]$sb.AppendLine()
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($outFile, $sb.ToString(), $utf8NoBom)

    $Summary.Add(("{0,-40} {1,5} file(s)  ->  {2}" -f $Label, $fileList.Count, $outFile))
}

# ------------------------------------------------------------------
# Step 1 - Discover files
# ------------------------------------------------------------------
$SpecificExtension = -not [string]::IsNullOrWhiteSpace($Extension)

foreach ($dir in @($BasesDir, $OthersDir)) {
    if (Test-Path -Path $dir) {
        Write-Host "Deleting previous output folder: $dir"
        Remove-Item -Path $dir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

foreach ($dir in @($BasesDir, $OthersDir)) {
    if (-not (Test-Path -Path $dir)) {
        New-Item -Path $dir -ItemType Directory -Force | Out-Null
        Write-Host "Created output directory: $dir"
    }
}

Write-Host "Search root    : $SearchPath"
Write-Host "Output root    : $OutputRoot"
Write-Host "Excluded dirs  : $($ExcludedFolders -join ', ')"
Write-Host "Excluded exts  : $($ExcludedExtensions -join ', ')"
Write-Host ("-" * 80)

$files = New-Object System.Collections.Generic.List[System.IO.FileInfo]

$dirQueue = New-Object System.Collections.Generic.Queue[string]
$dirQueue.Enqueue($SearchPath)

while ($dirQueue.Count -gt 0) {
    $currentDir = $dirQueue.Dequeue()
    if ($currentDir -like "$OutputRoot*") { continue }

    try {
        Get-ChildItem -Path $currentDir -File -Force -ErrorAction SilentlyContinue |
            Where-Object {
                if ($_.Name -ieq 'tree.txt') { return $false }
                $ext = Get-FileExtension -File $_
                if ($ExcludedExtensions -contains $ext) { return $false }
                if (Test-IsDotFile -File $_) {
                    if ($SpecificExtension) { return ($ext -eq $Extension) }
                    return $true
                }
                if ($SpecificExtension) {
                    return ($ext -eq $Extension)
                }
                if ($ext -eq '__noext__') {
                    return (Test-IsAllowedNoExtensionFile -File $_)
                }
                return ($AllowedExtensions -contains $ext)
            } |
            ForEach-Object { $files.Add($_) }
    }
    catch {
        Write-Warning "Could not read files in: $currentDir -- $($_.Exception.Message)"
    }

    try {
        Get-ChildItem -Path $currentDir -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { -not (Test-IsExcludedFolder -Path $_.FullName) -and $_.FullName -notlike "$OutputRoot*" } |
            ForEach-Object { $dirQueue.Enqueue($_.FullName) }
    }
    catch {
        Write-Warning "Could not read directories in: $currentDir -- $($_.Exception.Message)"
    }
}

Write-Host ("Discovered {0} candidate file(s)." -f $files.Count)
Write-Host ("-" * 80)

# ------------------------------------------------------------------
# Step 2 - Route and write
# ------------------------------------------------------------------
$buckets = @{}

foreach ($f in $files) {
    $dest = Get-Destination -File $f -RootPath $SearchPath
    if ($null -eq $dest) { continue }
    $key = ($dest.DestDir + '|' + $dest.BaseName)
    if (-not $buckets.ContainsKey($key)) {
        $buckets[$key] = New-Object System.Collections.Generic.List[System.IO.FileInfo]
    }
    $buckets[$key].Add($f)
}

$summary = New-Object System.Collections.Generic.List[string]

foreach ($key in ($buckets.Keys | Sort-Object)) {
    $parts    = $key -split '\|', 2
    $destDir  = $parts[0]
    $baseName = $parts[1]
    $relDest  = $destDir.Replace($OutputRoot, '').TrimStart('\')
    $label    = "$relDest\$baseName.txt"
    Write-FlatGroup -Files @($buckets[$key]) -Label $label `
                    -BaseName $baseName -DestDir $destDir -Summary $summary
}

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
Write-Host ("-" * 80)
Write-Host "Summary:"
$summary | ForEach-Object { Write-Host "  $_" }
Write-Host ("-" * 80)
Write-Host ("Total files collected: {0}" -f $files.Count)
Write-Host ("Output root          : {0}" -f $OutputRoot)