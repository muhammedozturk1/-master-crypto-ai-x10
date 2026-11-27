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
| 3 | 10 botun stratejileri | Tamamlandı (sinyal botları; BOT07-09 Aşama 5-6'da) |
| 4 | Ana robot karar motoru | Tamamlandı |
| 5 | Risk ve sermaye | Tamamlandı |
| 6 | Emir ve pozisyon yönetimi | Tamamlandı |
| 7 | Kâr koruma kilidi | Tamamlandı |
| 8 | Backtest ve paper trading | Tamamlandı |
| 9 | Panel ve Telegram | Tamamlandı |
| 10 | Testler ve güvenlik incelemesi | Sırada |
| 11 | Canlıya hazırlık | Bekliyor |

Strateji kodu yazılan botlar `/bots` ekranında `implemented: true` görünür. Sinyal botlarının
hepsi çalışıyor: BOT01, BOT02, BOT03, BOT06, BOT10 işlem sinyali; BOT05 takvim vetosu ve
duyarlılık; BOT04 arbitraj gözlemi üretir. BOT07 (risk), BOT08 (portföy) ve BOT09 (emir)
karar ve uygulama katmanıdır; Aşama 5 ve 6'da yazılacak. Botlar işlem açmaz, sinyalleri
kaydeder; işlem kararı Aşama 4'te ana robota ait olacak. Borsaya henüz emir gönderilmez.

**Bot döngüsü:** 5 dakikalık, 15 dakikalık ve 1 saatlik mum kapandıktan 8 saniye sonra, o zaman
diliminde çalışan botlar için coin
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
artıyor, EMA20'den tepki, hacim ≥1,2x), footprint emilim/tükenme + hacim profili (Bollinger
dönüşünün yerine; değer alanı kenarı VAL/VAH test edilir, uçta agresif hacim emilir veya hacim
tükenir, kapanış değer alanı içinde → hedef POC; ADX≥25 trendde çalışmaz). Gerçek footprint
Bybit websocket işlem akışından seviye başına alış/satış hacmidir; mum tamamen dinlenmediyse ve
backtest'te mum yapısından tahmin kullanılır ve sinyalde yazılır,
destek/direnç kırılımı (en az 2 temaslı seviye, hacim ≥1,5x, uzun fitil yok, hedefin
önünde başka seviye yok). Mum formasyonları tek başına sinyal üretmez, puanı düzeltir.

**BOT06 – Emir akışı ve likidite:** Funding geçmişi, open interest, long/short hesap oranı,
son 1000 işlemden taker alış/satış payı (CVD) ve emir defteri derinliği. Stratejiler: likidite
süpürmesi (dip/tepe fitille aşılıp kapanışla geri dönülmesi), birikim (yatay fiyat + OBV/CVD
pozitif ayrışma), dağıtım (tersi; risk uyarısı). Kalabalık pozisyon ve OI/fiyat uyumu yalnız
puanı düzeltir. Açıklanamayan hacim patlaması veya anormal spread görülürse sinyal yerine
manipülasyon uyarısı kaydedilir. **Bağlı değil:** on-chain veriler (cüzdan transferleri,
borsa giriş/çıkışları, stablecoin hareketleri) için ücretsiz güvenilir kaynak yok; her BOT06
sinyalinde "on-chain veri yok" uyarısı bulunur.

**BOT03 – Scalping (5 dk):** EMA9/21 geri çekilme sonrası momentum, VWAP reclaim ve retest,
mikro kırılım. 15 dakikalık yön uyumu zorunlu, spread en fazla %0,05, beklenen hareket (ATR)
işlem maliyetinin en az 4 katı. Başarısız işlem sonrası bekleme, günlük scalping sınırı ve
"beklenen değer negatifse otomatik pasif" kuralları işlem sonucu gerektirdiği için ana robot
ve performans katmanında (Aşama 5 ve 8) uygulanacak.

**BOT05 – Takvim ve duyarlılık:** ForexFactory haftalık takvim akışından yüksek etkili USD
verilerinden 15 dk önce ve 15 dk sonra işlem vetosu; Fear & Greed endeksinden -100..+100
duyarlılık. **Bağlı değil:** haber akışı, sosyal medya, token unlock takvimi, borsa
listeleme/delist ve regülasyon haberleri.

**BOT04 – Arbitraj (gözlem):** Bybit spot içinde üçgen arbitraj (USDT→BTC/ETH→coin→USDT)
ve Bybit, Binance, OKX spot fiyat farkları, komisyon sonrası net. Transfer ücreti ve süresi
dahil değil. 200 USD kasada işlem yapılmaz, yalnız rapor.

Tüm işlem sinyalleri ortak kurallardan geçer: stop doğru tarafta ve fiyatın en fazla
%4'ü uzaklıkta, komisyon ve spread sonrası ödül/risk en az 1:2.

Aşama 2 ile sistem Bybit'in herkese açık uçlarından gerçek fiyat, mum ve emir defteri
verisini okur (API anahtarı gerekmez). Coin tarayıcı 5 dakikada bir çalışır: işlem gören
USDT perpetual paritelerinden 24 saatlik hacmi 10 milyon USDT'nin altındakileri, spread'i
%0,10'un üstündekileri ve 30 günden yeni listelenenleri eler, en likit 30'u sıralar.
Mum uçları yalnızca kapanmış mumları döndürür ve veri kalitesini (boşluk, bayatlık,
OHLC tutarlılığı) kontrol eder.

## Ana robot (Aşama 4)

Her bot turundan sonra yeni işlem sinyali olan her parite için karar verir:

1. **Piyasa rejimi** (10 rejim): haber belirsizliği, anormal hareket, düşük likidite, yüksek
   volatilite, volatilite sıkışması, güçlü/zayıf yükseliş, güçlü/zayıf düşüş, yatay. EMA
   20/50/200, ADX, ATR ve Bollinger genişliğinin 120 mumluk yüzdelik sırası, spread ve son mum
   hareketi kullanılır. ADX 25/20 eşikleri başlangıç değeridir, backtest ile doğrulanacak.
2. **Strateji ağırlığı:** rejime göre (ör. güçlü yükselişte trend ve kırılım 1,3; trende karşı
   0; yatayda mean reversion 1,3; haber, anormal hareket ve düşük likiditede yeni işlem yok).
3. **100 puanlık birleştirme:** teknik 20, trend 20, hacim/likidite 15, rejim uyumu 15, haber
   10, akış/on-chain 10, geçmiş performans 10. Aynı kategorideki benzer sinyaller toplanmaz, en
   güçlüsü alınır. Eksik veri güveni düşürür; on-chain ve performans geçmişi yokken nötr puan.
4. **Karar:** 0-49 red, 50-64 izleme, 65-74 ek doğrulama, 75-84 risk değerlendirmesi, 85+
   yüksek öncelik. İki yön eşit güçteyse çelişki nedeniyle red. Haber vetosu, acil durdurma
   veya güvenli modda işlem kararı çıkmaz.

Her karar gerekçesi, puan dağılımı, rejimi, destekleyen sinyaller ve işlem planıyla (giriş,
stop, hedef) kaydedilir. Reddedilen fırsatlar da kaydedilir. 75 üstü kararlar şimdilik
yalnız kayıt: risk katmanı ve emir katmanı devreye girmeden emir oluşmaz.

## Risk ve sermaye (Aşama 5)

Ana robotun 75 üstü her kararı **BOT07** zorunlu risk katmanından geçer; ana robot bu
kontrolleri aşamaz. Onaylanan karar `ONAYLANDI`, reddedilen `RISK_RED` olarak gerekçesiyle
kaydedilir.

- **Hesap düzeyi:** günlük zarar %2 → gün sonuna kadar yeni işlem yok; haftalık %5 → işlemler
  durur; zirveden %10 düşüş (gerçekleşmemiş zarar dahil) → güvenli mod, kullanıcı onayı
  olmadan yeniden başlamaz; art arda 3 zarar → 30 dk bekleme; 5 zarar → işlemler durur.
- **İşlem düzeyi:** en fazla 2 açık pozisyon, aynı paritede ikinci pozisyon yok, net ödül/risk
  en az 1:2.
- **Pozisyon büyüklüğü:** risk bütçesi = özkaynak × %1 (şartname %0,5; kullanıcı kararıyla 27.11'de %1) (yüksek volatilitede yarısı); miktar =
  bütçe / (stop mesafesi + birim maliyet). Teminat tek pozisyonda özkaynağın %25'i, toplamda
  %50'si; toplam açık risk ve borsa miktar adımı/minimumu ile sınırlanır. Minimumu karşılamak
  için risk limiti aşılmaz, işlem reddedilir.

**BOT08** tek ortak kasayı izler: özkaynak, kullanılabilir bakiye, teminat, gerçekleşmiş ve
gerçekleşmemiş net K/Z, komisyon, zirve özkaynak, düşüş, günlük/haftalık/aylık sonuç, art arda
kayıp. Kapanan işlemin net kârı kasaya eklenir (bileşik). 5 dakikada bir kasa anlık görüntüsü
alınır.

## Emir ve pozisyon yönetimi (Aşama 6, BOT09)

BOT07'nin onayladığı kararlar uygulanır. **Yürütme ortamı:** `BYBIT_API_KEY` ve
`BYBIT_API_SECRET` varsa Bybit demo hesabı (`BYBIT_DEMO=true`), yoksa gerçek Bybit
fiyatlarıyla simülasyon. Açılışta anahtar bakiye sorgusuyla doğrulanır; geçersizse emir katmanı
kapanır, simülasyona düşülmez. Canlı modda anahtar yoksa hiçbir şey çalışmaz. Pozisyon boyutu
borsadaki bakiyeye değil, sistemin kendi 200 USD kasasına göre hesaplanır.

Açılış: güncel fiyat, kayma (stop mesafesinin %25'i) ve net 1:2 yeniden kontrol → benzersiz
emir kimliği (`x10-<karar>-E`) gönderimden önce kaydedilir → piyasa emri, stop-loss (mark
fiyat) ve hedef emirle birlikte borsa tarafına eklenir → gerçekleşme sorgulanır → gerçek
giriş fiyatı kaydedilir → stop borsada doğrulanır, yoksa yeniden kurulur; kurulamazsa pozisyon
kapatılır ve güvenli mod.

Güvenlik: aynı karar için ikinci emir gönderilmez; zaman aşımında emir tekrar gönderilmez,
durumu sorgulanır; doğrulanamayan emir veya borsada kayıtsız pozisyon güvenli moda alır.

İzleme (15 sn): anlık fiyat ve net K/Z; borsada stop/hedefle kapanan pozisyonun net K/Z'si
kendi hesabımızla (çıkış−giriş farkı − açılış ve kapanış komisyonu) kaydedilir, Bybit'in
`closedPnl` değeri karşılaştırma için saklanır. Simülasyonda stop ve hedef 1 dakikalık mum
uçlarıyla tetiklenir; aynı mumda ikisi de görülürse önce stop, boşluklu açılışta açılış fiyatı.

## Kâr koruma kilidi (Aşama 7)

Her açık pozisyon için ayrı çalışır (kasa bazlı değil). Net kâr = brüt fiyat kazancı − giriş
komisyonu − tahmini çıkış komisyonu, pozisyon tutarına oranla. Durum makinesi: 0 başlangıç
stopu → 1 başabaş (zirve net ≥ %0,5) → 2 kademeli kilit (≥ %1) → 3 ATR trailing (≥ %2) → 4 güvenli
çıkış → 5 kapandı → 6 hata kurtarma.

| Zirve net | Korunan net |
| --- | --- |
| %0,5 | başabaş |
| %1 / %1,5 / %2 | %0,5 / %0,8 / %1,2 |
| %3 / %4 / %5 | %2 / %3 / %4 |
| %7 / %10 | %5,6 / %8,5 |
| %10 üstü | zirvenin %85'i |

ATR trailing (15 dk ATR14): scalping 1,25, NR7 1,75, teknik 2,0, trend 2,5 katsayı. Yeni stop
long için MAX(önceki, başlangıç, kilit, ATR), short için MIN; etkin olmayan bileşen dahil
edilmez, stop asla geri çekilmez ve başlangıç stopu genişletilmez. Fiyat kilit seviyesini
zaten aşmışsa geçersiz stop yerine pozisyon kapatılır. Bybit'te stop `trading-stop` ile tek
işlemde değiştirilir; hata olursa borsadaki eski stop korunur ve durum "hata kurtarma" olur.
Her değişiklik kaydedilir. Tablo ve katsayılar başlangıç değeridir, Aşama 8'de backtest ile
doğrulanacak.

## Backtest ve performans (Aşama 8)

Gerçek Bybit geçmiş mumlarıyla (sayfalı çekim) çok sembollü, zaman hizalı simülasyon.
Gelecek verisi sızıntısı yok: sinyal kapanmış mumla, giriş sonraki mumun açılışında; üst zaman
dilimi yalnız kapanmış mumlardan; kâr kilidi stopu bir sonraki mumdan geçerli. Komisyon, yarım
spread ve kayma her iki yönde. Aynı mumda stop ve hedef → önce stop; boşlukta açılış fiyatı.
Bir testte veri kesilince kesimden önceki işlemlerin değişmediği doğrulanır.

- **strateji modu:** her sinyal bağımsız işlem, sabit 1 USD risk → strateji ve bot karşılaştırması.
- **sistem modu:** ana robot (75+) + BOT07 + 200 USD bileşik kasa.
- Metrikler (19.3): net kâr, getiri, profit factor, kazanma oranı, ortalama kazanç/kayıp,
  beklenen değer (USD ve R), Sharpe, Sortino, azami düşüş, toparlanma faktörü, komisyon, süre,
  kâr kilidi verimliliği; strateji/bot/sembol kırılımı; %70/%30 örnek içi/dışı; 4 pencereli
  walk-forward; 500 turlu Monte Carlo düşüş dağılımı; kâr kilidi açık/kapalı karşılaştırması.
- Bot ağırlık önerisi (en az 30 işlem; yoksa 1,0) yalnız onayla uygulanır.
- Dahil değil: BOT06, BOT05, BOT04 (geçmiş veri yok), emir defteri derinliği, kısmi gerçekleşme.

Sistem hiç backtest çalıştırmamışsa açılıştan 2 dakika sonra bir doğrulama backtest'i
başlatır. Paper/canlı kapanan işlemler `/performance`'ta; scalping 20 işlemde negatif beklenen
değerdeyse otomatik pasife alınır.

## Panel ve bildirimler (Aşama 9)

**Panel:** servis adresinin kökü (`/`). Türkçe, karanlık temalı, mobil uyumlu, 10 saniyede bir
yenilenir: kasa, risk kullanımı ve engeller, ana robot ve 24 saatlik karar dağılımı, pozisyonlar
(kâr kilidi durumu, elle kapatma), kararlar ve gerekçeleri, botlar (aç/kapat), NR7 tarayıcı,
ekonomik takvim, demo performansı, backtest başlatma ve ağırlık uygulama; acil durdurma ve
yeniden başlatma.

**Kimlik doğrulama:** `DASHBOARD_TOKEN` ayarlıysa panel sayfası, `/health`, giriş ve API belgesi
dışındaki tüm uçlar anahtar ister (`X-Dashboard-Token` başlığı veya panel girişindeki çerez:
HttpOnly, Secure, SameSite=Strict, 7 gün). Panel, borsa ve haber verisini HTML olarak işlemez.

**Telegram:** `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` Render Environment sayfasına
girilirse açılır: sistem başlatma, işlem açılış/kapanış, kâr kilidi yükselmesi, kâr kilidi
çıkışı, güvenli mod, risk durdurma, acil durdurma, emir hataları, scalping pasif, günlük rapor.
Bildirim hatası işlemleri etkilemez.

## Seçilen çalışma biçimi

- Şartnamedeki 10 bot ve risk kuralları.
- Piyasa: Bybit USDT vadeli (linear), **demo hesap**.
- Kaldıraç varsayılan ve tavan 10x (şartname 2x/3x; kullanıcı kararıyla 27.11, demo testi). Risk stopla
  sınırlıdır; BOT07 stop mesafesi tasfiye mesafesinin yarısını aşan işlemi reddeder. Spot seçilirse
  kaldıraç 1 olmak zorunda.

## Kilitli risk tavanları (`app/config.py`)

| Kural | Değer |
| --- | --- |
| İşlem başına risk | en fazla %1 (200 USD'de 2 USD; şartname %0,5, kullanıcı kararıyla 27.11) |
| Günlük zarar | en fazla %2 |
| Haftalık zarar | en fazla %5 |
| Azami düşüş | en fazla %10 |
| Eş zamanlı pozisyon | en fazla 2 |
| Tek pozisyon / toplam sermaye | %25 / %50 |
| Asgari ödül/risk | 1:2 |
| Kaldıraç | 1–10x (şartname 3x; kullanıcı kararı 27.11) |
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
| `GET /` | Panel |
| `POST /login`, `POST /logout` | Panel girişi (çerez) |
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
| `GET /calendar` | Takvim vetosu, yaklaşan yüksek etkili veriler, Fear & Greed |
| `GET /arbitrage` | Arbitraj gözlem raporu |
| `GET /decisions` | Ana robot kararları (`outcome=REDDEDILDI` gibi filtrelerle) |
| `GET /portfolio` | BOT08 kasa durumu |
| `GET /risk` | BOT07 limitleri, kullanım ve yeni işlemi engelleyen nedenler |
| `GET /positions` | Açık ve kapanmış pozisyonlar (`status=ACIK`) |
| `GET /positions/{id}/protection` | Kâr kilidi ve stop değişiklik geçmişi |
| `GET /orders` | Emir kayıtları |
| `POST /positions/{id}/close` | Elle kapatma, `confirm: true` ve token ister |
| `POST /backtest` | Backtest başlat (token): timeframe, days, symbols, mode, compare_profit_lock |
| `GET /backtest`, `GET /backtest/{id}` | Backtest listesi ve tam rapor |
| `POST /backtest/{id}/apply-weights` | Önerilen bot ağırlıklarını onayla uygula (token) |
| `GET /performance` | Paper/canlı işlem performansı |
| `GET /master` | Ana robot özeti: son karar, 24 saatlik dağılım, veto ve bot ağırlıkları |

Yazma uçları `X-Dashboard-Token` başlığı ister.

### Giriş emri: limit (maker)
BOT09 girişleri varsayılan olarak plan fiyatına **PostOnly limit emir** ile yapar (maker
komisyonu %0,02; piyasa emrinde taker %0,055). Fiyat kaçtıysa emir kovalanmaz, plan fiyatında
bekler: 5m kararlarda 2 dk, 15m'de 5 dk, 1h'de 15 dk. Dolmazsa iptal edilir; kısmen dolduysa o
kadarı pozisyon olur. Stop ve hedef emre bağlıdır, dolduğu anda borsada geçerlidir. Bekleyen emir
pozisyon sınırına sayılır; acil durdurmada iptal edilir; sunucu yeniden başlarsa açılışta
kurtarılır. Backtest hâlâ sonraki mumun açılışından taker girişi varsayar (muhafazakâr).
`ENTRY_ORDER_TYPE=market` ile eski davranışa dönülür.
