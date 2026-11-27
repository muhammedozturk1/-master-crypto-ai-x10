"""İşlem notu: robotun bir işlemi NEDEN aldığını okunur Türkçe metinle anlatır.

Kaynaklar yalnızca kayıtlı verilerdir (karar, puan dağılımı, piyasa rejimi, destekleyen botların
sinyal gerekçeleri, risk planı); not sonradan uydurulmaz. Telegram bildirimine ve panele gider.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Decision, SignalRecord

REGIMES = {
    "GUCLU_YUKSELIS": "güçlü yükseliş", "ZAYIF_YUKSELIS": "zayıf yükseliş", "GUCLU_DUSUS": "güçlü düşüş",
    "ZAYIF_DUSUS": "zayıf düşüş", "YATAY": "yatay piyasa", "VOLATILITE_SIKISMASI": "volatilite sıkışması",
    "YUKSEK_VOLATILITE": "yüksek volatilite", "DUSUK_LIKIDITE": "düşük likidite", "HABER": "haber dönemi",
    "BELIRSIZ": "belirsiz",
}
CAPS = {"teknik": ("teknik", 20), "trend": ("trend", 20), "hacim_likidite": ("hacim", 15),
        "rejim_uyumu": ("rejim", 15), "haber": ("haber", 10), "akis": ("akış", 10), "performans": ("performans", 10)}
BOTS = {"BOT01": "Teknik", "BOT02": "Trend", "BOT03": "Scalping", "BOT04": "Arbitraj", "BOT05": "Haber",
        "BOT06": "Emir akışı", "BOT10": "NR7"}


def _d(x) -> Decimal | None:
    try:
        return Decimal(str(x)) if x not in (None, "") else None
    except InvalidOperation:
        return None


def _px(x: Decimal | None) -> str:
    if x is None:
        return "—"
    return f"{x:.6g}" if abs(x) < 1000 else f"{x:,.1f}"


def build_note(session: Session, decision_id: int) -> str | None:
    d = session.get(Decision, decision_id)
    if d is None:
        return None
    c = d.contributions or {}
    plan = d.plan or {}
    long = d.direction == "LONG"
    lines = [f"Neden alındı — {d.symbol} {d.direction or ''} ({d.timeframe or '?'})"]

    rej = c.get("rejim") or {}
    side_score = c.get("AL" if long else "SAT")
    other = c.get("SAT" if long else "AL")
    head = f"• Ana robot puanı {d.score}/100"
    if side_score is not None and other is not None:
        head += f" ({'alış' if long else 'satış'} {side_score:.0f} – karşı yön {other:.0f})"
    regime = REGIMES.get(d.market_regime or "", (d.market_regime or "").lower())
    if regime:
        head += f"; piyasa: {regime}"
        if rej.get("adx") is not None:
            head += f" (ADX {rej['adx']:.0f})"
    lines.append(head)

    sigs = list(session.scalars(select(SignalRecord).where(SignalRecord.decision_id == decision_id)))
    want = "AL" if long else "SAT"
    for s in sorted(sigs, key=lambda x: -x.confidence):
        if s.action != want:
            continue
        reason = (s.payload or {}).get("reason") or ""
        name = BOTS.get(s.bot_id, s.bot_id)
        lines.append(f"• {s.bot_id} {name} — {s.strategy_id.split('+')[0]} (güven {s.confidence}): {reason}"[:400])
    against = [s for s in sigs if s.action not in (want, "BEKLE")]
    if against:
        lines.append("• Ters yönde: " + ", ".join(f"{s.bot_id} ({s.confidence})" for s in against))

    pts = c.get("puanlar") or {}
    if pts:
        parts = [f"{CAPS[k][0]} {float(v):.0f}/{CAPS[k][1]}" for k, v in pts.items() if k in CAPS]
        lines.append("• Puan dağılımı: " + ", ".join(parts))

    entry = _d(plan.get("entry_high") if long else plan.get("entry_low"))
    stop, tp = _d(plan.get("stop_loss")), _d(plan.get("take_profit"))
    if entry and stop and tp:
        sp = (stop - entry) / entry * 100
        tpp = (tp - entry) / entry * 100
        lines.append(f"• Plan: giriş {_px(entry)}, stop {_px(stop)} ({sp:+.2f}%), hedef {_px(tp)} ({tpp:+.2f}%), "
                     f"net ödül/risk {plan.get('expected_net_rr', '—')}")
    risk = c.get("risk") or {}
    r_usd, budget = _d(risk.get("risk_usd") or plan.get("risk_usd")), _d(risk.get("risk_budget"))
    notional = _d(risk.get("notional") or plan.get("notional"))
    if r_usd is not None:
        txt = f"• Risk: {r_usd:.2f} $"
        if budget:
            txt += f" (bütçe {budget:.2f} $)"
        if notional:
            txt += f", pozisyon {notional:.2f} $, {risk.get('leverage') or plan.get('leverage')}x"
        lines.append(txt)
    notes = [n for n in c.get("notlar") or [] if n]
    if notes:
        lines.append("• Not: " + "; ".join(notes)[:300])
    return "\n".join(lines)
