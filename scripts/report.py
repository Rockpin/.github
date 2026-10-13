"""조직 프로필 README(profile/README.md)에 여행 리포트를 쓴다.

- weekly: 이번 주 추천 여행지(날씨·물가·환율 점수) + 오늘의 환율
- daily : 오늘의 환율 블록만 교체
- auto  : 한국 시간 월요일이거나 주간 블록이 없으면 weekly, 아니면 daily

실행: python scripts/report.py --mode auto [--readme profile/README.md]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from exchange import ChartPeriod, NaverExchange, RateChart, RateType, mental_rule  # noqa: E402
from pricelevel import Country, PriceLevelClient  # noqa: E402
from weatheri import Continent, WeatheriClient  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_README = ROOT / "profile" / "README.md"

WEEKLY = ("<!-- weekly:start -->", "<!-- weekly:end -->")
FX = ("<!-- fx:start -->", "<!-- fx:end -->")

# 가중치: 날씨 50 · 물가 30 · 환율 20
W_WEATHER, W_PRICE, W_FX = 0.5, 0.3, 0.2


@dataclass
class Destination:
    city: str  # 웨더아이 도시명
    country: str  # 웨더아이 국가명
    continent: Continent
    currency: str
    price_country: Country


DESTINATIONS = [
    Destination("도쿄", "일본", Continent.ASIA, "JPY", Country.JAPAN),
    Destination("오사카", "일본", Continent.ASIA, "JPY", Country.JAPAN),
    Destination("후쿠오카", "일본", Continent.ASIA, "JPY", Country.JAPAN),
    Destination("삿포로", "일본", Continent.ASIA, "JPY", Country.JAPAN),
    Destination("타이베이", "타이완", Continent.ASIA, "TWD", Country.TAIWAN),
    Destination("홍콩", "홍콩", Continent.ASIA, "HKD", Country.HONG_KONG),
    Destination("상하이", "중국", Continent.ASIA, "CNY", Country.CHINA),
    Destination("방콕", "타이", Continent.ASIA, "THB", Country.THAILAND),
    Destination("하노이", "베트남", Continent.ASIA, "VND", Country.VIETNAM),
    Destination("다낭", "베트남", Continent.ASIA, "VND", Country.VIETNAM),
    Destination("세부", "필리핀", Continent.ASIA, "PHP", Country.PHILIPPINES),
    Destination("싱가포르", "싱가포르", Continent.ASIA, "SGD", Country.SINGAPORE),
    Destination("쿠알라룸푸르", "말레이시아", Continent.ASIA, "MYR", Country.MALAYSIA),
    Destination("파리", "프랑스", Continent.EUROPE, "EUR", Country.FRANCE),
    Destination("런던", "영국", Continent.EUROPE, "GBP", Country.UK),
    Destination("뉴욕", "미국", Continent.NORTH_AMERICA, "USD", Country.USA),
    Destination("시드니", "오스트레일리아", Continent.OCEANIA, "AUD", Country.AUSTRALIA),
]

FX_CURRENCIES = ["USD", "JPY", "EUR", "CNY", "TWD", "HKD", "THB", "VND", "PHP", "SGD", "MYR", "GBP", "AUD"]


# ──────────────────────────── 점수 ────────────────────────────


def clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


def weather_score(max_c: float, min_c: float, rain_days: int, days: int) -> float:
    """낮 최고 20~27°, 밤 최저 12~22° 를 쾌적 구간으로 보고, 비 온 날 비율로 감점."""
    dist = max(0, 20 - max_c) + max(0, max_c - 27) + 0.5 * (max(0, 12 - min_c) + max(0, min_c - 22))
    comfort = clamp(100 - 7 * dist)
    dry = 100 * (1 - rain_days / days)
    return 0.6 * comfort + 0.4 * dry


def price_score(ratio: Optional[float]) -> Optional[float]:
    """한국 대비 물가 0.4 → 100점, 1.0 → 60점, 1.9 → 0점."""
    return None if ratio is None else clamp(100 - (ratio - 0.4) * 66.7)


def fx_score(vs_avg_pct: Optional[float]) -> Optional[float]:
    """통화가 1년 평균보다 쌀수록 가점. 평균과 같으면 50, 10% 싸면 100, 10% 비싸면 0."""
    return None if vs_avg_pct is None else clamp(50 - 5 * vs_avg_pct)


@dataclass
class Row:
    dest: Destination
    icons: str = ""
    max_c: Optional[float] = None
    min_c: Optional[float] = None
    rain_days: Optional[int] = None
    days: int = 0
    precip: float = 0.0
    price_ratio: Optional[float] = None
    price_source: str = ""
    fx_vs_avg: Optional[float] = None  # 현재 / 1년 평균 - 1 (%)
    scores: dict[str, Optional[float]] = field(default_factory=dict)
    total: Optional[float] = None
    error: str = ""


# ──────────────────────────── 수집 ────────────────────────────


async def collect_weather(rows: list[Row], days: int = 7) -> None:
    async with WeatheriClient() as wx:
        countries: dict[Continent, dict[str, str]] = {}
        for row in rows:
            d = row.dest
            try:
                if d.continent not in countries:
                    countries[d.continent] = {c.name: c.code for c in await wx.get_countries(d.continent)}
                code = countries[d.continent].get(d.country)
                if not code:
                    raise LookupError(f"웨더아이 국가 없음: {d.country}")
                city = next((c for c in await wx.get_cities(d.continent, code) if c.name == d.city), None)
                if not city:
                    raise LookupError(f"웨더아이 도시 없음: {d.city}")
                w = await wx.get_weather(city)
                daily = w.daily[:days]
                row.days = len(daily)
                row.icons = "".join(x.icon.emoji for x in daily)
                row.max_c = sum(x.temp_max_c or 0 for x in daily) / len(daily)
                row.min_c = sum(x.temp_min_c or 0 for x in daily) / len(daily)
                row.rain_days = sum(1 for x in daily if (x.precip_mm or 0) >= 1)
                row.precip = sum(x.precip_mm or 0 for x in daily)
            except Exception as e:  # 한 도시 실패가 전체를 막지 않게
                row.error = f"날씨 실패: {e}"


async def collect_price(rows: list[Row], krw: dict[str, float], today: date) -> None:
    async with PriceLevelClient() as pl:
        levels = {p.country: p for p in await pl.compare({r.dest.price_country for r in rows}, krw_rates=krw, today=today)}
    for r in rows:
        p = levels.get(r.dest.price_country)
        if p and p.ratio is not None:
            r.price_ratio, r.price_source = p.ratio, "PPP"
        elif p and p.bigmac_ratio is not None:
            r.price_ratio, r.price_source = p.bigmac_ratio, "빅맥"


async def collect_fx(fx: NaverExchange, currencies: list[str]) -> dict[str, dict]:
    """통화별 현재 고시 · 1년 평균 대비 · 10년 위치."""
    rates = {r.currency: r for r in await fx.rates()}
    out: dict[str, dict] = {}
    for cur in currencies:
        r = rates.get(cur)
        if not r or not r.base_rate:
            continue
        info: dict = {"rate": r}
        try:
            y1 = await fx.chart(cur, ChartPeriod.YEAR_1)
            y10 = await fx.chart(cur, ChartPeriod.YEAR_10)
            if isinstance(y1, RateChart) and isinstance(y10, RateChart):
                c1 = [p.close for p in y1.points if p.close]
                c10 = [p.close for p in y10.points if p.close]
                info["vs_avg"] = (r.base_rate / (sum(c1) / len(c1)) - 1) * 100
                info["y1_change"] = (r.base_rate / c1[0] - 1) * 100
                info["pct10"] = sum(1 for x in c10 if x <= r.base_rate) / len(c10) * 100
        except Exception:
            pass
        out[cur] = info
    return out


# ──────────────────────────── 마크다운 ────────────────────────────


def fmt_amount(a: float) -> str:
    return f"{a:,.0f}" if a >= 100 else f"{a:,.2f}" if a >= 1 else f"{a:,.3f}"


def fx_block(fx_info: dict[str, dict], now: datetime) -> str:
    lines = [
        FX[0],
        f"## 💱 오늘의 환율",
        f"<sub>{now:%Y-%m-%d %H:%M} KST 기준 · 하나은행 매매기준율 · 매일 09:00 자동 갱신</sub>",
        "",
        "| 통화 | 기준율(원) | 전일대비 | 1,000원 = | 암산식(외화→원) | 1년 평균 대비 | 10년 중 위치 |",
        "|---|---:|---:|---:|---|---:|---:|",
    ]
    for cur, info in fx_info.items():
        r = info["rate"]
        unit = f"{r.basis}{r.currency}" if r.basis != 1 else r.currency
        per_unit = r.per_unit(RateType.BASE)
        rule, _ = mental_rule(per_unit, r.currency_name or cur)
        ch = r.change or 0
        arrow = "🔺" if ch > 0 else "🔻" if ch < 0 else "➖"
        vs = info.get("vs_avg")
        pct10 = info.get("pct10")
        lines.append(
            f"| {r.nation_name} {r.currency_name} ({unit}) | {r.base_rate:,.2f} | {arrow} {abs(ch):,.2f} ({r.change_rate or 0:+.2f}%) "
            f"| {fmt_amount(1000 / per_unit)} {cur} | {rule} "
            f"| {'-' if vs is None else f'{vs:+.1f}%'} | {'-' if pct10 is None else f'{pct10:.0f}%'} |"
        )
    lines += [
        "",
        "<sub>1년 평균 대비: −면 평소보다 외화가 쌈. 10년 중 위치: 지난 10년 주간 종가 중 지금보다 쌌던 비율(0% = 10년 최저).</sub>",
        FX[1],
    ]
    return "\n".join(lines)


def weekly_block(rows: list[Row], start: date) -> str:
    end = start + timedelta(days=6)
    ok = sorted([r for r in rows if r.total is not None], key=lambda r: -(r.total or 0))
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        WEEKLY[0],
        f"## 🧳 이번 주 추천 여행지 ({start:%m/%d} ~ {end:%m/%d})",
        f"<sub>매주 월요일 09:00 KST 자동 선정 · 점수 = 날씨 {W_WEATHER:.0%} + 물가 {W_PRICE:.0%} + 환율 {W_FX:.0%}</sub>",
        "",
    ]
    for i, r in enumerate(ok[:3]):
        d = r.dest
        if r.price_ratio is None:
            price = "자료 없음"
        else:
            diff = (1 - r.price_ratio) * 100
            word = f"{diff:.0f}% 쌈" if diff >= 0.5 else f"{-diff:.0f}% 비쌈" if diff <= -0.5 else "비슷"
            src = "빅맥지수 기준 — 외식 한 품목이라 전체 물가와 다를 수 있음" if r.price_source == "빅맥" else "PPP 기준"
            price = f"한국 대비 {r.price_ratio:.2f} ({word}, {src})"
        fxv = f"1년 평균보다 {abs(r.fx_vs_avg):.1f}% {'쌈' if r.fx_vs_avg < 0 else '비쌈'}" if r.fx_vs_avg is not None else "-"
        lines += [
            f"### {medals[i]} {d.city} ({d.country}) — {r.total:.0f}점",
            f"- 🌤️ 날씨: {r.icons}  낮 {r.max_c:.0f}° / 밤 {r.min_c:.0f}°, 비 온 날 {r.rain_days}/{r.days}일",
            f"- 💸 물가: {price}",
            f"- 💱 환율({d.currency}): {fxv}",
            "",
        ]
    lines += [
        "<details><summary>전체 후보 점수 보기</summary>",
        "",
        "| 순위 | 도시 | 점수 | 7일 날씨 | 낮/밤 | 비 온 날 | 물가(한국=1) | 통화 1년 평균 대비 |",
        "|---:|---|---:|---|---|---:|---:|---:|",
    ]
    for i, r in enumerate(ok, 1):
        lines.append(
            f"| {i} | {r.dest.city} | {r.total:.0f} | {r.icons} | {r.max_c:.0f}° / {r.min_c:.0f}° | {r.rain_days}/{r.days} "
            f"| {'-' if r.price_ratio is None else f'{r.price_ratio:.2f}'} | {'-' if r.fx_vs_avg is None else f'{r.fx_vs_avg:+.1f}%'} |"
        )
    failed = [r for r in rows if r.total is None]
    lines += ["", "</details>", ""]
    if failed:
        lines.append("<sub>제외: " + ", ".join(f"{r.dest.city}({r.error or '데이터 부족'})" for r in failed) + "</sub>")
        lines.append("")
    lines += [
        "<sub>날씨: 웨더아이 7일 예보(낮 20~27°·밤 12~22° 쾌적, 비 온 날 감점) · 물가: World Bank 민간소비 PPP×오늘 환율"
        "(물가상승률 보정, 대만은 빅맥지수) · 환율: 네이버 증권 1년 평균 대비. 항공권·태풍·현지 일정은 반영하지 않은 참고용입니다.</sub>",
        WEEKLY[1],
    ]
    return "\n".join(lines)


def replace_block(text: str, markers: tuple[str, str], block: str) -> str:
    start, end = markers
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text)
    return text.rstrip() + "\n\n" + block + "\n"


HEADER = "# 록핀 (Rockpin)\n\n매주 날씨·환율·물가로 고른 추천 여행지와 오늘의 환율입니다.\n"
FOOTER = (
    "<!-- footer:start -->\n---\n<sub>데이터: [웨더아이](https://www.weatheri.co.kr) · [네이버 증권](https://stock.naver.com) · "
    "[World Bank](https://data.worldbank.org) · [The Economist Big Mac Index](https://github.com/TheEconomist/big-mac-data) — "
    "투자·여행 판단의 참고 자료이며 정확성을 보장하지 않습니다.</sub>\n<!-- footer:end -->"
)


# ──────────────────────────── 실행 ────────────────────────────


async def run(mode: str, readme: Path) -> None:
    now = datetime.now(KST)
    today = now.date()
    text = readme.read_text(encoding="utf-8") if readme.exists() else ""
    if mode == "auto":
        mode = "weekly" if today.weekday() == 0 or WEEKLY[0] not in text else "daily"
    if WEEKLY[0] not in text and FX[0] not in text:
        text = HEADER  # 처음 실행: 기존 인사말을 리포트로 교체

    async with NaverExchange() as fx:
        fx_info = await collect_fx(fx, FX_CURRENCIES)
        fx_all = fx_info
        if mode == "weekly":
            extra = sorted({d.currency for d in DESTINATIONS} - set(fx_info))
            fx_all = {**fx_info, **(await collect_fx(fx, extra))}
    if not fx_info:
        raise SystemExit("환율 수집 실패 — README 를 바꾸지 않음")

    if mode == "weekly":
        rows = [Row(d) for d in DESTINATIONS]
        krw = {c: i["rate"].per_unit(RateType.BASE) for c, i in fx_all.items()}
        await collect_weather(rows)
        await collect_price(rows, krw, today)
        for r in rows:
            r.fx_vs_avg = fx_all.get(r.dest.currency, {}).get("vs_avg")
            if r.error or r.max_c is None:
                continue
            ws = weather_score(r.max_c, r.min_c or 0, r.rain_days or 0, r.days)
            ps, fs = price_score(r.price_ratio), fx_score(r.fx_vs_avg)
            r.scores = {"weather": ws, "price": ps, "fx": fs}
            parts = [(ws, W_WEATHER), (ps, W_PRICE), (fs, W_FX)]
            used = [(s, w) for s, w in parts if s is not None]
            r.total = sum(s * w for s, w in used) / sum(w for _, w in used)
        if not any(r.total is not None for r in rows):
            raise SystemExit("날씨 수집 전부 실패 — README 를 바꾸지 않음")
        start = today - timedelta(days=today.weekday())
        text = replace_block(text, WEEKLY, weekly_block(rows, start))

    text = replace_block(text, FX, fx_block(fx_info, now))
    text = replace_block(text, ("<!-- footer:start -->", "<!-- footer:end -->"), FOOTER)
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(text.rstrip() + "\n", encoding="utf-8")
    print(f"{mode} 갱신 완료 → {readme}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "daily", "weekly"], default="auto")
    ap.add_argument("--readme", type=Path, default=DEFAULT_README)
    a = ap.parse_args()
    getattr(sys.stdout, "reconfigure", lambda **_: None)(encoding="utf-8")
    asyncio.run(run(a.mode, a.readme))


if __name__ == "__main__":
    main()
