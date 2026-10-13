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
from weatheri import Continent, WeatheriClient, WeatherIcon  # noqa: E402

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
    """낮 최고 20–27°, 밤 최저 12–22° 를 쾌적 구간으로 보고, 비 온 날 비율로 감점."""
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
    icons: list = field(default_factory=list)  # 일별 WeatherIcon
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
                row.icons = [x.icon for x in daily]
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


WEEKDAY = "월화수목금토일"

# 아이콘은 이모지 대신 이미지로 넣는다 — OS 마다 이모지 모양이 다르고, Windows 는 국기를 'TW' 같은 글자로 보여주기 때문
# 날씨: Meteocons(MIT) · 그 외(국기·메달·화살표 등): Twemoji(CC-BY 4.0). 버전을 고정해 갑자기 깨지지 않게 한다.
TWEMOJI_URL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@17.0.3/assets/svg/{}.svg"
METEOCONS_URL = "https://cdn.jsdelivr.net/gh/basmilius/weather-icons@2.0.0/production/fill/all/{}.svg"

METEOCONS = {
    WeatherIcon.CLEAR: "clear-day",
    WeatherIcon.PARTLY_CLOUDY: "partly-cloudy-day",
    WeatherIcon.OVERCAST: "cloudy",
    WeatherIcon.MOSTLY_CLOUDY: "overcast-day",
    WeatherIcon.RAIN: "rain",
    WeatherIcon.SNOW: "snow",
    WeatherIcon.RAIN_SNOW: "sleet",
    WeatherIcon.SHOWER: "partly-cloudy-day-rain",
    WeatherIcon.SNOW_SHOWER: "partly-cloudy-day-snow",
    WeatherIcon.FOG: "fog",
    WeatherIcon.THUNDERSTORM: "thunderstorms-rain",
    WeatherIcon.CLEAR_THEN_CLOUDY: "partly-cloudy-day",
    WeatherIcon.CLEAR_THEN_THUNDER: "thunderstorms-day",
    WeatherIcon.CLEAR_THEN_RAIN: "partly-cloudy-day-rain",
    WeatherIcon.CLEAR_THEN_SNOW: "partly-cloudy-day-snow",
    WeatherIcon.CLEAR_THEN_RAIN_SNOW: "partly-cloudy-day-sleet",
    WeatherIcon.CLOUDY_THEN_CLEAR: "partly-cloudy-day",
    WeatherIcon.THUNDER_THEN_CLEAR: "thunderstorms-day",
    WeatherIcon.RAIN_THEN_CLEAR: "partly-cloudy-day-rain",
    WeatherIcon.SNOW_THEN_CLEAR: "partly-cloudy-day-snow",
    WeatherIcon.RAIN_SNOW_THEN_CLEAR: "partly-cloudy-day-sleet",
    WeatherIcon.YELLOW_DUST: "dust",
    WeatherIcon.UNKNOWN: "not-available",
}

FLAG_ISO2 = {
    "USD": "us", "JPY": "jp", "EUR": "eu", "CNY": "cn", "TWD": "tw", "HKD": "hk", "THB": "th",
    "VND": "vn", "PHP": "ph", "SGD": "sg", "MYR": "my", "GBP": "gb", "AUD": "au",
}
CITY_ISO2 = {"프랑스": "fr", "영국": "gb", "미국": "us", "오스트레일리아": "au"}


def _img(src: str, alt: str, size: int) -> str:
    return f'<img src="{src}" width="{size}" height="{size}" alt="{alt}" title="{alt}">'


def emoji_img(emoji: str, alt: str = "", size: int = 20) -> str:
    """이모지 문자 → Twemoji 이미지 (파일명은 코드포인트, 변형 선택자 FE0F 제외)."""
    code = "-".join(f"{ord(c):x}" for c in emoji if c != "️")
    return _img(TWEMOJI_URL.format(code), alt or emoji, size)


def flag_img(iso2: Optional[str], alt: str = "", size: int = 20) -> str:
    """국가코드 → Twemoji 국기 (지역 표시 문자 두 개)."""
    if not iso2:
        return ""
    return emoji_img("".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in iso2.lower()), alt, size)


def weather_img(icon: WeatherIcon, size: int = 28) -> str:
    return _img(METEOCONS_URL.format(METEOCONS.get(icon, "not-available")), icon.label, size)


def md(d: date) -> str:
    """10/13(화)"""
    return f"{d.month}/{d.day}({WEEKDAY[d.weekday()]})"


def clock(t: datetime) -> str:
    """'오전 9:00', '오후 2:30'"""
    h = t.hour % 12 or 12
    return f"{'오전' if t.hour < 12 else '오후'} {h}:{t.minute:02d}"


def josa(word: str, with_final: str, without_final: str) -> str:
    """마지막 글자 받침 유무로 조사 고르기: 엔→'이', 달러→'가'."""
    ch = word.strip()[-1:]
    if "가" <= ch <= "힣":
        return with_final if (ord(ch) - 0xAC00) % 28 else without_final
    return without_final


def fmt_amount(a: float) -> str:
    return f"{a:,.0f}" if a >= 100 else f"{a:,.2f}" if a >= 1 else f"{a:,.3f}"


def cheap_word(pct: Optional[float], same: str = "비슷해요") -> str:
    """기준보다 pct% 높으면 '3% 비싸요', 낮으면 '8% 싸요'."""
    if pct is None:
        return "-"
    if abs(pct) < 0.5:
        return same
    return f"{abs(pct):.0f}% 싸요" if pct < 0 else f"{pct:.0f}% 비싸요"


def currency_name(r) -> str:
    """'엔', '대만 달러' — 같은 이름(달러)이 많아서 나라 이름을 붙인다."""
    name = r.currency_name
    if name in ("달러", "파운드") and r.currency != "USD":
        name = f"{r.nation_name} {name}"
    return name


def currency_label(r) -> str:
    return f"{flag_img(FLAG_ISO2.get(r.currency), r.nation_name)} {currency_name(r)}".strip()


def fx_block(fx_info: dict[str, dict], now: datetime) -> str:
    lows = [currency_name(i["rate"]) for i in fx_info.values() if (i.get("pct10") or 100) <= 5]
    lines = [
        FX[0],
        f"## {emoji_img('💱', '환율', 28)} 오늘 환율",
        f"<sub>{md(now.date())} {clock(now)} · 하나은행 기준 · 매일 아침 9시에 바뀌어요</sub>",
        "",
    ]
    if lows:
        names = ", ".join(lows)
        lines += [f"> {emoji_img('💡', '알림', 18)} {names}{josa(names, '이', '가')} 지난 10년 중 가장 싼 편이에요.", ""]
    lines += [
        "| 통화 | 환율 | 어제보다 | 1,000원이면 | 빠른 계산 (현지 가격 → 원) | 1년 평균보다 |",
        "|---|---:|---:|---:|---|---:|",
    ]
    for cur, info in fx_info.items():
        r = info["rate"]
        per_unit = r.per_unit(RateType.BASE)
        rule, _ = mental_rule(per_unit, r.currency_name or cur)
        rule = rule.replace(" ≈ 원", "")
        unit = f" (100{r.currency_name})" if r.basis != 1 else ""
        ch = r.change or 0
        if ch > 0:
            move = f"{emoji_img('🔺', '올랐어요', 12)} {abs(ch):,.2f}"
        elif ch < 0:
            move = f"{emoji_img('🔻', '내렸어요', 12)} {abs(ch):,.2f}"
        else:
            move = "그대로"
        lines.append(
            f"| {currency_label(r)}{unit} | {r.base_rate:,.2f}원 | {move} "
            f"| {fmt_amount(1000 / per_unit)}{r.currency_name} | {rule} | {cheap_word(info.get('vs_avg'))} |"
        )
    lines.append(FX[1])
    return "\n".join(lines)


def _city(r: Row) -> str:
    iso2 = CITY_ISO2.get(r.dest.country) or (FLAG_ISO2.get(r.dest.currency) if r.dest.currency != "EUR" else None)
    return f"{flag_img(iso2, r.dest.country)} **{r.dest.city}**".strip()


def _price(r: Row) -> str:
    if r.price_ratio is None:
        return "-"
    word = cheap_word((r.price_ratio - 1) * 100)
    return word + ("*" if r.price_source == "빅맥" else "")


def weekly_block(rows: list[Row], start: date) -> str:
    end = start + timedelta(days=6)
    ok = sorted([r for r in rows if r.total is not None], key=lambda r: -(r.total or 0))
    medals = [emoji_img(m, f"{i}위", 24) for i, m in enumerate(["🥇", "🥈", "🥉"], 1)]
    head = "| 7일 날씨 | 낮 / 밤 | 비 오는 날 | 물가 (한국 대비) | 환율 (1년 평균 대비) |"

    def cells(r: Row) -> str:
        return (
            f"| {''.join(weather_img(i) for i in r.icons)} | {r.max_c:.0f}° / {r.min_c:.0f}° | {r.rain_days or 0}일 "
            f"| {_price(r)} | {cheap_word(r.fx_vs_avg)} |"
        )

    lines = [
        WEEKLY[0],
        f"## {emoji_img('🧳', '여행', 28)} 이번 주 떠나기 좋은 곳",
        f"<sub>{md(start)} – {md(end)} · 매주 월요일 아침 9시에 새로 골라요</sub>",
        "",
        f"| | 도시 {head}",
        "|:-:|---|---|:-:|:-:|---:|---:|",
    ]
    for i, r in enumerate(ok[:3]):
        lines.append(f"| {medals[i]} | {_city(r)} {cells(r)}")
    star = any(r.price_source == "빅맥" for r in ok)
    lines += [
        "",
        "<sub>" + ("* 대만은 물가 통계가 없어 빅맥 가격으로 비교했어요. 실제와 다를 수 있어요." if star else "") + "</sub>",
        "",
        f"<details><summary>다른 후보 {max(0, len(ok) - 3)}곳 보기</summary>",
        "",
        f"| 순위 | 도시 | 점수 {head}",
        "|---:|---|---:|---|:-:|:-:|---:|---:|",
    ]
    for i, r in enumerate(ok[3:], 4):
        lines.append(f"| {i} | {_city(r)} | {r.total:.0f} {cells(r)}")
    lines += ["", "</details>", ""]
    failed = [r for r in rows if r.total is None]
    if failed:
        lines += ["<sub>날씨 정보를 받지 못해 이번 주엔 뺐어요: " + ", ".join(r.dest.city for r in failed) + "</sub>", ""]
    lines += [
        f"<sub>점수는 날씨 {W_WEATHER:.0%}, 물가 {W_PRICE:.0%}, 환율 {W_FX:.0%}로 매겨요. "
        "낮 20–27°, 밤 12–22°이고 비가 적을수록 날씨 점수가 높아요. "
        "떠나기 전에 태풍 소식과 항공권 가격도 확인해 보세요.</sub>",
        WEEKLY[1],
    ]
    return "\n".join(lines)


def extract_block(text: str, markers: tuple[str, str]) -> Optional[str]:
    start, end = markers
    m = re.search(re.escape(start) + r".*?" + re.escape(end), text, re.S)
    return m.group(0) if m else None


HEADER = "# 록핀 (Rockpin)\n\n이번 주에 떠나기 좋은 여행지와 오늘 환율을 알려드려요."
FOOTER = (
    "---\n<sub>데이터: [웨더아이](https://www.weatheri.co.kr) · [네이버 증권](https://stock.naver.com) · "
    "[세계은행](https://data.worldbank.org) · [이코노미스트 빅맥지수](https://github.com/TheEconomist/big-mac-data) · "
    "참고용 정보예요<br>"
    "아이콘: [Meteocons](https://github.com/basmilius/weather-icons) (MIT) · "
    "[Twemoji](https://github.com/jdecked/twemoji) (CC-BY 4.0)</sub>"
)


# ──────────────────────────── 실행 ────────────────────────────


async def run(mode: str, readme: Path) -> None:
    now = datetime.now(KST)
    today = now.date()
    text = readme.read_text(encoding="utf-8") if readme.exists() else ""
    weekly = extract_block(text, WEEKLY)  # daily 에선 지난 주간 블록을 그대로 둔다
    if mode == "auto":
        mode = "weekly" if today.weekday() == 0 or weekly is None else "daily"

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
        weekly = weekly_block(rows, start)

    # README 는 매번 처음부터 조립한다(헤더·푸터 문구를 바꿔도 바로 반영되게)
    parts = [HEADER, weekly, fx_block(fx_info, now), FOOTER]
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text("\n\n".join(p for p in parts if p) + "\n", encoding="utf-8")
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
