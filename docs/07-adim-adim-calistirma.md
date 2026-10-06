# 7. Adım adım çalıştırma (editör gerekmez)

Kurulum için **VS Code veya başka bir editör gerekmez**. Tüm script'ler bir terminalden çalıştırılır. İki seçenek vardır:

| | Seçenek 1: Azure Cloud Shell | Seçenek 2: Windows'ta PowerShell |
|---|---|---|
| Kurulum gerekir mi? | **Hayır**: tarayıcıda çalışır; Azure CLI, PowerShell 7, Python ve Bicep hazırdır | Evet: Azure CLI, PowerShell 7, Python (bir kez, ~10 dk) |
| Ne zaman? | Bilgisayara program kurulamıyorsa veya hızlı başlamak için | Kurumsal ağ, Cloud Shell'in engellendiği ortamlar |
| Dikkat | 20 dk hareketsizlikte oturum kapanır; geçici oturumda dosyalar silinir | Script çalıştırma politikası (aşağıda) |

Her iki seçenekte de komutlar aynıdır; yalnızca **hazırlık** (0. ve 1. adım) farklıdır.

> 📄 Bu sayfanın müşteriyle paylaşılabilir Word sürümü: [Azure-Maliyet-Fabric-Calistirma-Rehberi.docx](Azure-Maliyet-Fabric-Calistirma-Rehberi.docx)

---

## 0. Çözüm paketini edinme

Repo özel (private) ise ya GitHub hesabınıza erişim verilir ya da size ZIP dosyası iletilir.

- **ZIP ile (önerilen, Git gerekmez):** GitHub → repo sayfası → yeşil **Code** düğmesi → **Download ZIP** → `azure-cost-to-fabric-main.zip`
- **Git ile:** `git clone https://github.com/<hesap>/azure-cost-to-fabric.git`

---

## 1A. Hazırlık: Azure Cloud Shell

1. [portal.azure.com](https://portal.azure.com) → üst çubuktaki **Cloud Shell** simgesi (`>_`) → **PowerShell**.
   İlk açılışta *No storage account required* (geçici oturum) veya kalıcı dosyalar için bir storage hesabı seçin.
2. Cloud Shell araç çubuğunda **Manage files → Upload** → `azure-cost-to-fabric-main.zip` dosyasını yükleyin (ev klasörüne gider).
3. Paketi açın ve Python ortamını kurun:

```powershell
Expand-Archive ~/azure-cost-to-fabric-main.zip -DestinationPath ~ -Force
cd ~/azure-cost-to-fabric-main

az account show -o table                          # Cloud Shell zaten oturum açmıştır
az account set --subscription <STORAGE_ABONELIK_ID>

python3 -m venv scripts/.venv
./scripts/.venv/bin/Activate.ps1                  # komut satırı başında (.venv) görünür
python -m pip install -r scripts/requirements.txt
```

> **Cloud Shell notları**
> - Oturum **20 dakika** etkileşim olmazsa kapanır. Uzun süren adımlarda (export bekleme, Fabric kurulumu) arada bir Enter'a basın. Kapanırsa yeniden bağlanıp kaldığınız komutu tekrar çalıştırın; script'ler idempotenttir.
> - **Geçici oturum** kullanıyorsanız oturum bitince dosyalar silinir. Kurulumdan sonra `.azure-outputs.json` ve `.fabric-outputs.json` dosyalarını **Manage files → Download** ile indirip saklayın (abonelik ekleme ve kaldırma işlemlerinde gerekir).

## 1B. Hazırlık: Windows'ta PowerShell

**Araçları bir kez kurun.** Başlat menüsü → **Terminal** (veya *Windows PowerShell*) açın:

```powershell
winget install --id Microsoft.PowerShell -e
winget install --id Microsoft.AzureCLI -e
winget install --id Python.Python.3.12 -e
```

**winget hata verirse:**

| Belirti | Çözüm |
|---|---|
| `winget` *is not recognized* | Microsoft Store → **Uygulama Yükleyici (App Installer)** güncelleyin veya aşağıdaki doğrudan kuruluma geçin |
| *No package found* / kaynak (source) hatası / anlaşma sorusu | `winget source reset --force` (yönetici), ardından komutlara `--source winget --accept-source-agreements --accept-package-agreements` ekleyin |
| Kurumsal politika engeli, yönetici izni yok, proxy | Aşağıdaki **doğrudan kurulum** (yönetici gerekmez) veya hiç kurulum gerektirmeyen **Seçenek 1: Cloud Shell** |

**Doğrudan kurulum (winget olmadan, yönetici gerekmez).** Windows PowerShell'de çalıştırın:

```powershell
$ProgressPreference = 'SilentlyContinue'

# PowerShell 7 → %LOCALAPPDATA%\Microsoft\powershell
iex "& { $(irm https://aka.ms/install-powershell.ps1) } -AddToPath"

# Azure CLI (ZIP paketi) → %LOCALAPPDATA%\AzureCLI
Invoke-WebRequest https://aka.ms/installazurecliwindowszipx64 -OutFile $env:TEMP\azcli.zip
Expand-Archive $env:TEMP\azcli.zip -DestinationPath $env:LOCALAPPDATA\AzureCLI -Force
$p = [Environment]::GetEnvironmentVariable('Path', 'User')
[Environment]::SetEnvironmentVariable('Path', "$p;$env:LOCALAPPDATA\AzureCLI\bin", 'User')

# Python 3.12 (yalnızca kullanıcı için, PATH'e eklenir)
Invoke-WebRequest https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe -OutFile $env:TEMP\python-setup.exe
Start-Process $env:TEMP\python-setup.exe -Wait -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0'
```

> Bu yolla kurulan PowerShell 7 Başlat menüsünde görünmeyebilir; yeni bir terminal açıp `pwsh` yazarak başlatın. Komut satırından indirme engelleniyorsa kurulum dosyaları ([Azure CLI](https://aka.ms/installazurecliwindowsx64), [PowerShell 7](https://github.com/PowerShell/PowerShell/releases/latest) → `PowerShell-7.x-win-x64.msi`, [Python](https://www.python.org/downloads/windows/)) tarayıcıdan indirilip çift tıklanarak veya BT ekibinin yazılım merkezi (Company Portal / Software Center) üzerinden kurulabilir.

Kurulumdan sonra **tüm terminal pencerelerini kapatın** ve Başlat menüsünden **PowerShell 7** (`pwsh`) açın. Kontrol:

```powershell
$PSVersionTable.PSVersion   # 7.x
az version                  # 2.60+
python --version            # 3.10+
```

**Paketi açın ve oturumu hazırlayın:**

```powershell
Unblock-File $HOME\Downloads\azure-cost-to-fabric-main.zip
Expand-Archive $HOME\Downloads\azure-cost-to-fabric-main.zip -DestinationPath C:\ -Force
cd C:\azure-cost-to-fabric-main

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # yalnızca bu pencere için

az login --tenant <TENANT_ID>
az account set --subscription <STORAGE_ABONELIK_ID>
az account show -o table

python -m venv scripts\.venv
.\scripts\.venv\Scripts\Activate.ps1               # komut satırı başında (.venv) görünür
python -m pip install -r scripts\requirements.txt
```

> **Neden `Set-ExecutionPolicy`?** Windows, internetten indirilen `.ps1` dosyalarını varsayılan olarak engeller (*running scripts is disabled on this system*). `-Scope Process` ayarı yalnızca açık pencere için geçerlidir; bilgisayarın genel ayarını değiştirmez.

### Yeni bir pencere açtığınızda

Pencere kapanırsa veya ertesi gün devam ederseniz yalnızca şunları tekrarlayın:

```powershell
cd C:\azure-cost-to-fabric-main                     # Cloud Shell: cd ~/azure-cost-to-fabric-main
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # Cloud Shell'de gerekmez
.\scripts\.venv\Scripts\Activate.ps1                # Cloud Shell: ./scripts/.venv/bin/Activate.ps1
az account show -o table                            # oturum süresi dolduysa: az login --tenant <TENANT_ID>
```

---

## 2. Çalıştırma sırası

Aşağıdaki komutlar her iki ortamda da aynıdır ve paketin kök klasöründe çalıştırılır.

### Adım 1 – (Opsiyonel) Yerel demo

Azure/Fabric'e dokunmadan örnek veriyle tüm akışı gösterir:

```powershell
./local-demo/run-demo.ps1
```

### Adım 2 – Azure: storage + günlük FOCUS export'ları

Kapsamınıza uyan **tek** komutu çalıştırın:

```powershell
./scripts/01-deploy-azure.ps1 -EnvironmentName prod -Location westeurope                          # yalnız aktif abonelik
./scripts/01-deploy-azure.ps1 -EnvironmentName prod -ExportSubscriptionIds <id1>,<id2>,<id3>      # seçili abonelikler
./scripts/01-deploy-azure.ps1 -EnvironmentName prod -AllSubscriptions                             # tenant'taki tüm abonelikler
./scripts/01-deploy-azure.ps1 -EnvironmentName prod -BillingScope /providers/Microsoft.Billing/billingAccounts/<id>  # EA/MCA
```

✅ Sonunda `.azure-outputs.json` oluşur. Portal → Cost Management → **Exports** altında `focus-daily-prod` görünür.

### Adım 3 – İlk veri yükleme

```powershell
./scripts/02-run-cost-export.ps1 -BackfillMonths 3
```

Çalışmalar uzun süre *Queued* kalırsa (yeniden tetiklemeden) beklemeye devam edin:

```powershell
./scripts/02-run-cost-export.ps1 -WaitOnly -SinceMinutes 60
```

✅ Kontrol:

```powershell
$o = Get-Content .azure-outputs.json | ConvertFrom-Json
az storage fs file list --account-name $o.AZURE_STORAGE_ACCOUNT_NAME -f costs --auth-mode login -o table
```

### Adım 4 – Fabric kapasitesini açın

Portal → *Microsoft Fabric capacity* → **Resume**, veya:

```powershell
az resource invoke-action --action resume --ids /subscriptions/<id>/resourceGroups/<rg>/providers/Microsoft.Fabric/capacities/<kapasite>
```

### Adım 5 – Fabric kurulumu

```powershell
python scripts/setup_fabric.py --capacity-name <KAPASITE_ADI> --schedule-time 06:00
```

Workspace → workspace identity + rol → bağlantı → Lakehouse + shortcut → notebook'lar + pipeline → ilk çalıştırma → semantic model → günlük zamanlama → rapor. Ayrıntılar: [03-fabric-kurulum.md](03-fabric-kurulum.md).

✅ Sonunda `.fabric-outputs.json` oluşur. Yarıda kalırsa:

```powershell
python scripts/setup_fabric.py --capacity-name <KAPASITE_ADI> --connection-id <GUID> --skip-run --schedule-time 06:00
```

(`connection_id` değeri `.fabric-outputs.json` içindedir.)

### Adım 6 – Doğrulama

1. Fabric → `CostLakehouse` → **SQL analytics endpoint** → [05-power-bi-rapor.md §5.5](05-power-bi-rapor.md#55-doğrulama)'teki sorgu.
2. Azure portal → Cost Management → **Cost analysis** (Amortized, Monthly) ile karşılaştırın. Kapanmış aylar birebir tutmalı.
3. **Azure Cost Report**'u açın; Subscription, Resource Group ve Yıl › Ay › Gün filtrelerini deneyin.

### Adım 7 – Paylaşım

Workspace → **Manage access** → okuyucu grubu → **Viewer**. F64'ten küçük kapasitelerde okuyucuların Power BI Pro lisansı olmalıdır.

---

## 3. Sonraki işlemler

| İşlem | Komut |
|---|---|
| Yeni abonelik ekleme | `./scripts/01-deploy-azure.ps1 -EnvironmentName prod -ExportSubscriptionIds <id1>,<id2>,<yeniId>` → `./scripts/02-run-cost-export.ps1 -BackfillMonths 3` |
| Notebook / model / rapor güncelleme | `python scripts/setup_fabric.py --capacity-name <K> --connection-id <GUID> --skip-run` |
| Yalnızca raporu yeniden yayımlama | yukarıdakine `--skip-semantic-model` ekleyin |
| Kapasiteyi durdurma | `az resource invoke-action --action suspend --ids <kapasite-resource-id>` |
| Kaldırma | [demo-adim-adim.md](demo-adim-adim.md) → Temizlik |

## 4. Terminalde sık karşılaşılan hatalar

| Hata | Çözüm |
|---|---|
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `az` / `python` *is not recognized* | Kurulumdan sonra terminali kapatıp yeniden açın |
| `winget` hata veriyor veya yok | 1B'deki **doğrudan kurulum** komutlarını kullanın ya da Cloud Shell'e geçin |
| `python` Microsoft Store'u açıyor | Ayarlar → Uygulamalar → *Gelişmiş uygulama ayarları* → **Uygulama yürütme diğer adları** → `python.exe` ve `python3.exe` kapatın |
| `No module named azure` / `click` | Sanal ortam etkin değil: `Activate.ps1` satırını tekrar çalıştırın |
| `AADSTS…` / *InteractionRequired* | `az login --tenant <TENANT_ID>` |
| Cloud Shell bağlantısı koptu | Yeniden bağlanın, *Yeni bir pencere açtığınızda* bölümündeki komutları çalıştırıp kaldığınız adımı tekrarlayın |
| `.azure-outputs.json` bulunamadı (Cloud Shell yeni oturum) | İndirdiğiniz dosyayı tekrar yükleyin veya `setup_fabric.py`'ye `--storage-account <ad>` verin |
