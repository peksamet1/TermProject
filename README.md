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
| 2 | Mesa ile ajan mantığı ve enerji profilleri | **Tamamlandı (bu repo)** |
| 3 | ZeroMQ P2P iletişim altyapısı | Placeholder (`microgrid/comm/`) |
| 4 | CDA pazar takas algoritması | Stub (`microgrid/market/`) |
| 5 | Mesa-SimPy ko-simülasyon entegrasyonu | Başlanmadı |
| 6 | Veri analizi ve final dokümantasyon | Başlanmadı |

## Kurulum ve Çalıştırma

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python run.py          # headless koşu: konsol özeti + results/summary.png
solara run app.py      # etkileşimli SolaraViz paneli (tarayıcıda açılır)
                       #   Page 0: topluluk enerji dengesi + grid ithalat/ihracat
                       #   Page 1: ortalama SoC + BUY/SELL emir sayilari
                       #   Page 2: hane bazli generation/consumption + buy/sell order
pytest                 # test paketi
```

## Mimari

```
microgrid/
├── config.py        SimulationConfig / BatteryConfig / TariffConfig — tüm parametreler tek yerde
├── model.py         MicrogridModel: ajan yaratımı, adım döngüsü, DataCollector
├── agents/
│   ├── household.py HouseholdAgent (soyut) + ProsumerAgent + ConsumerAgent
│   ├── battery.py   Battery: SoC penceresi, güç limitleri, round-trip verim
│   └── strategy.py  TradingStrategy: residual -> fiyatlı Order dönüşümü
├── profiles/
│   ├── solar.py     Sentetik PV üretimi (sinüs + bulut gürültüsü)
│   └── consumption.py  Çift tepeli hane yükü (sabah/akşam pikleri)
├── market/
│   └── base.py      Order, MarketInterface (ABC), LoggingMarket (Phase 2 stub)
└── comm/            Phase 3'te ZeroMQ katmanı (PUB-SUB + REQ-REP) buraya gelecek
```

Veri akışı (her 15 dakikalık adımda):

1. Model, ajanları rastgele sırayla aktive eder (`agents.shuffle_do("step")`).
2. Her ajan üretim ve tüketimini profillerinden okur, net enerjisini hesaplar.
3. Batarya varsa önce o düzeltir: fazla şarja, açık deşarja gider.
4. Kalan bakiye `TradingStrategy` üzerinden fiyatlandırılıp pazara `Order`
   olarak gönderilir (fazla → SELL, açık → BUY, ~0 → IDLE).
5. Model adım sonu agregalarını hesaplar ve `DataCollector` kaydeder.

## Kritik Tasarım Kararları

**Rol ≠ tip.** Alıcı/satıcı olmak sınıfın değil, o adımdaki net enerjinin
sonucudur: prosumer öğlen satıcı, akşam alıcıdır. Bu yüzden tüm karar akışı
soyut `HouseholdAgent.step()` içinde (template method); alt sınıflar yalnızca
yapısal farkları tanımlar. `ProsumerAgent` = PV + batarya, `ConsumerAgent` =
`generation() == 0`, bataryasız ince sınıf. Hiyerarşi bilinçli olarak iki
seviyede sabit — üretim tipi çeşitlenirse kalıtım değil profil kompozisyonu
değişir.

**Batarya-önce-pazar önceliği.** Round-trip verim kaybına rağmen öz-tüketim,
feed-in/perakende makasından her zaman daha kârlı olduğu için ajan önce
bataryasını kullanır, pazara yalnızca kalan bakiye gider.

**Pazar bir arayüz, Phase 2'de sadece log.** `LoggingMarket` hiçbir emri
eşleştirmez: her BUY makro-gridden ithalata, her SELL feed-in tarifesinden
ihracata düşer. Bu bilinçli bir tasarım — Phase 6'daki "no-P2P baseline"
tam olarak budur. Phase 4'te CDA motoru aynı `MarketInterface`'i implemente
edecek, ajan koduna dokunulmayacak.

**Fiyatlar tarife bandının içinde.** Türkiye'nin çift kademeli tarifesinde
feed-in (varsayılan 1.20 TL/kWh) ile perakende (3.50 TL/kWh) arasındaki her
yerel takas iki taraf için de kazançlıdır. `TradingStrategy` satış fiyatını
bandın altından, alış fiyatını üstünden marjla yerleştirir; bid > ask
kalması CDA'nın eşleşme bulabilmesinin ön koşuludur.

**Keyword-only model kurucusu.** SolaraViz modeli `model_params`'tan yeniden
yarattığı için `MicrogridModel` pozisyonel argüman kabul etmez. Slider'a
bağlanan skalerler (`num_prosumers`, `pv_peak_kw`, ...) config varsayılanlarını
ezer.

**Battery sınıfı framework-bağımsız.** Mesa'ya referansı yok; Phase 5'te aynı
sınıf SimPy süreçleriyle sürülecek. Round-trip kayıp, şarj ve deşarj yönüne
eşit bölünür (tek yön verim = sqrt(round_trip)).

## Birim Konvansiyonları ve Varsayılanlar

| Büyüklük | Birim | Varsayılan |
|---|---|---|
| Zaman adımı | 15 dk (96 adım/gün) | `steps_per_day=96` |
| Enerji | kWh/adım | — |
| Güç | kW (`step_hours` ile çevrilir) | PV tepe 5 kW, batarya 5 kW |
| SoC | [0, 1] kesir | pencere [0.10, 0.95], başlangıç 0.50 |
| Fiyat | TL/kWh | feed-in 1.20, perakende 3.50 |
| Rastgelelik | numpy `Generator`, tek seed | `seed=42` |

Tüm varsayılanlar `microgrid/config.py` içindeki dataclass'lardadır.

## Test Stratejisi

Her modülün kendi test dosyası vardır ve modül testiyle birlikte "bitti"
sayılır (`pytest` ile koşulur):

- `test_battery.py` — güç/kapasite limitleri, verim kaybı, SoC sınırları,
  geçersiz girdi reddi.
- `test_profiles.py` — gece PV=0, öğlen tepe, günlük tüketim 5-25 kWh bandı,
  aynı seed → aynı seri.
- `test_agents.py` — surplus→SELL / deficit→BUY / denge→IDLE; batarya-önce-pazar
  önceliği. Rastgeleliğe bağımlılığı kırmak için elle yazılmış `ConstantProfile`
  stub'ları kullanılır.
- `test_model.py` — tam gün koşu, **enerji korunumu değişmezi**
  (üretim − tüketim − batarya akışı = ihracat − ithalat, her adımda), SoC
  sınır ihlali olmaması, seed tekrarlanabilirliği, gündüz-satış/akşam-alış
  davranış doğrulaması.

Enerji korunumu testi, Phase 4-5'te pazar ve ko-simülasyon kodu değişirken
ana regresyon güvencemizdir.
