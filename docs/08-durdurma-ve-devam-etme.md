# 8. Veri hattını durdurma ve yeniden başlatma

Azure export'u ve Fabric pipeline'ı **iki ayrı zamanlamadır**. Yalnızca birini kapatmak diğerini durdurmaz.

| Bileşen | Nasıl durdurulur? | Ne korunur? |
|---|---|---|
| Azure FOCUS export | Export schedule → `Inactive` | Export tanımı, hedef storage ve mevcut dosyalar |
| Fabric pipeline | Pipeline → Home → Schedule → ilgili zamanlamaları kapatın | Notebook'lar, Lakehouse tabloları, model ve rapor |
| Fabric kapasitesi | Ayrı ve paylaşımlı değilse Azure portal → kapasite → Pause | Veriler korunur, fakat o kapasitedeki tüm workspace'ler etkilenir |

## 8.1 Hazırlık

Python veya sanal ortam gerekmez; PowerShell 7 ve Azure CLI yeterlidir. Repo paketinin kök klasöründe çalışın. Yeni script'i almak için yerel değişiklikleriniz yoksa `git pull --ff-only` kullanabilir veya güncel ZIP paketini indirebilirsiniz.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
az login --tenant <TENANT_ID>
az account show --query "{Tenant:tenantId, Subscription:id}" -o table
```

Abonelik export'larını değiştirmek için ilgili aboneliklerde **Cost Management Contributor** veya eşdeğer export yazma yetkisi gerekir. Fatura hesabı / profili kapsamındaki export'lar için o kapsamda uygun billing yetkisi gerekir.

## 8.2 Yalnızca bu kurulumun export'larını durdurma

`scripts\03-set-cost-export-schedule.ps1`, varsayılan olarak repo kökündeki `.azure-outputs.json` dosyasının `COST_EXPORT_IDS` / eski `COST_EXPORT_ID` alanlarını kullanır. Bu yöntem kurulumda oluşturulan billing-scope export'unu da kapsar.

```powershell
# Once hedefleri ve mevcut durumlarini inceleyin; yazma yapilmaz
.\scripts\03-set-cost-export-schedule.ps1 -WhatIf

# Ayni kapsamdaki export zamanlamalarini kapatin
.\scripts\03-set-cost-export-schedule.ps1
```

Dosya farklı bir yerdeyse `-OutputsFile "C:\Kurulum\.azure-outputs.json"` parametresini ekleyin.

## 8.3 Tenant'taki tüm erişilebilir aboneliklerin FOCUS export'larını durdurma

Kurulum çıktısı dosyasına ihtiyaç duymaz:

```powershell
# Once listeyi kontrol edin
.\scripts\03-set-cost-export-schedule.ps1 -AllSubscriptions -WhatIf

# Tum hedefleri Inactive yapin
.\scripts\03-set-cost-export-schedule.ps1 -AllSubscriptions
```

**Kapsam uyarısı:** Bu seçenek, mevcut Azure CLI tenant'ında hesabınızın görebildiği **Enabled** aboneliklerdeki **tüm abonelik-kapsamlı FOCUS export'larını**, bu repo dışında oluşturulanlar dahil, bulur. Başka tenant'ları, erişemediğiniz abonelikleri ve billing-scope export'larını keşfetmez. Adında `focus` geçmesine değil, `properties.definition.type = FocusCost` olmasına bakar; ActualCost ve AmortizedCost export'larına dokunmaz.

Yalnızca seçili abonelikler:

```powershell
.\scripts\03-set-cost-export-schedule.ps1 -SubscriptionIds <ID1>,<ID2> -WhatIf
.\scripts\03-set-cost-export-schedule.ps1 -SubscriptionIds <ID1>,<ID2>
```

Fatura hesabı veya MCA fatura profili kapsamındaki export'lar ayrıca seçilmelidir. Aşağıdaki iki scope örneğinden ortamınıza uyanı kullanın:

```powershell
$scope = '/providers/Microsoft.Billing/billingAccounts/<ACCOUNT_ID>'
# MCA fatura profili icin:
# $scope = '/providers/Microsoft.Billing/billingAccounts/<ACCOUNT_ID>/billingProfiles/<PROFILE_ID>'

.\scripts\03-set-cost-export-schedule.ps1 -Scope $scope -WhatIf
.\scripts\03-set-cost-export-schedule.ps1 -Scope $scope
```

## 8.4 Script'in yaptığı kontroller

- Herhangi bir güncellemeden önce tüm hedefler keşfedilir ve export tanımları okunur. Okuma/yetki hatası olursa hiçbir güncelleme başlamaz.
- Önizlemede mevcut ve istenen durum ile tam export resource ID gösterilir. `-WhatIf` okuma yapar, yazma yapmaz; `-Confirm` her değişiklik için onay sorar.
- Mevcut hedef, FOCUS sürümü, sıkıştırma, partition/overwrite ayarları, managed identity ve zamanlama tarihleri korunur. Yalnızca `schedule.status` değiştirilir; güncel `eTag` istekle gönderilir.
- Zaten istenen durumda olan export atlanır. Her PUT işleminden sonra GET ile durum doğrulanır ve `Verified Inactive` / `Verified Active` yazılır.
- Eşzamanlı düzenleme, erişim hatası veya doğrulama hatasında script durur. Daha önce doğrulanmış güncellemeler geri alınmaz; hatayı giderip aynı komutu tekrar çalıştırın. Eksik yetki yüzünden yarım kalan bir çalışmayı tüm hedefler durmuş gibi değerlendirmeyin.
- Hiç FOCUS export bulunamazsa açık bir uyarı verir. Billing kapsamı veya farklı tenant kullanıp kullanmadığınızı kontrol edin.

Azure portal → **Cost Management → Exports** altında da zamanlamanın pasif olduğunu doğrulayabilirsiniz.

**Önemli:** `Inactive` sadece gelecek zamanlanmış çalışmaları durdurur. Kuyruktaki/çalışan export'ların iptal edildiği anlamına gelmez; run history'yi kontrol edin. Manuel **Run now** veya `02-run-cost-export.ps1` ile yeni bir çalıştırma hâlâ tetiklenebilir. Export'lar, storage hesabı ve mevcut veriler silinmez.

## 8.5 Fabric pipeline'ını da durdurma

1. Fabric → **Azure Cost Analytics** workspace → **Cost Refresh Pipeline** öğesini açın.
2. **Home → Schedule** bölümündeki tüm ilgili zamanlamaların anahtarını **Off** yapın. Bu repo günlük/sabit zamanlama oluşturur. Sonradan eklediğiniz zamanlamalar varsa onları da kontrol edin.
3. **Monitoring hub / Run history** üzerinden çalışan pipeline varsa **Cancel** uygulayın ve durumunu kontrol edin. Zamanlamayı kapatmak mevcut çalıştırmayı iptal etmez.
4. Sonradan event trigger, başka bir pipeline veya dış otomasyon eklediyseniz onları da kapatın. Manuel **Run** engellenmiş olmaz.

Yalnızca Azure export'larını durdurursanız Fabric pipeline'ı eski veriyi işlemeye devam edebilir. Yalnızca Fabric'i durdurursanız Azure export'ları storage'a yazmaya devam eder.

## 8.6 Yeniden başlatma

Önce aynı hedef kapsamını `-Status Active -WhatIf` ile kontrol edin, ardından `-WhatIf` olmadan çalıştırın:

```powershell
# Bu kurulumdaki export'lar
.\scripts\03-set-cost-export-schedule.ps1 -Status Active -WhatIf
.\scripts\03-set-cost-export-schedule.ps1 -Status Active

# Tum erisilebilir abonelikler secildiyse:
# .\scripts\03-set-cost-export-schedule.ps1 -AllSubscriptions -Status Active -WhatIf
# .\scripts\03-set-cost-export-schedule.ps1 -AllSubscriptions -Status Active
```

`Active`, seçilen kapsamdaki daha önce başka nedenlerle pasifleştirilmiş export'ları da açabilir; durdurmadan önceki listeyi saklayın ve yeniden başlatma kapsamını dikkatli seçin. Süresi bitmiş zamanlamaları script açmaz; önce portalda tarihleri düzenleyin. Durdurma sırasında kaçırılan geçmiş dönemler otomatik doldurulmaz.

Ardından gerekiyorsa [ilk yükleme / backfill](02-azure-cost-export.md) çalıştırın ve Fabric günlük zamanlamasını tekrar **On** yapın.

`01-deploy-azure.ps1` ile Bicep'i yeniden deploy etmek export zamanlamasını tekrar **Active** yapar. Durdurma sonrası yeniden deploy edecekseniz bu etkiyi göz önünde bulundurun.

## 8.7 Maliyet etkisi

Export'ları kapatmak Azure servislerinizin kendisini durdurmaz ve servis maliyetlerini kesmez. Cost Management export hizmetinin ayrı bir export ücreti yoktur; yeni dosya yazımı azalır ancak mevcut storage için depolama ücreti sürer.

Pipeline zamanlamasını kapatmak da ücretli Fabric kapasitesini durdurmaz. Kapasite size özel bir Azure F kapasitesiyse ve diğer işler etkilenmeyecekse Azure portal üzerinden **Pause** uygulayın. Rapor erişimi etkilenir; aynı kapasitedeki diğer workspace'ler için de geçerlidir.

## Kaynaklar

- [Azure Exports Create or Update API](https://learn.microsoft.com/en-us/rest/api/cost-management/exports/create-or-update?view=rest-cost-management-2025-03-01)
- [Fabric pipeline zamanlamalarını yönetme](https://learn.microsoft.com/en-us/fabric/data-factory/pipeline-runs#manage-scheduled-runs)
