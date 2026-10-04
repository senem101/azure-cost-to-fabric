# 2. Azure Cost Management Export

Bu adımda Azure tarafında üç şey kurulur:

1. **Resource group** `rg-costfabric-<env>`
2. **ADLS Gen2 storage account** ve `costs` container'ı
3. **Günlük FOCUS cost export(lar)** — seçilen aboneliklerin (veya fatura hesabının) tüm maliyetini her gün bu container'a yazar

## 2.1 Ön koşullar

| Gereksinim | Neden |
|---|---|
| `az login` yapılmış Azure CLI (2.60+) | Deploy ve REST çağrıları |
| Abonelikte **Owner** (veya Contributor + User Access Administrator) | RG, storage ve rol ataması |
| **Cost Management Contributor** (Owner içinde var) | Export oluşturma/çalıştırma |
| Destekli hesap türü | EA, MCA, MOSP (Pay-as-you-go). *Sponsorship / bazı CSP abonelikleri FOCUS export'u desteklemeyebilir* |

## 2.2 Deploy

```powershell
./scripts/01-deploy-azure.ps1 -EnvironmentName demo -Location westeurope
```

Script sırasıyla:

| Adım | Ne yapar |
|---|---|
| 1/5 | Storage'ın kurulacağı aboneliği (aktif abonelik, `-SubscriptionId` ile değiştirilebilir) ve export alınacak abonelikleri belirler |
| 2/5 | `Microsoft.Storage` ve her export aboneliğinde `Microsoft.CostManagementExports` resource provider'larını kaydeder |
| 3/5 | `infra/main.bicep`'i **abonelik kapsamında** deploy eder: storage + her abonelik için bir export; oturum açan kullanıcıya *Storage Blob Data Reader* verir |
| 4/5 | Yalnız `-BillingScope` verildiyse: fatura hesabı kapsamında tek bir export oluşturur |
| 5/5 | Çıktıları `.azure-outputs.json` dosyasına yazar (sonraki scriptler buradan okur) |

Birden fazla abonelik için bkz. [2.8](#28-birden-fazla-abonelik).

Örnek çıktı:

```
AZURE_RESOURCE_GROUP         rg-costfabric-demo
AZURE_STORAGE_ACCOUNT_NAME   stcostdemo3x7k2...
AZURE_STORAGE_DFS_URL        https://stcostdemo3x7k2....dfs.core.windows.net/
COST_EXPORT_NAME             focus-daily-demo
COST_EXPORT_IDS              ["/subscriptions/<A>/providers/Microsoft.CostManagement/exports/focus-daily-demo", ...]
AZURE_SUBSCRIPTION_ID        9dc2...
```

## 2.3 Export ayarları (`infra/modules/cost-export.bicep`)

| Özellik | Değer | Açıklama |
|---|---|---|
| `identity` | `SystemAssigned` (location `global`) | Export, storage'a Entra ID ile yazar; paylaşılan anahtar gerekmez. Cost Management bu kimliğe `costs` container'ında *Storage Blob Data Contributor* rolünü otomatik atar |
| `definition.type` | `FocusCost` | Actual + amortized maliyeti tek veri setinde veren FOCUS formatı |
| `dataSet.configuration.dataVersion` | `1.0` | FOCUS şema sürümü |
| `timeframe` | `MonthToDate` | Her çalışmada ayın başından bugüne kadar olan veri |
| `granularity` | `Daily` | Satır başına bir gün |
| `format` / `compressionMode` | `Parquet` / `snappy` | Küçük ve hızlı |
| `partitionData` | `true` | Büyük aylarda birden fazla `part_*.parquet` dosyası |
| `dataOverwriteBehavior` | `OverwritePreviousReport` | Aynı ayın önceki dosyalarını değiştirir |
| `schedule.recurrence` | `Daily`, 5 yıl | İlk çalışma deploy'dan sonraki gün |
| `deliveryInfo` | container `costs`, klasör `focus/<abonelik-id>` | Hedef; her abonelik kendi klasörüne yazar, böylece aynı adlı export'lar çakışmaz |

> **Neden MonthToDate + günlük?** Azure maliyetleri geriye dönük olarak 72 saate kadar düzeltilebilir. Her gün tüm ayı yeniden yazmak, geç gelen düzeltmelerin de yakalanmasını sağlar. Ay kapandıktan sonraki ilk günlerde önceki ayın son hali de yazılır.

## 2.4 Klasör yapısı

Export her çalıştığında şu yapıyı üretir:

```
costs/                                   ← container
└── focus/
    ├── 1111.../                         ← rootFolderPath = focus/<abonelik-id>
    │   └── focus-daily-demo/            ← export adı
    │       ├── 20260901-20260930/       ← fatura dönemi
    │       │   └── 3f8706fc-.../        ← run ID (her çalıştırma ayrı GUID)
    │       │       ├── part_0_0001.parquet
    │       │       └── manifest.json
    │       └── 20261001-20261031/
    │           ├── acf857ee-.../        ← dünkü run  (eski)
    │           └── d092a721-.../        ← bugünkü run (güncel)
    ├── 2222.../focus-daily-demo/...     ← ikinci abonelik
    └── billing/focus-daily-demo/...     ← (-BillingScope kullanıldıysa)
```

**Önemli:** Aynı fatura dönemi için birden fazla run klasörü bulunabilir (örneğin overwrite devre dışıyken ya da manuel çalıştırmalar sonrasında). Her run **o ana kadarki tüm ayı** içerdiği için hepsini toplamak maliyeti 2-3 kat şişirir. Silver katmanı bu yüzden **her export klasörü ve fatura dönemi** için yalnızca **en son run**'ı tutar (bkz. [04-medallion-notebooklar.md](04-medallion-notebooklar.md)). Anahtarın yalnızca dönem olmaması önemlidir; aksi hâlde aynı ay için en son çalışan aboneliğin export'u diğer tüm abonelikleri silerdi.

## 2.5 Export'u hemen çalıştırma ve geçmişi doldurma

Zamanlanmış export ancak ertesi gün çalışır. Demo için hemen veri almak ve geçmiş ayları doldurmak için:

```powershell
./scripts/02-run-cost-export.ps1 -BackfillMonths 3
```

Script:

1. `.azure-outputs.json` içindeki **her** export (`COST_EXPORT_IDS`) için bu ayı `POST .../exports/<ad>/run` ile çalıştırır.
2. Geçmiş her ay için aynı API'yi `{"timePeriod": {"from": ..., "to": ...}}` gövdesiyle çağırır.
3. `runHistory` üzerinden çalışmaların bitmesini bekler (genelde 2–10 dakika, büyük hesaplarda daha uzun).
4. `costs` container'ındaki Parquet dosyalarını listeler.

Portal karşılığı: **Cost Management → Exports → focus-daily-demo → Run now** (geçmiş aylar için **Export selected dates**).

## 2.6 Doğrulama

```powershell
$o = Get-Content .azure-outputs.json | ConvertFrom-Json
az storage fs file list --account-name $o.AZURE_STORAGE_ACCOUNT_NAME -f costs --auth-mode login -o table
```

Hiç dosya yoksa: [06-sorun-giderme.md](06-sorun-giderme.md#export-çalıştı-ama-dosya-yok).

## 2.7 Manuel (portal) alternatif

1. Portal → **Cost Management** → Scope olarak aboneliği seçin → **Exports** → **+ Create**.
2. Template: **Cost and usage (FOCUS)**, Frequency: **Daily export of month-to-date costs**.
3. Destination: storage account, container `costs`, directory `focus/<abonelik-id>`.
4. Format: **Parquet**, compression **Snappy**, **Overwrite data** işaretli.

## 2.8 Birden fazla abonelik

Birden fazla aboneliğin maliyetini tek raporda görmek için üç yol vardır:

```powershell
# a) Seçili abonelikler — her birine ayrı export, hepsi aynı storage'a
./scripts/01-deploy-azure.ps1 -EnvironmentName demo -ExportSubscriptionIds <id1>,<id2>,<id3>

# b) Oturum açılan tenant'taki tüm etkin abonelikler
./scripts/01-deploy-azure.ps1 -EnvironmentName demo -AllSubscriptions

# c) EA fatura hesabı / MCA fatura profili kapsamında TEK export
./scripts/01-deploy-azure.ps1 -EnvironmentName demo -BillingScope /providers/Microsoft.Billing/billingAccounts/<id>
#    MCA için: /providers/Microsoft.Billing/billingAccounts/<id>/billingProfiles/<profileId>
```

| Kapsam | FOCUS export | Gereken yetki | Not |
|---|---|---|---|
| Abonelik (a, b) | ✅ | Her abonelikte Cost Management Contributor (Owner dahil) | Tüm anlaşma türleri. Rezervasyon/savings plan **satın alma** satırları abonelik kapsamında görünmeyebilir |
| EA billing account / MCA billing profile (c) | ✅ | EA: Enterprise Administrator; MCA: Billing profile owner/contributor | Tüm abonelikler + satın almalar tek veri setinde. En eksiksiz görünüm |
| Management group | ❌ (önerilmez) | — | Yalnız EA'da, yalnız *usage* maliyeti; amortized maliyet, satın almalar ve FOCUS yok |

Dikkat edilmesi gerekenler:

- **Aynı tenant:** Export'lar başka bir Entra tenant'ındaki storage'a yazamaz. `-AllSubscriptions` ve `-ExportSubscriptionIds` farklı tenant'taki abonelikleri uyarı vererek atlar. Birden fazla tenant için her tenant'ta ayrı storage + export kurun, Fabric'te her storage için ayrı bir shortcut ekleyin (ör. `Files/costs_tenant2`) ve Bronze'daki `.load(SOURCE_PATH)` çağrısına bu yolu da ekleyin: `.load(["Files/costs/", "Files/costs_tenant2/"])`.
- **Yetki:** Bicep deployment'ı tek bir işlemdir; bir abonelikte yetkiniz yoksa tüm deployment başarısız olur. Önce `-ExportSubscriptionIds` ile yetkili olduğunuz abonelikleri verin.
- **Çift sayım:** (a)/(b) ile (c)'yi birlikte kullanmayın; aynı maliyet hem abonelik hem fatura hesabı export'unda bulunur ve iki kez sayılır.
- **Fabric tarafı değişmez:** Bronze `Files/costs/` altındaki tüm export'ları okur; Gold `gold_dim_subscription` boyutunu üretir ve rapor abonelik bazında filtrelenebilir.