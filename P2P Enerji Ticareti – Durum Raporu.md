# P2P Enerji Ticareti – Durum Raporu

Oct 16, 2026 · @Kutluay Talay

Faz 1 bitti: tüm kod tek ve çalışan bir `main` dalında, 58 testin hepsi geçiyor. Sıradaki büyük iş Faz 3 (ZeroMQ iletişimi) ve paralelde Faz 2'nin kalanı; Kasım'daki M2 kilometre taşına yetişmeleri gerekiyor.

## Fazların durumu

Altı fazdan biri bitti, ikisinin çekirdeği hazır, üçü henüz başlamadı.

| Faz | Ne yapıyor | Lider | Durum |
| --- | --- | --- | --- |
| 1 | Gereksinimler, tek kod tabanı, ortak veri yapıları | Tüm ekip | Bitti |
| 2 | Ajanlar ve enerji profilleri (Mesa) | Erol | Çekirdek hazır; CSV verisi ve SoC'ye duyarlı strateji kaldı |
| 3 | Ajanlar arası mesajlaşma (ZeroMQ) | Kutluay | Kod ve ölçüm tamam; ekip makinelerinde 3 düğüm denemesi (C9) ve `main` PR'ı bekliyor |
| 4 | Alım-satım eşleştirmesi (CDA) | Yusuf | Tek süreçte çalışıyor; ZeroMQ üzerinden çalışması kaldı |
| 5 | Batarya fiziği (SimPy) ve performans testleri | Erol + Kutluay | Başlanmadı |
| 6 | Sonuç analizi ve final rapor | Yusuf | Başlanmadı |

## Bugüne kadar yapılanlar

Faz 1 iki PR ile `main`'e girdi; ikisi de 16 Ekim'de birleştirildi.

1. **Tek kod tabanı ([PR #2](https://github.com/peksamet1/TermProject/pull/2)).** Yusuf'un `yusuf` dalındaki kod `main`'e alındı. Mesa 3'te çalışmayan eski iskelet (`core/`, `main.py`) silindi.
2. **Ortak veri yapıları sabitlendi ([PR #4](https://github.com/peksamet1/TermProject/pull/4)).** Mesaj protokolü sürüm 2'ye çıktı:
   - her mesajda gönderim zamanı (`sent_at`) var, gecikme ölçümü için;
   - emirlerde gönderim sırası (`seq`) var, böylece her düğüm aynı eşleşmeyi bulur;
   - TRADE ve NACK mesajlarını artık merkezi bir "market" değil, gönderen ajan imzalıyor.
3. **Simülasyon adımı dört aşamaya bölündü:** ajan kararları → piyasa ve ödeme → batarya fiziği (şimdilik boş) → kayıt. Her faz yalnızca kendi aşamasına yazacağı için paralel çalışırken kod çakışmaz. Sonuçlar eskisiyle birebir aynı.
4. **Kurulum hazır:** `pyzmq`, `simpy` ve `plotly` gereksinimlere eklendi; README Faz 1–6 planına göre güncellendi. Python 3.12 veya üstü gerekiyor.

Kontrol: 58 testin 58'i geçiyor ve `python run.py` özet grafiğini (`results/summary.png`) üretiyor.

**Güncelleme (25 Ekim 2026) — Faz 3: kod ve ölçüm tamam.** Faz, kalan iki şart da sağlanınca kapanacak: 3 düğümlü denemenin Kutluay ve Yusuf'un makinelerinde yapılması (C9, henüz yalnızca macOS'ta denendi) ve `faz3` → `main` PR'ı.

1. **Ağ katmanı:** her ajana bir `PeerNode`, hepsini bağlayan `PeerNetwork`. Kayıpsız başlangıç için el sıkışması, her adımda tüm mesajları bekleyen ve asla asılmayan bir bariyer, tek iş parçacığında kilitlenmeyen üç turlu REQ-REP.
2. **Ölçek ölçümü repoda:** `experiments/comm_scale.py`, 3/10/30/50 düğüm, tcp ve `inproc`, 192 adım. Hiçbir koşuda mesaj kaybı ya da zaman aşımı yok. Adım süresi p50: N = 10'da 2.25 ms, N = 50'de 57.09 ms (sentetik emir akışı, her düğüm her adım yayın yapıyor, macOS arm64). Tablolar README'de ve Mimari 4.7'de.
3. **`publish_status` kancası:** her ajan her adım, IDLE olsa bile, durumunu piyasaya bildiriyor. Şimdilik boş; Faz 4 ZeroMQ'ya bağlayacak. Sonuçlar kanca öncesiyle birebir aynı.
4. **Faz 4'e yazılı sözleşme:** `microgrid/comm/__init__.py` docstring'inde.

Kontrol: 198 test geçiyor, 30/50 düğümlü 4 ağır test `pytest -m slow` ile ayrıca geçiyor.

## Sıradaki adımlar

Faz 2 ve Faz 3 aynı anda yürüyebilir; Faz 4 ikisini birleştirir. Faz 5'in batarya kısmı Faz 2 biter bitmez başlayabilir.

1. **Faz 3 – ZeroMQ iletişimi (kod ve ölçüm tamam, 25 Ekim).** Kapanması için kalan: 3 düğümlü prototipin Kutluay ve Yusuf'un makinelerinde denenmesi (özellikle Windows) ve `faz3` → `main` PR'ı.
2. **Faz 2'nin kalanı (paralelde).** Gerçekçi tüketim ve güneş verisi CSV'den okunacak, İstanbul'a göre ölçeklenecek. Ajanlar batarya doluluk oranına (SoC) göre fiyat vermeye başlayacak.
3. **Faz 4 – CDA'nın ZeroMQ'ya taşınması.** Her düğüm aynı eşleştirmeyi kendi kopyasında yapacak, işlemi karşı tarafa onaylatacak (TRADE ya da NACK). Onaylanmayan hacim şebekeye düşecek.
4. **Faz 5 – Batarya fiziği ve performans.** SimPy ile dakikalık batarya simülasyonu, kapasite kaybı ve ajan sayısı artarken hız testleri.
5. **Faz 6 – Analiz ve rapor.** P2P ticaretli ve ticaretsiz (baseline) koşular karşılaştırılacak; asıl sonuç şebekeye bağımlılığın ne kadar azaldığı olacak.

## Kim ne yapacak

Görev dağılımı PSD'deki paylara göre; her fazın lideri o fazın %40–60'sını üstleniyor.

| Kişi | Şimdi | Sonra |
| --- | --- | --- |
| Kutluay Talay | Faz 3: `PeerNode`, `PeerNetwork`, el sıkışması, 3 düğümlü prototip | Faz 5: SimPy ve performans testleri (Erol ile) |
| Erol Kahyaoğlu | Faz 2: CSV veri hattı, SoC'ye duyarlı strateji | Faz 5: batarya fiziği (Kutluay ile) |
| Yusuf Samet Pek | Faz 4 hazırlığı: ödemenin backend'den bağımsız hale getirilmesi | Faz 4: `cda_zmq`; Faz 6: analiz ve rapor |

## Dikkat edilecekler

En büyük risk zaman: M2 için Faz 3 ve Faz 4'ün Kasım 2026'da bitmiş olması gerekiyor.

- **Takvim:** M2 (Kasım 2026) için Faz 3 ve 4'ün kabul kriterleri sağlanmalı; M3 (Ocak 2027) final sonuçlar. Kritik yol Faz 3.
- **"Merkezsizlik" sorusu:** Her şey tek bir Python sürecinde çalışıyor. Jüri bunu sorabilir; raporda bunun bir simülasyon varsayımı olduğu ve REQ-REP onayının hata toleransı için yapıldığı açıkça yazılmalı.
- **Ortam:** Mesa 3.5, Python 3.12 veya üstünü istiyor. Windows'ta bazı ölçek ayarları (`resource` modülü) yok; 3 düğümlü prototip herkesin bilgisayarında denenmeli.
- ~~**Doğrulanmamış rakamlar:**~~ **Kapandı (25 Ekim 2026).** İletişim katmanının rakamları (50 düğüm, mesaj kaybı 0) artık repodaki `experiments/comm_scale.py` ile üretiliyor ve koşuluyla README ile Mimari 4.7'de yazılı. Prototipin adım süresi rakamları (\~5 / \~150 ms) ölçülenden yüksekti; ölçülen esas alındı. Mimari 4.7'deki CDA kopyaları ve SimPy iddiaları Faz 4 ve 5'te yeniden üretilecek; onlar hâlâ doğrulanmamış.
- **Faz 4 için zaman aşımı payı:** 50 düğümde herkesin herkese istek attığı turda yanıt süresi p95 472 ms (5 tur), bağlantıların henüz kurulmadığı ilk turda 525 ms ve tur süresi 692 ms; varsayılan zaman aşımı 1 sn. Handler gerçek CDA doğrulaması yaptığında yeniden ölçülmeli.
