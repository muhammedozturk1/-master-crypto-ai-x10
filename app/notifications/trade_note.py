"""İşlem notu: robotun bir işlemi NEDEN aldığını kısa, sade Türkçe ile anlatır.

Kaynaklar yalnızca kayıtlı verilerdir (karar, piyasa durumu, destekleyen botların sinyalleri,
risk planı); not sonradan uydurulmaz. Teknik terim yerine günlük dil kullanılır.
Telegram bildirimine ve panele gider.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Decision, SignalRecord

REGIMES = {
    "GUCLU_YUKSELIS": "Piyasa güçlü bir yükseliş trendinde.",
    "ZAYIF_YUKSELIS": "Piyasa hafif yükseliş eğiliminde.",
    "GUCLU_DUSUS": "Piyasa güçlü bir düşüş trendinde.",
    "ZAYIF_DUSUS": "Piyasa hafif düşüş eğiliminde.",
    "YATAY": "Piyasa yatay seyrediyor, belirgin bir yön yok.",
    "VOLATILITE_SIKISMASI": "Fiyat dar bir alanda sıkışmış; sert bir hareket bekleniyor.",
    "YUKSEK_VOLATILITE": "Piyasa çok hareketli; risk yarıya indirildi.",
    "DUSUK_LIKIDITE": "Piyasada işlem az.",
}
BOT_NAMES = {"BOT01": "Teknik analiz botu", "BOT02": "Trend botu", "BOT03": "Kısa vade botu",
             "BOT05": "Haber botu", "BOT06": "Para akışı botu", "BOT10": "Sıkışma botu"}
# strateji → (alış için, satış için) sade açıklama
PLAIN = {
    "ema_rsi_trend": ("yükseliş trendi güç kazanıyor", "düşüş trendi güç kazanıyor"),
    "footprint_absorption": ("fiyat yoğun işlem bölgesinin alt kenarından döndü, satıcılar tükendi",
                             "fiyat yoğun işlem bölgesinin üst kenarından döndü, alıcılar tükendi"),
    "sr_breakout": ("önemli bir direnç seviyesi yukarı kırıldı", "önemli bir destek seviyesi aşağı kırıldı"),
    "trend_pullback": ("yükselen trendde kısa bir geri çekilmeden sonra fiyat yeniden yukarı döndü",
                       "düşen trendde kısa bir tepkiden sonra fiyat yeniden aşağı döndü"),
    "donchian_breakout": ("fiyat son dönemin en yüksek seviyesini aştı", "fiyat son dönemin en düşük seviyesini kırdı"),
    "supertrend_momentum": ("trend yukarı döndü ve hareket hızlanıyor", "trend aşağı döndü ve hareket hızlanıyor"),
    "ema9_21_scalp": ("kısa vadeli ortalamalar yukarı kesişti", "kısa vadeli ortalamalar aşağı kesişti"),
    "vwap_reclaim": ("fiyat günün ortalama işlem fiyatının üstüne geri çıktı",
                     "fiyat günün ortalama işlem fiyatının altına geri indi"),
    "micro_breakout": ("kısa süreli sıkışma yukarı kırıldı", "kısa süreli sıkışma aşağı kırıldı"),
    "liquidity_sweep": ("fiyat aşağıdaki stopları süpürüp hızla geri döndü",
                        "fiyat yukarıdaki stopları süpürüp hızla geri döndü"),
    "accumulation": ("büyük alıcıların sessizce mal topladığı görülüyor", "büyük alıcıların mal topladığı görülüyor"),
    "distribution": ("büyük satıcıların mal dağıttığı görülüyor", "büyük satıcıların elindekini dağıttığı görülüyor"),
    "nr7_breakout": ("fiyat son günlerin en dar aralığından yukarı kırıldı",
                     "fiyat son günlerin en dar aralığından aşağı kırıldı"),
    "nr7_retest": ("dar aralık kırılımından sonra fiyat geri test edip yukarı yönü doğruladı",
                   "dar aralık kırılımından sonra fiyat geri test edip aşağı yönü doğruladı"),
}


def _d(x) -> Decimal | None:
    try:
        return Decimal(str(x)) if x not in (None, "") else None
    except InvalidOperation:
        return None


def tr_num(x: Decimal | float | None, digits: int | None = None) -> str:
    """Türkçe sayı biçimi: binlik nokta, ondalık virgül."""
    if x is None:
        return "—"
    x = Decimal(str(x))
    if digits is None:
        s = f"{x:.6g}" if abs(x) < 1000 else f"{x:,.1f}"
        if "e" in s.lower():
            s = f"{x:.8f}".rstrip("0").rstrip(".")
        elif abs(x) < 1000 and "." in s:
            s = s.rstrip("0").rstrip(".")
    else:
        s = f"{x:,.{digits}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _coin(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _why(sig: SignalRecord, long: bool) -> str:
    base = sig.strategy_id.split("+")[0]
    tags = sig.strategy_id.split("+")[1:]
    plain = PLAIN.get(base)
    text = plain[0 if long else 1] if plain else ((sig.payload or {}).get("reason") or base)
    det = (sig.payload or {}).get("details") or {}
    extra = []
    vr = det.get("volume_ratio")
    if isinstance(vr, (int, float)) and vr >= 1.2:
        extra.append(f"işlem hacmi normalin {tr_num(round(vr, 1))} katı")
    htf = det.get("htf_trend")
    if htf in ("UP", "DOWN") and (htf == "UP") == long:
        extra.append("büyük zaman diliminde de yön aynı")
    if det.get("absorption") and det.get("exhaustion"):
        extra.append("hem karşı taraf emildi hem hareket tükendi")
    if "squeeze" in tags:
        extra.append("öncesinde fiyat uzun süre sıkışmıştı")
    return text + ("; " + ", ".join(extra) if extra else "")


def build_note(session: Session, decision_id: int) -> str | None:
    d = session.get(Decision, decision_id)
    if d is None:
        return None
    c = d.contributions or {}
    plan = d.plan or {}
    long = d.direction == "LONG"
    coin = _coin(d.symbol)
    lines = [f"📌 Neden alındı: {coin} için {'ALIŞ (yükseliş beklentisi)' if long else 'SATIŞ (düşüş beklentisi)'}"]
    if d.market_regime in REGIMES:
        lines.append(REGIMES[d.market_regime])

    sigs = list(session.scalars(select(SignalRecord).where(SignalRecord.decision_id == decision_id)))
    want = "AL" if long else "SAT"
    for s in sorted((s for s in sigs if s.action == want), key=lambda x: -x.confidence)[:3]:
        lines.append(f"• {BOT_NAMES.get(s.bot_id, s.bot_id)}: {_why(s, long)}.")
    against = [s for s in sigs if s.action not in (want, "BEKLE")]
    if against:
        lines.append(f"• {len(against)} bot ters yönde sinyal verdi, ama ağır basmadı.")
    lines.append(f"Robotun güveni: {d.score}/100.")

    entry = _d(plan.get("entry_high") if long else plan.get("entry_low"))
    stop, tp = _d(plan.get("stop_loss")), _d(plan.get("take_profit"))
    if entry and stop and tp:
        sp = abs(stop - entry) / entry * 100
        tpp = abs(tp - entry) / entry * 100
        rr = _d(plan.get("expected_net_rr"))
        lines.append(f"Plan: {tr_num(entry)} seviyesinden giriş. Zarar durdur {tr_num(stop)} "
                     f"(%{tr_num(sp, 2)} {'aşağıda' if long else 'yukarıda'}), hedef {tr_num(tp)} "
                     f"(%{tr_num(tpp, 2)} {'yukarıda' if long else 'aşağıda'}).")
        if rr:
            lines.append(f"Hedefe ulaşırsa kazanç, riske edilen tutarın yaklaşık {tr_num(rr, 1)} katı olur.")
    risk = c.get("risk") or {}
    r_usd = _d(risk.get("risk_usd") or plan.get("risk_usd"))
    notional = _d(risk.get("notional") or plan.get("notional"))
    lev = risk.get("leverage") or plan.get("leverage")
    if r_usd is not None:
        txt = f"Riske edilen: {tr_num(r_usd, 2)} $"
        if notional:
            txt += f" · pozisyon büyüklüğü {tr_num(notional, 2)} $" + (f" ({lev}x kaldıraç)" if lev else "")
        lines.append(txt + ".")
    return "\n".join(lines)
