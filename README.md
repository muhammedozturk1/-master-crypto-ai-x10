# MASTER CRYPTO AI X10

1 yönetici robot ve 10 uzman bottan oluşan, Bybit üzerinde çalışan kripto işlem sistemi.
Varsayılan mod **PAPER** (Bybit demo hesabı). Canlı mod iki ayrı açık onay olmadan açılmaz.

> Bu yazılım test aşamasındadır ve kâr garantisi vermez. Kaldıraçlı işlemler
> bakiyenin tamamını kaybettirebilir. Test edilmemiş sistemi gerçek parayla çalıştırmayın.

## Durum

| Aşama | Konu | Durum |
| --- | --- | --- |
| 1 | Mimari, ayarlar, veritabanı, API iskeleti | Tamamlandı |
| 2 | Bybit veri bağlantısı ve coin tarayıcı | Tamamlandı |
| 3 | 10 botun stratejileri | Devam ediyor (BOT01, BOT02, BOT10 ve bot döngüsü hazır) |
| 4 | Ana robot karar motoru | Bekliyor |
| 5 | Risk ve sermaye | Bekliyor |
| 6 | Emir ve pozisyon yönetimi | Bekliyor |
| 7 | Kâr koruma kilidi | Bekliyor |
| 8 | Backtest ve paper trading | Bekliyor |
| 9 | Panel ve Telegram | Bekliyor |
| 10 | Testler ve güvenlik incelemesi | Bekliyor |
| 11 | Canlıya hazırlık | Bekliyor |

Strateji kodu yazılan botlar `/bots` ekranında `implemented: true` görünür. Şu an BOT01
(teknik analiz), BOT02 (trend ve momentum) ve BOT10 (NR7) sinyal üretiyor; diğerleri sırayla
eklenecek. Botlar işlem açmaz, sinyalleri
kaydeder; işlem kararı Aşama 4'te ana robota ait olacak. Borsaya henüz emir gönderilmez.

**Bot döngüsü:** 15 dakikalık ve 1 saatlik mum kapandıktan 8 saniye sonra, coin
tarayıcının son 30 adayı için kapanmış mumları çeker, veri kalitesini kontrol eder ve açık
botları çalıştırır. Aynı sinyal ikinci kez kaydedilmez.

**BOT10 – NR7:** Son mumun aralığı önceki 6 mumun her birinden küçükse NR7 kaydedilir ve
sonraki 3 kapanmış mum izlenir. Kırılım yalnızca kapanışla sayılır. Hacim önceki 20 mumun
ortalamasının altındaysa, fiyat kırılım seviyesinden 1 ATR'den fazla uzaklaşmışsa, stop
%4'ten genişse veya spread anormalse sinyal iptal edilir. Stop NR7'nin karşı ucunun 0,1 ATR
ötesinde; hedef, komisyon ve spread düşüldükten sonra 1:2 ödül/risk verecek şekilde
hesaplanır. EMA trendi, inside bar, Bollinger/ATR sıkışması, hacim gücü ve üst zaman dilimi
yönü güven puanını artırır ya da düşürür. Kırılımdan sonraki 3 mum içinde başarılı retest
ayrı sinyal üretir.

**BOT02 – Trend ve momentum:** Trend pullback (EMA20'ye düşük hacimli geri çekilme ve hacimli
dönüş, son higher low korunmuş, üst zaman dilimi uyumlu), Donchian breakout (önceki 20
kapanmış mumun kanalı, hacim ≥1,3x, MACD onayı, 1 ATR'den geç değil), Supertrend momentum
(yön değişimi + EMA50 eğimi + ADX ≥20 + RSI aşırı değil). BTC'ye göre göreceli güç yalnız
puan düzeltmesi yapar.

**BOT01 – Teknik analiz:** EMA+RSI trend devamı (EMA dizilimi, RSI 45-65, MACD histogramı
artıyor, EMA20'den tepki, hacim ≥1,2x), Bollinger mean reversion (yalnız ADX<20; bant
dışından içeri kapanış ve RSI dönüşü; hedef orta bant, net 1:2 vermiyorsa sinyal yok),
destek/direnç kırılımı (en az 2 temaslı seviye, hacim ≥1,5x, uzun fitil yok, hedefin
önünde başka seviye yok). Mum formasyonları tek başına sinyal üretmez, puanı düzeltir.

Tüm işlem sinyalleri ortak kurallardan geçer: stop doğru tarafta ve fiyatın en fazla
%4'ü uzaklıkta, komisyon ve spread sonrası ödül/risk en az 1:2.

Aşama 2 ile sistem Bybit'in herkese açık uçlarından gerçek fiyat, mum ve emir defteri
verisini okur (API anahtarı gerekmez). Coin tarayıcı 5 dakikada bir çalışır: işlem gören
USDT perpetual paritelerinden 24 saatlik hacmi 10 milyon USDT'nin altındakileri, spread'i
%0,10'un üstündekileri ve 30 günden yeni listelenenleri eler, en likit 30'u sıralar.
Mum uçları yalnızca kapanmış mumları döndürür ve veri kalitesini (boşluk, bayatlık,
OHLC tutarlılığı) kontrol eder.

## Seçilen çalışma biçimi

- Şartnamedeki 10 bot ve risk kuralları.
- Piyasa: Bybit USDT vadeli (linear), **demo hesap**.
- Kaldıraç varsayılan 2x, kodda kilitli tavan 3x. Spot seçilirse kaldıraç 1 olmak zorunda.

## Kilitli risk tavanları (`app/config.py`)

| Kural | Değer |
| --- | --- |
| İşlem başına risk | en fazla %0,5 (200 USD'de 1 USD) |
| Günlük zarar | en fazla %2 |
| Haftalık zarar | en fazla %5 |
| Azami düşüş | en fazla %10 |
| Eş zamanlı pozisyon | en fazla 2 |
| Tek pozisyon / toplam sermaye | %25 / %50 |
| Asgari ödül/risk | 1:2 |
| Kaldıraç | 1–3x |
| Martingale, kontrolsüz DCA | yasak |

`.env` ile bu değerler düşürülebilir ama aşılamaz; aşılırsa uygulama açılmaz.

## Yerelde çalıştırma

```bash
pip install -r requirements.txt
cp .env.example .env        # DASHBOARD_TOKEN'a uzun rastgele bir değer yazın
uvicorn app.main:app --reload
python -m pytest -q
```

## Render'a kurulum

Repo kökündeki `render.yaml` bir web servisi ve PostgreSQL veritabanı tanımlar.
Render'da **New > Blueprint** ile bu repoyu seçin. `BYBIT_API_KEY` ve `BYBIT_API_SECRET`
değerlerini Render panelinden girin; `DASHBOARD_TOKEN` otomatik üretilir.

Bybit API anahtarına **para çekme yetkisi vermeyin**.

## API uçları

| Uç | Açıklama |
| --- | --- |
| `GET /health` | Veritabanı, mod, işlem ve güvenli mod durumu |
| `GET /config` | Gizli bilgi içermeyen ayar özeti |
| `GET /bots` | 10 botun durumu |
| `PATCH /bots/{id}` | Botu aç/kapat, ağırlık (0–2). Risk botları kapatılamaz. Token ister |
| `POST /system/kill-switch` | Acil durdurma, güvenli mod. Token ister |
| `POST /system/resume` | `confirm: true` ile yeniden başlatma. Token ister |
| `GET /audit` | Ayar ve sistem olay kaydı |
| `GET /market/scan` | Son coin taraması: adaylar ve elenme nedenleri |
| `GET /market/candles/{symbol}?timeframe=15m&limit=100` | Kapanmış mumlar ve veri kalitesi |
| `GET /signals` | Son işlem sinyalleri (`include_wait=true` ile bekleme sinyalleri de) |
| `GET /nr7` | NR7 tarayıcı: tespit edilen pariteler, seviyeler, kırılım durumu |
| `GET /runner` | Son bot turlarının raporu |

Yazma uçları `X-Dashboard-Token` başlığı ister.
