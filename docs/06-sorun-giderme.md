# 6. Sorun giderme

## Azure tarafı

### `The subscription is not registered to use namespace 'Microsoft.CostManagementExports'`
`01-deploy-azure.ps1` provider'ı kaydeder; kayıt birkaç dakika sürebilir. Manuel: `az provider register -n Microsoft.CostManagementExports --wait`.

### Export oluşturulamıyor: `FocusCost is not supported for this scope / offer`
FOCUS export'u EA, MCA ve Pay-as-you-go abonelikleri destekler. Bazı **Sponsorship, MSDN, Visual Studio, CSP (eski)** abonelikleri desteklemeyebilir. Çözüm: export'u fatura hesabı (billing account) kapsamında oluşturun veya `definition.type` değerini `AmortizedCost` yapın. Silver'daki `pick()` fonksiyonu `CostInBillingCurrency` gibi alternatif kolon adlarını eklemenizi kolaylaştırır.

### `scheduleStart must be in the future`
`exportStartDate` parametresi geçmiş bir tarihe ayarlanmış. Parametreyi boş bırakın (varsayılan: bugün → ilk çalışma yarın).

### Export çalıştı ama dosya yok
- `runHistory` içinde `status` alanına bakın: `az rest --method get --url "<COST_EXPORT_IDS içindeki ID>?api-version=2025-03-01&$expand=runHistory"`.
- **Yeni abonelik:** Maliyet verisi ilk 24–48 saat oluşmayabilir; boş ay için dosya yazılmaz.
- Export'un managed identity'sinin storage'da rolü olmalı: `az role assignment list --assignee <export identity.principalId> --all -o table` → `costs` container'ında *Storage Blob Data Contributor*. Yoksa export'u portaldan açıp **Save** edin (rol yeniden atanır) veya rolü elle verin.
- Storage firewall'u "Selected networks" ise *Allow trusted Microsoft services* işaretli olmalı.

### `Key-based authentication is currently disabled on this storage account`
Export **managed identity olmadan** oluşturulmaya çalışılıyor (eski şablon veya portal'da "Use system-assigned managed identity" kapalı). Bu repo export'ları `identity: SystemAssigned` ile oluşturur. Kurumsal tenant'larda "Azure Security Baseline" gibi politikalar paylaşılan anahtarı zorla kapatır; anahtar erişimini açmak yerine managed identity kullanın.

### `az storage fs file list` → `AuthorizationPermissionMismatch`
Rol ataması yayılıyor (~5 dk). Bekleyip tekrar deneyin.

## Çoklu abonelik

### `WARNING: Skipping subscriptions that are not enabled in tenant ...`
Export'lar başka tenant'taki storage'a yazamaz; script bu abonelikleri atlar. Diğer tenant için `az login --tenant <id>` ile ayrı bir ortam kurun ([2.8](02-azure-cost-export.md#28-birden-fazla-abonelik)).

### Deployment bir abonelikte `AuthorizationFailed` ile duruyor
Bicep tüm export'ları tek deployment'ta oluşturur; bir abonelikte yetki yoksa hepsi başarısız olur. Yetkili olduğunuz abonelikleri `-ExportSubscriptionIds` ile açıkça verin veya ilgili abonelikte *Cost Management Contributor* rolü isteyin.

### `-BillingScope` → `401/403` veya `RBACAccessDenied`
Fatura hesabı kapsamı Azure RBAC değil, **billing** rolleri ister: EA'da *Enterprise Administrator*, MCA'da *Billing profile owner/contributor*. Yetkiniz yoksa abonelik bazlı seçenekleri kullanın.

### Raporda bazı abonelikler eksik
- `az storage fs directory list -f costs --path focus --account-name <storage> --auth-mode login -o table` ile her aboneliğin klasörünü kontrol edin.
- Yeni eklenen aboneliğin export'u ertesi gün çalışır; hemen veri için `02-run-cost-export.ps1`'i tekrar çalıştırın.

### Toplam maliyet beklenenin 2 katı (çoklu abonelik)
Hem abonelik export'ları hem `-BillingScope` export'u aynı storage'a yazıyor olabilir. `focus/billing/` ile `focus/<abonelikId>/` klasörlerinden yalnızca birini tutun.

## Fabric tarafı

### `Capacity ... is not Active (state: Inactive)`
Kapasite duraklatılmış. Azure portal → *Microsoft Fabric capacity* → **Resume**, ya da:
```powershell
az resource invoke-action --action resume --ids <capacity-resource-id>
```
Demo sonrası **Suspend** etmeyi unutmayın (çalıştığı her saat ücretlendirilir).

### Bağlantı oluşturma `Credentials provided are invalid` / `Forbidden` ile başarısız
Workspace identity'nin rolü henüz yayılmadı. Script 15 kez × 30 sn tekrar dener. Yine olmuyorsa:
- Storage → **Access control (IAM)** → *Role assignments* → workspace adıyla aynı isimdeki servis sorumlusunun **Storage Blob Data Reader** olduğunu doğrulayın.
- Tenant'ta workspace identity kullanımı kısıtlıysa [manuel bağlantı](03-fabric-kurulum.md#34-manuel-alternatif-bağlantıyı-portaldan-oluşturma) oluşturup `--connection-id` ile verin.

### Shortcut oluştu ama `Files/costs` boş görünüyor
- Export henüz dosya yazmamış olabilir (yukarıya bakın).
- Bağlantının `path` değeri container'ı (`costs`) göstermeli; shortcut `subpath` = `/costs`.

### Bronze: `Path does not exist: Files/costs`
Notebook'un **varsayılan lakehouse**'u bağlı değil. Notebook → sol panel → *Lakehouses* → `CostLakehouse` → **Set as default**. (Script bunu otomatik yapar; elle import edilen notebook'larda gerekir.)

### Silver: toplam maliyet portaldakinin 2–3 katı
Eski run'lar elenmiyor demektir. `_source_file` yolunun `.../<export>/<yyyyMMdd-yyyyMMdd>/<runId>/` desenine uyduğunu kontrol edin:
```python
spark.read.table("bronze_costs").select("_source_file").distinct().show(truncate=False)
```
Desen tutmazsa `_billing_period` boş döner ve tüm satırlar tutulur. Regex'i kendi klasör yapınıza göre güncelleyin.

### Gold: `AssertionError: Gold ve Silver toplamları uyuşmuyor!`
Silver ile Gold toplamı farklı. Genellikle Silver'da `charge_date` veya `effective_cost` NULL olan satırlar ya da para birimi karışımı nedeniyle olur. `silver_costs`'ta `billing_currency` dağılımını kontrol edin.

### Semantic model: `Direct Lake: table not found`
Model, Gold tabloları oluşmadan yaratılmış. Pipeline'ı çalıştırın, ardından:
```powershell
python scripts/setup_fabric.py --capacity-name <kapasite> --skip-run
```
(script modeli günceller ve refresh eder) veya model → **Refresh**.

### Rapor eski veriyi gösteriyor
Direct Lake model, Delta tablosu değişince otomatik yenilenir (*Keep your Direct Lake data up to date* ayarı açıksa). Kapalıysa pipeline'ın sonuna bir **Semantic model refresh** aktivitesi ekleyin.

## Genel

| Belirti | Kontrol |
|---|---|
| `az` komutları `AADSTS...` hatası veriyor | `az login --tenant <tenant-id>` |
| `setup_fabric.py` → 401 | Fabric API token'ı alınamadı: `az account get-access-token --resource https://api.fabric.microsoft.com` |
| 429 Too Many Requests | Script `Retry-After` süresine uyar; çok sık tekrar çalıştırmayın |
