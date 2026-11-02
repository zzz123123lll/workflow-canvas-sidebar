param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference = 'Stop'
$Path = (Resolve-Path -LiteralPath $Path).ProviderPath
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] > $null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] > $null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] > $null
$AwaitMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Result($Operation, [Type]$ResultType) {
  $Task = $AwaitMethod.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
  try { $Task.Wait() } catch { throw $Task.Exception.GetBaseException().Message }
  return $Task.Result
}
$Stream = $null
$Bitmap = $null
try {
  $File = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $Stream = Await-Result ($File.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $Decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($Stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $Bitmap = Await-Result ($Decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $Engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $Engine) { throw 'Windows OCR language pack is unavailable' }
  $Result = Await-Result ($Engine.RecognizeAsync($Bitmap)) ([Windows.Media.Ocr.OcrResult])
  $Lines = @($Result.Lines | ForEach-Object { $_.Text })
  @{parser='Windows.Media.Ocr'; language=$Engine.RecognizerLanguage.LanguageTag; lines=$Lines} | ConvertTo-Json -Depth 5 -Compress
} finally {
  if ($Bitmap) { $Bitmap.Dispose() }
  if ($Stream) { $Stream.Dispose() }
}
