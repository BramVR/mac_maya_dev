param(
    [Parameter(Mandatory = $true)][string]$SetupRoot,
    [Parameter(Mandatory = $true)][string]$PythonInstallDir,
    [Parameter(Mandatory = $true)][string]$McpVenvDir,
    [Parameter(Mandatory = $true)][string]$McpPython,
    [Parameter(Mandatory = $true)][string]$McpModule,
    [Parameter(Mandatory = $true)][string]$DeployRoot,
    [Parameter(Mandatory = $true)][string]$SessiondPython,
    [Parameter(Mandatory = $true)][string]$SessiondStateDir,
    [Parameter(Mandatory = $true)][string]$MayaExe,
    [Parameter(Mandatory = $true)][string]$SessiondModule,
    [Parameter(Mandatory = $true)][string]$Launcher,
    [Parameter(Mandatory = $true)][string]$InteractiveTask,
    [Parameter(Mandatory = $true)][string]$InteractiveUser,
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][string]$ExpectedPythonVersion,
    [Parameter(Mandatory = $true)][string]$ExpectedPythonArchitecture,
    [Parameter(Mandatory = $true)][int]$ExpectedPythonBits,
    [Parameter(Mandatory = $true)][string]$ExpectedMcpLockHash,
    [Parameter(Mandatory = $true)][string]$ExpectedMcpPackagesJson,
    [Parameter(Mandatory = $true)][string]$ExpectedLauncherHash,
    [Parameter(Mandatory = $true)][string]$ExpectedTaskArguments
)

$ErrorActionPreference = "Stop"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONNOUSERSITE = "1"
Remove-Item Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
$checks = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    param(
        [string]$Id,
        [bool]$Ok,
        [bool]$Required,
        [object]$Actual,
        [object]$Expected,
        [string]$Remediation,
        [string]$Status = $(if ($Ok) { "pass" } else { "fail" })
    )
    $checks.Add([ordered]@{
        id = $Id
        status = $Status
        ok = $Ok
        required = $Required
        actual = $Actual
        expected = $Expected
        remediation = $Remediation
    })
}

function Test-SamePath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) { return $false }
    $leftFull = [System.IO.Path]::GetFullPath($Left).TrimEnd("\")
    $rightFull = [System.IO.Path]::GetFullPath($Right).TrimEnd("\")
    return $leftFull -ieq $rightFull
}

function Get-McpPackageInventory([string]$Python) {
    $inventoryCode = @'
import importlib.metadata as metadata
import json
import re

skip = {"pip", "setuptools"}
normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
packages = {
    normalize(dist.metadata["Name"]): dist.version
    for dist in metadata.distributions()
    if dist.metadata["Name"] and normalize(dist.metadata["Name"]) not in skip
}
print(json.dumps(packages, sort_keys=True))
'@
    $text = (& $Python -I -B -c $inventoryCode 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { return $null }
    try { return $text | ConvertFrom-Json } catch { return $null }
}

function Compare-McpPackageInventory([object]$Actual, [string]$ExpectedJson) {
    $expected = $ExpectedJson | ConvertFrom-Json
    $actualProperties = if ($Actual) { @($Actual.PSObject.Properties) } else { @() }
    $expectedProperties = @($expected.PSObject.Properties)
    $actualNames = @($actualProperties | ForEach-Object { $_.Name })
    $expectedNames = @($expectedProperties | ForEach-Object { $_.Name })
    $missing = @($expectedNames | Where-Object { $_ -notin $actualNames })
    $unexpected = @($actualNames | Where-Object { $_ -notin $expectedNames })
    $mismatched = @($expectedProperties | Where-Object {
        $_.Name -in $actualNames -and [string]$Actual.($_.Name) -ne [string]$_.Value
    } | ForEach-Object {
        [ordered]@{ name = $_.Name; expected = $_.Value; actual = $Actual.($_.Name) }
    })
    return [ordered]@{
        ok = -not $missing.Count -and -not $unexpected.Count -and -not $mismatched.Count
        missing = $missing
        unexpected = $unexpected
        mismatched = $mismatched
    }
}

$consoleUser = (Get-CimInstance Win32_ComputerSystem).UserName
Add-Check "host.console_user" ($consoleUser -ieq $InteractiveUser) $true $consoleUser `
    $InteractiveUser "Log in as the configured interactive Windows user before setup or start."

$gitCommand = Get-Command git -ErrorAction SilentlyContinue
$gitOk = [bool]$gitCommand
Add-Check "host.git" $gitOk $false $(if ($gitCommand) { $gitCommand.Source } else { $null }) `
    "Git available to the SSH environment" "Install Git for Windows if remote Git tooling is needed." `
    $(if ($gitOk) { "pass" } else { "warn" })

$mayaExists = Test-Path -LiteralPath $MayaExe -PathType Leaf
Add-Check "maya.executable" $mayaExists $true $MayaExe $MayaExe "Install Maya 2024 at the configured path."

$basePython = Join-Path $PythonInstallDir "python.exe"
$pythonInstallDirExists = Test-Path -LiteralPath $PythonInstallDir -PathType Container
$basePythonExists = Test-Path -LiteralPath $basePython -PathType Leaf
$baseVersion = $null
$baseArchitecture = $null
$baseBits = $null
if ($basePythonExists) {
    $baseProbeText = (& $basePython -I -B -c "import json,platform,struct,sys; print(json.dumps({'version':'.'.join(map(str,sys.version_info[:3])),'architecture':platform.machine(),'bits':struct.calcsize('P')*8}))" 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) {
        try {
            $baseProbe = $baseProbeText | ConvertFrom-Json
            $baseVersion = [string]$baseProbe.version
            $baseArchitecture = [string]$baseProbe.architecture
            $baseBits = [int]$baseProbe.bits
        } catch { $baseVersion = $null }
    }
}
Add-Check "python.base" ($baseVersion -eq $ExpectedPythonVersion -and $baseArchitecture -eq $ExpectedPythonArchitecture -and $baseBits -eq $ExpectedPythonBits) $true ([ordered]@{
    install_dir = $PythonInstallDir
    directory_exists = $pythonInstallDirExists
    executable = $basePython
    executable_exists = $basePythonExists
    version = $baseVersion
    architecture = $baseArchitecture
    bits = $baseBits
}) ([ordered]@{
    version = $ExpectedPythonVersion
    architecture = $ExpectedPythonArchitecture
    bits = $ExpectedPythonBits
    clean_install_requires_absent_directory = $true
}) "Install pinned CPython into an absent target; inspect incompatible occupied targets."

$mcpVersion = $null
$mcpArchitecture = $null
$mcpBits = $null
$mcpRunnable = $false
if (Test-Path -LiteralPath $McpPython -PathType Leaf) {
    $mcpProbeText = (& $McpPython -I -B -c "import json,platform,struct,sys; print(json.dumps({'version':'.'.join(map(str,sys.version_info[:3])),'architecture':platform.machine(),'bits':struct.calcsize('P')*8}))" 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) {
        try {
            $mcpProbe = $mcpProbeText | ConvertFrom-Json
            $mcpVersion = [string]$mcpProbe.version
            $mcpArchitecture = [string]$mcpProbe.architecture
            $mcpBits = [int]$mcpProbe.bits
        } catch { $mcpVersion = $null }
    }
    $mcpRunnable = $mcpVersion -eq $ExpectedPythonVersion -and $mcpArchitecture -eq $ExpectedPythonArchitecture -and $mcpBits -eq $ExpectedPythonBits
}
Add-Check "mcp.runtime" $mcpRunnable $true ([ordered]@{ version = $mcpVersion; architecture = $mcpArchitecture; bits = $mcpBits }) `
    ([ordered]@{ version = $ExpectedPythonVersion; architecture = $ExpectedPythonArchitecture; bits = $ExpectedPythonBits }) `
    "Create the isolated MCP virtual environment from pinned CPython."

$installedLockHash = $null
$installedLockPath = Join-Path $McpVenvDir ".maya-dev-requirements.lock"
if (Test-Path -LiteralPath $installedLockPath -PathType Leaf) {
    $installedLockHash = (Get-FileHash -LiteralPath $installedLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
}
$lockMatches = $installedLockHash -eq $ExpectedMcpLockHash
Add-Check "mcp.lock" $lockMatches $true $installedLockHash $ExpectedMcpLockHash `
    "Rebuild the isolated MCP environment from the repository hash lock."

$mcpImports = $false
$pipCheck = $null
$inventoryComparison = [ordered]@{ ok = $false; missing = @(); unexpected = @(); mismatched = @() }
if ($mcpRunnable) {
    & $McpPython -I -B -c "import fastmcp, typing_extensions" 2>$null
    $importsOk = $LASTEXITCODE -eq 0
    $pipCheck = (& $McpPython -I -B -m pip check 2>&1 | Out-String).Trim()
    $pipOk = $LASTEXITCODE -eq 0
    $inventory = Get-McpPackageInventory $McpPython
    $inventoryComparison = Compare-McpPackageInventory $inventory $ExpectedMcpPackagesJson
    $mcpImports = $importsOk -and $pipOk -and $inventoryComparison.ok
}
Add-Check "mcp.dependencies" $mcpImports $true ([ordered]@{
    pip_check = $pipCheck
    inventory = $inventoryComparison
}) "Pinned lock inventory, imports, and pip check pass" `
    "Rebuild the isolated MCP environment from the repository hash lock."

$currentFile = Join-Path $DeployRoot "current.json"
$sourceImportDetail = "No deployment selected; run deploy after host setup."
$sourceImportOk = $true
$sourceImportRequired = $false
$sourceImportStatus = "warn"
if ($mcpRunnable -and (Test-Path -LiteralPath $currentFile -PathType Leaf)) {
    $sourceImportRequired = $true
    $sourceImportStatus = "fail"
    try {
        $current = Get-Content -Raw -LiteralPath $currentFile | ConvertFrom-Json
        $sourcePath = Join-Path ([string]$current.path) "src"
        & $McpPython -I -B -c "import importlib,sys; sys.path[:0]=sys.argv[1:]; importlib.import_module('$McpModule')" $sourcePath ([string]$current.path) 2>$null
        $sourceImportOk = $LASTEXITCODE -eq 0
        if ($sourceImportOk) { $sourceImportStatus = "pass" }
        $sourceImportDetail = [ordered]@{ module = $McpModule; deployment = $current.path }
    } catch {
        $sourceImportOk = $false
        $sourceImportDetail = $_.Exception.Message
    }
}
Add-Check "mcp.source_import" $sourceImportOk $sourceImportRequired $sourceImportDetail `
    "Selected immutable deployment imports with the configured MCP runtime" `
    "Deploy a compatible GG_MayaMCP snapshot or rebuild its pinned dependencies." $sourceImportStatus

$sessiondDetail = [ordered]@{
    path = $SessiondPython
    python = $null
    prefix = $null
    base_prefix = $null
    executable = $null
    version = $null
    required_start_options = $false
}
$sessiondOk = $false
if (Test-Path -LiteralPath $SessiondPython -PathType Leaf) {
    $probeCode = "import json,sys; import $SessiondModule; print(json.dumps({'python': '.'.join(map(str,sys.version_info[:3])), 'prefix':sys.prefix, 'base_prefix':sys.base_prefix, 'executable':sys.executable}))"
    $probeText = (& $SessiondPython -I -B -c $probeCode 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) {
        try {
            $probe = $probeText | ConvertFrom-Json
            $sessiondDetail.python = $probe.python
            $sessiondDetail.prefix = $probe.prefix
            $sessiondDetail.base_prefix = $probe.base_prefix
            $sessiondDetail.executable = $probe.executable
            $sessiondDetail.version = (& $SessiondPython -I -B -m $SessiondModule --version 2>&1 | Out-String).Trim()
            $help = (& $SessiondPython -I -B -m $SessiondModule start --help 2>&1 | Out-String)
            $requiredOptions = @("--python-exe", "--state-dir", "--maya-exe", "--mcp-python", "--mcp-module", "--mcp-src", "--port", "--wait-timeout-seconds", "--json")
            $sessiondDetail.required_start_options = -not @($requiredOptions | Where-Object { $help -notmatch [regex]::Escape($_) }).Count
            $sessiondOk = $probe.python.StartsWith("3.11.") -and $probe.prefix -ne $probe.base_prefix -and $sessiondDetail.required_start_options
        } catch { $sessiondOk = $false }
    }
}
Add-Check "sessiond.runtime" $sessiondOk $true $sessiondDetail `
    "Importable Python 3.11 venv with the launcher start contract" `
    "Configure an existing compatible gg_maya_sessiond venv; setup never modifies it."
Add-Check "sessiond.reproducibility" $true $false "configured existing runtime; verified only" `
    "repository-owned source and lock" "Provide an approved gg_maya_sessiond source/lock in a future change." "warn"

$directories = [ordered]@{
    setup_root = $SetupRoot
    python_install = $PythonInstallDir
    mcp_venv = $McpVenvDir
    deploy_root = $DeployRoot
    incoming = Join-Path $DeployRoot "incoming"
    deployments = Join-Path $DeployRoot "deployments"
    sessiond_state = $SessiondStateDir
}
foreach ($entry in $directories.GetEnumerator()) {
    $exists = Test-Path -LiteralPath $entry.Value
    $isDirectory = Test-Path -LiteralPath $entry.Value -PathType Container
    $kind = if (-not $exists) { "absent" } elseif ($isDirectory) { "directory" } else { "other" }
    Add-Check "directory.$($entry.Key)" $isDirectory $true ([ordered]@{
        path = $entry.Value
        exists = $exists
        kind = $kind
    }) ([ordered]@{ path = $entry.Value; kind = "directory" }) `
        "Create an absent directory; inspect any non-directory occupant."
}

$launcherExists = Test-Path -LiteralPath $Launcher
$launcherIsFile = Test-Path -LiteralPath $Launcher -PathType Leaf
$launcherHash = $null
if ($launcherIsFile) {
    $launcherHash = (Get-FileHash -LiteralPath $Launcher -Algorithm SHA256).Hash.ToLowerInvariant()
}
$launcherKind = if (-not $launcherExists) { "absent" } elseif ($launcherIsFile) { "file" } else { "other" }
Add-Check "launcher.content" ($launcherHash -eq $ExpectedLauncherHash) $true ([ordered]@{
    path = $Launcher
    exists = $launcherExists
    kind = $launcherKind
    sha256 = $launcherHash
}) ([ordered]@{ path = $Launcher; kind = "file"; sha256 = $ExpectedLauncherHash }) `
    "Install into an absent/file target; inspect any non-file occupant."

$task = Get-ScheduledTask -TaskName $InteractiveTask -ErrorAction SilentlyContinue
$taskExecutable = Join-Path $PSHOME "powershell.exe"
$taskActual = $null
$taskOk = $false
if ($task) {
    $actions = @($task.Actions)
    $triggers = @($task.Triggers)
    $taskActual = [ordered]@{
        user = [string]$task.Principal.UserId
        logon_type = [string]$task.Principal.LogonType
        run_level = [string]$task.Principal.RunLevel
        execute = if ($actions.Count -eq 1) { [string]$actions[0].Execute } else { $null }
        arguments = if ($actions.Count -eq 1) { [string]$actions[0].Arguments } else { $null }
        working_directory = if ($actions.Count -eq 1) { [string]$actions[0].WorkingDirectory } else { $null }
        action_count = $actions.Count
        trigger_count = $triggers.Count
        enabled = [bool]$task.Settings.Enabled
        multiple_instances = [string]$task.Settings.MultipleInstances
        execution_time_limit = [string]$task.Settings.ExecutionTimeLimit
        state = [string]$task.State
    }
    $userMatches = $task.Principal.UserId -ieq $InteractiveUser -or $consoleUser -ieq $InteractiveUser -and $task.Principal.UserId -ieq ($InteractiveUser -split "\\")[-1]
    $taskOk = $actions.Count -eq 1 -and $userMatches -and `
        $triggers.Count -eq 0 -and `
        [string]$task.Principal.LogonType -eq "Interactive" -and `
        [string]$task.Principal.RunLevel -eq "Limited" -and `
        [bool]$task.Settings.Enabled -and `
        [string]$task.Settings.MultipleInstances -eq "IgnoreNew" -and `
        [string]$task.Settings.ExecutionTimeLimit -in @("PT0S", "00:00:00", "0") -and `
        (Test-SamePath ([string]$actions[0].Execute) $taskExecutable) -and `
        [string]$actions[0].Arguments -ceq $ExpectedTaskArguments -and `
        [string]::IsNullOrEmpty([string]$actions[0].WorkingDirectory)
}
$taskExpected = [ordered]@{
    user = $InteractiveUser
    logon_type = "Interactive"
    execute = $taskExecutable
    arguments = $ExpectedTaskArguments
    working_directory = $null
    trigger_count = 0
    run_level = "Limited"
    enabled = $true
    multiple_instances = "IgnoreNew"
    execution_time_limit = "PT0S"
    password_stored = $false
}
Add-Check "task.interactive" $taskOk $true $taskActual $taskExpected `
    "Register the passwordless interactive task with the repository launcher action."

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
$listenerDetails = @()
$portSafe = $true
foreach ($listener in $listeners) {
    $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    $detail = [ordered]@{
        address = $listener.LocalAddress
        pid = $listener.OwningProcess
        process = if ($process) { $process.ProcessName } else { $null }
        path = if ($process) { $process.Path } else { $null }
    }
    $listenerDetails += $detail
    if ($listener.LocalAddress -notin @("127.0.0.1", "::1") -or -not $process -or -not (Test-SamePath $process.Path $MayaExe)) {
        $portSafe = $false
    }
}
Add-Check "port.command" $portSafe $true $listenerDetails `
    "Unused, or loopback-only and owned by configured Maya" `
    "Stop or reconfigure the unexpected listener; setup never changes ports or firewall rules."

$requiredFailures = @($checks | Where-Object { $_.required -and -not $_.ok })
$summary = [ordered]@{
    pass = @($checks | Where-Object { $_.status -eq "pass" }).Count
    warn = @($checks | Where-Object { $_.status -eq "warn" }).Count
    fail = @($checks | Where-Object { $_.status -eq "fail" }).Count
}
[ordered]@{
    schema_version = 1
    command = "windows.check"
    mode = "check"
    ok = $requiredFailures.Count -eq 0
    changed = $false
    summary = $summary
    checks = $checks
} | ConvertTo-Json -Depth 8 -Compress
