# MASTER CRYPTO AI X10

1 yönetici robot ve 10 uzman bottan oluşan, Bybit üzerinde çalışan kripto işlem sistemi.
Varsayılan mod **PAPER** (Bybit demo hesabı). Canlı mod iki ayrı açık onay olmadan açılmaz.

> Bu yazılım test aşamasındadır ve kâr garantisi vermez. Kaldıraçlı işlemler
> bakiyenin tamamını kaybettirebilir. Test edilmemiş sistemi gerçek parayla çalıştırmayın.

## Durum

| Aşama | Konu | Durum |
| --- | --- | --- |
| 1 | Mimari, ayarlar, veritabanı, API iskeleti | Tamamlandı |
| 2 | Bybit veri bağlantısı ve coin tarayıcı | Sırada |
| 3 | 10 botun stratejileri | Bekliyor |
| 4 | Ana robot karar motoru | Bekliyor |
| 5 | Risk ve sermaye | Bekliyor |
| 6 | Emir ve pozisyon yönetimi | Bekliyor |
| 7 | Kâr koruma kilidi | Bekliyor |
| 8 | Backtest ve paper trading | Bekliyor |
| 9 | Panel ve Telegram | Bekliyor |
| 10 | Testler ve güvenlik incelemesi | Bekliyor |
| 11 | Canlıya hazırlık | Bekliyor |

Şu an botlar kayıtlı ama strateji kodu yok: `/bots` her birini `implemented: false` gösterir
ve hiçbir bot sinyal üretmez. Borsaya henüz emir gönderilmez.

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

Yazma uçları `X-Dashboard-Token` başlığı ister.
