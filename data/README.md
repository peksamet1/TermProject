# Veri

Simülasyonun ortak profil formatı (her satır = bir hane, bir 15 dakikalık adım):

```
timestamp,household_id,pv_kw,load_kw
2012-12-30 00:00:00,H01,0.0,0.21
```

`pv_kw` ve `load_kw` o 15 dakikanın ortalama gücüdür (kW). Okuyucu:
`microgrid/profiles/timeseries.py`.

| Dosya | Ne | Git'e girer mi |
|---|---|---|
| `sample/profiles_sample.csv` | 4 hane, 2 gün, sentetik (testler ve deneme için) | Evet |
| `raw/` | İndirilen ham veri | Hayır (`.gitignore`) |
| `profiles_*.csv` | Üretilen büyük profil dosyaları | Hayır (`.gitignore`) |

## 1. Tüketim: Ausgrid Solar Home Electricity Data

- Sayfa: https://www.ausgrid.com.au/Industry/Our-Research/Data-to-share/Solar-home-electricity-data
- İndir: "Solar home half-hour data - 1 July 2012 to 30 June 2013" (zip).
- Zip'i aç, içindeki CSV'yi `data/raw/ausgrid/` içine koy.
- Lisans: CC BY 4.0 (raporda kaynak gösterilmeli).
- Biçim: başta bir başlık satırı, sonra `Customer, Generator Capacity, Postcode,
  Consumption Category, date, 0:30 ... 0:00, Row Quality`. Değerler yarım
  saatlik kWh. Tüketim = `GC` + `CL`; `GG` (Avustralya PV'si) kullanılmaz.

## 2. Güneş: PVGIS (İstanbul)

Kolay yol: `python scripts/fetch_pvgis.py` (dosya `data/raw/pvgis/istanbul_2019.json`).

Script çalışmazsa site üzerinden:

1. https://re.jrc.ec.europa.eu/pvg_tools/en/ aç, "HOURLY DATA" sekmesi.
2. Konum: 41.01, 28.98 (İstanbul). Veritabanı PVGIS-SARAH3, yıl 2019 – 2019.
3. "PV power" işaretle: installed peak power **1** kWp, system loss 14 %,
   slope 30°, azimuth 0°.
4. CSV indir, `data/raw/pvgis/istanbul_2019.csv` olarak kaydet.

## 3. Profil dosyasını üret ve çalıştır

```
python scripts/build_profiles.py --ausgrid "data/raw/ausgrid/<dosya>.csv" --pvgis data/raw/pvgis/istanbul_2019.json
python run.py --profiles data/profiles_istanbul.csv --start 2013-01-15 --days 7
```

Dönüşüm (`microgrid/profiles/real_data.py`): yarım saat → iki eşit 15 dk,
ortalama hane ~2.400 kWh/yıl olacak şekilde tek katsayıyla ölçek, tarihler
182 gün kaydırılır (Avustralya kışı → İstanbul yazı), PV = PVGIS kW/kWp ×
hanenin kWp'si (5 kW ±%20). Script sonunda günlük ortalama tüketim ve PV'yi
yazar; tüketim ~6.6 kWh/gün çıkmalı.

## Doğrulama (isteğe bağlı): OPSD Household Data

https://data.open-power-system-data.org/household_data/ — Almanya'dan 15 dakikalık
gerçek hane verisi (`household_data_15min_singleindex.csv`, CC BY 4.0). Ana
hatta kullanılmaz; sonuçların gerçekçi olduğunu karşılaştırmak için.
