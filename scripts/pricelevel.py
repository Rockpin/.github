"""나라별 물가 수준(한국 대비) · 1인당 GDP 비동기 조회 모듈.

- 출처: 세계은행 공개 API(api.worldbank.org/v2, 키 불필요) + 네이버 실시간 환율(exchange.py)
        + 이코노미스트 빅맥지수(github.com/TheEconomist/big-mac-data) 교차검증
- 핵심 지표: 한국 대비 물가 수준 = PPP(현지통화/국제달러) × 현재환율(원/현지통화) ÷ 한국 PPP(원/국제달러, 같은 연도)
  · 0.70 이면 같은 소비 바구니를 한국 돈으로 70% 에 산다 → "30% 싸게 느껴진다"
  · PPP 는 여행자 체감에 가까운 '민간소비 PPP'(PA.NUS.PRVT.PP)를 기본으로 쓴다
  · PPP 는 연간 통계라 기준연도(7/1) 이후 오늘까지의 물가상승률 차이를 최신 연간 상승률로 추정 보정한다
    (튀르키예처럼 물가상승률이 큰 나라는 보정 전후 차이가 크다)
  · 빅맥 기준 값은 외식 한 품목이라 전체 물가와 다를 수 있다 — 두 값의 차이가 크면 해석 주의

사용 예
    async with PriceLevelClient() as pl:
        rows = await pl.compare([Country.JAPAN, Country.THAILAND, Country.VIETNAM])
        print(pretty_compare(rows))
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from typing import Any, Iterable, Optional

import httpx

WB_URL = "https://api.worldbank.org/v2"
BIGMAC_URL = "https://raw.githubusercontent.com/TheEconomist/big-mac-data/master/output-data/big-mac-raw-index.csv"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0"


# ──────────────────────────── Enums ────────────────────────────


class WBIndicator(Enum):
    """세계은행 지표 코드."""

    PPP_CONSUMPTION = "PA.NUS.PRVT.PP"  # 민간소비 PPP 환율 (현지통화/국제달러)
    PPP_GDP = "PA.NUS.PPP"  # GDP PPP 환율
    GDP_PER_CAPITA = "NY.GDP.PCAP.CD"  # 1인당 GDP (현재 USD)
    GDP_PER_CAPITA_PPP = "NY.GDP.PCAP.PP.CD"  # 1인당 GDP (PPP, 국제달러)
    CPI = "FP.CPI.TOTL"  # 소비자물가지수 (2010=100)
    INFLATION = "FP.CPI.TOTL.ZG"  # 소비자물가 상승률 (%)

    @property
    def label(self) -> str:
        return {
            WBIndicator.PPP_CONSUMPTION: "민간소비 PPP",
            WBIndicator.PPP_GDP: "GDP PPP",
            WBIndicator.GDP_PER_CAPITA: "1인당 GDP(USD)",
            WBIndicator.GDP_PER_CAPITA_PPP: "1인당 GDP(PPP)",
            WBIndicator.CPI: "소비자물가지수",
            WBIndicator.INFLATION: "물가상승률",
        }[self]


class Country(Enum):
    """value = (ISO3, 한글명, 통화코드). 세계은행에 대만(TWN)은 없다."""

    KOREA = ("KOR", "한국", "KRW")
    JAPAN = ("JPN", "일본", "JPY")
    TAIWAN = ("TWN", "대만", "TWD")
    CHINA = ("CHN", "중국", "CNY")
    HONG_KONG = ("HKG", "홍콩", "HKD")
    MACAO = ("MAC", "마카오", "MOP")
    THAILAND = ("THA", "태국", "THB")
    VIETNAM = ("VNM", "베트남", "VND")
    PHILIPPINES = ("PHL", "필리핀", "PHP")
    INDONESIA = ("IDN", "인도네시아", "IDR")
    MALAYSIA = ("MYS", "말레이시아", "MYR")
    SINGAPORE = ("SGP", "싱가포르", "SGD")
    MONGOLIA = ("MNG", "몽골", "MNT")
    USA = ("USA", "미국", "USD")
    CANADA = ("CAN", "캐나다", "CAD")
    AUSTRALIA = ("AUS", "호주", "AUD")
    NEW_ZEALAND = ("NZL", "뉴질랜드", "NZD")
    UK = ("GBR", "영국", "GBP")
    FRANCE = ("FRA", "프랑스", "EUR")
    GERMANY = ("DEU", "독일", "EUR")
    ITALY = ("ITA", "이탈리아", "EUR")
    SPAIN = ("ESP", "스페인", "EUR")
    SWITZERLAND = ("CHE", "스위스", "CHF")
    TURKEY = ("TUR", "튀르키예", "TRY")
    UAE = ("ARE", "아랍에미리트", "AED")

    @property
    def iso3(self) -> str:
        return self.value[0]

    @property
    def label(self) -> str:
        return self.value[1]

    @property
    def currency(self) -> str:
        return self.value[2]

    @classmethod
    def from_value(cls, value: "str | Country") -> "Country":
        if isinstance(value, cls):
            return value
        v = str(value).strip()
        for c in cls:
            if v.upper() in (c.iso3, c.name) or v == c.label:
                return c
        by_currency = [c for c in cls if c.currency == v.upper()]
        if len(by_currency) == 1:  # 통화 코드는 쓰는 나라가 하나일 때만 (EUR 은 여러 나라라 모호)
            return by_currency[0]
        if by_currency:
            raise ValueError(f"{v} 는 여러 나라 통화입니다: {', '.join(c.name for c in by_currency)} 중 하나로 지정")
        raise ValueError(f"알 수 없는 나라: {value!r}")


class Feel(Enum):
    """체감 물가 구간(한국 대비 물가 수준)."""

    VERY_CHEAP = "아주 쌈"  # 0.5 미만
    CHEAP = "쌈"  # 0.5~0.8
    SIMILAR = "비슷"  # 0.8~1.1
    EXPENSIVE = "비쌈"  # 1.1~1.4
    VERY_EXPENSIVE = "아주 비쌈"  # 1.4 이상

    @classmethod
    def from_ratio(cls, ratio: Optional[float]) -> Optional["Feel"]:
        if ratio is None:
            return None
        if ratio < 0.5:
            return cls.VERY_CHEAP
        if ratio < 0.8:
            return cls.CHEAP
        if ratio < 1.1:
            return cls.SIMILAR
        if ratio < 1.4:
            return cls.EXPENSIVE
        return cls.VERY_EXPENSIVE

    @property
    def emoji(self) -> str:
        return {
            Feel.VERY_CHEAP: "🟢🟢",
            Feel.CHEAP: "🟢",
            Feel.SIMILAR: "⚪",
            Feel.EXPENSIVE: "🔴",
            Feel.VERY_EXPENSIVE: "🔴🔴",
        }[self]


# ──────────────────────────── helpers ────────────────────────────


def _g(d: dict, snake: str, camel: Optional[str] = None, default: Any = None) -> Any:
    if snake in d:
        return d[snake]
    if camel and camel in d:
        return d[camel]
    return default


def _jsonable(v: Any) -> Any:
    if isinstance(v, Enum):
        return v.name
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def _enum(cls, v):
    if v is None or isinstance(v, cls):
        return v
    try:
        return cls[v]
    except KeyError:
        return cls(v)


class _Model:
    def to_dict(self) -> dict:
        return _jsonable(asdict(self))  # type: ignore[call-overload]


# ──────────────────────────── 모델 ────────────────────────────


@dataclass
class Observation(_Model):
    """세계은행 지표 값 한 건(나라·연도)."""

    iso3: str
    country_name: str
    indicator: WBIndicator
    year: int
    value: Optional[float]

    @classmethod
    def from_dict(cls, d: dict) -> "Observation":
        """세계은행 원본({'countryiso3code','country':{'value'},'indicator':{'id'},'date','value'}) / to_dict() 둘 다."""
        ind = _g(d, "indicator")
        if isinstance(ind, dict):
            ind = WBIndicator(ind["id"])
        return cls(
            iso3=_g(d, "iso3", "countryiso3code", ""),
            country_name=_g(d, "country_name") or (d.get("country") or {}).get("value", ""),
            indicator=_enum(WBIndicator, ind) or WBIndicator.PPP_CONSUMPTION,
            year=int(_g(d, "year", "date")),
            value=None if _g(d, "value") is None else float(_g(d, "value")),
        )


@dataclass
class PriceLevel(_Model):
    """한 나라의 한국 대비 물가 수준."""

    country: Country
    ppp: Optional[float]  # 현지통화 / 국제달러
    ppp_year: Optional[int]
    krw_per_unit: Optional[float]  # 오늘 환율 (원 / 현지통화 1단위)
    korea_ppp: Optional[float]  # 원 / 국제달러 (ppp_year 와 같은 연도)
    ratio: Optional[float]  # 한국 = 1.0, 물가상승률 보정 후(보정을 끄면 ratio_raw 와 같음)
    ratio_raw: Optional[float] = None  # PPP 기준연도 그대로
    inflation_factor: Optional[float] = None  # 보정 배수 ((1+현지)/(1+한국))^(경과개월/12)
    months_since_ppp: Optional[float] = None
    gdp_per_capita: Optional[float] = None  # USD
    gdp_year: Optional[int] = None
    inflation: Optional[float] = None  # 최근 연간 물가상승률 %
    inflation_year: Optional[int] = None
    bigmac_ratio: Optional[float] = None  # 빅맥 가격 기준 한국 대비(교차검증용)
    bigmac_price: Optional[float] = None  # 현지통화
    bigmac_date: Optional[str] = None
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "PriceLevel":
        return cls(
            country=_enum(Country, d["country"]) or Country.KOREA,
            ppp=d.get("ppp"),
            ppp_year=d.get("ppp_year"),
            krw_per_unit=d.get("krw_per_unit"),
            korea_ppp=d.get("korea_ppp"),
            ratio=d.get("ratio"),
            ratio_raw=d.get("ratio_raw"),
            inflation_factor=d.get("inflation_factor"),
            months_since_ppp=d.get("months_since_ppp"),
            gdp_per_capita=d.get("gdp_per_capita"),
            gdp_year=d.get("gdp_year"),
            inflation=d.get("inflation"),
            inflation_year=d.get("inflation_year"),
            bigmac_ratio=d.get("bigmac_ratio"),
            bigmac_price=d.get("bigmac_price"),
            bigmac_date=d.get("bigmac_date"),
            note=d.get("note", ""),
        )

    @property
    def disagreement(self) -> Optional[float]:
        """PPP 와 빅맥 결과의 차이(배수). 1.3 이상이면 해석 주의."""
        if self.ratio is None or self.bigmac_ratio is None:
            return None
        hi, lo = max(self.ratio, self.bigmac_ratio), min(self.ratio, self.bigmac_ratio)
        return hi / lo if lo else None

    @property
    def feel(self) -> Optional[Feel]:
        return Feel.from_ratio(self.ratio)

    @property
    def cheaper_pct(self) -> Optional[float]:
        """한국보다 몇 % 싸게 느껴지는지(음수면 비쌈)."""
        return None if self.ratio is None else (1 - self.ratio) * 100

    def krw_equivalent(self, korea_price: float) -> Optional[float]:
        """한국에서 korea_price 원짜리 소비가 이 나라에선 원화로 대략 얼마인지."""
        return None if self.ratio is None else korea_price * self.ratio

    @property
    def summary(self) -> str:
        if self.ratio is None:
            return f"{self.country.label}: 자료 없음 ({self.note})"
        c = self.cheaper_pct or 0
        word = f"{c:.0f}% 싸게" if c > 0 else f"{-c:.0f}% 비싸게"
        return f"{self.country.label}: 한국 대비 물가 {self.ratio:.2f} → 약 {word} 느껴짐"


# ──────────────────────────── 클라이언트 ────────────────────────────


class PriceLevelClient:
    """세계은행 지표 + 네이버 환율로 한국 대비 물가 수준을 계산."""

    def __init__(self, *, timeout: float = 30.0, client: Optional[httpx.AsyncClient] = None):
        self._own = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT})

    async def __aenter__(self) -> "PriceLevelClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own:
            await self._client.aclose()

    async def indicator(
        self, indicator: WBIndicator, countries: Iterable["Country | str"], recent: int = 5
    ) -> list[Observation]:
        """지표 최근 recent 개 연도(값 없는 연도 포함)."""
        def iso3(c: "Country | str") -> str:
            try:
                return Country.from_value(c).iso3  # Country · 이름 · ISO3 · 통화코드(JPY 등)
            except ValueError:
                return str(c).upper()  # Country 에 없는 나라는 ISO3 그대로 전달

        codes = ";".join(iso3(c) for c in countries)
        url = f"{WB_URL}/country/{codes}/indicator/{indicator.value}"
        data: Any = None
        for attempt in range(3):
            try:
                r = await self._client.get(url, params={"format": "json", "mrv": recent, "per_page": 1000})
                r.raise_for_status()
                data = r.json()
                break
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 2:
                    raise
                await asyncio.sleep(1 + attempt)
        rows = data[1] if isinstance(data, list) and len(data) > 1 and data[1] else []
        return [Observation.from_dict(x) for x in rows]

    async def latest(
        self, indicator: WBIndicator, countries: Iterable["Country | str"]
    ) -> dict[str, Observation]:
        """나라별 값이 있는 최신 연도."""
        out: dict[str, Observation] = {}
        for o in sorted(await self.indicator(indicator, countries), key=lambda o: -o.year):
            if o.value is not None and o.iso3 not in out:
                out[o.iso3] = o
        return out

    async def by_year(self, indicator: WBIndicator, countries: Iterable["Country | str"], recent: int = 6) -> dict[str, dict[int, float]]:
        """{iso3: {연도: 값}} (값 있는 연도만)."""
        out: dict[str, dict[int, float]] = {}
        for o in await self.indicator(indicator, countries, recent):
            if o.value is not None:
                out.setdefault(o.iso3, {})[o.year] = o.value
        return out

    async def bigmac(self) -> tuple[str, dict[str, tuple[str, float]]]:
        """이코노미스트 빅맥지수 최신 회차: (날짜, {iso3: (통화, 현지가격)})."""
        import csv
        import io

        r = await self._client.get(BIGMAC_URL)
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        last = max(x["date"] for x in rows)
        return last, {x["iso_a3"]: (x["currency_code"], float(x["local_price"])) for x in rows if x["date"] == last}

    async def compare(
        self,
        countries: Iterable["Country | str"],
        *,
        ppp: WBIndicator = WBIndicator.PPP_CONSUMPTION,
        krw_rates: Optional[dict[str, float]] = None,
        adjust_inflation: bool = True,
        with_bigmac: bool = True,
        today: Optional[date] = None,
    ) -> list[PriceLevel]:
        """한국 대비 물가 수준.

        - krw_rates(통화→원/1단위)를 안 주면 네이버 환율을 가져온다.
        - 한국 PPP 는 비교 나라와 같은 연도 값을 쓴다.
        - adjust_inflation: PPP 기준시점(해당 연도 7/1)부터 오늘까지의 물가상승률 차이를 최신 연간 상승률로 추정 보정.
        - with_bigmac: 빅맥지수로 교차검증 값(bigmac_ratio)을 채운다(대만처럼 PPP 가 없는 나라도 나옴).
        """
        today = today or date.today()
        targets = [Country.from_value(c) for c in countries]
        all_c = list(dict.fromkeys([Country.KOREA, *targets]))
        if krw_rates is None:
            krw_rates = await _naver_krw_rates()
        krw_rates = {**krw_rates, "KRW": 1.0}
        jobs = [
            self.by_year(ppp, all_c),
            self.latest(WBIndicator.GDP_PER_CAPITA, all_c),
            self.latest(WBIndicator.INFLATION, all_c),
        ]
        pp, gdp, inf = await asyncio.gather(*jobs)
        bm_date, bm = (await self.bigmac()) if with_bigmac else (None, {})
        kr_ppp = pp.get("KOR", {})
        kr_inf = inf.get("KOR")
        kr_bm = bm.get("KOR")
        out = []
        for c in targets:
            years = pp.get(c.iso3, {})
            common = sorted(set(years) & set(kr_ppp))
            year = common[-1] if common else None
            rate = krw_rates.get(c.currency)
            g, i = gdp.get(c.iso3), inf.get(c.iso3)
            note = ""
            raw = ratio = factor = months = None
            if year is None:
                note = "세계은행 PPP 없음" + (" (대만은 세계은행 통계 미포함)" if c is Country.TAIWAN else "")
            elif rate is None:
                note = f"{c.currency} 환율 없음"
            else:
                raw = years[year] * rate / kr_ppp[year]
                ratio = raw
                if adjust_inflation and i and kr_inf:
                    months = (today - date(year, 7, 1)).days / 30.44
                    factor = ((1 + i.value / 100) / (1 + kr_inf.value / 100)) ** (months / 12)  # type: ignore[operator]
                    ratio = raw * factor
            bm_ratio = bm_price = None
            b = bm.get(c.iso3) or (bm.get("EUZ") if c.currency == "EUR" else None)  # 유로 나라는 유로존 평균
            if b and kr_bm and krw_rates.get(b[0]):
                bm_price = b[1]
                bm_ratio = b[1] * krw_rates[b[0]] / kr_bm[1]
            out.append(
                PriceLevel(
                    country=c,
                    ppp=years.get(year) if year else None,
                    ppp_year=year,
                    krw_per_unit=rate,
                    korea_ppp=kr_ppp.get(year) if year else None,
                    ratio=ratio,
                    ratio_raw=raw,
                    inflation_factor=factor,
                    months_since_ppp=round(months, 1) if months else None,
                    gdp_per_capita=g.value if g else None,
                    gdp_year=g.year if g else None,
                    inflation=i.value if i else None,
                    inflation_year=i.year if i else None,
                    bigmac_ratio=bm_ratio,
                    bigmac_price=bm_price,
                    bigmac_date=bm_date if b else None,
                    note=note,
                )
            )
        return out


async def _naver_krw_rates() -> dict[str, float]:
    """exchange.py 로 오늘 매매기준율(원 / 1단위)."""
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))  # 같은 폴더의 exchange.py
    from exchange import NaverExchange, RateType  # type: ignore[import-not-found]

    async with NaverExchange() as fx:
        return {r.currency: r.per_unit(RateType.BASE) for r in await fx.rates() if r.per_unit(RateType.BASE)}


# ──────────────────────────── 출력 ────────────────────────────


def _width(s: str) -> int:
    import unicodedata

    w = 0
    for i, ch in enumerate(s):
        if ch in "️‍":
            continue
        wide = ord(ch) >= 0x1F000 or unicodedata.east_asian_width(ch) in "WF" or (i + 1 < len(s) and s[i + 1] == "️")
        w += 2 if wide else 1
    return w


def _table(headers: list[str], rows: list[list[str]], right: Iterable[int] = ()) -> str:
    right = set(right)
    widths = [max(_width(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]

    def line(r: list[str]) -> str:
        cells = []
        for i, c in enumerate(r):
            gap = " " * (widths[i] - _width(str(c)))
            cells.append(gap + str(c) if i in right else str(c) + gap)
        return "  " + "  ".join(cells).rstrip()

    return "\n".join([line(headers), "  " + "  ".join("─" * w for w in widths), *(line(r) for r in rows)])


def pretty_compare(rows: list[PriceLevel], korea_price: float = 10_000) -> str:
    """한국 대비 물가 표. korea_price 원짜리가 그 나라에선 원화로 얼마쯤인지도 함께."""
    def pct(ratio: Optional[float]) -> str:
        if ratio is None:
            return "-"
        c = (1 - ratio) * 100
        return f"▼{c:.0f}%" if c >= 0.5 else f"▲{-c:.0f}%" if c <= -0.5 else "0%"

    rows = sorted(rows, key=lambda r: (r.ratio is None and r.bigmac_ratio is None, r.ratio or r.bigmac_ratio or 0))
    body = []
    for r in rows:
        feel = f"{r.feel.emoji} {r.feel.value}" if r.feel else "자료 없음"
        check = ""
        if r.disagreement and r.disagreement >= 1.3:
            check = "⚠ 차이 큼"
        body.append(
            [
                r.country.label,
                f"{r.ratio:.2f}" if r.ratio is not None else "-",
                pct(r.ratio),
                feel,
                f"{r.krw_equivalent(korea_price):,.0f}원" if r.ratio is not None else "-",
                f"{r.bigmac_ratio:.2f} ({pct(r.bigmac_ratio)})" if r.bigmac_ratio is not None else "-",
                check,
                f"${r.gdp_per_capita:,.0f}" if r.gdp_per_capita else "-",
            ]
        )
    head = ["나라", "물가(한국=1)", "체감", "구간", f"한국 {korea_price:,.0f}원짜리", "빅맥 기준", "교차검증", "1인당 GDP"]
    notes = [f"  ※ {r.country.label}: {r.note}" for r in rows if r.ratio is None and r.note]
    return "\n".join([_table(head, body, right=(1, 2, 4, 5, 7)), *notes])


async def _demo() -> None:
    getattr(sys.stdout, "reconfigure", lambda **_: None)(encoding="utf-8")
    async with PriceLevelClient() as pl:
        rows = await pl.compare([c for c in Country if c is not Country.KOREA])
    print("💸 한국 대비 체감 물가 (민간소비 PPP × 오늘 환율, 물가상승률 보정)")
    print(pretty_compare(rows))
    years = sorted({r.ppp_year for r in rows if r.ppp_year})
    bm = next((r.bigmac_date for r in rows if r.bigmac_date), None)
    print(
        f"\n  ※ PPP 기준연도 {years} → 오늘까지 물가상승률 차이 추정 보정. 빅맥지수 {bm} 회차 × 오늘 환율.\n"
        "  ※ ▼ = 한국보다 쌈, ▲ = 비쌈. '차이 큼' = PPP 와 빅맥 결과가 1.3배 이상 달라 해석 주의."
    )


if __name__ == "__main__":
    asyncio.run(_demo())
