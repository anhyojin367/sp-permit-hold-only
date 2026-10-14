param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ImageDirectory
)

$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
$errors = New-Object System.Collections.Generic.List[string]
$pages = New-Object System.Collections.Generic.List[object]

try {
    $resolvedDirectory = (Resolve-Path -LiteralPath $ImageDirectory -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolvedDirectory -PathType Container)) {
        throw "Image directory does not exist."
    }

    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

    function Wait-WinRtOperation([object]$Operation, [Type]$ResultType) {
        $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
            Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
            Select-Object -First 1
        return $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation)).GetAwaiter().GetResult()
    }

    $language = New-Object Windows.Globalization.Language('ko-KR')
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) {
        throw "Windows Media OCR does not support ko-KR on this computer."
    }

    $files = Get-ChildItem -LiteralPath $resolvedDirectory -File -Filter 'page-*.png' |
        Where-Object { $_.Name -match '^page-(\d+)\.png$' } |
        Sort-Object @{ Expression = { [int][regex]::Match($_.Name, '^page-(\d+)\.png$').Groups[1].Value } }, Name
    foreach ($file in $files) {
        $pageNumber = [int][regex]::Match($file.Name, '^page-(\d+)\.png$').Groups[1].Value
        try {
            $storageFile = Wait-WinRtOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($file.FullName)) ([Windows.Storage.StorageFile])
            $stream = Wait-WinRtOperation ($storageFile.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
            $decoder = Wait-WinRtOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Wait-WinRtOperation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $result = Wait-WinRtOperation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $text = ($result.Lines | ForEach-Object { $_.Text }) -join "`n"
            $pages.Add([ordered]@{ page_number = $pageNumber; text = $text })
            $bitmap.Dispose()
            $stream.Dispose()
        } catch {
            $errors.Add("page ${pageNumber}: $($_.Exception.Message)")
        }
    }
} catch {
    $errors.Add($_.Exception.Message)
}

[ordered]@{ pages = $pages.ToArray(); errors = $errors.ToArray() } | ConvertTo-Json -Compress -Depth 4
