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
`generate_sample_focus.py`, gerçek Cost Management export'uyla **aynı klasör yapısında**, **3 abonelik** için FOCUS 1.0 Parquet dosyaları üretir (her abonelik `focus/<abonelikId>/` altına):

```
latest run costs\focus\1111...\focus-daily-demo\20260701-20260731\7df7a0bb...\part_0_0001.parquet  (187 rows)  ← contoso-prod
latest run costs\focus\2222...\focus-daily-demo\20260701-20260731\b43b8753...\part_0_0001.parquet  (125 rows)  ← contoso-platform
latest run costs\focus\3333...\focus-daily-demo\20260701-20260731\36592992...\part_0_0001.parquet  ( 54 rows)  ← contoso-dev
...
older run  costs\focus\1111...\focus-daily-demo\20261001-20261031\42a60c2b...\part_0_0001.parquet  (13 rows)
latest run costs\focus\1111...\focus-daily-demo\20261001-20261031\9df92ad3...\part_0_0001.parquet  (19 rows)
older run  costs\focus\2222...\focus-daily-demo\20261001-20261031\ae0213ed...\part_0_0001.parquet  ( 9 rows)
latest run costs\focus\2222...\focus-daily-demo\20261001-20261031\eecdad1b...\part_0_0001.parquet  (13 rows)
...
```

Veride bilinçli olarak bulunanlar:
- 3 abonelik: **contoso-prod** (rg-agent-prod, rg-ai-prod, rg-web-prod), **contoso-platform** (rg-data-prod, rg-shared), **contoso-dev** (rg-dev)
- 10 servis, 12 kaynak, 6 resource group (VM, SQL, Cosmos DB, OpenAI, AKS, App Service, Storage, Log Analytics, VNet, Support)
- Dev VM'leri hafta sonu kapalı → hafta sonu maliyet düşüşü
- `environment` / `costcenter` / `project` etiketleri (bir kaynakta büyük harfli anahtarlar)
- Rezervasyonlu VM (`PricingCategory = Committed`) → **tasarruf** hesabı
- Ayın 1'inde kaynağı olmayan **Purchase** satırları (rezervasyon → prod, destek planı → platform)
- Her ayın 14–16. günlerinde Azure OpenAI **maliyet sıçraması** (anomali)
- Bu ay için her abonelikte **iki run** (dünkü ve bugünkü) → çift sayım senaryosu

### Adım 2 — Bronze → Silver → Gold
`run_local_pipeline.py`, Fabric notebook'larının birebir pandas karşılığıdır:

```
BRONZE  bronze_costs: 1147 rows   (tüm abonelikler, tüm run'lar, değiştirilmemiş)
SILVER  latest export run per export folder and billing period:
          1111.../focus-daily-demo  20261001-20261031  run 9df92ad3...  (2 run(s) found)
          2222.../focus-daily-demo  20261001-20261031  run eecdad1b...  (2 run(s) found)
          3333.../focus-daily-demo  20261001-20261031  run 0e8594c4...  (2 run(s) found)
        note: keying on billing period alone would keep only 365 of 1121 rows
        rows: 1147 -> 1121 after dropping superseded export runs     ← çift sayım engellendi
        effective cost by subscription: contoso-prod 4576.85 · contoso-platform 1663.39 · contoso-dev 172.26
GOLD    gold_fact_cost_daily 1121 · gold_dim_subscription 3 · gold_dim_resource 14 · gold_dim_service 10
        gold_dim_date 365 · gold_cost_monthly 48
        reconciliation silver vs gold effective cost: diff = 0.0010 OK
```

**Anlatılacak noktalar:**
- Eski run'ların 26 satırı atılmasaydı, bu ayın ilk günlerinin maliyeti iki kez sayılacaktı.
- `note:` satırı: Silver anahtarı yalnızca fatura dönemi olsaydı, 1121 satırın yalnızca 365'i kalacak, **diğer iki aboneliğin verisi sessizce kaybolacaktı**. Bu yüzden anahtar *(export klasörü, dönem)*'dir.

### Adım 3 — Rapor önizlemesi
`build_report.py`, Gold tablolarından Power BI raporunun HTML önizlemesini üretir ve tarayıcıda açar: KPI kartları (abonelik sayısı dahil), servis kategorisine göre günlük trend (OpenAI sıçraması görünür), abonelik/servis/resource group/environment kırılımları, top 10 kaynak, abonelik ve servis bazında aylık MoM tabloları.

---

## B. Canlı demo

> ⚠️ Fabric kapasitesini **Resume** etmek ücretlidir (F2 ≈ $0.36/saat, F64 ≈ $11.5/saat). Demo bitince **Suspend** edin.

| # | Komut / işlem | Gösterilecek |
|---|---|---|
| 1 | `./scripts/01-deploy-azure.ps1 -EnvironmentName demo` (çoklu abonelik: `-ExportSubscriptionIds <id1>,<id2>` veya `-AllSubscriptions`) | Portal → RG `rg-costfabric-demo` → storage; her abonelikte Cost Management → **Exports** → `focus-daily-demo`. Tam liste: [2.6 Portaldan kontrol listesi](02-azure-cost-export.md#portaldan-kontrol-listesi-adım-1-sonrası) |
| 2 | `./scripts/02-run-cost-export.ps1 -BackfillMonths 2` | Run history'de *Completed*; storage → `costs/focus/<abonelikId>/...` klasör yapısı |
| 3 | Fabric kapasitesini **Resume** | Portal → Fabric capacity → *Active* |
| 4 | `python scripts/setup_fabric.py --capacity-name <kapasite> --schedule-time 06:00` | Konsoldaki 8 adım; workspace'in açılması |
| 5 | Lakehouse → `Files/costs` | Shortcut ikonu, Parquet dosyaları (kopya değil!) |
| 6 | `02_silver_costs` notebook → çıktı | "Bronze: N satır → en güncel run'lar: M satır", abonelik ve servis bazında tablolar |
| 7 | SQL analytics endpoint | [05-power-bi-rapor.md §5.4](05-power-bi-rapor.md#54-doğrulama) sorgusu ↔ portal Cost analysis |
| 8 | `Azure Cost Model` → **Create report** | [05-power-bi-rapor.md §5.2](05-power-bi-rapor.md#52-raporu-oluşturma-adım-adım)'deki sayfalar |
| 9 | Pipeline → **Schedule** | Her gün 06:00 UTC otomatik yenileme |
| 10 | Kapasiteyi **Suspend** | Maliyet kontrolü |

### Temizlik

```powershell
az group delete -n rg-costfabric-demo --yes --no-wait
foreach ($id in (Get-Content .azure-outputs.json | ConvertFrom-Json).COST_EXPORT_IDS) {
    az rest --method delete --url "https://management.azure.com$($id)?api-version=2025-03-01"
}
# Fabric: workspace → Workspace settings → Remove this workspace
```
