# Autonomous P2P Energy Sharing and Trading in Decentralized Microgrids

Marmara Üniversitesi CSE4197 bitirme projesi — merkezî bir otorite olmadan,
prosumer'ların (üretici-tüketici haneler) fazla enerjilerini komşularıyla
doğrudan takas edebildiği bir mikrogrid simülasyonu.

**Ekip:** Erol Kahyaoğlu, Kutluay Talay, Yusuf Samet Pek
**Danışman:** Doç. Dr. Ali Haydar Özer

## Faz Durumu

| Faz | Kapsam | Durum |
|---|---|---|
| 1 | Literatür taraması ve gereksinim analizi | Tamamlandı |
| 2 | Mesa ile ajan mantığı ve enerji profilleri | Tamamlandı |
| 3 | ZeroMQ P2P iletişim altyapısı | Protokol hazır; socket'ler bekliyor (`comm/protocol.py`) |
| 4a | CDA eşleştirme + wire protokolü | **Tamamlandı (bu repo)** |
| 4b | Zengin SoC/fiyat batarya stratejisi | Ertelendi (ZeroMQ sonrası) |
| 5 | Mesa-SimPy ko-simülasyon entegrasyonu | Başlanmadı |
| 6 | Veri analizi ve final dokümantasyon | Başlanmadı |

## Kurulum ve Çalıştırma

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

`market_backend`: varsayılan `"cda"`; baseline için `"logging"`.

## Mimari

```
microgrid/
├── config.py        SimulationConfig (market_backend dahil)
├── model.py         MicrogridModel + DataCollector + cash_tl settlement
├── agents/          HouseholdAgent, Battery, TradingStrategy (Phase 2; 4a'da aynı)
├── profiles/        Sentetik PV + tüketim
├── market/
│   ├── base.py      Order, MarketInterface, SettlementResult, LoggingMarket
│   └── cda.py       CDAMarket, Trade (submit anında match, settle → grid)
└── comm/
    └── protocol.py  ZeroMQ-ready JSON wire şeması (STATUS/ORDER/TRADE/SETTLE/NACK)
```

Veri akışı (her 15 dakikalık adımda):

1. `shuffle_do("step")` — ajanlar residual hesaplar, batarya-önce, emir gönderir.
2. Her `submit` CDA order book'ta anında eşleşmeyi dener (midpoint fiyat).
3. `settle_step` kalan BUY/SELL'i makro-gride yazar; book temizlenir.
4. Ajan `cash_tl` local trade + grid fallback ile güncellenir; DataCollector kaydeder.

## Phase 4a CDA kuralları

- Bid fiyatı >= ask fiyatı → match; clearing = midpoint.
- Kısmi dolum desteklenir.
- Öncelik: alışta en yüksek / satışta en düşük fiyat; eşitlikte erken arrival.
- `LoggingMarket` silinmedi (`market_backend="logging"`) — Phase 6 baseline.

## Wire protokolü (Phase 3 zemini)

Taşıma birimi: UTF-8 JSON string. ZeroMQ'ya Python object verilmez.

Zarf (her mesaj): `type`, `v`, `msg_id`, `sender_id`, `step`, `corr_id?`, `topic?`

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

**Batarya-önce-pazar (4a).** Strateji zenginleştirilmedi; satılması önemli, SoC eşikleri 4b.

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
| Pazar | — | `market_backend=cda` |

## Test Stratejisi

- `test_battery.py`, `test_profiles.py`, `test_agents.py` — Phase 2
- `test_protocol.py` — JSON round-trip, zarf, bozuk mesaj, sürüm
- `test_cda.py` — match, midpoint, partial fill, priority, grid fallback, baseline karşılaştırma
- `test_model.py` — enerji korunumu (`sum(residual) == export - import`), SoC, DataCollector

Enerji korunumu: yerel trade'ler birbirini götürür; grid yalnızca unmatched hacimdir.
