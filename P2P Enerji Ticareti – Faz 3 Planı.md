# P2P Enerji Ticareti – Faz 3 Planı (ZeroMQ İletişim Katmanı)

Oct 22, 2026 · @Erol

Faz 3, ajanlar arası brokersız P2P iletişim katmanını kurar: her ajana bir `PeerNode`,
hepsini birbirine bağlayan bir `PeerNetwork`, kayıpsız başlangıç için el sıkışması,
adım bariyeri, kilitlenmeyen REQ-REP primitifi ve iletişim ölçümleri. Faz sonunda
Mimari dokümanı 4.7'deki doğrulanmamış rakamlar (3/10/30/50 düğüm, mesaj kaybı 0,
p50 ~5 ms / ~150 ms) repoda yeniden üretilmiş ve `results/` altında kayıtlı olur.

Dayanak: Mimari dokümanı 4.3 (tasarımın tamamı), 4.4 (Faz 4 ile sınır), 5 (mesaj
formatları), 8 (başarı metriği 2 ve 5), PSD 5.1 / 6.2 / 9.1 / 9.2.

---

## 1. Başlamadan önce (ortam, ~1 saat)

Bu makinede henüz `.venv` yok; varsayılan `python3` 3.14 ve bağımlılıkların hiçbiri
kurulu değil. Mesa 3.5 ile Python 3.14 uyumu doğrulanmadı; README'nin istediği taban
3.12. Mevcut en güvenli sürüm `python3.13` (3.13.1, Homebrew).

```
python3.13 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest                         # 58 testin 58'i geçmeli (Faz 3 taban çizgisi)
```

`pytest` çıktısı 58/58 değilse Faz 3'e başlamadan önce bu düzeltilir: Faz 3'ün her
adımı "mevcut 58 test + yeni testler yeşil" şartına bağlı.

Dal: `git checkout -b faz3`. Her alt adım kendi commit'i, faz sonunda `main`'e tek PR.

---

## 2. Kapsam: Faz 3 ne yapar, ne yapmaz

Faz 3 ile Faz 4 arasındaki sınır, Durum Raporu'ndaki iş bölümünün temelidir; bu sınır
bilerek keskin tutulur, yoksa iki faz aynı dosyalarda çakışır.

**Faz 3'e dahil (Mimari 4.3'ün tamamı)**

- `comm/node.py` → `PeerNode`: PUB, SUB, REP kalıcı soketleri; istek başına REQ.
- `comm/network.py` → `PeerNetwork`: tam mesh kurulum, port tahsisi, el sıkışması,
  adım bariyeri, tur bazlı istek/cevap sürüşü, `close()`.
- Hazır olma el sıkışması (slow joiner probleminin çözümü, sabit bekleme yok).
- Adım senkronizasyonu: `N − 1` STATUS + IDLE olmayan her peer'den bir ORDER.
- Bloklamama garantisi: tüm okumalar `zmq.Poller` ile zaman aşımlı.
- İletişim sayaçları: kayıp, zaman aşımı, geç mesaj, PUB-SUB gecikmesi,
  REQ-REP gidiş-dönüş süresi, adım başına iletişim süresi.
- `config.py`'ye `CommConfig` (taşıma, zaman aşımları, HWM, el sıkışma periyodu).
- `experiments/comm_scale.py` → 3/10/30/50 düğüm ölçek deneyi + `results/` çıktısı.
- `MarketInterface.publish_status(agent)` kancası (no-op) ve ajanın onu her adım
  çağırması — bariyerin ön şartı (Mimari 4.2, 4.4).

**Faz 3'e dahil değil (Faz 4'ün işi)**

- `market/zmq_market.py`, `ZmqMarket`, `market_backend="cda_zmq"`.
- Kopya başına CDA eşleştirmesi ve kopyaların tutarlılık karşılaştırması.
- TRADE/NACK müzakere semantiği (Faz 3 yalnızca taşıma primitifini verir).
- `trades_at(step)` ve `model.py`'deki `isinstance(self.market, CDAMarket)`
  kontrolünün kaldırılması (Durum Raporu'nda Yusuf'ta).
- `MicrogridModel.close()`, SimPy, CSV profilleri, SoC'ye duyarlı strateji.

**Veri varsayımı: şimdilik mock**

Veri katmanı şimdilik mevcut sentetik üreticilerdir (`SolarProfile`,
`ConsumptionProfile`); gerçek veri sonra entegre edilecek. Bu bir sapma değil,
dokümante edilmiş durum: Mimari 4.1 zaten "uygun veri bulunamazsa mevcut sentetik
üreticilerle aynı formatta algoritmik veri üretilir (sunum B-planı)" diyor ve PSD
5.3 / 5.4 gerçek akıllı sayaç verisi olmadığını (KVKK) baştan kabul ediyor.

Faz 3 için sonucu: **plan yapısı değişmez.** İletişim katmanı enerji değerlerini
taşır, yorumlamaz; kayıp, zaman aşımı ve gecikme adım başına **mesaj sayısına** ve
mesaj boyutuna bağlıdır, enerji rakamlarına değil. Mesaj boyutu da protokolün sabit
alan kümesiyle belirlenir (v2 dondurulmuş), yani veri kaynağından bağımsızdır. Bu
yüzden Faz 3 ne `data/*.csv` üretir ne de bir mock veri üreticisi yazar: ölçek deneyi
(Adım 5) Mesa profillerine hiç bağlanmaz, kendi deterministik emir akışını kurar.

`TimeSeriesProfile`, `profile_source` / `profile_csv_path` ve CSV veri hattı Faz 2'de
(Erol) ve Faz 3 kapsamı dışındadır. Mock veriye geçiş bunları **ertelediği** için
aşağıdaki Kural 6 ve Risk R3 doğrudan bu karardan doğuyor.

**Değişmez kurallar**

1. **Protokol v2 dondurulmuştur.** `comm/protocol.py`'ye alan eklenmez, `PROTOCOL_VERSION`
   artırılmaz. Faz 1'de iki PR ile sabitlendi; Faz 3 yalnızca bu mesajları taşır.
   Yeni alan gerekiyorsa önce ekiple konuşulur, çünkü v2 testleri ve Faz 4 buna bağlı.
2. **Yalnızca PUB-SUB ve REQ-REP** (PSD 5.1). ROUTER/DEALER, PUB üzerinde XPUB abonelik
   tespiti ya da `zmq.asyncio` kullanılmaz; PSD deseni tanımlıyor ve jüriye karşı
   savunulan tasarım bu.
3. **Dokümante edilen klasör yapısına yeni dosya eklenmez** (Mimari 7). `comm/` altında
   yalnızca `protocol.py`, `node.py`, `network.py` olur; sayaç dataclass'ı `node.py`
   içinde durur, ayrı `stats.py` açılmaz.
4. **Tek Python süreci, tek iş parçacığı** (Mimari 3, PSD 5.3). Thread/async kullanmak
   "soketler Mesa adımını bloklarsa" B-planıdır (Mimari 4.3) ve ancak ölçüm bunu
   gösterirse devreye alınır.
5. **`comm/` + `config.py` + `experiments/` + `tests/` dışına çıkılmaz.** Adım 1–5
   bu zarfın içinde kalır; `model.py`, `agents/`, `market/` dosyalarına tek dokunuş
   Adım 6'daki `publish_status` kancasıdır. Faz 2 (Erol) ve Faz 4 hazırlığı (Yusuf)
   paralel yürüdüğü için bu çakışma disiplini planın bir parçası.
6. **Profil bir adımda yalnızca bir kez okunur** (Mimari 4.1). Sentetik profiller
   her `energy_at()` çağrısında paylaşılan `model.rng`'den yeni bir sayı çeker
   ([solar.py:48](microgrid/profiles/solar.py#L48),
   [consumption.py:56](microgrid/profiles/consumption.py#L56)); ikinci okuma hem
   farklı bir değer döner hem de ortak RNG akışını kaydırarak **ondan sonraki tüm
   ajanların** değerlerini değiştirir ve tekrarlanabilirliği bozar
   (`test_model.py::test_same_seed_reproducible`). Faz 3'ün mesaj gövdeleri bu yüzden
   **yalnızca ajanın adım içinde önbelleklediği alanlardan** doldurulur
   (`generation_kwh`, `consumption_kwh`, `residual_kwh`, `state`, `soc`), hiçbir
   koşulda `energy_at(step)` yeniden çağrılmaz. Mock veride bu kural canlı bir
   doğruluk şartı; CSV'ye geçildiğinde zararsız hale gelir ama kural kalır.

---

## 3. Çıkış kriterleri (Faz 3 "bitti" tanımı)

Hepsi ölçülebilir ve PSD 9.1'deki bir kritere bağlı. Faz 3 ancak bu tablonun tamamı
sağlandığında kapanır.

| # | Kriter | Hedef | Kaynak |
|---|---|---|---|
| C1 | Mesaj kaybı = beklenen − alınan | 3/10/30/50 düğümde **0** | PSD 9.1 metrik 2 |
| C2 | Zaman aşımı sayısı | **0** | PSD 9.1 metrik 2 |
| C3 | El sıkışmasından sonraki ilk mesajın kaybı | **0** (prototipte %100'dü) | Mimari 4.3 |
| C4 | Bloklanma | Hiçbir okuma zaman aşımsız değil; 50 düğümlü koşu kilitlenmez | Mimari 4.3 |
| C5 | Adım süresi p50 | N=10 ~5 ms, N=50 ~150 ms mertebesinde ve kayıtlı | Mimari 4.7 |
| C6 | `orders_at(step)` sırası | Tüm düğümlerde `(seq, agent_id)` ile birebir aynı | Mimari 4.4 (2) |
| C7 | Soket/port sızıntısı | Tüm `pytest` koşusunda sıfır; süit asılmadan biter | — |
| C8 | Testler | Mevcut 58 + yeni Faz 3 testleri yeşil | — |
| C9 | Taşınabilirlik | 3 düğümlü prototip üç ekip üyesinin makinesinde çalışır | Durum Raporu |
| C10 | Dokümantasyon | README + Mimari 4.7 gerçek rakamlarla güncel; Faz 4 API sözleşmesi yazılı | Durum Raporu |

C5 bir eşik değil, bir **kayıt** şartıdır: rakam hedeften saparsa sebebi (io_threads,
REQ churn, fd limiti) yazılır. Uydurma rakam raporlamaktansa ölçülen rakamı açıklamak
doğru olan; Durum Raporu'ndaki "doğrulanmamış rakamlar" riski tam olarak bu.

---

## 4. Altı alt adım

Sıra bağımlayıcıdır: her adım öncekinin testleri yeşilken başlar. Adım 1–2 altyapı,
3–4 asıl iletişim, 5 ölçüm, 6 teslim.

### Adım 1 — Soket yaşam döngüsü: `PeerNode` iskeleti, `PeerNetwork` kurulumu ve kapanışı

**Amaç.** N düğümlü tam mesh'i açıp hiçbir şey sızdırmadan kapatabilmek. Hiç mesaj
akışı yok; bu adımın tek konusu kaynak yönetimi, çünkü Faz 3'ün en sinsi hatası
(asılı kalan `ctx.term()`, tükenen dosya tanıtıcısı) buradan çıkar.

**Dosyalar.** `microgrid/comm/node.py` (yeni), `microgrid/comm/network.py` (yeni),
`microgrid/comm/__init__.py` (export), `microgrid/config.py` (`CommConfig`),
`tests/test_comm_node.py` (yeni).

**Yapılacaklar.**

1. `CommConfig` dataclass'ı `config.py`'ye eklenir ve `SimulationConfig`'e
   `comm: CommConfig = field(default_factory=CommConfig)` olarak bağlanır. Alanlar:

   | Alan | Varsayılan | Neden |
   |---|---|---|
   | `transport` | `"tcp"` | `"inproc"` fd limiti yetmezse yedek (Mimari 4.3) |
   | `bind_host` | `"127.0.0.1"` | yerel soketler (Mimari 3) |
   | `handshake_interval_ms` | `10` | Mimari 4.3'teki ~10 ms |
   | `handshake_timeout_s` | `5.0` | el sıkışması bitmezse hata |
   | `step_timeout_s` | `2.0` | Mimari 4.3'teki 1–2 sn |
   | `request_timeout_s` | `1.0` | REQ-REP turu |
   | `poll_interval_ms` | `5` | Poller dilimi |
   | `sndhwm` / `rcvhwm` | `100_000` | PUB'ın sessiz mesaj düşürmesini engeller (bkz. Risk R1) |
   | `io_threads` | `1` | N=50'de ölçüme göre artırılabilir |
   | `raise_fd_limit` | `True` | Windows'ta güvenli şekilde atlanır |

   `SimulationConfig`'e eklenen tek satır, dosya sonundaki alanlardan sonra gelir;
   Faz 2'nin ekleyeceği `profile_source` / `profile_csv_path` ile çakışmaması için
   ekip içinde "config.py'ye ekleme yalnızca dosya sonuna" kuralı konuşulur.

2. `PeerNode`: `node_id` (ajanın `unique_id`'si), paylaşılan `zmq.Context`, PUB + SUB +
   REP soketleri. PUB ve REP `bind_to_random_port` ile `tcp://127.0.0.1` üzerinde boş
   porta bağlanır (aynı anda çalışan iki model çakışmasın — Mimari 4.3); `inproc`
   modunda adres `inproc://node-<id>-pub` olur, başka hiçbir şey değişmez.
   Tüm soketlerde `LINGER=0`, `SNDHWM`/`RCVHWM` config'ten.
3. `PeerNode.close()` idempotent: soketleri kapatır, tekrar çağrılması hata vermez.
4. `PeerNetwork(node_ids, config)`: önce **tüm** PUB/REP'leri bind eder, **sonra**
   SUB'ları bağlar. Sıra zorunlu: `inproc` bind-öncesi-connect'i garanti etmez ve
   tcp'de de bind bitmeden connect etmek gereksiz yeniden deneme üretir.
5. SUB'lar `status` ve `order` topic'lerine abone olur. Topic eşleşmesi ZeroMQ'da
   **önek** eşleşmesidir: `status` aynı zamanda `status2`'yi de yakalar, bu yüzden
   topic isimleri tam bu ikisiyle sınırlı tutulur ve bu kısıt kodda yorum olarak yazılır.
6. İşletim sistemi sınırları: `resource.setrlimit(RLIMIT_NOFILE, ...)` ile soft limit
   hard limite (ya da 65536'ya) çekilir, `ctx.set(zmq.MAX_SOCKETS, ...)` yükseltilir.
   `import resource` Windows'ta yoktur → `try/except ImportError` ile atlanır ve
   uyarı basılır (Durum Raporu, "Ortam" maddesi).
7. `PeerNetwork.close()` + `__enter__`/`__exit__`: tüm düğümler kapanır, ardından
   `ctx.destroy(linger=0)`. `ctx.term()` açık soket varsa süresiz bloklar; pytest'in
   asılmasının bir numaralı sebebi bu, o yüzden kapanış sırası testle sabitlenir.
8. Hata tipleri: `CommError` (taban), `HandshakeTimeout`, `StepTimeout` — `node.py`
   içinde. `ProtocolError` `protocol.py`'de kalır, dokunulmaz.

**Testler (~6).** 3 düğümlü ağ kurulur ve kapanır; `close()` iki kez çağrılabilir;
`with PeerNetwork(...)` bloğundan çıkınca hiçbir soket açık kalmaz; endpoint'ler
birbirinden farklı; aynı anda iki `PeerNetwork` kurulabilir (port çakışması yok);
`transport="inproc"` ile aynı testler geçer.

**Bitti şartı.** `pytest` 58 + yeni testlerle yeşil ve **asılmadan** bitiyor; 30
düğümlü bir ağ kurulup kapanıyor (henüz mesaj yok).

**Tahmini süre.** 1–1,5 gün.

---

### Adım 2 — Hazır olma el sıkışması (slow joiner)

**Amaç.** PUB-SUB bağlantısı kurulduktan hemen sonra yayınlanan mesajlar kaybolur;
prototipte ilk mesajların %100'ü kayboldu (Mimari 4.3). Bu adım, "ilk gerçek mesaj
asla kaybolmaz" garantisini kurar. Sabit `sleep` kullanılmaz — sabit bekleme ya yetmez
(kayıp) ya da gereksiz yavaşlar, ikisi de ölçümü bozar.

**Dosyalar.** `comm/node.py`, `comm/network.py`, `tests/test_comm_network.py` (yeni).

**Yapılacaklar.**

1. Her düğüm `step = -1` olan bir STATUS yayınlar (`make_status_message` ile; protokol
   v2 bunu zaten destekliyor, yeni alan gerekmez). İçerik anlamsızdır, varlığı sinyal.
2. Her düğüm `seen_peers: set[int]` tutar; `step == -1` STATUS geldiğinde göndereni
   bu kümeye yazar ve mesajı adım kutularına **koymaz** (el sıkışma trafiği step
   kutularını kirletmemeli).
3. SUB kendi PUB'ını duymaz → düğüm kendi mesajını doğrudan kendi gelen kutusuna
   koyar (Mimari 4.3). Bariyer sayımı bu yüzden "peer'lerden N−1 + kendi 1" olarak
   tanımlanır ve bu tanım tek yerde, tek fonksiyonda durur.
4. `PeerNetwork.handshake()`: `handshake_interval_ms` periyoduyla yayın + `drain`
   döngüsü; **tüm** düğümlerde `seen_peers == diğer tüm düğümler` olana kadar sürer.
   Tek süreçte olduğumuz için ağ bunu global olarak görebilir; "herkes herkesten
   duydu" şartı budur ve tek yönlü bir kontrolden (yalnız kendi duyduğunu saymak)
   daha güçlüdür.
5. `handshake_timeout_s` aşılırsa `HandshakeTimeout` fırlatılır; mesajda hangi
   düğümün kimi duymadığı yazılır (50 düğümde hata ayıklamanın tek yolu bu).
6. El sıkışması bittikten sonra yolda kalmış fazladan `step = -1` STATUS'lar gelir;
   bunlar sessizce atılır, **kayıp ya da geç sayılmaz**. Aksi halde sayaçlar baştan
   kirlenir ve C1 kriteri hiç sağlanmaz.
7. Ölçüm: el sıkışma süresi (ms) ve tur sayısı sayaca yazılır — N arttıkça nasıl
   büyüdüğü Adım 5'in tablosunda raporlanır.

**Testler (~5).** 3 düğümde el sıkışması biter ve `seen_peers` tamdır; **regresyon
testi:** el sıkışmasından hemen sonra her düğüm bir ORDER yayınlar ve hepsi
istisnasız alınır (kayıp = 0; slow joiner kanıtı); bir düğüm hiç yayın yapmazsa
`HandshakeTimeout` kalkar ve mesaj eksik peer'i isimlendirir; el sıkışması
`drain`'den sonra artık `step=-1` mesajı adım kutularında yok; 10 düğümde el
sıkışması `handshake_timeout_s` içinde bitiyor.

**Bitti şartı.** C3 sağlanıyor: el sıkışması sonrası ilk mesaj kaybı 0, 3 ve 10
düğümde tekrarlanabilir şekilde.

**Tahmini süre.** 1 gün.

---

### Adım 3 — PUB-SUB mesajlaşma ve adım bariyeri

**Amaç.** Faz 3'ün kalbi. Her adımda her düğümün, o adımın tüm STATUS ve ORDER'larını
eksiksiz ve **aynı sırada** görmesi. Faz 4'ün kopya determinizmi (Mimari 4.4 madde 2)
tamamen buna dayanır.

**Dosyalar.** `comm/node.py`, `comm/network.py`, `tests/test_comm_network.py`.

**Yapılacaklar.**

1. Yayın API'si: `publish_status(msg)` ve `publish_order(msg)`, multipart
   `[topic_bytes, json_utf8]` (Mimari 4.3). Gövde `encode_message` ile üretilir;
   ZeroMQ'ya Python nesnesi verilmez.
2. **`seq` düğümde üretilmez, parametre olarak gelir.** `seq` o adımdaki *global*
   gönderim sırasıdır, yani Mesa aktivasyon sırası (Mimari 4.4 madde 1). Her düğüm
   kendi sayacından başlatırsa tüm emirler `seq = 0` olur ve sıralama `agent_id`'ye
   düşer: kopyalar hâlâ tutarlı kalır ama CDA'nın zaman önceliği sessizce yok olur
   (`test_cda.py::test_time_priority_earlier_ask_at_same_price` bunun testi). Sayacı
   Faz 4'te `ZmqMarket.submit` tutar; Faz 3 yalnızca taşır. Bu, planın en kolay
   gözden kaçan doğruluk tuzağı.
3. Gelen kutusu: `inbox[step] = {"status": dict[int, StatusMessage], "orders": list[OrderMessage]}`.
   `drain(timeout_ms)` `zmq.Poller` ile hazır soketleri okur, `decode_message` ile
   çözer ve kutulara dağıtır. Bozuk mesaj `ProtocolError` olarak sayılır, süreci
   düşürmez.
4. **İleri adım mesajları tamponlanır**, atılmaz: ZeroMQ teslimi asenkron olduğu için
   adım `t+1`'in mesajı adım `t`'nin bariyeri kapanmadan gelebilir. Atılırsa bir
   sonraki adımda kayıp olarak görünür ve C1 asla sağlanmaz.
5. **Bariyer** `await_step(step)`: önce peer'lerden `N − 1` STATUS beklenir; sonra
   `state != "IDLE"` olan her peer'den bir ORDER beklenir (STATUS'un durumu, bir
   ORDER geleceğinin taahhüdüdür — Mimari 4.3). `step_timeout_s` içinde eksik kalan
   mesaj **kayıp** sayılır ve adım "degraded" işaretlenir.
6. **Geç gelenler**: bariyeri kapanmış bir adımın mesajı sonradan gelirse `late`
   sayacına yazılır ve **o adımın kutusuna enjekte edilmez**. Enjekte edilirse her
   düğüm farklı bir emir kümesi görür ve kopya determinizmi (C6) kırılır. Bu,
   "güvenli taraf" kuralının (Mimari 4.4) iletişim katmanındaki karşılığı.
7. Beklenen mesaj sayımının dürüst tanımı: STATUS beklentisi her zaman `N − 1`;
   ORDER beklentisi **alınan** STATUS'lardan türetilir. Dolayısıyla bir STATUS
   kaybolursa, ona bağlı ORDER "kayıp" değil "bilinmiyor" durumuna düşer. Kayıpsız
   koşuda (hedef durum) bu belirsizlik hiç oluşmaz; `status_loss > 0` olan adım
   raporda ayrıca işaretlenir. Rapora "0 kayıp" yazarken bu tanımın arkasında durulur.
8. `orders_at(step)`: `(seq, agent_id)` sırasıyla sıralı liste döner — Faz 4'ün
   `CDAMarket` kopyasını besleyeceği tek giriş noktası. Varış sırası kullanılmaz.
9. `statuses_at(step)` ve adım sonunda kutu temizliği: bariyeri kapanan ve okunan
   adımlar bellekten düşer (192 adım × 50 düğüm birikmemeli).
10. Gecikme ölçümü: alınma anı − zarftaki `sent_at`. `sent_at` `time.time()` ile
    damgalandığı için (protokol v2, dondurulmuş) alıcı da `time.time()` kullanır;
    `monotonic` ile karıştırılırsa anlamsız sayı çıkar. Tek süreçte bu fark büyük
    ölçüde "döngüde sıra bekleme" süresidir, bu yüzden Adım 5'te **adım başına
    iletişim süresiyle birlikte** raporlanır (Mimari 4.3).

**Testler (~9).** 3 düğüm, 5 adım boyunca tüm STATUS ve ORDER'ları eksiksiz alır;
`orders_at` üç düğümde de birebir aynı listeyi verir (C6); IDLE peer için ORDER
beklenmez ve bariyer takılmaz; ORDER'ı STATUS'undan önce gelen peer doğru işlenir;
`t+1` mesajı `t`'nin bariyeri açıkken gelirse tamponlanır; eksik peer'de bariyer
`step_timeout_s` içinde döner ve kaybı sayar (asılmaz); bariyer sonrası gelen mesaj
`late` sayılır ve kutuya girmez; bozuk JSON sayacı artırır ama koşuyu düşürmez;
10 düğüm × 10 adım kayıpsız.

**Bitti şartı.** C1, C4, C6 3 ve 10 düğümde sağlanıyor; bariyer hiçbir senaryoda
asılmıyor.

**Tahmini süre.** 2–2,5 gün. (Fazın en riskli adımı; buffer buraya konur.)

---

### Adım 4 — REQ-REP istek/cevap primitifi (kilitlenmesiz)

**Amaç.** Faz 4'ün TRADE/NACK müzakeresini üzerine kuracağı taşıma primitifi. Faz 3
**semantiği değil mekanizmayı** verir: "şu peer'e şu gövdeyi gönder, cevabı ya da
zaman aşımını al" ve "kendine gelen istekleri şu handler ile cevapla".

**Dosyalar.** `comm/node.py`, `comm/network.py`, `tests/test_comm_reqrep.py` (yeni).

**Yapılacaklar.**

1. `request(peer_id, body: str, timeout_s) -> str | None`: istek başına açılıp
   kapanan REQ soketi, **yalnızca** hedefin REP'ine `connect` edilir. Tek REQ birden
   çok REP'e bağlanırsa ZeroMQ istekleri sırayla (round-robin) dağıtır ve hedef
   seçilemez — mesaj yanlış ajana gider (Mimari 4.3). Bu kısıt kodda yorumla sabitlenir.
   > **Uygulama notu (Adım 4, 23 Ekim 2026, ekip onaylı):** "istek başına açılıp
   > kapanan" kısmı madde 6'daki yedek planla değiştirildi (gerekçe madde 6'nın
   > notunda). "Yalnızca hedefin REP'ine connect" kısıtı aynen korunuyor. Ayrıca
   > `request` tek iş parçacığında bloklayarak cevap alamayacağı için
   > `send_request` + `collect_reply` olarak ikiye bölündü; `request` ikisinin
   > bileşimi olarak duruyor, `negotiation_rounds` parçaları kullanıyor.
2. Cevap `zmq.Poller` ile beklenir; zaman aşımında `None` döner, `timeouts` sayacı
   artar, soket `LINGER=0` ile kapatılır. **Bloklayan `REQ.recv` kullanılmaz:** tek
   iş parçacığında satıcının REP'ine hizmet edecek kimse kalmadığı için süreç
   kilitlenir (Mimari 4.3).
3. `serve_requests(handler, timeout_s) -> int`: REP'te bekleyen **tüm** istekleri
   `Poller` ile boşaltır, her biri için `handler(body) -> str` çağırır ve cevabı
   gönderir. Handler'ın fırlattığı istisna isteği cevapsız bırakmaz; hata cevabı
   döner (Faz 4 bunu NACK olarak dolduracak).
4. `PeerNetwork.negotiation_rounds(...)`: üç turlu sürüş (Mimari 4.4 madde 3) —
   (1) tüm isteyen düğümler `request` çağırır, (2) tüm hedefler `serve_requests`
   ile REP'lerini boşaltır, (3) tüm isteyenler cevaplarını toplar. Faz 3 bunu
   alıcı/satıcı kavramından bağımsız, "istek sahibi / hedef" olarak yazar; TRADE ve
   NACK kelimeleri Faz 4'te girer.
5. REQ-REP gidiş-dönüş süresi (RTT) ölçülür ve sayaca örnek olarak yazılır.
6. REQ churn maliyeti ölçülür: N=50'de çok sayıda istek-başına-soket açmak baskın
   maliyet olursa, yedek plan `(istek sahibi, hedef)` çiftine göre önbelleklenmiş
   kalıcı REQ soketleridir — bir REQ hâlâ bir REP'e bağlı kaldığı için tasarım
   kısıtı korunur. Yedek plan ancak ölçüm gösterirse uygulanır, önceden değil.
   > **Uygulama notu (Adım 4, 23 Ekim 2026, ekip onaylı):** Yedek plan devreye
   > alındı. Ölçülen baskın maliyet, öngörülen soket açma süresi değil, tcp port
   > tükenmesiydi. Soket açma/kapama ~25–30 µs/istek tuttu (tur süresinin
   > %12–31'i), yani tek başına baskın değildi. Ama istek başına REQ, istek başına
   > bir TCP bağlantısı demek ve kapanan her bağlantı bir geçici portu TIME_WAIT'te
   > tutuyor (macOS: 2×MSL = 30 s, 16384 port). Arka arkaya yapılan ölçüm
   > koşularında N=10'da zaman aşımı, N=50'de "Can't assign requested address"
   > görüldü. Koşul: macOS 26.6.2 arm64, Python 3.13.1, herkes herkese 20 tur, tek
   > seferlik oturum içi gözlem; repoda bu rakamları üreten bir betik yok, Adım 5'te
   > yeniden ölçülecek. Uygulama: hedef başına boştaki REQ havuzu. Cevap gelince
   > soket havuza döner; zaman aşımında kapatılır ve havuza alınmaz; aynı hedefe
   > eşzamanlı ikinci istek ek REQ açar. Adım 5'e etkisi: N=50'de her düğüm herkese
   > istek atarsa N(N−1) = 2450 kalıcı REQ bağlantısı oluşur ve bu fd/soket
   > bütçesine girer (`max_sockets_needed`).
   >
   > **Adım 5'te yeniden ölçüldü (25 Ekim 2026):** `experiments/comm_scale.py
   > --req-mode per-request` istek başına REQ'i yeniden üretir. Koşul: Darwin 25.6.0
   > arm64, Python 3.13.1, libzmq 4.3.5, tcp, `active_ratio = 1.0`, herkes herkese 5
   > tur, her hücre TIME_WAIT'te 0 bağlantıyla başlayan tek koşu. Soket açma +
   > bağlanma istek başına 0.03–0.06 ms (p50), yani baskın değil. İstek başına REQ
   > ağ açıkken her istek için bir TIME_WAIT bıraktı (N=10'da 450, N=50'de 12250;
   > havuzlu: 0). N=50'de bir tur istek başına 901 ms, havuzlu 283 ms sürdü
   > (`request_timeout_s` 1.0): havuzun asıl gerekçesi bu zaman aşımı payı. Zaman
   > aşımı ve "Can't assign requested address" tek, temiz başlatılmış koşuda
   > yeniden ÜRETİLMEDİ; o gözlem 30 sn içinde arka arkaya yapılan koşulardandı.
   > Havuzlu REQ'ler kapanışta açık çift başına yaklaşık bir TIME_WAIT bırakır.

**Testler (~6).** Echo handler ile tek istek-cevap; 3 düğümde herkes herkese istek
gönderir ve üç turda kilitlenmeden tamamlanır; REP'i hiç boşaltılmayan peer'e istek
`timeout_s` içinde `None` döner ve `timeouts` artar (süreç asılmaz); handler istisnası
hata cevabına dönüşür; `request` sonrası açık REQ soketi kalmaz (fd sayısı sabit);
aynı adımda bir düğüme gelen iki istek de cevaplanır.

**Bitti şartı.** C2 ve C4 sağlanıyor: 3 ve 10 düğümde tüm turlar zaman aşımı
olmadan tamamlanıyor, hiçbir senaryoda kilitlenme yok.

**Tahmini süre.** 1,5–2 gün.

---

### Adım 5 — Ölçüm ve ölçek deneyi (3 / 10 / 30 / 50 düğüm)

**Amaç.** Mimari 4.7'deki doğrulanmamış rakamları repoda yeniden üretmek — Durum
Raporu'nun "Dikkat edilecekler" bölümündeki açık borç bu.

**Dosyalar.** `comm/node.py` (`CommStats`), `comm/network.py` (toplama),
`experiments/comm_scale.py` (yeni), `tests/test_comm_scale.py` (yeni).

**Yapılacaklar.**

1. `CommStats` dataclass'ı (`node.py` içinde, ayrı dosya değil — Mimari 7):
   tip başına beklenen/alınan, `loss`, `timeouts`, `late`, `protocol_errors`,
   PUB-SUB gecikme örnekleri, REQ-REP RTT örnekleri, adım başına iletişim süresi,
   el sıkışma süresi. `PeerNetwork.stats()` düğümleri toplar ve p50/p95 hesaplar.
   Faz 5/6'daki `metrics.py` bu sayaçtan okuyacak (Mimari 4.6) — Faz 3 ham ölçümü
   verir, KPI biçimlendirmesini yapmaz.
2. `experiments/comm_scale.py`: Mesa'dan ve veri katmanından **bağımsız** bir koşum.
   Sentetik ve deterministik bir emir akışı (sabit seed'li `numpy.random.Generator`)
   üretir; `active_ratio` parametresi o adımda IDLE olmayan (yani ORDER yayınlayan)
   düğüm oranını belirler. Mesa, profiller ve CDA'ya bağlanmaz: Faz 4 henüz yok,
   gerçek veri de yok, ve iletişim ölçümü ikisini de beklememeli.
3. Koşu matrisi: `N ∈ {3, 10, 30, 50}` × `transport ∈ {tcp, inproc}` ×
   `active_ratio ∈ {0.5, 1.0}`, 192 adım (2 gün × 96 adım — Mimari 4.7'deki "2 günlük
   koşu"), her N için aynı emir akışı. Her kombinasyon için ayrıca bir REQ-REP
   müzakere turu sürülür.
   > **Uygulama notu (Adım 5, 24 Ekim 2026, ekip onaylı):** müzakere 1 değil 5 tur
   > sürülür (`--negotiation-rounds`, varsayılan 5). Tek turda her çift yalnızca bir
   > kez istek attığı için REQ havuzu hiç yeniden kullanılmıyor ve Adım 4 madde
   > 6'nın gerekçesi ölçülemiyordu. Sonuç: RTT 5 turun hepsini kapsar; ilki bağlantı
   > kurulumunu da içerdiğinden tek turluk ölçümden düşüktür. N=50 tcp oran 1.0,
   > `results/comm_scale.csv`: 5 tur RTT p50 179 ms (`baseline` satırı), tek tur
   > 451 ms (`rounds=1` satırı, `--negotiation-rounds 1`). Tek tur üç ayrı koşuda
   > 451–498 ms arasında ölçüldü.
4. **`active_ratio = 1.0` manşet rakamdır, çünkü üst sınırdır.** Adım başına mesaj
   sayısı = `N` STATUS + (IDLE olmayan düğüm sayısı) ORDER; `active_ratio = 1.0`
   bunun mümkün olan maksimumudur (her düğüm hem STATUS hem ORDER yayınlar). Mock
   veri bugün ne üretiyorsa, gerçek veri de bu tavanı aşamaz. Böylece ölçülen kayıp
   ve gecikme rakamları veri kaynağı değiştiğinde **geçerliliğini korur**; aksi halde
   gerçek veri geldiğinde tüm ölçek deneyinin tekrarlanması gerekirdi. `0.5` satırı
   tipik yükü, `1.0` satırı en kötü durumu gösterir.
5. Çıktı: `results/comm_scale.csv` (N, transport, `active_ratio`, adım sayısı,
   adım başına mesaj sayısı, kayıp, zaman aşımı, geç, gecikme p50/p95, RTT p50/p95,
   adım süresi p50/p95, el sıkışma süresi) ve `results/comm_scale.png` (N'e karşı
   adım süresi + kayıp). `results/` zaten `.gitignore`'da; **tablo README'ye ve
   Mimari 4.7'ye metin olarak işlenir**, yoksa rakamlar yine repoda görünmez ve aynı
   borç tekrarlanır.
6. Ölçek engelleri: tcp tam mesh'te açık dosya sayısı ~N² büyür; N=50'de birkaç bin
   fd gerekir (Mimari 4.3). Adım 1'in limit yükseltmesi yetmezse koşu `inproc://`
   ile tekrarlanır — yalnızca adres değişir, soketler aynı. Hangi N'de hangi
   taşımanın gerektiği CSV'ye yazılır; bu, "standart bir öğrenci dizüstü
   bilgisayarıyla sınırlı" kısıtının (PSD 5.3) somut cevabı.
7. p50 hedeften (N=10 ~5 ms, N=50 ~150 ms) belirgin saparsa sırayla denenir:
   `io_threads` artırmak, HWM ayarı, REQ önbelleği (Adım 4 yedek planı), `inproc`.
   Her denemenin etkisi CSV'ye ayrı satır olarak girer.

**Testler (~4).** Hızlı alt küme CI/`pytest` içinde kalır: N=3 ve N=10, ~20 adım,
`active_ratio = 1.0` (en kötü durum), kayıp = 0 ve zaman aşımı = 0 doğrulanır. 30 ve
50 düğümlü ağır koşular
`@pytest.mark.slow` ile işaretlenir ve varsayılan `pytest` bunları atlar
(`pytest -m slow` ile çalışır) — süit hızlı kalmalı, yoksa kimse çalıştırmaz.
Ek test: `CommStats` toplaması tek düğümün sayaçlarıyla tutarlı; p50/p95 hesabı
bilinen örnek üzerinde doğru.

**Bitti şartı.** C1, C2, C5 dört N değerinde ölçüldü ve `results/comm_scale.csv`
üretildi; 50 düğümlü koşu kilitlenmeden bitti.

**Tahmini süre.** 2 gün.

---

### Adım 6 — Doğrulama, `publish_status` kancası, dokümantasyon ve teslim

**Amaç.** Fazı kapatmak: platformlar arası doğrulama, bariyerin ön şartını koda
bağlamak, dokümanları gerçek rakamlarla güncellemek ve Faz 4'e yazılı bir API
sözleşmesi bırakmak.

**Dosyalar.** `microgrid/market/base.py` (+3 satır), `microgrid/agents/household.py`
(+1 satır), `tests/test_agents.py` (+2 test: her ajan her adım — IDLE dahil —
`publish_status` çağırıyor; kanca ortak RNG akışını kaydırmıyor, yani `cda` ve
`logging` sonuçları kanca öncesiyle birebir aynı), `README.md`,
`P2P Enerji Ticareti – Sistem Mimarisi.md` (4.7), `P2P Enerji Ticareti – Durum Raporu.md`.

**Yapılacaklar.**

1. **`publish_status` kancası** (Mimari 4.2, 4.4): `MarketInterface`'e varsayılan
   no-op `publish_status(agent)` eklenir (`cda` ve `logging` için boş), ve
   `HouseholdAgent.step()` sonunda **IDLE olsa bile** `self.model.market.publish_status(self)`
   çağrılır. Bu, bariyerin "her adım N−1 STATUS" ön şartının tek garantisi; Faz 4
   bunu `ZmqMarket`'te doldurur. Davranış değişmez (no-op), sonuçlar birebir aynı kalır.

   **Kanca ajanın önbelleğinden okur** (Kural 6, Risk R3): `publish_status(agent)`
   STATUS gövdesini `agent.generation_kwh`, `agent.consumption_kwh`,
   `agent.residual_kwh`, `agent.state.value`, `agent.soc` alanlarından doldurur.
   `agent.consumption_profile.energy_at(step)` ya da `agent.generation(step)`
   **çağrılmaz**: mock veride bu hem farklı bir değer döndürür hem de ortak RNG
   akışını kaydırır. Kanca `step()`'in sonunda çağrıldığı için raporlanan `soc`
   batarya planı uygulandıktan **sonraki** değerdir; bu bilinçli bir seçim ve Faz 6
   yanlış okumasın diye docstring'e yazılır (stratejinin kullandığı "adım başındaki
   SoC" ile aynı şey değil — Mimari 4.2).

   **Çakışma notu:** `comm/` zarfının dışındaki tek dokunuş budur ve `household.py`
   Faz 2'de (Erol) aktif. Veri hattı (CSV) mock veriye geçişle ertelendiği için
   Faz 2'nin kalan işi büyük ölçüde `strategy.py`'ye indi; yine de kanca 4 satır
   olduğu için Faz 2'nin strateji PR'ı ile sıralanır: ya o PR'dan sonra ya onunla
   aynı PR'da. Faz 3 bu adım yüzünden **beklemez** — gerekirse kanca en sona bırakılır.
2. Tam doğrulama: `pytest` (58 + yeni) yeşil ve asılmadan bitiyor; `pytest -m slow`
   ile 30/50 düğüm koşuları geçiyor; `python run.py` eskisiyle aynı özeti ve
   `results/summary.png`'i üretiyor; `solara run app.py` çalışıyor ve **hiç ZeroMQ
   soketi açmıyor** (Mimari 7: `app.py` yalnızca süreç içi backend'lerle çalışır).
3. Taşınabilirlik (C9): 3 düğümlü prototip Kutluay ve Yusuf'un makinelerinde de
   çalıştırılır. Windows'ta `resource` yok (Adım 1 madde 6) ve 30/50 düğümlü tcp
   koşusu beklenen şekilde `inproc`'a düşebilir; sonuç ne olursa olsun yazılır.
4. README güncellemesi: "Wire protokolü (Phase 3 zemini)" bölümü "Faz 3: iletişim
   katmanı"na çevrilir — `PeerNode`/`PeerNetwork` sorumlulukları, el sıkışması,
   bariyer kuralı, zaman aşımı varsayılanları, ölçülen ölçek tablosu, yeni test
   dosyaları. Faz durumu tablosunda Faz 3 "Tamamlandı" olur.
5. Mimari dokümanı 4.7 güncellenir: ön prototip rakamları **ölçülen** rakamlarla
   değiştirilir; hangi N'de hangi taşıma kullanıldığı ve hedeften sapma varsa sebebi
   yazılır. Durum Raporu'ndaki "Doğrulanmamış rakamlar" riski kapatılır.
   **Rakamlar etiketlenir:** "sentetik emir akışı, `active_ratio = 1.0` (adım başına
   maksimum mesaj), Mesa profillerinden bağımsız". Gerçek veri geldiğinde ne kimse
   bu rakamların geçersiz olduğunu sansın, ne de gerçek veriyle ölçülmüş sansın.
   Jüriye karşı da savunulabilir tek biçim bu.
6. **Faz 4 teslim sözleşmesi** (aşağıdaki Bölüm 5) README'ye ya da `comm/__init__.py`
   docstring'ine işlenir ki Yusuf `zmq_market.py`'yi yazarken API'yi tahmin etmesin.
7. `faz3` → `main` PR'ı: kapsam, çıkış kriterleri tablosu (C1–C10, ölçülen değerlerle),
   test sayısı, bilinen sınırlar.

**Bitti şartı.** C7–C10 sağlandı, PR `main`'e birleşti, Durum Raporu'nda Faz 3
"Bitti".

**Tahmini süre.** 1–1,5 gün.

---

## 5. Faz 4'e teslim sözleşmesi

Faz 4 (`market/zmq_market.py`, Yusuf) Faz 3'ten yalnızca şunları görür. Bu liste
Faz 3'ün API taahhüdüdür; dışında kalan her şey iç detaydır.

| Faz 4'ün ihtiyacı | Faz 3'ün verdiği | Faz 4'ün sorumluluğu |
|---|---|---|
| Emirleri herkese yaymak | `publish_order(msg)` — multipart `[order, json]` | `seq`'i aktivasyon sırasına göre `submit`'te atamak |
| Durumu herkese yaymak | `publish_status(msg)`; `MarketInterface.publish_status` kancası | Kancayı `ZmqMarket`'te doldurmak |
| Adımın tüm emirlerini aynı sırada görmek | `await_step(step)` + `orders_at(step)` → `(seq, agent_id)` sıralı | Listeyi kendi `CDAMarket` kopyasına vermek, adım sonunda kopyayı temizlemek |
| Karşı tarafla müzakere | `request(peer_id, body, timeout)`, `serve_requests(handler)`, `negotiation_rounds(...)` | Gövdeyi TRADE/NACK olarak doldurmak, kopyada eşleşmeyi doğrulamak |
| Kayıp/zaman aşımı/gecikme | `PeerNetwork.stats()` → `CommStats` | Onaylanan/eşleşen oranını ve settlement'ı raporlamak |
| Kaynak yönetimi | `PeerNetwork.close()`, context manager | `MicrogridModel.close()` yazıp buna bağlamak |

Faz 3'ün **bilerek yapmadıkları**: `cda_zmq` backend seçimi, kopya eşleştirmesi,
TRADE/NACK semantiği, `trades_at`, settlement ve grid fallback, `model.py` entegrasyonu.

---

## 6. Riskler ve tuzaklar

Sıralama olasılık × etki. R1–R4 planın içine çözümüyle gömülü; R5–R8 izlenir.

| # | Risk | Neden tehlikeli | Önlem |
|---|---|---|---|
| R1 | **PUB sessizce mesaj düşürür** | PUB kayıplı bir desendir: `SNDHWM` (varsayılan 1000 mesaj) dolunca mesajı hata vermeden atar. "Kayıp = 0" kriterini (C1) görünür sebep olmadan bozacak tek mekanizma bu ve iki dokümanda da yazılı değil. | HWM config'ten büyük ve **sonlu** verilir (sınırsız HWM belleği şişirir); kutular her adım boşaltılır; C1 testi her ölçek koşusunda kayıp = 0 doğrular. |
| R2 | **`seq`'in düğümde üretilmesi** | Kopyalar tutarlı kalır ama CDA'nın zaman önceliği sessizce kaybolur; test yeşil, sonuç yanlış. | `seq` parametre olarak alınır, düğümde sayaç tutulmaz (Adım 3 madde 2); kodda yorum + Faz 4 sözleşmesinde madde. |
| R3 | **Profilin adım içinde ikinci kez okunması** (mock veriye özgü) | Sentetik profiller her `energy_at()` çağrısında ortak `model.rng`'den çeker: ikinci okuma farklı değer döner **ve** akışı kaydırarak sonraki tüm ajanların değerlerini değiştirir. Belirti uzak bir yerde çıkar — `test_same_seed_reproducible` kırılır, STATUS ile piyasaya giden değer ayrışır — ve iletişim hatası gibi görünür. CSV'ye geçildiğinde zararsız hale gelir, yani bugün en tehlikeli. | Kural 6: mesaj gövdeleri yalnızca ajanın önbelleklediği alanlardan doldurulur; `publish_status` kancası `energy_at`/`generation` çağırmaz (Adım 6 madde 1); testi `tests/test_agents.py`'ye eklenen "kanca RNG akışını kaydırmaz" kontrolü. |
| R4 | **`ctx.term()` asılması** | Açık soket kalırsa süresiz bloklar; `pytest` hiç bitmez, CI'da teşhisi zor. | `LINGER=0` + idempotent `close()` + `ctx.destroy(linger=0)`; kapanış sırası Adım 1'de testle sabitlenir (C7). |
| R5 | **N=50'de fd / soket limiti** | Tam mesh'te fd ~N² büyür; macOS varsayılanı 256. Ölçek deneyi hiç başlamaz. | `RLIMIT_NOFILE` + `MAX_SOCKETS` yükseltme (Windows'ta guard'lı); yetmezse `inproc://` (yalnız adres değişir). |
| R6 | **Tek iş parçacığında REQ kilitlenmesi** | Bloklayan `REQ.recv` tüm süreci durdurur, çünkü REP'e hizmet edecek kimse kalmaz. | Her okuma `Poller` + zaman aşımı; üç turlu sürüş; "herkes herkese istek" testi (Adım 4). B-planı: ağ I/O'sunu ayrı olay döngüsüne taşımak (Mimari 4.3) — ancak ölçüm gerektirirse. |
| R7 | **Takvim / kritik yol** | Faz 4 Faz 3'ü bekliyor, ikisi de M2 (Kasım 2026) kapsamında. | Adım 3 en riskli adım, buffer oraya kondu; Adım 1–2 ve 5–6 kısa tutuldu; hedef bitiş 7 Kasım 2026 (bkz. Bölüm 7). |
| R8 | **Dosya çakışması** | `config.py` (Faz 2 ile) ve `household.py` (Faz 2 ile) paralel değişiyor. | Adım 1–5 `comm/` + `experiments/` + `tests/` zarfında kalır; `config.py` eklemeleri dosya sonuna; `household.py` dokunuşu Adım 6'ya ertelendi ve Faz 2 PR'ına göre sıralanır. Mock veriye geçişle Faz 2'nin CSV işi (ve `profile_source` / `profile_csv_path` alanları) ertelendiği için `config.py` çakışma riski düştü; `household.py` riski aynı kaldı. |

İki doküman arasında Faz 3 kapsamında çelişki bulunmadı; Durum Raporu'nun Faz 3
tanımı (`PeerNode`, `PeerNetwork`, el sıkışması, 3→10→30→50 ölçek) Mimari 4.3'ün
tamamına birebir oturuyor. Tek belirsizlik `publish_status` kancasının hangi fazda
eklendiğiydi (Mimari 4.4 onu "eklenecek" diye işaretliyor ama fazı söylemiyor);
bariyerin ön şartı olduğu için Faz 3'e, en az çakışmalı yere (Adım 6) kondu.

---

## 7. Takvim

Bugün 22 Ekim 2026; M2 Kasım 2026 ve kritik yol Faz 3. Toplam ~9–11 iş günü.
Faz 4'e en az üç hafta bırakmak için hedef bitiş **7 Kasım 2026**.

| Adım | Konu | Süre | Hedef |
|---|---|---|---|
| 0 | Ortam (venv 3.13, 58 test taban çizgisi) | ~1 saat | 23 Ekim |
| 1 | Soket yaşam döngüsü, `CommConfig` | 1–1,5 gün | 24 Ekim |
| 2 | El sıkışması | 1 gün | 27 Ekim |
| 3 | PUB-SUB + adım bariyeri | 2–2,5 gün | 30 Ekim |
| 4 | REQ-REP primitifi | 1,5–2 gün | 3 Kasım |
| 5 | Ölçüm + ölçek deneyi | 2 gün | 5 Kasım |
| 6 | Doğrulama, dokümantasyon, PR | 1–1,5 gün | 7 Kasım |

Paralel yürüyebilenler: Faz 2'nin kalanı (SoC stratejisi — Erol; CSV veri hattı mock
veriye geçişle ertelendi) ve Faz 4'ün hazırlığı (ödemenin backend'den
bağımsızlaştırılması, `trades_at` — Yusuf). Faz 4'ün `zmq_market.py` yazımı Adım 4'ün
bitişini bekler; Adım 5–6 ile paralel yürüyebilir.

---

## 8. Gerçek veri geldiğinde Faz 3'ün nesi tekrarlanır

Kısa cevap: **yapısal olarak hiçbir şey.** Bu bölüm, veri entegrasyonu PR'ı
geldiğinde kontrol listesi olarak kullanılır.

| Faz 3 çıktısı | Gerçek veriden etkilenir mi? | Gerekçe / yapılacak |
|---|---|---|
| `PeerNode`, `PeerNetwork`, el sıkışması, bariyer, REQ-REP | Hayır | Enerji değerlerini taşır, yorumlamaz. Tek bağımlılık adım başına mesaj **sayısı**; o da `N` ve IDLE oranıyla belirlenir. |
| Ölçülen kayıp / zaman aşımı (C1, C2) | Hayır | `active_ratio = 1.0` adım başına mesaj sayısının tavanı; gerçek veri bunu aşamaz. |
| Ölçülen gecikme / adım süresi (C5) | Hayır | Aynı tavan argümanı. Mesaj boyutu protokolün sabit alan kümesinden gelir (v2 dondurulmuş), veri kaynağından değil. |
| `comm_scale.py` koşusu | Yeniden ölçüm **gerekmez** | Yalnızca tek bir doğrulama koşusu: gerçek veriyle `cda_zmq` koşusunda kayıp hâlâ 0 mı. Tam matris tekrarlanmaz. |
| Kural 6 / Risk R3 (profil tek okuma) | Zararsız hale gelir | `TimeSeriesProfile` CSV'den okur, RNG çekmez. Kural yine de kalır: `cda` ve `logging` backend'leri sentetik profille koşmaya devam edecek. |
| Dokümandaki rakamların etiketi | Güncellenir | Adım 6 madde 5'teki etiket korunur, altına gerçek veriyle yapılan doğrulama koşusu eklenir. |

Faz 3 dışında kalan ama şimdiden bilinmesi gereken: **Faz 6'nın şebeke bağımlılığı
ve maliyet rakamları mock veriyle anlamlı değildir** (Mimari 8, metrik 5 — "baseline'a
göre azalma %"). Bunlar gerçek veri geldikten sonra yeniden koşulmalı. Faz 3'ün
iletişim rakamları bu kümenin dışındadır ve tekrar edilmesi gerekmez; final raporda
bu ayrım açıkça yazılmalı, yoksa "hangi sonuç hangi veriyle üretildi" sorusu jüride
cevapsız kalır.
