param([switch]$Build)
$ErrorActionPreference = 'Stop'
$contractRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $contractRoot '../..')).Path
$contextCode = Join-Path $repoRoot 'src/agent'
$normalizerCode = Join-Path $repoRoot 'src/llm/message_normalization.py'
$image = 'shikigami-harness-contract:0.2.1rc2'
if ($Build) {
    & docker build --tag $image $contractRoot
    if ($LASTEXITCODE -ne 0) { throw 'Contract image build failed.' }
}
# Only this synthetic test integration is mounted, read-only. No private/app
# runtime, credential file, Docker socket, host network or published port.
& docker run --rm --network none --read-only --cap-drop ALL `
    --security-opt no-new-privileges --pids-limit 256 --memory 1g --cpus 2 `
    --tmpfs /tmp:rw,exec,nosuid,nodev `
    --tmpfs /workspace:rw,nosuid,nodev,uid=1000,gid=1000 `
    --tmpfs /home/tester:rw,exec,nosuid,nodev,uid=1000,gid=1000 `
    --mount "type=bind,source=$contractRoot,target=/opt/contract,readonly" `
    --mount "type=bind,source=$contextCode,target=/opt/app/src/agent,readonly" `
    --mount "type=bind,source=$normalizerCode,target=/opt/app/src/llm/message_normalization.py,readonly" `
    $image python /opt/contract/tests/smoke.py
if ($LASTEXITCODE -ne 0) { throw 'Harness contract test failed.' }
