param(
    [Parameter(Mandatory = $true)][string]$BundleDir,
    [Parameter(Mandatory = $true)][string]$SetupRoot,
    [Parameter(Mandatory = $true)][string]$PythonInstallDir,
    [Parameter(Mandatory = $true)][string]$McpVenvDir,
    [Parameter(Mandatory = $true)][string]$McpPython,
    [Parameter(Mandatory = $true)][string]$DeployRoot,
    [Parameter(Mandatory = $true)][string]$SessiondPython,
    [Parameter(Mandatory = $true)][string]$SessiondModule,
    [Parameter(Mandatory = $true)][string]$SessiondStateDir,
    [Parameter(Mandatory = $true)][string]$MayaExe,
    [Parameter(Mandatory = $true)][string]$Launcher,
    [Parameter(Mandatory = $true)][string]$InteractiveTask,
    [Parameter(Mandatory = $true)][string]$InteractiveUser,
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][string]$ExpectedMcpPackagesJson,
    [Parameter(Mandatory = $true)][string]$ExpectedTaskArguments
)

$ErrorActionPreference = "Stop"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONNOUSERSITE = "1"
Remove-Item Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
$changes = [System.Collections.Generic.List[object]]::new()
$setupMutex = [System.Threading.Mutex]::new($false, "Global\mac_maya_dev_windows_setup")
$modeMutex = [System.Threading.Mutex]::new($false, "Global\mac_maya_dev_command_port_$Port")
$setupMutexAcquired = $false
$modeMutexAcquired = $false

function Enter-Mutex([System.Threading.Mutex]$Mutex) {
    try { return $Mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { return $true }
}

function Add-Change([string]$Id, [string]$Action, [string]$Detail) {
    $changes.Add([ordered]@{ id = $Id; action = $Action; status = "changed"; detail = $Detail })
}

function Get-LowerHash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-SamePath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) { return $false }
    return [System.IO.Path]::GetFullPath($Left).TrimEnd("\") -ieq [System.IO.Path]::GetFullPath($Right).TrimEnd("\")
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

function Test-McpPackageInventory([string]$Python, [string]$ExpectedJson) {
    $actual = Get-McpPackageInventory $Python
    if (-not $actual) { return $false }
    $expected = $ExpectedJson | ConvertFrom-Json
    $actualProperties = @($actual.PSObject.Properties)
    $expectedProperties = @($expected.PSObject.Properties)
    if ($actualProperties.Count -ne $expectedProperties.Count) { return $false }
    foreach ($entry in $expectedProperties) {
        $actualEntry = $actualProperties | Where-Object { $_.Name -eq $entry.Name }
        if (-not $actualEntry -or [string]$actualEntry.Value -ne [string]$entry.Value) {
            return $false
        }
    }
    return $true
}

function Install-FileAtomically([string]$Source, [string]$Destination) {
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temp = Join-Path $parent ".$([System.IO.Path]::GetFileName($Destination)).$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        Copy-Item -LiteralPath $Source -Destination $temp
        if (Test-Path -LiteralPath $Destination -PathType Leaf) {
            [System.IO.File]::Replace($temp, $Destination, $null)
        } else {
            [System.IO.File]::Move($temp, $Destination)
        }
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    }
}

try {
    $setupMutexAcquired = Enter-Mutex $setupMutex
    if (-not $setupMutexAcquired) {
        throw "Another mac_maya_dev Windows setup apply is active."
    }
    $modeMutexAcquired = Enter-Mutex $modeMutex
    if (-not $modeMutexAcquired) {
        throw "Another mac_maya_dev MCP mode is active for command port $Port."
    }
    $manifestPath = Join-Path $BundleDir "setup-manifest.json"
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($manifest.schema -ne 1) { throw "Unsupported setup manifest schema." }
    $lockPath = Join-Path $BundleDir ([string]$manifest.mcp_lock.file)
    $launcherSource = Join-Path $BundleDir ([string]$manifest.launcher.file)
    if ((Get-LowerHash $lockPath) -ne [string]$manifest.mcp_lock.sha256) {
        throw "MCP requirements lock hash does not match setup manifest."
    }
    if ((Get-LowerHash $launcherSource) -ne [string]$manifest.launcher.sha256) {
        throw "Launcher hash does not match setup manifest."
    }

    $consoleUser = (Get-CimInstance Win32_ComputerSystem).UserName
    if ($consoleUser -ine $InteractiveUser) {
        throw "Configured interactive user is not logged in: expected $InteractiveUser, found $consoleUser"
    }
    if (-not (Test-Path -LiteralPath $MayaExe -PathType Leaf)) {
        throw "Configured Maya executable is missing: $MayaExe"
    }
    if (-not (Test-Path -LiteralPath $SessiondPython -PathType Leaf)) {
        throw "Configured reuse-only sessiond Python is missing: $SessiondPython"
    }
    $sessiondHelp = (& $SessiondPython -I -B -m $SessiondModule start --help 2>&1 | Out-String)
    $requiredOptions = @("--python-exe", "--state-dir", "--maya-exe", "--mcp-python", "--mcp-module", "--mcp-src", "--port", "--wait-timeout-seconds", "--json")
    if ($LASTEXITCODE -ne 0 -or @($requiredOptions | Where-Object { $sessiondHelp -notmatch [regex]::Escape($_) }).Count) {
        throw "Configured reuse-only sessiond runtime does not expose the required start contract."
    }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        if ($listener.LocalAddress -notin @("127.0.0.1", "::1") -or -not $process -or -not (Test-SamePath $process.Path $MayaExe)) {
            throw "Command port $Port acquired an unexpected listener after the setup preview; retry after resolving it."
        }
    }
    if ((Test-Path -LiteralPath $Launcher) -and -not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
        throw "Launcher path is occupied by a non-file; inspect it before retrying: $Launcher"
    }

    $requiredDirectories = @(
        $SetupRoot,
        $DeployRoot,
        (Join-Path $DeployRoot "incoming"),
        (Join-Path $DeployRoot "deployments"),
        $SessiondStateDir
    )
    foreach ($directory in @($requiredDirectories + @($PythonInstallDir, $McpVenvDir))) {
        if ((Test-Path -LiteralPath $directory) -and -not (Test-Path -LiteralPath $directory -PathType Container)) {
            throw "Required directory path is occupied by a non-directory; inspect it before retrying: $directory"
        }
    }
    foreach ($directory in $requiredDirectories) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $directory | Out-Null
            Add-Change "directory" "create" $directory
        }
    }

    $basePython = Join-Path $PythonInstallDir "python.exe"
    $baseVersion = $null
    if (Test-Path -LiteralPath $basePython -PathType Leaf) {
        $baseProbe = (& $basePython -I -B -c "import json,platform,struct,sys; print(json.dumps({'version':'.'.join(map(str,sys.version_info[:3])),'architecture':platform.machine(),'bits':struct.calcsize('P')*8}))" 2>&1 | Out-String).Trim() | ConvertFrom-Json
        if ($baseProbe.version -ne [string]$manifest.python.version -or $baseProbe.architecture -ne [string]$manifest.python.architecture -or $baseProbe.bits -ne [int]$manifest.python.bits) {
            throw "Existing configured Python does not match pinned version/architecture. Refusing to overwrite it."
        }
    } else {
        if (Test-Path -LiteralPath $PythonInstallDir) {
            throw "Python install directory exists without python.exe; inspect it before retrying: $PythonInstallDir"
        }
        $installer = Join-Path $SetupRoot ".python-$($manifest.python.version)-$([Guid]::NewGuid().ToString('N')).exe"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri ([string]$manifest.python.url) -OutFile $installer
            $installerHash = Get-LowerHash $installer
            if ($installerHash -ne [string]$manifest.python.sha256) {
                throw "Downloaded Python installer checksum mismatch."
            }
            $installerArgs = @(
                "/quiet",
                "InstallAllUsers=0",
                "TargetDir=`"$PythonInstallDir`"",
                "Include_pip=1",
                "Include_launcher=0",
                "Include_test=0",
                "Include_doc=0",
                "Shortcuts=0",
                "AssociateFiles=0",
                "PrependPath=0"
            )
            $install = Start-Process -FilePath $installer -ArgumentList $installerArgs -Wait -PassThru
            if ($install.ExitCode -ne 0) { throw "Python installer exited $($install.ExitCode)." }
            $installedProbe = (& $basePython -I -B -c "import json,platform,struct,sys; print(json.dumps({'version':'.'.join(map(str,sys.version_info[:3])),'architecture':platform.machine(),'bits':struct.calcsize('P')*8}))" 2>&1 | Out-String).Trim() | ConvertFrom-Json
            if ($installedProbe.version -ne [string]$manifest.python.version -or $installedProbe.architecture -ne [string]$manifest.python.architecture -or $installedProbe.bits -ne [int]$manifest.python.bits) {
                throw "Installed Python version/architecture verification failed."
            }
            Add-Change "python.base" "install" "$basePython ($($installedProbe.version) $($installedProbe.architecture) $($installedProbe.bits)-bit)"
        } catch {
            if (Test-Path -LiteralPath $PythonInstallDir) { Remove-Item -LiteralPath $PythonInstallDir -Recurse -Force }
            throw
        } finally {
            if (Test-Path -LiteralPath $installer) { Remove-Item -LiteralPath $installer -Force }
        }
    }

    $installedLockPath = Join-Path $McpVenvDir ".maya-dev-requirements.lock"
    $installedLockHash = if (Test-Path -LiteralPath $installedLockPath -PathType Leaf) {
        Get-LowerHash $installedLockPath
    } else { $null }
    $mcpHealthy = $false
    if ($installedLockHash -eq [string]$manifest.mcp_lock.sha256 -and (Test-Path -LiteralPath $McpPython -PathType Leaf)) {
        & $McpPython -I -B -c "import fastmcp, typing_extensions" 2>$null
        $importsOk = $LASTEXITCODE -eq 0
        & $McpPython -I -B -m pip check *> $null
        $pipOk = $LASTEXITCODE -eq 0
        $inventoryOk = Test-McpPackageInventory $McpPython $ExpectedMcpPackagesJson
        $mcpHealthy = $importsOk -and $pipOk -and $inventoryOk
    }
    if (-not $mcpHealthy) {
        $liveMcp = @(Get-CimInstance Win32_Process | Where-Object { Test-SamePath $_.ExecutablePath $McpPython })
        if ($liveMcp.Count) { throw "Configured MCP Python is in use; stop the MCP/session before rebuilding it." }
        if ((Test-Path -LiteralPath $McpVenvDir) -and -not (Test-Path -LiteralPath $McpVenvDir -PathType Container)) {
            throw "MCP venv path exists but is not a directory; inspect it before retrying: $McpVenvDir"
        }
        $backupVenv = "$McpVenvDir.backup-$([Guid]::NewGuid().ToString('N'))"
        $backedUp = $false
        if (Test-Path -LiteralPath $McpVenvDir -PathType Container) {
            [System.IO.Directory]::Move($McpVenvDir, $backupVenv)
            $backedUp = $true
        }
        try {
            # Windows venv launchers embed absolute paths, so create directly at the final path.
            & $basePython -I -B -m venv $McpVenvDir
            if ($LASTEXITCODE -ne 0) { throw "Creating the MCP venv failed with exit $LASTEXITCODE." }
            & $McpPython -I -B -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r $lockPath
            if ($LASTEXITCODE -ne 0) { throw "Installing pinned MCP dependencies failed with exit $LASTEXITCODE." }
            & $McpPython -I -B -m pip check
            if ($LASTEXITCODE -ne 0) { throw "Pinned MCP dependency check failed with exit $LASTEXITCODE." }
            & $McpPython -I -B -c "import fastmcp, typing_extensions"
            if ($LASTEXITCODE -ne 0) { throw "Pinned MCP dependency import failed with exit $LASTEXITCODE." }
            if (-not (Test-McpPackageInventory $McpPython $ExpectedMcpPackagesJson)) {
                throw "Installed MCP package inventory does not exactly match the repository lock."
            }
            Copy-Item -LiteralPath $lockPath -Destination (Join-Path $McpVenvDir ".maya-dev-requirements.lock")
            [ordered]@{
                schema = 1
                lock_sha256 = [string]$manifest.mcp_lock.sha256
                source_commit = [string]$manifest.mcp_lock.source_commit
                python_version = [string]$manifest.python.version
            } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $McpVenvDir ".maya-dev-lock.json") -Encoding UTF8
        } catch {
            $setupError = $_.Exception.Message
            try {
                if (Test-Path -LiteralPath $McpVenvDir) {
                    Remove-Item -LiteralPath $McpVenvDir -Recurse -Force
                }
                if ($backedUp) {
                    [System.IO.Directory]::Move($backupVenv, $McpVenvDir)
                    $backedUp = $false
                }
            } catch {
                throw "MCP venv setup failed ($setupError); rollback also failed. Existing backup may remain at ${backupVenv}: $($_.Exception.Message)"
            }
            throw $setupError
        }
        Add-Change "mcp.runtime" "rebuild" "$McpVenvDir from lock $($manifest.mcp_lock.sha256)"
        if ($backedUp) {
            try {
                Remove-Item -LiteralPath $backupVenv -Recurse -Force
                $backedUp = $false
            } catch {
                throw "MCP venv was replaced, but backup cleanup failed at ${backupVenv}: $($_.Exception.Message)"
            }
        }
    }

    $expectedLauncherHash = Get-LowerHash $launcherSource
    $actualLauncherHash = $null
    if (Test-Path -LiteralPath $Launcher -PathType Leaf) { $actualLauncherHash = Get-LowerHash $Launcher }
    if ($actualLauncherHash -ne $expectedLauncherHash) {
        Install-FileAtomically $launcherSource $Launcher
        Add-Change "launcher.content" "install" $Launcher
    }

    $taskExecutable = Join-Path $PSHOME "powershell.exe"
    $task = Get-ScheduledTask -TaskName $InteractiveTask -ErrorAction SilentlyContinue
    $taskMatches = $false
    if ($task) {
        $actions = @($task.Actions)
        $triggers = @($task.Triggers)
        $userMatches = $task.Principal.UserId -ieq $InteractiveUser -or $task.Principal.UserId -ieq ($InteractiveUser -split "\\")[-1]
        $taskMatches = $actions.Count -eq 1 -and $userMatches -and `
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
    if (-not $taskMatches) {
        $action = New-ScheduledTaskAction -Execute $taskExecutable -Argument $ExpectedTaskArguments
        $principal = New-ScheduledTaskPrincipal -UserId $InteractiveUser -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $InteractiveTask -Action $action -Principal $principal -Settings $settings -Force | Out-Null
        Add-Change "task.interactive" "register" "$InteractiveTask for $InteractiveUser (no password)"
    }

    [ordered]@{
        schema_version = 1
        command = "windows.setup"
        mode = "apply"
        ok = $true
        changed = $changes.Count -gt 0
        changes = $changes
    } | ConvertTo-Json -Depth 6 -Compress
} catch {
    [ordered]@{
        schema_version = 1
        command = "windows.setup"
        mode = "apply"
        ok = $false
        changed = $changes.Count -gt 0
        changes = $changes
        error = $_.Exception.Message
    } | ConvertTo-Json -Depth 6 -Compress
    throw
} finally {
    if ($modeMutexAcquired) { $modeMutex.ReleaseMutex() }
    if ($setupMutexAcquired) { $setupMutex.ReleaseMutex() }
    $modeMutex.Dispose()
    $setupMutex.Dispose()
}
