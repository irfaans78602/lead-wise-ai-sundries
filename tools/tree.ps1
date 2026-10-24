	<#
.SYNOPSIS
    Writes an ASCII folder tree of the current directory to tree.txt
    in the current folder, encoded as UTF-8 without BOM.

.DESCRIPTION
    Runs `tree /f /a` (ASCII box characters, files included), prepends a
    header line with the folder path, and writes the result to tree.txt
    in the current working directory.

    Output is UTF-8 without a BOM so the file displays cleanly in VS Code,
    git diffs, and any UTF-8-aware tool. ASCII box characters (+---, \---, |)
    avoid the mojibake you get from tree's default Unicode mode when the
    console codepage doesn't match.

    tree.txt itself is not excluded from the tree output on subsequent runs,
    but that is harmless — it is a single line at the root. If you want it
    excluded, run this script from a parent folder or clean the file first.

.EXAMPLE
    # From your project root:
    .\tree.ps1
    # Writes: .\tree.txt
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# Current folder as the search root.
$SearchPath = (Get-Location).Path
$TreeFile   = Join-Path -Path $SearchPath -ChildPath 'tree.txt'

Write-Host "Generating ASCII tree in $SearchPath ..."

try {
    # tree /f  = include files
    # tree /a  = ASCII characters instead of Unicode box-drawing
    $treeOutput = cmd /c "tree /f /a"

    # Prepend a header, join with platform newline.
    $header   = "Folder: $SearchPath"
    $treeText = (@($header) + $treeOutput) -join [Environment]::NewLine

    # UTF-8 without BOM.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($TreeFile, $treeText, $utf8NoBom)

    $size = (Get-Item -Path $TreeFile).Length
    Write-Host ("tree.txt created: {0} ({1:N0} bytes)" -f $TreeFile, $size)
}
catch {
    Write-Warning "Failed to generate tree.txt: $($_.Exception.Message)"
    exit 1
}