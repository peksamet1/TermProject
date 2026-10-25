# P2P Enerji Ticareti – Sistem Mimarisi

Oct 8, 2026 · @Erol

## 1. Genel bakış

Sistem, bir mikrogriddeki hanelerin fazla enerjilerini merkezi bir otorite olmadan birbirleriyle alıp sattığı bir yazılım simülasyonudur. Dört ana parçadan oluşur: Mesa ile modellenen otonom ajanlar, ZeroMQ ile brokersız P2P iletişim, Continuous Double Auction (CDA) ile piyasa eşleştirme ve SimPy ile batarya ve enerji akışının ayrık olaylı simülasyonu.

Yerelde eşleşmeyen açık veya fazla makro şebekeye düşer (grid fallback). Sistemin başarısı, aynı senaryonun P2P ticaret olmadan koşulan baseline'ı ile karşılaştırılarak ölçülür.

Bu doküman yalnızca PSD'de (16.04.2026) ve ara sunumda tanımlı kapsamı tasarlar. Mevcut kod `peksamet1/TermProject` reposunun `yusuf` dalı esas alınarak değerlendirildi; `main` dalındaki ilk iskelet bu dalda yeniden yazılmış durumda.

## 2. Kapsam sınırları

Tasarımdaki her bileşen aşağıdaki "dahil" maddelerinden birine karşılık gelir; "hariç" listesindeki hiçbir şey tasarlanmaz.

**Dahil (PSD 5.1)**

- Kendi enerji profili, depolama durumu ve karar mantığı olan otonom prosumer ve consumer ajanları (Python, Mesa 3)
- ZeroMQ ile brokersız, gerçek zamanlı P2P iletişim katmanı (PUB-SUB ve REQ-REP)
- Alıcı ve satıcıları dinamik olarak eşleştiren CDA piyasa mekanizması
- SimPy ile zaman içinde enerji akışı ve batarya davranışı simülasyonu
- Simüle zaman serisi verisiyle performans değerlendirmesi: işlem başarı oranı, iletişim gecikmesi, makro şebeke bağımlılığı

**Hariç (PSD 5.2)**

- Fiziksel donanım, güneş paneli veya batarya kurulumu
- Blockchain tabanlı uzlaşma mekanizmaları
- AC güç akışı, frekans regülasyonu, reaktif güç gibi düşük seviye elektrik modelleri
- Üretime hazır kullanıcı arayüzü veya herkese açık web uygulaması

**Kısıtlar ve varsayımlar (PSD 5.3, 5.4)**

- Tamamen açık kaynak yığın: Python, Mesa, ZeroMQ, SimPy, Pandas, NumPy, Matplotlib/Plotly
- Simülasyon ölçeği standart bir öğrenci dizüstü bilgisayarıyla sınırlı
- Gerçek akıllı sayaç verisi yok (KVKK); İstanbul'a göre ölçeklenmiş sentetik veya herkese açık profiller kullanılır
- Fiyatlar Türkiye'deki iki kademeli tarifeye göre: şebekeye satış (feed-in) fiyatı düşük, şebekeden perakende alış fiyatı yüksek; P2P fiyatı bu bant içinde oluşur

## 3. Yüksek seviye mimari

Sistem altı katmandan oluşur ve her simülasyon adımında yukarıdan aşağı çalışır. Ajanlar piyasayı yalnızca `MarketInterface` üzerinden görür; arkadaki backend (`logging`, `cda`, `cda_zmq`) değişse de ajan kodu aynı kalır.

&#91;embedded content: Sistem mimarisi · 6 katman\]

Makro şebeke sistemin dışında durur ve yalnızca eşleşmeyen ya da onaylanmayan hacmi alır veya verir (PSD Şekil 1, "fallback only").

Temel tasarım kararları:

- Rol tipten gelmez: bir hanenin alıcı mı satıcı mı olduğu o adımdaki net enerjisine bağlıdır (mevcut kod).
- Merkezi broker ve merkezi piyasa düğümü yoktur: her düğüm emirleri PUB-SUB ile alır, aynı CDA eşleştirmesini kendi kopyasında yapar ve işlemi karşı tarafla REQ-REP ile müzakere eder (4.4).
- Tek Python süreci, yerel ZeroMQ soketleri: standart bir dizüstü bilgisayarda çalışır (PSD 5.3).
- Batarya modeli Mesa ve SimPy'den bağımsızdır: ajan planlar, SimPy fiziksel olarak uygular (4.5).

## 4. Bileşenler

Her bileşen mevcut koddaki bir modüle ya da eklenecek tek bir dosyaya karşılık gelir. "Mevcut" işaretli kısımlar `yusuf` dalında yazılmış ve test edilmiş durumda.

### 4.1 Senaryo ve veri

- `SimulationConfig` (`config.py`, mevcut): adım süresi (varsayılan 15 dk, günde 96 adım), gün sayısı, prosumer ve consumer sayısı, PV tepe gücü, batarya ve tarife parametreleri (feed-in 1.20, perakende 3.50 TL/kWh), `market_backend`, `seed`. Senaryo değişiklikleri yalnızca buradan yapılır.
- Profil arayüzü: `energy_at(step)` o adımdaki enerjiyi kWh olarak döner.
  - Sentetik (mevcut): `SolarProfile` ve `ConsumptionProfile`.
  - Zaman serisi (yeni): `TimeSeriesProfile` (`profiles/timeseries.py`), CSV'yi pandas ile okur. Config'e `profile_source` (`synthetic` veya `csv`) ve `profile_csv_path` eklenir.
- Veri kaynağı (PSD 5.3, 5.4, 9.2): birincil kaynak açık kaynak sentetik veri setleri ve herkese açık tüketim profilleridir, İstanbul güneş ışınımına ölçeklenir. Uygun veri bulunamazsa mevcut sentetik üreticilerle aynı formatta algoritmik veri üretilir (sunum B-planı).
- CSV formatı: `timestamp, household_id, pv_kw, load_kw`, her satır 15 dakikalık ortalama güç. Okuyucu kW değerini `step_hours` ile kWh'e çevirir. i. hane CSV'deki (i mod k). haneye bağlanır; koşu veri süresinden uzunsa veri başa sarar.
- Kural: her ajan profilini adım başına bir kez okur ve aynı değerler piyasaya ve SimPy'ye gider. Sentetik profiller her çağrıda yeni rastgele sayı çektiği için ikinci okuma sonucu ve tekrarlanabilirliği bozar.

### 4.2 Ajanlar (Mesa)

- `MicrogridModel` (`model.py`, mevcut): ajanları kurar, her adımda dört aşamayı sırayla çalıştırır: ajan adımı, piyasa ve settlement, fiziksel slot (SimPy), veri toplama (Bölüm 6).
- `HouseholdAgent.step()` (mevcut template method): profili okur, net = üretim − tüketim hesaplar, stratejiye göre bataryayı planlar, kalan fazla için SELL (miktar + minimum fiyat), kalan açık için BUY (miktar + maksimum fiyat) emri verir, \~0 ise IDLE kalır. Emir `market.submit(order)` ile gider; adımın sonunda her ajan, IDLE olsa bile, `market.publish_status(self)` çağırır.
- `ProsumerAgent`: PV ve batarya var. `ConsumerAgent`: PV ve batarya yok. Rol tipten gelmez: alıcı ya da satıcı olmak o adımdaki net enerjiye bağlıdır.
- `TradingStrategy` SoC'ye duyarlı hale gelir (PSD Faz 2). Önerilen başlangıç kuralı, adım başındaki SoC ile, s = (SoC − soc\_min) / (soc\_max − soc\_min) ve bant = perakende − feed-in olmak üzere:
  - Fazla varsa: fazlanın (1 − s) kadarı bataryaya, kalanı piyasaya; ask = feed-in + sell\_margin × (1 − s) × bant.
  - Açık varsa: açığın s kadarı bataryadan, kalanı piyasadan; bid = perakende − buy\_margin × s × bant.
  - Bataryasız ajan: mevcut sabit marj. Fiyat her zaman \[feed-in, perakende\] bandında ve sonlu bir sayıdır; NaN fiyat reddedilir.
- Neden adım başındaki SoC: mevcut "önce batarya" kuralında emir anındaki SoC hep 0.95 (satıcı) ya da 0.10 (alıcı) çıkıyor, bu yüzden fiyatı hiç değiştirmiyor.
- Topoloji (PSD Tablo 2, "Spatial Topology"): tüm haneler tek mikrogridde ve tek CDA'ya katılır (PSD Şekil 1); iletişim tam mesh; peer listesi config'ten gelir. Mesafe ya da hat modeli yoktur (PSD 5.2).

### 4.3 İletişim (ZeroMQ)

- Her ajanın bir `PeerNode`'u vardır (`comm/node.py`):
  - PUB: kendi STATUS ve ORDER mesajları, multipart `[topic, json]`.
  - SUB: diğer tüm düğümlerin PUB'larına bağlı, `status` ve `order` topic'lerine abone. SUB kendi PUB'ını duymadığı için düğüm kendi mesajını doğrudan kendi gelen kutusuna koyar.
  - REP: kendisine gelen ticaret isteklerini cevaplar.
  - REQ: yalnızca hedef satıcının REP'ine bağlı soket. Tek bir REQ birden çok REP'e bağlanırsa istekleri sırayla dağıtır ve hedef seçilemez. İstek başına açılıp kapanmaz, hedef başına havuzda tutulur ve yeniden kullanılır: istek başına REQ her istekte yeni bir tcp bağlantısı açıp kapanışta bir portu TIME_WAIT'te bırakıyordu (ölçüm 4.7'de). Zaman aşımına düşen REQ kapatılır, havuza dönmez.
- `PeerNetwork` (`comm/network.py`): düğümleri kurar, `tcp://127.0.0.1` üzerinde rastgele boş porta bağlar (`bind_to_random_port`, böylece aynı anda çalışan iki model çakışmaz), tam mesh bağlantıyı kurar, tüm soketlerde `LINGER=0` kullanır ve `close()` sağlar.
- Hazır olma el sıkışması: PUB-SUB bağlantısı kurulduktan hemen sonra yayınlanan mesajlar kaybolur ("slow joiner"; prototipte ilk mesajların %100'ü). Her düğüm yaklaşık 10 ms'de bir `step = -1` olan bir STATUS yayınlar ve herkes herkesten duyana kadar sürer; zaman aşımında hata verir. Sabit bekleme kullanılmaz.
- Adım senkronizasyonu: düğüm, adım t için N − 1 STATUS'u ve IDLE olmayan her peer'den bir ORDER'ı bekler (STATUS.state ≠ IDLE, bir ORDER geleceği anlamına gelir). Zaman aşımında (örn. 1–2 sn) eksikler kayıp, geç gelenler geç olarak sayılır.
- Bloklamama: tüm okumalar `zmq.Poller` ile zaman aşımlıdır. Tek iş parçacığında bloklayan bir `REQ.recv` kilitlenir, çünkü satıcının REP'ine hizmet edecek kimse kalmaz; müzakere bu yüzden turlarla yapılır (4.4). Birincil önlem 3 düğümlü prototiptir (PSD 9.2). Soketler yine de Mesa adımını bloklarsa ağ I/O'su ayrı bir olay döngüsü sarmalayıcıya taşınır (sunum B-planı).
- Ölçek: tam mesh tcp'de açık dosya sayısı yaklaşık N² büyür (N = 30'da \~2.500; Linux varsayılan sınırı 1024, macOS 256). `PeerNetwork` başlangıçta işletim sistemi sınırını ve ZeroMQ'nun soket sınırını (`MAX_SOCKETS`) yükseltir. Yetmezse aynı soketler `inproc://` adresleriyle çalıştırılır; yalnızca adres değişir.
- Gecikme: zarftaki `sent_at` ile alınma anı arasındaki fark ve REQ-REP gidiş-dönüş süresi ölçülür. Tek süreçte PUB-SUB farkı büyük ölçüde döngünün sırasını bekleme süresidir; bu yüzden adım başına iletişim süresiyle birlikte raporlanır.

### 4.4 Piyasa (CDA)

- `MarketInterface` (mevcut): `submit`, `settle_step`, `orders_at`. Eklenecek: `trades_at(step)` ve `publish_status(agent)`; ikincisi `cda` ve `logging` için boş bir metottur.
- `CDAMarket` (mevcut): bid fiyatı ≥ ask fiyatı ise işlem olur; fiyat-zaman önceliği (alışta en yüksek, satışta en düşük, eşitlikte önce gelen); işlem fiyatı orta noktadır; kısmi dolum desteklenir.
- Merkezsiz çalışma bir tasarım kararıdır: PSD mekanizmayı tanımlamaz, ama 6.2'deki "tek hata noktası yok" ve 6.3'teki "merkezi fiyat otoritesi yok" gereksinimlerini karşılaması gerekir. `ZmqMarket` (`market/zmq_market.py`) üç adımla çalışır:
  1. Yayın: ORDER, PUB-SUB ile herkese gider. `seq` o adımdaki gönderim sırasıdır, yani Mesa aktivasyon sırası.
  2. Her düğümde aynı eşleştirme: bariyer tamamlanınca her düğüm adım t'nin ORDER'larını `(seq, agent_id)` sırasıyla kendi `CDAMarket` kopyasına verir. Varış sırası değil `seq` kullanılır; aynı girdi tüm kopyalarda aynı işlem listesini üretir. Kopyalar her adım sonunda temizlenir.
  3. REQ-REP müzakere (PSD 6.2): alıcı eşleşen işlemi (adım, satıcı, miktar, fiyat) satıcıya önerir; satıcı kendi kopyasında aynı işlemi bulursa TRADE, bulamazsa NACK döner. Tek iş parçacığında üç tur: tüm alıcılar ister, tüm satıcılar REP'lerini boşaltıp cevaplar, tüm alıcılar cevapları toplar. Onaylanan işlemler kitap sırasıyla kaydedilir.
- Bir düğüm ORDER kaçırırsa kopyası farklılaşır, o düğümün işlemleri NACK alır ve hacim şebekeye düşer. Bu güvenli taraftır: settlement tutarlı kalır.
- Settlement ve grid fallback (`model.py`): ajan başına şebeke akışı = residual − onaylı işlem miktarı. Kalan alım perakende, kalan satış feed-in fiyatından ödenir ve `cash_tl` güncellenir. Mevcut `isinstance(self.market, CDAMarket)` kontrolü kaldırılır; tüm backend'ler `trades_at` ile aynı yoldan geçer. Hiç eşleşme olmazsa tüm hacim şebekeye gider (PSD risk planı).
- Backend seçimi: `logging` (P2P'siz baseline), `cda` (süreç içi, hızlı; SolaraViz paneli bunu kullanır), `cda_zmq` (asıl sistem; `run.py` ve deneyler). Kayıpsız bir koşuda `cda` ve `cda_zmq` aynı işlemleri ve aynı ajan bakiyelerini üretir.

### 4.5 Fiziksel katman (SimPy)

- `PhysicalSimulator` (`physics.py`): bir `simpy.Environment` tutar. Zaman birimi tam sayı dakikadır; bir slot bir Mesa adımıdır (15 dk).
- Her adımda, settlement'tan sonra ve veri toplamadan önce `run_slot(t)` çağrılır. Her batarya için o slotun planını argüman alan kısa bir süreç başlatır, ardından `env.run(until=(t+1)×15)` çalışır.
- Batarya süreci planı dakikalık alt adımlarla uygular: önce uygular, sonra `timeout(1)` ile bekler. Dakikalık güç sınırını ve SoC sınırlarını zorlar, geçen enerjiye bağlı kapasite azalması (degradation) uygular. Kapasite azalınca depolanan enerji soc\_max × kapasite'ye kırpılır ve kırpılan miktar kayıp olarak yazılır. Slot sonucu son bekleme anından önce kaydedilir.
- Uygulanamayan plan miktarı makro şebeke fallback'ine eklenir ve ikinci bir nakit geçişiyle settle edilir (PSD 6.4). Kısıt ihlali, herhangi bir dakikada SoC'nin \[soc\_min, soc\_max\] dışına çıkması ya da güç sınırının aşılmasıdır ve sayısı 0 olmalıdır.
- Enerji talep ve teslim olayları (PSD 6.4): onaylanan işlemlerin teslimi slot içinde zaman damgalı olay olarak kaydedilir. Bu yalnızca kayıttır; hat modeli yoktur.
- Battery değişiklikleri: durumu değiştirmeyen `plan_charge` ve `plan_discharge` (ajan kullanır), `charge` ve `discharge`'a süre parametresi (SimPy dakikalık çağırır) ve `apply_fade`. Ajanın `plan_*`'a geçişi SimPy ile aynı değişiklikte yapılır; aksi halde SoC donar ya da batarya iki kez uygulanır.
- SimPy tüm backend'lerde çalışır, baseline dahil; karşılaştırma aynı fizik üzerinden yapılır.
- Senkronizasyon kontrolleri her adımda: `env.now == model.time × 15` (iki bağımsız saat) ve slot t'nin tüm batarya süreçleri bitmiş olmalı.

### 4.6 Metrik ve analiz

- Mesa `DataCollector` (mevcut) adım verilerini toplar. `metrics.py`'deki fonksiyonlar KPI'ları bu çıktıdan ve iletişim sayacından hesaplar (Bölüm 8).
- `experiments/` aynı seed ve veriyle baseline (`logging`) ve P2P (`cda_zmq`) koşularını çalıştırır; sonuçlar `results/` altında CSV ve grafik (Matplotlib, Plotly) olarak çıkar.

### 4.7 Ön doğrulama

Bu tasarım 8 Ekim 2026'da atılıp kullanılacak prototiplerle denendi; o prototiplerin rakamları repoda üretilmemişti. ZeroMQ katmanının rakamları Faz 3'te repodaki kodla yeniden ölçüldü ve aşağıdaki tablo onların yerini alır.

**Koşul:** `experiments/comm_scale.py`; sentetik emir akışı (sabit seed'li, Mesa profillerinden ve CDA'dan bağımsız); `active_ratio = 1.0`, yani her düğüm her adım hem STATUS hem ORDER yayınlar, adım başına mümkün olan en fazla mesaj; 192 adım (2 gün), seed 42; tcp; ardından herkesin herkese istek attığı 5 REQ-REP turu. macOS (Darwin 25.6.0 arm64), Python 3.13.1, pyzmq 27.2.0, libzmq 4.3.5; her hücre tek koşu. Gerçek veri bu mesaj tavanını aşamayacağı için rakamlar veri kaynağı değişince geçerliliğini korur. Adım süresi, bir adımın ilk yayınından bariyerin tüm düğümlerde kapanmasına kadar geçen süredir.

| N | Mesaj / adım | Kayıp | Zaman aşımı | Adım süresi p50 / p95 | REQ-REP RTT p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| 3 | 6 | 0 | 0 | 0.35 / 0.77 ms | 0.47 / 1.70 ms |
| 10 | 20 | 0 | 0 | 2.25 / 2.39 ms | 2.77 / 14.27 ms |
| 30 | 60 | 0 | 0 | 19.08 / 20.72 ms | 41.70 / 148.45 ms |
| 50 | 100 | 0 | 0 | 57.09 / 60.87 ms | 179.13 / 472.02 ms |

- Dört N'nin hepsinde degraded adım, geç mesaj ve protokol hatası 0; her adımda tüm düğümler aynı emir listesini aynı sırada gördü. `active_ratio = 0.5` ve `inproc` satırları README'de; tcp her N'de yetti, `inproc`'a geçmek gerekmedi.
- Prototipin "N = 10'da \~5 ms, N = 50'de \~150 ms" adım süresi rakamları ölçülenden yüksek. Sebebi bilinmiyor: prototip ve onun ölçüm tanımı repoda yok. Ölçülen rakam esas alınır.
- RTT 5 turun hepsini kapsar; ilk turda bağlantı kurulumu da var, bu yüzden tek turluk bir ölçüm daha yüksek çıkar. N = 50'deki 472 ms'lik p95, varsayılan 1 sn'lik istek zaman aşımının yarısına yakın; Faz 4'te handler gerçek doğrulama yaptığında yeniden ölçülmeli.
- REQ havuzu (4.3): istek başına REQ ile N = 50'de bir tur 901 ms (p50) sürdü ve her istek bir tcp bağlantısını TIME_WAIT'te bıraktı; havuzla aynı tur 283 ms, ağ açıkken TIME_WAIT 0.
- Kopyaların süreç içi CDA ile aynı işlemleri ürettiği iddiası Faz 4'ün (`cda_zmq`) işidir ve henüz yeniden üretilmedi. SimPy katmanına ait iddia da Faz 5'te yeniden üretilecek; ikisi de şimdilik doğrulanmamış prototip gözlemidir.

## 5. Veri modelleri ve mesaj formatları

Veri yapıları mevcut kodda tanımlı; yeni olan yalnızca profil CSV'si ve birkaç alan.

| Yapı | Alanlar | Dosya |
| --- | --- | --- |
| Order | agent\_id, side (BUY/SELL), quantity\_kwh, price\_tl\_per\_kwh, step | `market/base.py` (mevcut) |
| Trade | buyer\_id, seller\_id, quantity\_kwh, price\_tl\_per\_kwh, step | `market/cda.py` (mevcut) |
| SettlementResult | grid\_import\_kwh, grid\_export\_kwh, local\_traded\_kwh, trade\_count, avg\_trade\_price | `market/base.py` (mevcut) |
| Battery | capacity\_kwh, enerji, soc, soc\_min/soc\_max, güç sınırları, verim; eklenecek: degradation katsayısı | `agents/battery.py`, `config.py` |
| Profil CSV | timestamp (15 dk), household\_id, pv\_kw, load\_kw | `data/*.csv` (yeni) |

Mesajlar UTF-8 JSON'dur (`comm/protocol.py`, mevcut). Her mesajın zarfı: `type`, `v`, `msg_id`, `sender_id`, `step`, `corr_id`, `topic`. Eklenecek alanlar: tüm mesajlara `sent_at` (gecikme ölçümü), ORDER'a `seq` (gönderim sırası). Çözücü bilinmeyen alanları sessizce atıyor; bu yüzden ikisi gerçek dataclass alanı olarak eklenir ve protokol sürümü 2'ye çıkar.

| Tip | Desen | Gönderen → alan | İçerik |
| --- | --- | --- | --- |
| STATUS | PUB-SUB, topic `status` | her ajan → herkes, her adım | üretim, tüketim, residual, SoC, durum (IDLE/BUYING/SELLING); `step = -1` el sıkışması |
| ORDER | PUB-SUB, topic `order` | alıcı veya satıcı → herkes | taraf, miktar, fiyat, seq |
| TRADE | REQ-REP | alıcı → satıcı (öneri), satıcı → alıcı (kabul) | alıcı, satıcı, miktar, fiyat, corr\_id |
| NACK | REQ-REP | satıcı → alıcı | reddedilen mesajın id'si, sebep |
| SETTLE | yayınlanmaz | — | mevcut şemada kalır; brokersız tasarımda adım özeti her düğümde yerel tutulur |

TRADE ve NACK'in `sender_id` değeri, mevcut koddaki varsayılan `"market"` yerine gönderen ajanın id'si olur, çünkü merkezi bir piyasa düğümü yoktur.

## 6. Bir ticaret döngüsünün akışı

Her adım PSD Şekil 2'deki akışı izler; ZeroMQ üzerinden çalışırken emirler önce tüm düğümlere yayılır, sonra her düğüm aynı eşleşmeyi kendisi bulur.

&#91;embedded content: Bir simülasyon adımı · 8 aşama\]

Adımlar 1–8 `MicrogridModel.step()` içinde dört aşamaya ayrılır: ajan adımı (1), piyasa ve settlement (2–6), fiziksel slot (7), kayıt (8). `cda` ve `logging` backend'lerinde 2–5 yerine piyasa süreç içinde çalışır; 6–8 aynıdır.

## 7. Klasör yapısı ve teknoloji yığını

Klasör yapısı `yusuf` dalındaki `microgrid/` paketini korur; yeni olanlar işaretli.

```
microgrid/
├── config.py
├── model.py            adım aşamaları, settlement, close()
├── agents/             household.py, battery.py, strategy.py
├── profiles/           solar.py, consumption.py, timeseries.py (yeni)
├── comm/               protocol.py, node.py (yeni), network.py (yeni)
├── market/             base.py, cda.py, zmq_market.py (yeni)
├── physics.py          PhysicalSimulator + batarya süreci (yeni)
└── metrics.py          KPI hesapları (yeni)
data/                   profil CSV'leri (yeni)
experiments/            baseline karşılaştırma koşuları (yeni)
tests/
run.py                  tek koşu, konsol özeti + grafik
app.py                  SolaraViz geliştirme paneli, yalnızca cda/logging
requirements.txt
```

`app.py` mevcut bir geliştirme panelidir; üretime hazır bir arayüz değildir (PSD 5.2) ve ZeroMQ soketleri açmaması için yalnızca süreç içi backend'lerle çalışır.

| Kategori | Araç (PSD Tablo 1) | Not |
| --- | --- | --- |
| Dil | Python 3.x | Mesa 3.5 için en az 3.12 |
| Ajan tabanlı modelleme | Mesa 3 | `mesa[viz]~=3.5` (mevcut) |
| İletişim | ZeroMQ | `pyzmq` (eklenecek) |
| Ayrık olaylı simülasyon | SimPy | `simpy` 4 (eklenecek) |
| Veri | Pandas, NumPy | mevcut |
| Görselleştirme | Matplotlib, Plotly | `plotly` eklenecek |
| Sürüm kontrolü | Git, GitHub | `peksamet1/TermProject` |
| Dokümantasyon | Google Drive, Google Docs |  |
| Test | pytest | mevcut test paketi (54 test) |

## 8. Başarı metrikleri

Her metrik PSD 9.1'deki bir başarı kriterine bağlıdır ve DataCollector çıktısıyla iletişim sayaçlarından hesaplanabilir.

| Amaç (PSD 3) | Başarı kriteri (PSD 9.1) | Nasıl ölçülür |
| --- | --- | --- |
| 1. Ajan modelleme (Mesa) | Her ajan farklı senaryolarda enerji dengesini, depolama durumunu ve karar mantığını hatasız korur | Her adımda Σ residual = export − import (tam eşitlik; eşik altı küçük residual'lar da şebekeye yazılır). SoC her dakika \[soc\_min, soc\_max\] içinde ve bir gün içinde gerçekten değişiyor. |
| 2. P2P iletişim (ZeroMQ) | Tüm ajanlar mesajları kayıpsız ve bloklanmadan değiş tokuş eder | Kayıp = beklenen − alınan mesaj (0 olmalı). Zaman aşımı sayısı (0 olmalı). Onaylanan / eşleşen işlem = 1.0. |
| 3. Piyasa (CDA) | Uyumlu teklif ve taleplerin çoğunluğu her döngüde eşleşir; tüm işlemler fiyat ve miktarla kaydedilir | Eşleşme oranı = eşleşen hacim / uyumlu hacim; uyumlu hacim, fiyatı kesişen (bid ≥ ask) emirler için min(Σ alış, Σ satış). Her adımda > 0.5 olmalı. Her işlem kaydında fiyat ve miktar var. |
| 4. Simülasyon ortamı (SimPy) | Zaman serisi verisiyle tüm dönem boyunca kararlı; fiziksel kısıt ihlali yok; Mesa-SimPy senkron | CSV verisinin tamamıyla koşu hatasız biter. Kısıt ihlali = 0. Her adımda env.now = model.time × 15. Fiziksel enerji dengesi: üretim − tüketim − batarya akışı = export − import (bataryanın iki kez uygulanmasını yakalar). |
| 5. Performans değerlendirmesi | P2P'siz baseline'a göre makro şebeke bağımlılığında azalma; en az bir tam koşuda işlem oranı, gecikme ve bağımlılık raporlanır | Şebeke bağımlılığı = şebekeden alım / toplam tüketim; baseline'a göre azalma (%). İşlem başarı oranı = yerelde karşılanan emir hacmi / toplam emir hacmi. Gecikme: PUB-SUB (sent\_at farkı), REQ-REP gidiş-dönüş, adım başına iletişim süresi. Topluluk enerji maliyeti (TL), PSD Faz 5. |

CDA her adımda kesişen tüm emirleri eşleştirdiği için eşleşme oranının 1'e yakın çıkması beklenir; asıl ayırt edici sonuçlar şebeke bağımlılığındaki azalma ve işlem başarı oranıdır.
