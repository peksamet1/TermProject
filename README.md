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
| 2 | Mesa ile ajan mantığı ve enerji profilleri | Erol | Çekirdek hazır; CSV verisi ve SoC'ye duyarlı strateji kaldı |
| 3 | ZeroMQ P2P iletişim altyapısı | Kutluay | Kod ve ölçüm tamam; ekip makinelerinde 3 düğüm denemesi (C9) ve `main` PR'ı bekliyor |
| 4 | CDA piyasa mekanizması (ZeroMQ üzerinden) | Yusuf | Süreç içi CDA hazır; `cda_zmq` kaldı |
| 5 | Mesa-SimPy ko-simülasyonu ve performans testleri | Erol + Kutluay | Başlanmadı |
| 6 | Veri analizi ve final dokümantasyon | Yusuf | Başlanmadı |

## Kurulum ve Çalıştırma

**Gereksinim:** Python 3.12 veya üstü (Mesa 3.5 bunu istiyor).

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

`market_backend`: varsayılan `"cda"`; baseline için `"logging"`.

## Mimari

```
microgrid/
├── config.py        SimulationConfig (market_backend dahil)
├── model.py         MicrogridModel + DataCollector + cash_tl settlement
├── agents/          HouseholdAgent, Battery, TradingStrategy (Phase 2; 4a'da aynı)
├── profiles/        Sentetik PV + tüketim
├── market/
│   ├── base.py      Order, MarketInterface (+ publish_status kancası), SettlementResult, LoggingMarket
│   └── cda.py       CDAMarket, Trade (submit anında match, settle → grid)
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
3. `_physics_slot()` — SimPy batarya simülasyonu; şimdilik boş. (Faz 5)
4. `_collect()` — DataCollector kaydeder.

## CDA kuralları

- Bid fiyatı >= ask fiyatı → match; clearing = midpoint.
- Kısmi dolum desteklenir.
- Öncelik: alışta en yüksek / satışta en düşük fiyat; eşitlikte erken arrival.
- `LoggingMarket` silinmedi (`market_backend="logging"`) — Phase 6 baseline.

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
turluk ölçümden düşük çıkar. Her taşıma 50 düğümde çalıştı; `inproc`'a
geçmek hiçbir N'de gerekmedi.

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
- **Ölçümler tek makinede alındı.** Windows'ta `resource` modülü yok; dosya
  limiti yükseltilemezse büyük tcp mesh'ler `inproc`'a düşebilir.

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
| Fiyat | TL/kWh | feed-in 1.20, perakende 3.50 |
| Pazar | — | `market_backend=cda` |

## Test Stratejisi

- `test_battery.py`, `test_profiles.py`, `test_agents.py` — Phase 2
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

İletişim testlerinde her bekleme bir son tarihe bağlı poll'dur; doğruluk hiçbir
yerde "yeterince uzun" bir `sleep`'e bırakılmaz. Bozuk bir uygulama testi asmaz,
kırar.

Enerji korunumu: yerel trade'ler birbirini götürür; grid yalnızca unmatched hacimdir.
