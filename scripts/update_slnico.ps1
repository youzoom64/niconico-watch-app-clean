param(
    [string]$OutputDirectory = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$blogUrl = "https://person-of-ehomaki.blog.jp/"
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) "downloads"
}

Write-Host "[SlNicoLiveRec] Checking the official distribution page..."
$blog = Invoke-WebRequest -Uri $blogUrl -UseBasicParsing
$links = [regex]::Matches(
    $blog.Content,
    'href="(?<url>[^"]+)"[^>]*>[^<]*SlNicoLiveRec\s+V(?<version>[0-9.]+)[^<]*</a>',
    [Text.RegularExpressions.RegexOptions]::IgnoreCase
)
if ($links.Count -eq 0) {
    throw "The latest SlNicoLiveRec download link was not found on $blogUrl"
}

$candidates = foreach ($match in $links) {
    [pscustomobject]@{
        Version = [version]$match.Groups['version'].Value
        Url = [Net.WebUtility]::HtmlDecode($match.Groups['url'].Value)
    }
}
$latest = $candidates | Sort-Object Version -Descending | Select-Object -First 1
Write-Host "[SlNicoLiveRec] Latest version: $($latest.Version)"
Write-Host "[SlNicoLiveRec] Download page: $($latest.Url)"
if ($CheckOnly) {
    exit 0
}

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$driveMatch = [regex]::Match($latest.Url, 'drive\.google\.com/file/d/(?<id>[^/]+)', 'IgnoreCase')
if ($driveMatch.Success) {
    $build = $latest.Version.ToString().Replace('.', '')
    $fileName = "SlNicoLiveRec$build.zip"
    $downloadUrl = "https://drive.usercontent.google.com/download?id=$($driveMatch.Groups['id'].Value)&export=download&confirm=t"
    $postBody = $null
} else {
    $downloadPage = Invoke-WebRequest -Uri $latest.Url -WebSession $session -UseBasicParsing
    $fileMatch = [regex]::Match($downloadPage.Content, 'SlNicoLiveRec(?<build>\d+)\.zip', 'IgnoreCase')
    $tokenMatch = [regex]::Match($downloadPage.Content, 'name="token"\s+value="(?<token>[^"]+)"', 'IgnoreCase')
    if (-not $fileMatch.Success -or -not $tokenMatch.Success) {
        throw "The ZIP name or download token was not found: $($latest.Url)"
    }
    $build = $fileMatch.Groups['build'].Value
    $fileName = $fileMatch.Value
    $downloadUrl = $latest.Url
    $postBody = @{ token = $tokenMatch.Groups['token'].Value; yes = 'Download' }
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$outputPath = Join-Path $OutputDirectory $fileName
if (Test-Path -LiteralPath $outputPath) {
    Write-Host "[SlNicoLiveRec] Already downloaded: $outputPath"
} else {
    $temporaryPath = "$outputPath.part"
    Write-Host "[SlNicoLiveRec] Downloading: $fileName"
    try {
        if ($null -eq $postBody) {
            Invoke-WebRequest -Uri $downloadUrl -WebSession $session -UseBasicParsing -OutFile $temporaryPath
        } else {
            Invoke-WebRequest -Uri $downloadUrl -Method Post -Body $postBody -WebSession $session -UseBasicParsing -OutFile $temporaryPath
        }
        if ((Get-Item $temporaryPath).Length -lt 1MB) {
            throw "The downloaded file is too small."
        }
        Move-Item -Force -LiteralPath $temporaryPath -Destination $outputPath
    } catch {
        Remove-Item -Force -LiteralPath $temporaryPath -ErrorAction SilentlyContinue
        throw
    }
    Write-Host "[SlNicoLiveRec] Saved: $outputPath"
}

$projectRoot = Split-Path $PSScriptRoot -Parent
$installRoot = Split-Path $projectRoot -Parent
$installDirectory = Join-Path $installRoot "SlNicoLiveRec$build"
$recorderExe = Join-Path $installDirectory "SlNicoLiveRec.exe"
if (-not (Test-Path -LiteralPath $recorderExe)) {
    Write-Host "[SlNicoLiveRec] Installing: $installDirectory"
    Expand-Archive -LiteralPath $outputPath -DestinationPath $installRoot -Force
}
if (-not (Test-Path -LiteralPath $recorderExe)) {
    throw "SlNicoLiveRec.exe was not found after extraction: $recorderExe"
}

$recorderConfig = Join-Path $installDirectory "SlNicoLiveRec_config.json"
if (-not (Test-Path -LiteralPath $recorderConfig)) {
    $previousConfig = Get-ChildItem -Path $installRoot -Directory -Filter 'SlNicoLiveRec*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'SlNicoLiveRec_config.json' } |
        Where-Object { (Test-Path -LiteralPath $_) -and ($_ -ne $recorderConfig) } |
        Sort-Object -Descending |
        Select-Object -First 1
    if ($previousConfig) {
        Copy-Item -LiteralPath $previousConfig -Destination $recorderConfig
        Write-Host "[SlNicoLiveRec] Previous login settings copied."
    }
}

if (Test-Path -LiteralPath $recorderConfig) {
    $settings = Get-Content -Raw -LiteralPath $recorderConfig | ConvertFrom-Json
} else {
    $settings = [pscustomobject]@{}
}
$recommended = [ordered]@{
    PurgeCredentials = $false; Login = 2; ConvertFormat = $true
    DeleteOriginal = $true; ToTrash = $true; ConvertOptions = '-c:v copy -c:a copy'
    ChangeFilenameFormat = $true; FilenameFormat = '{id}_{year}_{month}{day}_{hour}{minute}{second}_{title}'
    ChangeFolderFormat = $true; FolderFormat = '{supplier_id}_{author}'
    TitleBarFormat = '{author} - SlNicoLiveRec'; ReconnectionAttempt = $false
    RetryInterval = 10; RetryLimit = 0; WaitUntilBegin = $false
    WaitUntilBeginSecond = 60; CloseWindowOnExit = $true; DebugMode = $false
}
foreach ($entry in $recommended.GetEnumerator()) {
    $settings | Add-Member -NotePropertyName $entry.Key -NotePropertyValue $entry.Value -Force
}
$settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $recorderConfig -Encoding UTF8

$appConfig = Join-Path $projectRoot 'config.json'
$utf8 = New-Object Text.UTF8Encoding($false)
$jsonExe = $recorderExe.Replace('\', '\\')
if (Test-Path -LiteralPath $appConfig) {
    $appText = [IO.File]::ReadAllText($appConfig, [Text.Encoding]::UTF8)
    $pathPattern = '("slnico_live_rec_exe"\s*:\s*")[^"]*(")'
    if ([regex]::IsMatch($appText, $pathPattern)) {
        $appText = [regex]::Replace(
            $appText,
            $pathPattern,
            { param($match) $match.Groups[1].Value + $jsonExe + $match.Groups[2].Value },
            1
        )
        [IO.File]::WriteAllText($appConfig, $appText, $utf8)
    }
} else {
    $initialConfig = "{`n  `"slnico_live_rec_exe`": `"$jsonExe`"`n}`n"
    [IO.File]::WriteAllText($appConfig, $initialConfig, $utf8)
}
Write-Host "[SlNicoLiveRec] Ready: $recorderExe"
