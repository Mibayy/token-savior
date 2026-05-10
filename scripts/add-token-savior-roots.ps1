param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $Roots
)

$ErrorActionPreference = "Stop"

$ServerName = "token-savior-recall"
$Scope = "user"
$ClientName = "claude-code"

function Write-Usage {
    Write-Host "Usage:"
    Write-Host "  .\scripts\add-token-savior-roots.ps1 C:\absolute\project\path [C:\another\project]"
    Write-Host ""
    Write-Host "Adds one or more project folders to the token-savior-recall Claude Code MCP"
    Write-Host "server by updating its WORKSPACE_ROOTS environment setting."
    Write-Host ""
    Write-Host "From the workspace you want to add:"
    Write-Host "  & C:\path\to\token-savior\scripts\add-token-savior-roots.ps1 `$PWD.Path"
}

if (-not $Roots -or $Roots.Count -lt 1) {
    Write-Usage
    exit 2
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Error "claude CLI not found on PATH."
    exit 1
}

$serverInfo = & claude mcp get $ServerName 2>$null
if (-not $serverInfo) {
    Write-Error "MCP server '$ServerName' is not registered. Register it first with: claude mcp add $ServerName /path/to/token-savior"
    exit 1
}

$commandPath = $null
$currentRoots = $null
foreach ($line in $serverInfo) {
    if ($null -eq $commandPath -and $line -match '^\s*Command:\s*(.+)$') {
        $commandPath = $Matches[1].Trim()
        continue
    }
    if ($null -eq $currentRoots -and $line -match '^\s*WORKSPACE_ROOTS=(.*)$') {
        $currentRoots = $Matches[1].Trim()
        continue
    }
}

if (-not $commandPath) {
    Write-Error "Could not find command path for '$ServerName'."
    exit 1
}

$allRoots = New-Object System.Collections.Generic.List[string]
if ($currentRoots) {
    foreach ($root in $currentRoots.Split(',')) {
        if ($root) {
            $allRoots.Add($root)
        }
    }
}

foreach ($root in $Roots) {
    if (-not [System.IO.Path]::IsPathRooted($root)) {
        Write-Error "Root must be an absolute path: $root"
        exit 1
    }
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        Write-Error "Root is not a directory: $root"
        exit 1
    }
    $resolved = (Resolve-Path -LiteralPath $root).ProviderPath
    $allRoots.Add($resolved)
}

$seen = @{}
$uniqueRoots = New-Object System.Collections.Generic.List[string]
foreach ($root in $allRoots) {
    if (-not $root) {
        continue
    }
    if (-not $seen.ContainsKey($root)) {
        $seen[$root] = $true
        $uniqueRoots.Add($root)
    }
}

$joinedRoots = [string]::Join(",", $uniqueRoots.ToArray())

& claude mcp remove $ServerName -s $Scope | Out-Null
& claude mcp add $ServerName $commandPath `
    -s $Scope `
    -e "WORKSPACE_ROOTS=$joinedRoots" `
    -e "TOKEN_SAVIOR_CLIENT=$ClientName" | Out-Null

Write-Host "Updated $ServerName WORKSPACE_ROOTS:"
foreach ($root in $uniqueRoots) {
    Write-Host "  $root"
}
Write-Host ""
Write-Host "Restart Claude Code or restart the MCP server from /mcp."
