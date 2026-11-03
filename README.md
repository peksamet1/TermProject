# Autonomous P2P Energy Sharing and Trading in Decentralized Microgrids

Marmara Üniversitesi CSE4197 bitirme projesi — merkezî bir otorite olmadan,
prosumer'ların (üretici-tüketici haneler) fazla enerjilerini komşularıyla
doğrudan takas edebildiği bir mikrogrid simülasyonu.

**Ekip:** Erol Kahyaoğlu, Kutluay Talay, Yusuf Samet Pek
**Danışman:** Doç. Dr. Ali Haydar Özer

## Faz Durumu

Fazlar PSD 8.1'i izler; ayrıntılar Geliştirme Planı dokümanında.

| Faz | Kapsam | Lider | Durum |
|---|---|---|---|
| 1 | Literatür, gereksinimler, tek kod tabanı, sabit veri yapıları | Tüm ekip | Tamamlandı |
| 2 | Mesa ile ajan mantığı ve enerji profilleri | Erol | Çekirdek + veri hattı hazır (kalibre mock, CSV, Ausgrid/PVGIS); SoC'ye duyarlı strateji kaldı |
| 3 | ZeroMQ P2P iletişim altyapısı | Kutluay | Tamamlandı; açık not: Windows'ta 50 düğüm tcp (aşağıda) |
| 4 | CDA piyasa mekanizması (ZeroMQ üzerinden) | Yusuf | `cda_zmq` hazır: her düğümde CDA kopyası, TRADE/NACK onayı, grid fallback |
| 5 | Mesa-SimPy ko-simülasyonu ve performans testleri | Erol + Kutluay | SimPy batarya fiziği hazır (plan/uygula, dakikalık slot, kapasite kaybı, saat kontrolü); grid fallback ve performans testleri kaldı |
| 6 | Veri analizi ve final dokümantasyon | Yusuf | Başlanmadı |

## Kurulum ve Çalıştırma

**Gereksinim:** Python 3.12 veya üstü (Mesa 3.5 bunu istiyor). Önce
`python --version` ile kontrol edin: macOS'un sistem Python'u 3.9 olabilir ve
onunla kurulan sanal ortama paketler yüklenmez. Bu durumda sanal ortamı
`python3.12 -m venv .venv` ya da `uv venv --python 3.12 .venv` ile kurun.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python run.py          # headless koşu: konsol özeti + results/summary.png
$env:SOLARA_TELEMETRY_MIXPANEL_ENABLE = "False"
solara run app.py      # etkileşimli SolaraViz paneli
                       #   Page 0: enerji dengesi + LocalTradedKwh + grid
                       #   Page 1: SoC, emirler, local trade hacmi/fiyat
                       #   Page 2: hane bazli generation/consumption + orders
pytest                 # test paketi (30/50 düğümlü koşular hariç)
pytest -m slow         # 30/50 düğümlü iletişim koşuları (~30 sn)
python -m experiments.comm_scale   # iletişim ölçek deneyi -> results/comm_scale.*
```

macOS / Linux'ta etkinleştirme `source .venv/bin/activate`, ortam değişkeni
`SOLARA_TELEMETRY_MIXPANEL_ENABLE=False solara run app.py` biçimindedir. Solara
varsayılan olarak kullanım istatistiğini dışarıya (Mixpanel) gönderir; bu
değişken bunu kapatır. Panel ZeroMQ soketi açmaz, yalnızca süreç içi
backend'lerle çalışır.

Gerçek veriyle koşu (indirme adımları: [data/README.md](data/README.md)):

```powershell
python scripts/fetch_pvgis.py                     # İstanbul PV, 1 kWp, saatlik
python scripts/build_profiles.py --ausgrid "data/raw/ausgrid/<dosya>.csv" --pvgis data/raw/pvgis/istanbul_2019.json
python run.py --profiles data/profiles_istanbul.csv --start 2013-01-15 --days 7
python run.py --profiles data/sample/profiles_sample.csv --days 2   # küçük örnek dosya
```

`market_backend`: varsayılan `"cda"` (süreç içi, panel bunu kullanır); asıl merkezsiz
sistem `"cda_zmq"` (`python run.py --backend cda_zmq`); baseline `"logging"`.

## Mimari

```
microgrid/
├── config.py        SimulationConfig (market_backend dahil)
├── model.py         MicrogridModel + DataCollector + cash_tl settlement
├── physics.py       PhysicalSimulator: SimPy slotu, batarya planlarını dakikalık uygular
├── agents/          HouseholdAgent, Battery, TradingStrategy (Phase 2; 4a'da aynı)
├── profiles/        Sentetik PV + tüketim (İstanbul mevsimleri), CSV profilleri,
│                    Ausgrid + PVGIS dönüştürücü (real_data.py)
├── market/
│   ├── base.py      Order, Trade, MarketInterface (publish_status, trades_at, close), SettlementResult, LoggingMarket
│   ├── cda.py       CDAMarket (submit anında match, settle → grid)
│   └── zmq_market.py ZmqMarket (cda_zmq): ZeroMQ üzerinden CDA kopyaları + TRADE/NACK
└── comm/
    ├── __init__.py  Faz 4 sözleşmesi (docstring)
    ├── protocol.py  JSON wire şeması (STATUS/ORDER/TRADE/SETTLE/NACK), v2, dondurulmuş
    ├── node.py      PeerNode, CommStats
    └── network.py   PeerNetwork: kurulum, el sıkışması, bariyer, REQ-REP turları
experiments/
└── comm_scale.py    3/10/30/50 düğüm iletişim ölçek deneyi
```

Her 15 dakikalık adım (`MicrogridModel.step()`) dört aşamadan oluşur. Her faz
yalnızca kendi aşamasına yazar, böylece paralel çalışmada çakışma olmaz:

1. `_agents_step()` — ajanlar residual hesaplar, batarya-önce, emir gönderir;
   her `submit` CDA order book'ta anında eşleşmeyi dener (midpoint fiyat). (Faz 2)
2. `_market_settle()` — `settle_step` kalan BUY/SELL'i makro-gride yazar, book
   temizlenir; `cash_tl` local trade + grid fallback ile güncellenir. (Faz 4)
3. `_physics_slot()` — SimPy, ajanların batarya planlarını dakika dakika uygular. (Faz 5)
4. `_collect()` — DataCollector kaydeder.

## Faz 5: SimPy batarya fiziği

Mesa karar verir, SimPy uygular (Mimari 4.5):

- Ajan adımında batarya yalnızca **planlanır** (`plan_charge` / `plan_discharge`,
  durumu değiştirmez); kalan residual piyasaya gider. Ajanın gördüğü ve STATUS'ta
  yayınlanan SoC adım başı SoC'sidir.
- Piyasadan sonra `PhysicalSimulator.run_slot(t)` SimPy'de 15 dakikalık slotu
  çalıştırır: her batarya için bir süreç planı 1 dakikalık alt adımlarla uygular
  (`charge` / `discharge` süre parametresiyle), dakikalık güç sınırını ve SoC
  penceresini zorlar, taşınan enerji kadar kapasite kaybı (`apply_fade`) uygular.
  Kapasite azalınca soc_max üstünde kalan enerji kırpılır ve kayıp olarak yazılır.
- Her adımda iki saat kontrol edilir: slot sonunda `env.now == model.time × 15`;
  tutmazsa ya da bir batarya süreci bitmezse `SyncError`.
- Kısıt ihlali (bir dakikada SoC pencere dışı ya da güç sınırı aşımı) sayılır;
  `ConstraintViolations` 0 olmalı. DataCollector'a `BatteryShortfall`,
  `BatteryFadeLoss`, `ConstraintViolations` ve ajan başına `BatteryApplied` eklendi.
- Kapasite kaybı yüzünden slot içinde uygulanamayan plan `BatteryShortfall` olarak
  kaydedilir; bunun şebekeye yazılıp ödenmesi (grid fallback) sonraki adım.
  30 günlük koşuda toplamı ~0.001 kWh.

Kapasite kaybı kapalıyken (`fade_per_cycle=0`) sonuçlar Faz 5 öncesiyle aynı
(fark ≤ 1e-13, kayan nokta). Varsayılan ayarlarla 30 günlük koşu (yaz ve kış
başlangıcı) hatasız bitti, kısıt ihlali 0; 10 ajanda SimPy adım süresine ~%30 ekliyor.

## CDA kuralları

- Bid fiyatı >= ask fiyatı → match; clearing = midpoint.
- Kısmi dolum desteklenir.
- Öncelik: alışta en yüksek / satışta en düşük fiyat; eşitlikte erken arrival.
- `LoggingMarket` silinmedi (`market_backend="logging"`) — Phase 6 baseline.
- Ödeme her backend'de aynı yoldan: yalnızca `trades_at(step)` işlemleri yerel
  fiyattan ödenir, kalan alım perakende, kalan satış feed-in fiyatından.

## Faz 4: merkezsiz CDA (`cda_zmq`)

Merkezde piyasa düğümü yoktur; her ajanın bir `PeerNode`'u vardır
(`microgrid/market/zmq_market.py`). Bir adım:

1. `submit` ORDER'ı PUB-SUB ile yayar; `seq` adımın global gönderim sırasıdır
   (Mesa aktivasyon sırası). `publish_status` her ajanın STATUS'unu yayar.
2. `settle_step` adım bariyerini bekler, sonra her düğüm kendi ORDER'larını
   `(seq, agent_id)` sırasıyla kendi `CDAMarket` kopyasına verir. Aynı girdi →
   her kopyada aynı işlem listesi.
3. Alıcı, kopyasındaki her işlemi REQ-REP ile satıcıya önerir (TRADE). Satıcı kendi
   kopyasında aynı işlem varsa TRADE ile onaylar, yoksa NACK döner. Onaylanmayan
   ya da zaman aşımına düşen hacim makro şebekeye gider (güvenli taraf).

Kayıpsız bir koşuda `cda_zmq`, `cda` ile birebir aynı işlemleri ve aynı ajan
bakiyelerini üretir (`test_zmq_market.py`). Bir düğüm ORDER kaçırırsa onun
işlemleri NACK alır, enerji dengesi yine tutar. Model `close()` ile (veya
`with MicrogridModel(...)`) soketleri kapatır; `comm_summary()` iletişim ve
onay sayaçlarını verir.

Ölçüm (Linux konteyner, 1 gün = 96 adım, seed 42, tek koşu):

| Ajan | Taşıma | Süre | Bariyer p50 / p95 | Onay RTT p95 | Onaylanan / NACK / zaman aşımı | Kayıp |
|---|---|---|---|---|---|---|
| 10 | tcp | 0.6 sn | 2.6 / 4.5 ms | 1.7 ms | 175 / 0 / 0 | 0 |
| 50 | inproc | 9.3 sn | 64.9 / 101.4 ms | 3.3 ms | 909 / 0 / 0 | 0 |

Gerçek CDA doğrulamasıyla RTT, varsayılan 1 sn'lik `request_timeout_s`'in çok
altında kalıyor. 50 düğüm tcp'de bu konteynerde el sıkışması 5 sn'de bitmedi
(Windows notuyla aynı belirti); `handshake_timeout_s=60` ile 35.7 sn'de tamamlandı,
yani engel değil yavaşlık.

## Faz 3: iletişim katmanı

Ajanlar arası brokersız P2P iletişim: her ajana bir `PeerNode`, hepsini tam
mesh bağlayan bir `PeerNetwork`. Yalnızca PUB-SUB ve REQ-REP kullanılır
(PSD 5.1); her şey tek Python süreci ve tek iş parçacığında çalışır.

**`PeerNode`** (`microgrid/comm/node.py`), bir ajanın soketleri:

- PUB: kendi STATUS ve ORDER yayınları, multipart `[topic, json]`.
- SUB: diğer tüm düğümlerin PUB'larına bağlı. Kendi PUB'ını duymadığı için
  düğüm kendi mesajını doğrudan kendi gelen kutusuna koyar.
- REP: kendisine gelen istekleri cevaplar.
- REQ: her biri yalnızca tek bir hedefin REP'ine bağlı, hedef başına havuzda
  tutulur ve yeniden kullanılır. Bir REQ birden çok REP'e bağlansaydı istekleri
  sırayla dağıtır, hedef seçilemezdi.
- Gelen kutusu adım bazlıdır; `orders_at(step)` emirleri her düğümde aynı
  `(seq, agent_id)` sırasıyla verir, varış sırasıyla değil.

**`PeerNetwork`** (`microgrid/comm/network.py`), tüm ağ:

- Kurulum: işletim sistemi dosya limitini ve ZeroMQ soket limitini yükseltir,
  önce tüm PUB/REP'leri bağlar (`bind`), sonra SUB'ları bağlar (`connect`).
  tcp'de rastgele boş port kullanılır; `inproc://` aynı soketlerle çalışır.
- **El sıkışması** (`handshake()`): yeni bağlanan bir SUB ilk mesajları
  kaçırır ("slow joiner"). Sabit bekleme yerine her düğüm 10 ms'de bir
  `step = -1` olan bir STATUS yayınlar; herkes herkesten duyunca biter, 5 sn'de
  bitmezse `HandshakeTimeout` verir. El sıkışmadan önce yayın reddedilir.
- **Adım bariyeri** (`await_step(step)`): her düğüm her peer'den bir STATUS ve
  IDLE olmayan her peer'den bir ORDER bekler (STATUS'taki durum, ORDER'ın
  geleceğinin taahhüdüdür). Tüm okumalar `zmq.Poller` ile zaman aşımlıdır;
  bariyer asla asılmaz ve hata fırlatmaz. Süre dolunca eksikler kayıp sayılır
  ve adım "degraded" işaretlenir; sonradan gelen mesaj "geç" sayılır ve
  kutuya eklenmez, yoksa düğümlerin CDA kopyaları ayrışırdı.
- **REQ-REP** (`negotiation_rounds`): tek iş parçacığında bloklayan bir
  `REQ.recv` süreci kilitler, çünkü hedefin REP'ine hizmet edecek kimse
  kalmaz. Bu yüzden üç tur: tüm isteyenler gönderir, tüm hedefler REP'lerini
  boşaltıp cevaplar, tüm isteyenler cevapları toplar.
- Kapanış: `close()` ya da `with PeerNetwork(...)`. Tüm soketlerde
  `LINGER=0`; context `destroy(linger=0)` ile kapanır, böylece süit asılmaz.

**Zaman aşımı ve kuyruk varsayılanları** (`CommConfig`, `microgrid/config.py`):

| Ayar | Varsayılan |
|---|---|
| `transport` | `tcp` (yedek: `inproc`) |
| `handshake_interval_ms` / `handshake_timeout_s` | 10 ms / 5 s |
| `step_timeout_s` (bariyer) | 2 s |
| `request_timeout_s` (REQ-REP) | 1 s |
| `sndhwm` / `rcvhwm` | 100 000 (sonlu; PUB dolunca sessizce atar) |
| `io_threads` | 1 |

**Faz 4 sözleşmesi:** `ZmqMarket`'in Faz 3'ten neye güvenebileceği, adım
içindeki çağrı sırası ve sorumluluk sınırı `microgrid/comm/__init__.py`
docstring'inde yazılı. Özet: `seq`'i `ZmqMarket.submit` adımın global gönderim
sırasından atar (düğüm başına sayaç asla); `publish_status` kancası STATUS'u
ajanın önbelleğinden doldurur; `await_step` → `orders_at` → `CDAMarket`
kopyası → `negotiation_rounds` ile TRADE/NACK.

### Ölçek ölçümü

`experiments/comm_scale.py`, iletişim katmanını Mesa'dan, profillerden ve
CDA'dan **bağımsız** olarak ölçer: sabit seed'li bir `numpy` üreticisi
deterministik bir emir akışı kurar. Kayıp ve gecikme adım başına mesaj
sayısına bağlıdır, enerji değerlerine değil. `active_ratio` o adımda IDLE
olmayan (ORDER da yayınlayan) düğüm oranıdır; `1.0` adım başına mümkün olan
en fazla mesajdır, bu yüzden manşet rakamdır ve gerçek veri bu tavanı aşamaz.

```
python -m experiments.comm_scale        # results/comm_scale.csv + .png
```

Koşul: sentetik emir akışı, Mesa profillerinden bağımsız; 192 adım (2 gün),
seed 42, her adımın sonunda bariyer; ardından 5 müzakere turu (aktif düğümler
birbirine istek atar, `1.0`'da N(N−1) istek/tur). macOS (Darwin 25.6.0
arm64), Python 3.13.1, pyzmq 27.2.0, libzmq 4.3.5; her hücre tek koşu.
"Adım süresi" bir adımın ilk yayınından bariyerin kapanmasına kadardır. RTT
5 turun hepsini kapsar; ilk tur bağlantı kurulumunu da içerdiği için tek
turluk ölçümden düşük çıkar. Bu makinede her taşıma 50 düğümde çalıştı;
`inproc`'a geçmek hiçbir N'de gerekmedi. Windows'ta durum farklı, bkz.
"Bilinen sınırlar".

| N | taşıma | oran | mesaj/adım | kayıp | zaman aşımı | adım p50 / p95 (ms) | PUB-SUB gecikme p50 / p95 (ms) | RTT p50 / p95 (ms) | el sıkışma (ms) |
|---|---|---|---|---|---|---|---|---|---|
| 3 | tcp | 0.5 | 5 | 0 | 0 | 0.31 / 0.37 | 0.16 / 0.27 | 0.23 / 0.65 | 11.9 |
| 3 | tcp | 1.0 | 6 | 0 | 0 | 0.35 / 0.77 | 0.17 / 0.39 | 0.47 / 1.70 | 12.0 |
| 3 | inproc | 0.5 | 5 | 0 | 0 | 0.22 / 0.25 | 0.11 / 0.17 | 0.19 / 0.29 | 0.4 |
| 3 | inproc | 1.0 | 6 | 0 | 0 | 0.26 / 0.35 | 0.13 / 0.22 | 0.33 / 0.58 | 0.4 |
| 10 | tcp | 0.5 | 15 | 0 | 0 | 1.95 / 2.28 | 1.09 / 1.69 | 1.00 / 2.78 | 14.1 |
| 10 | tcp | 1.0 | 20 | 0 | 0 | 2.25 / 2.39 | 1.24 / 1.91 | 2.77 / 14.27 | 13.6 |
| 10 | inproc | 0.5 | 15 | 0 | 0 | 1.47 / 1.53 | 0.70 / 1.23 | 0.74 / 1.19 | 1.4 |
| 10 | inproc | 1.0 | 20 | 0 | 0 | 1.87 / 1.96 | 0.90 / 1.58 | 2.32 / 3.62 | 1.4 |
| 30 | tcp | 0.5 | 45 | 0 | 0 | 15.27 / 16.04 | 9.07 / 13.57 | 7.72 / 28.18 | 58.1 |
| 30 | tcp | 1.0 | 60 | 0 | 0 | 19.08 / 20.72 | 10.91 / 17.05 | 41.70 / 148.45 | 65.0 |
| 30 | inproc | 0.5 | 45 | 0 | 0 | 11.16 / 12.00 | 5.49 / 9.99 | 5.97 / 8.82 | 9.2 |
| 30 | inproc | 1.0 | 60 | 0 | 0 | 15.02 / 15.45 | 7.34 / 13.38 | 21.83 / 36.08 | 8.9 |
| 50 | tcp | 0.5 | 75 | 0 | 0 | 47.76 / 48.98 | 29.50 / 42.90 | 33.24 / 94.86 | 337.0 |
| 50 | tcp | 1.0 | 100 | 0 | 0 | 57.09 / 60.87 | 34.44 / 51.85 | 179.13 / 472.02 | 250.1 |
| 50 | inproc | 0.5 | 75 | 0 | 0 | 30.47 / 31.29 | 14.86 / 27.48 | 15.14 / 26.46 | 21.4 |
| 50 | inproc | 1.0 | 100 | 0 | 0 | 40.71 / 41.89 | 20.01 / 36.96 | 72.98 / 111.61 | 22.0 |

"Kayıp" STATUS ve ORDER kaybının toplamıdır; "zaman aşımı" REQ-REP zaman
aşımıdır. 16 hücrenin hepsinde ayrıca degraded adım, geç mesaj, protokol hatası
ve düğümler arası `orders_at` farkı da 0'dır. Tek süreçte PUB-SUB gecikmesi
büyük ölçüde döngünün sırasını bekleme süresidir; bu yüzden adım süresiyle
birlikte okunmalıdır.

**REQ havuzu.** REQ'ler istek başına açılıp kapanmak yerine hedef başına
havuzda tutulur. Gerekçe, `--req-mode per-request` ile yeniden ölçüldü (tcp,
`active_ratio = 1.0`, aynı koşul; her hücre TIME_WAIT'te 0 bağlantıyla
başladı):

| N | mod | istek | açılan REQ | TIME_WAIT (ağ açıkken) | tur süresi p50 (ms) |
|---|---|---|---|---|---|
| 10 | havuzlu | 450 | 90 | 0 | 3.96 |
| 10 | istek başına | 450 | 450 | 450 | 13.89 |
| 50 | havuzlu | 12250 | 2450 | 0 | 282.61 |
| 50 | istek başına | 12250 | 12250 | 12250 | 900.85 |

İstek başına REQ, her istekte yeni bir tcp bağlantısı açar ve her biri
kapanınca bir geçici portu 30 sn TIME_WAIT'te tutar (macOS'ta 16384 port).
N = 50'de bir tur 1 sn'lik `request_timeout_s` sınırına yaklaşır. Havuzlu
REQ'ler bağlantılarını ağ kapanana kadar tutar; kapanışta açık çift başına
yaklaşık bir TIME_WAIT bırakır.

### Bilinen sınırlar

- **Zaman aşımı payı:** N = 50, tcp, herkes herkese istekte RTT p95 472 ms
  (5 tur); bağlantıların henüz kurulmadığı ilk turda tek başına 525 ms ve tur
  süresi 692 ms (`rounds=1` satırı). Varsayılan `request_timeout_s` 1 sn. Faz 4'te handler gerçek CDA doğrulaması
  yapınca bu pay daralır, tekrar ölçülmeli.
- **libzmq 4.3.5'te nadir fd sızıntısı:** ağ, bağlantıları henüz kurulurken
  (el sıkışmadan önce) kapatılırsa libzmq bazen bir tcp fd'sini kalıcı olarak
  tutar; ölçümde 300–500 kurulum-kapanışta bir (saf pyzmq 1500'de 5, bizim
  ağımız 1000'de 2). Bizim kodumuz olmadan,
  yalnızca pyzmq ile de yeniden üretildi (tek seferlik ölçüm, macOS arm64,
  betik repoda değil). Bizim katmanımızda sızıntı yok. El sıkışmadan sonraki
  kapanışta görülmedi; bu yüzden fd testi ağı el sıkışmadan sonra kapatır.
- **Arka arkaya tcp koşuları:** tam matris kapanışta binlerce bağlantıyı
  TIME_WAIT'te bırakır. 30 sn içinde arka arkaya koşulursa geçici portlar
  tükenebilir; bu tek ve temiz başlatılmış koşularda görülmedi.
- **Windows'ta 50 düğüm tcp: açık, sebebi henüz araştırılmadı.** Windows 11'de
  el sıkışma 5 sn'lik `handshake_timeout_s` içinde tamamlanmadı (iki koşu, her
  seferinde farklı düğümler eksik). Aynı makinede 30 düğüm tcp ve 50 düğüm
  `inproc` geçti. Yavaşlık mı, Windows'a özgü bir engel mi olduğu, Durum
  Raporu'nda yazılı tanı komutuyla ileride netleşecek. O zamana kadar
  Windows'ta 30 düğümün üstünde `transport="inproc"` kullanın. İlgili test
  (`test_large_mesh_is_lossless[50-tcp]`) yalnızca Windows'ta `xfail` olarak
  işaretli; macOS ve Linux'ta normal koşar.
- **Windows'ta `resource` modülü yok**, bu yüzden dosya limiti yükseltilemez ve
  bir uyarı basılır; dosya limitiyle ilgili 2 test Windows'ta atlanır.

### Platform denemesi (C9)

Aynı `faz3` commit'i (`d679d49`), üç ekip üyesinin makinesinde. Ölçüm
satırları her makinede tek koşudur; ekip arkadaşlarının satırları kendi
bildirdikleri çıktılardır.

| Makine | İşletim sistemi | Python | `pytest` | `pytest -m slow` | 3 ve 10 düğüm deneyi | Adım p50, N=10 tcp, oran 1.0 |
|---|---|---|---|---|---|---|
| Erol | macOS (Darwin 25.6.0, arm64) | 3.13.1 | 198 geçti | 4/4 | kayıp 0, zaman aşımı 0 | 2.25 ms |
| Kutluay | macOS (Darwin 25.3.0, Apple Silicon) | 3.12.14 | 198 geçti | 4/4 | kayıp 0, zaman aşımı 0 | 2.02 ms |
| Yusuf | Windows 11 Pro (10.0.26200) | 3.13.5 (Anaconda) | 196 geçti, 6 atlandı | 3/4 (50 düğüm tcp, yukarıda) | kayıp 0, zaman aşımı 0 | 4.43 ms |

### Wire protokolü (v2, dondurulmuş)

Taşıma birimi: UTF-8 JSON string. ZeroMQ'ya Python object verilmez.

Zarf (her mesaj): `type`, `v`, `msg_id`, `sender_id`, `step`, `corr_id?`, `topic?`, `sent_at`

Protokol sürümü 2 (`PROTOCOL_VERSION = 2`):

- `sent_at`: gönderenin Unix zamanı (sn); gecikme ölçümü için her mesajda.
- `seq`: ORDER'ın o adımdaki gönderim sırası; her düğüm CDA kopyasını
  (seq, agent_id) sırasıyla besler, böylece tüm kopyalar aynı işlemleri bulur.
- TRADE ve NACK'te `sender_id` gönderen ajanın id'sidir (zorunlu); merkezi
  "market" düğümü yoktur.
- Sürüm 1 mesajları reddedilir.

| type | Taşıma | Rol |
|---|---|---|
| `STATUS` | PUB-SUB topic `status` (Faz 3) | Enerji durumu yayını; `step = -1` el sıkışması |
| `ORDER` | PUB-SUB topic `order` (Faz 3) | Bid/ask, `seq` ile |
| `TRADE` | REQ-REP gövdesi (Faz 4 doldurur) | Eşleşme önerisi ve onayı (`corr_id` korunur) |
| `NACK` | REP cevabı (Faz 4 doldurur) | Emir reddi |
| `SETTLE` | Faz 3'te taşınmıyor | Period sonu grid/local özet |

Framing: PUB-SUB multipart `[topic, json]`; REQ-REP tek frame. Topic'ler
yalnızca `status` ve `order`: ZeroMQ abonelikte önek eşleşmesi yaptığı için
bunlardan biriyle başlayan yeni bir topic tüm abonelere sızar.

API: `encode_message` / `decode_message` — `microgrid.comm.protocol`.

## Kritik Tasarım Kararları

**Rol ≠ tip.** Al/sat o adımdaki net enerjiden gelir; sınıflar yapısal farkı taşır.

**Batarya-önce-pazar.** Strateji henüz sabit marjlı; SoC'ye duyarlı strateji Faz 2'de.

**Pazar arayüzü.** Ajanlar yalnızca `market.submit(order)` ve adım sonunda,
IDLE olsalar bile, `market.publish_status(self)` çağırır; CDA veya Logging
arkada. Kanca bu iki backend'de boştur; Faz 4'te ZeroMQ backend'i STATUS'u
ajanın o adımda önbelleklediği değerlerden yayınlar. Raporlanan `soc` batarya
planı uygulandıktan sonraki değerdir, stratejinin kullandığı adım başındaki
SoC değil.

**Keyword-only model kurucusu.** SolaraViz uyumu.

**Battery framework-bağımsız.** Phase 5 SimPy için hazır.

## Birim Konvansiyonları ve Varsayılanlar

| Büyüklük | Birim | Varsayılan |
|---|---|---|
| Zaman adımı | 15 dk (96 adım/gün) | `steps_per_day=96` |
| Enerji | kWh/adım | — |
| Güç | kW | PV tepe 5 kW, batarya 5 kW |
| SoC | [0, 1] | [0.10, 0.95], başlangıç 0.50 |
| Kapasite kaybı | döngü başına, nominalin oranı | `fade_per_cycle=5e-5` (~4.000 döngüde %20, varsayım) |
| Fiyat | TL/kWh | feed-in 1.20, perakende 3.50 |
| Tüketim | kWh/gün | ~6.6 (≈200 kWh/ay, Türkiye ortalaması) |
| Başlangıç günü | yılın günü | `start_day_of_year=172` (21 Haziran) |
| Profil kaynağı | — | `profile_source=synthetic` (veya `csv`) |
| Pazar | — | `market_backend=cda` |

## Test Stratejisi

- `test_battery.py`, `test_profiles.py`, `test_agents.py` — Phase 2 (+ Faz 5: plan/uygula, süre, kapasite kaybı)
- `test_physics.py` — dakikalık slot, sınırlar, kısıt ihlali 0, fiziksel enerji dengesi ve batarya defteri
  (çift uygulamayı yakalar), Mesa-SimPy saat kontrolü; üç backend'de
- `test_timeseries.py` — CSV okuma, i mod k eşleme, başa sarma, hatalı veri, iki kaynakla enerji korunumu
- `test_real_data.py` — sahte Ausgrid/PVGIS dosyalarıyla dönüşüm (30→15 dk enerji korunur, ölçek, mevsim kaydırma)
- `test_protocol.py` — JSON round-trip, zarf, bozuk mesaj, sürüm, v2 alanları (`sent_at`, `seq`, `sender_id`)
- `test_cda.py` — match, midpoint, partial fill, priority, grid fallback, baseline karşılaştırma
- `test_model.py` — enerji korunumu (`sum(residual) == export - import`), SoC, DataCollector
- `test_agents.py` ayrıca: her ajan her adım `publish_status` çağırır (IDLE dahil);
  önbellekten STATUS üreten bir kanca koşu sonucunu tek değer bile değiştirmez ve
  profiller adım başına tam bir kez okunur
- `test_comm_node.py` — soket yaşam döngüsü, kapanış, fd sızıntısı
- `test_comm_network.py` — el sıkışması, bariyer, kayıp/geç sayımı, `orders_at` sırası
- `test_comm_reqrep.py` — REQ-REP, üç tur, zaman aşımı, REQ havuzu
- `test_comm_scale.py` — `CommStats` toplaması, p50/p95, 3/10 düğüm kayıpsızlık;
  30/50 düğüm `@pytest.mark.slow`
- `test_zmq_market.py` — `cda_zmq` = `cda` (işlemler, bakiyeler), enerji dengesi,
  STATUS içeriği, kaybolan ORDER → NACK + grid fallback, tekrarlanabilirlik, soket kapanışı

İletişim testlerinde her bekleme bir son tarihe bağlı poll'dur; doğruluk hiçbir
yerde "yeterince uzun" bir `sleep`'e bırakılmaz. Bozuk bir uygulama testi asmaz,
kırar.

Enerji korunumu: yerel trade'ler birbirini götürür; grid yalnızca unmatched hacimdir.
