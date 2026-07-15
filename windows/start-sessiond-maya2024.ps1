param(
    [Parameter(Mandatory = $true)]
    [string]$CurrentFile,

    [Parameter(Mandatory = $true)]
    [string]$SessiondPython,

    [Parameter(Mandatory = $true)]
    [string]$StateDir,

    [Parameter(Mandatory = $true)]
    [string]$MayaExe,

    [Parameter(Mandatory = $true)]
    [string]$McpPython,

    [int]$Port = 7001,
    [int]$WaitTimeoutSeconds = 180,
    [string]$SessiondModule = "gg_maya_sessiond.cli",
    [string]$McpModule = "maya_mcp.server"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $CurrentFile -PathType Leaf)) {
    throw "No selected GG_MayaMCP deployment: $CurrentFile"
}

$current = Get-Content -Raw -LiteralPath $CurrentFile | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $current.path -PathType Container)) {
    throw "Selected GG_MayaMCP deployment is missing: $($current.path)"
}

$env:PYTHONDONTWRITEBYTECODE = "1"

& $SessiondPython -m $SessiondModule start `
    --python-exe $SessiondPython `
    --state-dir $StateDir `
    --maya-exe $MayaExe `
    --mcp-python $McpPython `
    --mcp-module $McpModule `
    --mcp-src $current.path `
    --port $Port `
    --wait-timeout-seconds $WaitTimeoutSeconds `
    --json

exit $LASTEXITCODE
