# Demo — adım adım

İki senaryo vardır:

| Senaryo | Süre | Maliyet | Ne gösterir |
|---|---|---|---|
| **A. Yerel demo** | ~2 dk | Ücretsiz | Veri yapısı, Bronze/Silver/Gold mantığı, çift sayım çözümü, rapor önizlemesi |
| **B. Canlı demo** | ~30–45 dk | Fabric kapasitesi saatlik + kuruşluk storage | Gerçek Azure maliyetiniz Fabric'te ve Power BI'da |

---

## A. Yerel demo

```powershell
cd azure-cost-to-fabric
./local-demo/run-demo.ps1          # her adımda Enter bekler
./local-demo/run-demo.ps1 -NoPause # durmadan çalıştırır
```

### Adım 0 — Hazırlık
`.venv` sanal ortamı oluşturulur, `pandas` ve `pyarrow` kurulur.

### Adım 1 — "Azure export'u" simülasyonu
`generate_sample_focus.py`, gerçek Cost Management export'uyla **aynı klasör yapısında** FOCUS 1.0 Parquet dosyaları üretir:

```
latest run costs\focus\focus-daily-demo\20260701-20260731\fd0ee751...\part_0_0001.parquet  (366 rows)
latest run costs\focus\focus-daily-demo\20260801-20260831\54ab065d...\part_0_0001.parquet  (364 rows)
latest run costs\focus\focus-daily-demo\20260901-20260930\3f8706fc...\part_0_0001.parquet  (354 rows)
older run  costs\focus\focus-daily-demo\20261001-20261031\acf857ee...\part_0_0001.parquet  (7 rows)
latest run costs\focus\focus-daily-demo\20261001-20261031\d092a721...\part_0_0001.parquet  (14 rows)
```

Veride bilinçli olarak bulunanlar:
- 10 servis, 12 kaynak, 6 resource group (VM, SQL, Cosmos DB, OpenAI, AKS, App Service, Storage, Log Analytics, VNet, Support)
- Dev VM'leri hafta sonu kapalı → hafta sonu maliyet düşüşü
- `environment` / `costcenter` / `project` etiketleri (bir kaynakta büyük harfli anahtarlar)
- Rezervasyonlu VM (`PricingCategory = Committed`) → **tasarruf** hesabı
- Ayın 1'inde kaynağı olmayan **Purchase** satırları (rezervasyon + destek planı)
- Her ayın 14–16. günlerinde Azure OpenAI **maliyet sıçraması** (anomali)
- Bu ay için **iki run** (dünkü ve bugünkü) → çift sayım senaryosu

### Adım 2 — Bronze → Silver → Gold
`run_local_pipeline.py`, Fabric notebook'larının birebir pandas karşılığıdır:

```
BRONZE  bronze_costs: 1105 rows   (tüm run'lar, değiştirilmemiş)
SILVER  20261001-20261031  run d092a721...  (2 run(s) found)
        rows: 1105 -> 1098 after dropping superseded export runs     ← çift sayım engellendi
GOLD    gold_fact_cost_daily 1098 · gold_dim_resource 13 · gold_dim_service 10
        gold_dim_date 365 · gold_cost_monthly 40
        reconciliation silver vs gold effective cost: diff = 0.0009 OK
```

**Anlatılacak nokta:** Eski run'ın 7 satırı atılmasaydı, ayın ilk 7 gününün maliyeti iki kez sayılacaktı.

### Adım 3 — Rapor önizlemesi
`build_report.py`, Gold tablolarından Power BI raporunun HTML önizlemesini üretir ve tarayıcıda açar: KPI kartları, servis kategorisine göre günlük trend (OpenAI sıçraması görünür), servis/resource group/environment kırılımları, top 10 kaynak ve aylık MoM tablosu.

---

## B. Canlı demo

> ⚠️ Fabric kapasitesini **Resume** etmek ücretlidir (F2 ≈ $0.36/saat, F64 ≈ $11.5/saat). Demo bitince **Suspend** edin.

| # | Komut / işlem | Gösterilecek |
|---|---|---|
| 1 | `./scripts/01-deploy-azure.ps1 -EnvironmentName demo` | Portal → RG `rg-costfabric-demo` → storage; Cost Management → **Exports** → `focus-daily-demo` |
| 2 | `./scripts/02-run-cost-export.ps1 -BackfillMonths 2` | Run history'de *Completed*; storage → `costs/focus/...` klasör yapısı |
| 3 | Fabric kapasitesini **Resume** | Portal → Fabric capacity → *Active* |
| 4 | `python scripts/setup_fabric.py --capacity-name <kapasite> --schedule-time 06:00` | Konsoldaki 8 adım; workspace'in açılması |
| 5 | Lakehouse → `Files/costs` | Shortcut ikonu, Parquet dosyaları (kopya değil!) |
| 6 | `02_silver_costs` notebook → çıktı | "Bronze: N satır → en güncel run'lar: M satır" ve servis bazında tablo |
| 7 | SQL analytics endpoint | [05-power-bi-rapor.md §5.4](05-power-bi-rapor.md#54-doğrulama) sorgusu ↔ portal Cost analysis |
| 8 | `Azure Cost Model` → **Create report** | [05-power-bi-rapor.md §5.2](05-power-bi-rapor.md#52-raporu-oluşturma-adım-adım)'deki sayfalar |
| 9 | Pipeline → **Schedule** | Her gün 06:00 UTC otomatik yenileme |
| 10 | Kapasiteyi **Suspend** | Maliyet kontrolü |

### Temizlik

```powershell
az group delete -n rg-costfabric-demo --yes --no-wait
az rest --method delete --url "https://management.azure.com$((Get-Content .azure-outputs.json | ConvertFrom-Json).COST_EXPORT_ID)?api-version=2025-03-01"
# Fabric: workspace → Workspace settings → Remove this workspace
```
