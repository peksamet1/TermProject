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
| 3 | ZeroMQ P2P iletişim altyapısı | Kutluay | Protokol v2 hazır; soketler bekliyor |
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
solara run app.py      # etkileşimli SolaraViz paneli
                       #   Page 0: enerji dengesi + LocalTradedKwh + grid
                       #   Page 1: SoC, emirler, local trade hacmi/fiyat
                       #   Page 2: hane bazli generation/consumption + orders
pytest                 # test paketi
```

Gerçek veriyle koşu (indirme adımları: [data/README.md](data/README.md)):

```powershell
python scripts/fetch_pvgis.py                     # İstanbul PV, 1 kWp, saatlik
python scripts/build_profiles.py --ausgrid "data/raw/ausgrid/<dosya>.csv" --pvgis data/raw/pvgis/istanbul_2019.json
python run.py --profiles data/profiles_istanbul.csv --start 2013-01-15 --days 7
python run.py --profiles data/sample/profiles_sample.csv --days 2   # küçük örnek dosya
```

`market_backend`: varsayılan `"cda"`; baseline için `"logging"`.

## Mimari

```
microgrid/
├── config.py        SimulationConfig (market_backend dahil)
├── model.py         MicrogridModel + DataCollector + cash_tl settlement
├── agents/          HouseholdAgent, Battery, TradingStrategy (Phase 2; 4a'da aynı)
├── profiles/        Sentetik PV + tüketim (İstanbul mevsimleri), CSV profilleri,
│                    Ausgrid + PVGIS dönüştürücü (real_data.py)
├── market/
│   ├── base.py      Order, MarketInterface, SettlementResult, LoggingMarket
│   └── cda.py       CDAMarket, Trade (submit anında match, settle → grid)
└── comm/
    └── protocol.py  ZeroMQ-ready JSON wire şeması (STATUS/ORDER/TRADE/SETTLE/NACK), v2
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

## Wire protokolü (Phase 3 zemini)

Taşıma birimi: UTF-8 JSON string. ZeroMQ'ya Python object verilmez.

Zarf (her mesaj): `type`, `v`, `msg_id`, `sender_id`, `step`, `corr_id?`, `topic?`, `sent_at`

Protokol sürümü 2 (`PROTOCOL_VERSION = 2`):

- `sent_at`: gönderenin Unix zamanı (sn); gecikme ölçümü için her mesajda.
- `seq`: ORDER'ın o adımdaki gönderim sırası; her düğüm CDA kopyasını
  (seq, agent_id) sırasıyla besler, böylece tüm kopyalar aynı işlemleri bulur.
- TRADE ve NACK'te `sender_id` gönderen ajanın id'sidir (zorunlu); merkezi
  "market" düğümü yoktur.
- Sürüm 1 mesajları reddedilir.

| type | Phase 3 pattern | Rol |
|---|---|---|
| `STATUS` | PUB-SUB topic `status` | Enerji durumu yayını |
| `ORDER` | PUB `order` veya REQ | Bid/ask |
| `TRADE` | PUB `trade` veya REP | Eşleşme sonucu (`corr_id` korunur) |
| `SETTLE` | PUB `settle` | Period sonu grid/local özet |
| `NACK` | REP | Emir reddi |

Framing (Phase 3'te kodlanacak): PUB-SUB multipart `[topic, json]`; REQ-REP tek frame JSON.

API: `encode_message` / `decode_message` — `microgrid.comm.protocol`.

## Kritik Tasarım Kararları

**Rol ≠ tip.** Al/sat o adımdaki net enerjiden gelir; sınıflar yapısal farkı taşır.

**Batarya-önce-pazar.** Strateji henüz sabit marjlı; SoC'ye duyarlı strateji Faz 2'de.

**Pazar arayüzü.** Ajanlar yalnızca `market.submit(order)`; CDA veya Logging arkada.

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
| Tüketim | kWh/gün | ~6.6 (≈200 kWh/ay, Türkiye ortalaması) |
| Başlangıç günü | yılın günü | `start_day_of_year=172` (21 Haziran) |
| Profil kaynağı | — | `profile_source=synthetic` (veya `csv`) |
| Pazar | — | `market_backend=cda` |

## Test Stratejisi

- `test_battery.py`, `test_profiles.py`, `test_agents.py` — Phase 2
- `test_timeseries.py` — CSV okuma, i mod k eşleme, başa sarma, hatalı veri, iki kaynakla enerji korunumu
- `test_real_data.py` — sahte Ausgrid/PVGIS dosyalarıyla dönüşüm (30→15 dk enerji korunur, ölçek, mevsim kaydırma)
- `test_protocol.py` — JSON round-trip, zarf, bozuk mesaj, sürüm, v2 alanları (`sent_at`, `seq`, `sender_id`)
- `test_cda.py` — match, midpoint, partial fill, priority, grid fallback, baseline karşılaştırma
- `test_model.py` — enerji korunumu (`sum(residual) == export - import`), SoC, DataCollector

Enerji korunumu: yerel trade'ler birbirini götürür; grid yalnızca unmatched hacimdir.
