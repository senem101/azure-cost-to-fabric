<#
.SYNOPSIS
  Offline, step-by-step demo of the Azure Cost -> Fabric flow (no Azure or Fabric needed).

.EXAMPLE
  ./local-demo/run-demo.ps1            # pauses between steps
  ./local-demo/run-demo.ps1 -NoPause   # runs straight through
#>
param([switch] $NoPause)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Step($n, $title, $text) {
    Write-Host "`n############################################################" -ForegroundColor DarkCyan
    Write-Host " ADIM $n - $title" -ForegroundColor Cyan
    Write-Host "############################################################" -ForegroundColor DarkCyan
    Write-Host $text -ForegroundColor Gray
    if (-not $NoPause) { Read-Host "`n[Enter] ile devam" | Out-Null }
}

Step 0 'Hazirlik' 'Python sanal ortami (.venv) kuruluyor: pandas + pyarrow.'
$sysPy = if (Get-Command python3 -ErrorAction SilentlyContinue) { 'python3' } else { 'python' }
if (-not (Test-Path .venv)) { & $sysPy -m venv .venv }
$py = if ($IsWindows -or $env:OS -eq 'Windows_NT') { Resolve-Path .venv/Scripts/python.exe } else { Resolve-Path .venv/bin/python }
& $py -m pip install -q --disable-pip-version-check -r requirements.txt

Step 1 'Azure Cost Management export (simulasyon)' @'
Gercekte Azure her gun FOCUS 1.0 formatinda Parquet dosyalarini storage'a yazar.
Her abonelik icin ayri bir export vardir ve hepsi ayni container'a, kendi klasorune yazar:
  costs/focus/<abonelik-id>/<export>/<yyyyMMdd-yyyyMMdd>/<run-id>/part_0_0001.parquet
Burada ayni yapiyi 3 abonelik icin ornek veriyle uretiyoruz
(contoso-prod, contoso-platform, contoso-dev; 3 tam ay + bu ay, bu ay icin 2 run).
'@
Remove-Item -Recurse -Force sample-data, output -ErrorAction SilentlyContinue
& $py generate_sample_focus.py --months 3

Step 2 'Bronze -> Silver -> Gold (Fabric notebooklarinin yerel karsiligi)' @'
Bronze : dosyalar degistirilmeden okunur, lineage kolonlari eklenir.
Silver : her (abonelik export'u, fatura donemi) icin EN GUNCEL run tutulur (cift sayim engellenir),
         FOCUS kolonlari sabit bir sozlesmeye cevrilir, etiketler ayristirilir.
Gold   : Power BI icin yildiz sema (fact + tarih/abonelik/kaynak/servis boyutlari + aylik ozet).
'@
& $py run_local_pipeline.py

Step 3 'Rapor onizlemesi' 'Gold tablolarindan Power BI raporunun HTML onizlemesi uretiliyor ve aciliyor.'
& $py build_report.py
$html = Resolve-Path output/cost_report.html
if ($IsWindows -or $IsMacOS -or $env:OS -eq 'Windows_NT') { Start-Process $html }
else { Write-Host "Rapor: $html (Cloud Shell'de 'Manage files > Download' ile indirip tarayicida acin)" -ForegroundColor Yellow }

Write-Host "`nDemo tamamlandi. Ciktilar: $(Resolve-Path output)" -ForegroundColor Green
