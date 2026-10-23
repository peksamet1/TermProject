# P2P Enerji Ticareti – Durum Raporu

Oct 16, 2026 · @Kutluay Talay

Faz 1 bitti: tüm kod tek ve çalışan bir `main` dalında, 58 testin hepsi geçiyor. Sıradaki büyük iş Faz 3 (ZeroMQ iletişimi) ve paralelde Faz 2'nin kalanı; Kasım'daki M2 kilometre taşına yetişmeleri gerekiyor.

## Fazların durumu

Altı fazdan biri bitti, ikisinin çekirdeği hazır, üçü henüz başlamadı.

| Faz | Ne yapıyor | Lider | Durum |
| --- | --- | --- | --- |
| 1 | Gereksinimler, tek kod tabanı, ortak veri yapıları | Tüm ekip | Bitti |
| 2 | Ajanlar ve enerji profilleri (Mesa) | Erol | Çekirdek hazır; CSV verisi ve SoC'ye duyarlı strateji kaldı |
| 3 | Ajanlar arası mesajlaşma (ZeroMQ) | Kutluay | Mesaj formatı hazır; ağ kodu yazılacak |
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

## Sıradaki adımlar

Faz 2 ve Faz 3 aynı anda yürüyebilir; Faz 4 ikisini birleştirir. Faz 5'in batarya kısmı Faz 2 biter bitmez başlayabilir.

1. **Faz 3 – ZeroMQ iletişimi (öncelik bu).** Her ajana bir ağ düğümü (`PeerNode`) ve hepsini birbirine bağlayan bir ağ (`PeerNetwork`) yazılacak. Düğümler başlarken birbirini duyana kadar beklemeli, yoksa ilk mesajlar kaybolur. Önce 3 düğümlü bir prototip, sonra 10, 30 ve 50 düğümle ölçek denemesi yapılacak.
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
- **Doğrulanmamış rakamlar:** Mimari dokümanındaki ön deneme sonuçları (50 düğüm, mesaj kaybı 0) repoda yok; Faz 3'te yeniden üretilmeli.
