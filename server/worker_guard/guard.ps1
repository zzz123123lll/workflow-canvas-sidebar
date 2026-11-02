param([Parameter(Mandatory)][string]$EncodedCommand, [string]$WorkspaceRoot)

# This is an accident guard for the Harness PowerShell tool, not an OS sandbox.
# Existing Harness sandbox and permission checks remain in the executor chain.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($WorkspaceRoot) {
    # The Windows restricted-token launcher can start pwsh in its install folder.
    # Pin relative task paths to the explicitly resolved node workspace.
    Set-Location -LiteralPath $WorkspaceRoot
}

function Assert-DshDeleteTarget {
    param([Parameter(Mandatory)][string]$Candidate)
    $literalPrefix = ($Candidate -split '[*?\[]', 2)[0]
    if ([string]::IsNullOrWhiteSpace($literalPrefix)) { $literalPrefix = '.' }
    $candidateFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($literalPrefix).TrimEnd('\', '/')
    if ($WorkspaceRoot) {
        $allowedRoot = [IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\', '/')
        if (-not $candidateFull.StartsWith($allowedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "DSH workflow guard: deletion must remain inside this node's workspace: $Candidate"
        }
    }
    $userRoot = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\', '/')
    $harnessRoot = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $userRoot '.dsh' }
    $dataRoots = @($harnessRoot, (Join-Path $userRoot '.dsh'), 'D:\dsh-backups', 'D:\dsh-recovery', 'D:\dsh-protection')
    foreach ($protected in @($userRoot) + $dataRoots) {
        $protectedFull = [IO.Path]::GetFullPath($protected).TrimEnd('\', '/')
        if ($candidateFull.Equals($protectedFull, [StringComparison]::OrdinalIgnoreCase) -or
            $protectedFull.StartsWith($candidateFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "DSH safety guard: refusing deletion of a protected directory or its ancestor: $Candidate"
        }
    }
    foreach ($protected in $dataRoots) {
        $protectedFull = [IO.Path]::GetFullPath($protected).TrimEnd('\', '/')
        if ($candidateFull.StartsWith($protectedFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "DSH safety guard: refusing deletion inside Harness data or recovery storage: $Candidate"
        }
    }
}

function Remove-Item {
    [CmdletBinding(SupportsShouldProcess, DefaultParameterSetName='Path')]
    param(
        [Parameter(Position=0, Mandatory, ValueFromPipeline, ValueFromPipelineByPropertyName, ParameterSetName='Path')]
        [string[]]$Path,
        [Parameter(Mandatory, ValueFromPipelineByPropertyName, ParameterSetName='LiteralPath')]
        [Alias('PSPath','LP')][string[]]$LiteralPath,
        [switch]$Recurse,
        [switch]$Force,
        [string]$Filter,
        [string[]]$Include,
        [string[]]$Exclude,
        [PSCredential]$Credential
    )
    process {
        $targets = if ($PSCmdlet.ParameterSetName -eq 'LiteralPath') { $LiteralPath } else { $Path }
        foreach ($target in $targets) {
            # Other providers (Env:, Alias:, etc.) are not filesystem deletions.
            $provider = $null
            $drive = $null
            $null = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($target, [ref]$provider, [ref]$drive)
            if ($provider.Name -ne 'FileSystem') { continue }
            Assert-DshDeleteTarget -Candidate $target
            if ($PSCmdlet.ParameterSetName -eq 'Path' -and [Management.Automation.WildcardPattern]::ContainsWildcardCharacters($target)) {
                foreach ($resolved in $ExecutionContext.SessionState.Path.GetResolvedProviderPathFromPSPath($target, [ref]$provider)) {
                    Assert-DshDeleteTarget -Candidate $resolved
                }
            }
        }
        Microsoft.PowerShell.Management\Remove-Item @PSBoundParameters
    }
}

try {
    $commandText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedCommand))
    $tokens = $null
    $parseErrors = $null
    $commandAst = [Management.Automation.Language.Parser]::ParseInput($commandText, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -gt 0) { throw ('DSH safety guard: invalid PowerShell: ' + $parseErrors[0].Message) }
    $reservedNames = @('home', 'pwd', 'pshome', 'pid', 'host', 'shellid', 'psversiontable', 'psedition', 'executioncontext')
    $writeNodes = $commandAst.FindAll({ param($node)
        $node -is [Management.Automation.Language.AssignmentStatementAst] -or
        $node -is [Management.Automation.Language.ForEachStatementAst] -or
        $node -is [Management.Automation.Language.ParameterAst] -or
        ($node -is [Management.Automation.Language.UnaryExpressionAst] -and $node.TokenKind.ToString() -match 'PlusPlus|MinusMinus')
    }, $true)
    foreach ($writeNode in $writeNodes) {
        $targetAst = switch ($writeNode.GetType().Name) {
            'AssignmentStatementAst' { $writeNode.Left }
            'ForEachStatementAst' { $writeNode.Variable }
            'ParameterAst' { $writeNode.Name }
            'UnaryExpressionAst' { $writeNode.Child }
        }
        foreach ($variableNode in $targetAst.FindAll({ param($node) $node -is [Management.Automation.Language.VariableExpressionAst] }, $true)) {
            $variableName = ($variableNode.VariablePath.UserPath -split ':')[-1].ToLowerInvariant()
            if ($reservedNames -contains $variableName) {
                throw "DSH safety guard: assignment to system variable `$$variableName is blocked. Use a task-specific variable such as `$taskTempPath. No commands were executed."
            }
        }
    }
    $global:LASTEXITCODE = 0
    & ([scriptblock]::Create($commandText))
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if (-not $?) { exit 1 }
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
