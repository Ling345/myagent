# Build the AgentCode code-execution sandbox image.
#
# Why this exists: the container runs with --network none, so it cannot install
# anything at run time. Test dependencies such as pytest must be baked into the
# image at build time.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build_sandbox_image.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_sandbox_image.ps1 -Tag agentcode-sandbox:1.1
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI
# unless the file has a BOM, which corrupts non-ASCII text and breaks parsing.
param(
    [string]$Tag = "agentcode-sandbox:1.0",
    [string]$PipIndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$DockerCommand = "wsl -d Ubuntu -- docker"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$contextPath = Join-Path $projectRoot "docker\sandbox"

if (-not (Test-Path $contextPath)) {
    Write-Error "Build context not found: $contextPath"
}

# Allow the docker command to be a prefix such as "wsl -d Ubuntu -- docker"
$dockerParts = -split $DockerCommand.Trim()
$dockerExe = $dockerParts[0]
$dockerArgs = @()
if ($dockerParts.Count -gt 1) { $dockerArgs = $dockerParts[1..($dockerParts.Count - 1)] }

# WSL eats backslashes when translating arguments, so a Windows path such as
# D:\repo\docker\sandbox arrives as "D:repodockersandbox". Convert it to the
# /mnt/<drive>/... form that the docker daemon inside WSL actually understands.
function ConvertTo-DockerPath([string]$Path) {
    $full = (Resolve-Path $Path).Path
    if ($dockerExe -notlike "wsl*") { return $full }
    $drive = $full.Substring(0, 1).ToLower()
    $rest = $full.Substring(2).Replace("\", "/")
    return "/mnt/$drive$rest"
}

$contextForDocker = ConvertTo-DockerPath $contextPath

Write-Output "[1/3] Checking Docker daemon ..."
& $dockerExe @dockerArgs info --format "{{.ServerVersion}}"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker is not available: $DockerCommand"
}

Write-Output "[2/3] Building image $Tag (pip index: $PipIndexUrl) ..."
& $dockerExe @dockerArgs build -t $Tag --build-arg "PIP_INDEX_URL=$PipIndexUrl" $contextForDocker
if ($LASTEXITCODE -ne 0) {
    Write-Error "Image build failed."
}

Write-Output "[3/3] Self-check: expecting uid 65534 and a pytest version ..."
& $dockerExe @dockerArgs run --rm --network none --read-only --user 65534:65534 `
    --tmpfs /tmp:size=32m $Tag `
    python -c "import os, pytest; print('uid', os.getuid(), '| pytest', pytest.__version__)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Image self-check failed."
}

Write-Output "Done. Set AGENT_DOCKER_IMAGE=$Tag in .env"
