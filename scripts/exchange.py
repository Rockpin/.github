"""네이버 증권(stock.naver.com) 환율 비동기 조회 모듈.

HAR(stock.naver.com.har) 기준으로 확인한 공개 JSON API 를 감싼다. 로그인 불필요.

- 현재 환율: 전체 통화 고시환율(하나·신한), 통화별 상세(시가·고가·저가·52주)
- 과거 데이터: 일별 고시환율(날짜 커서로 원하는 날짜부터), 당일 회차별 고시, 차트(회차·1/3/6개월·1/3/5/10년)
- 주요 지표: 달러인덱스 등 주요 환율, 국제시장 환율(EUR/USD 등), 코스피·다우·금·WTI 등
- 뉴스: 환율 섹션(sid=429) 날짜별 기사 목록
- 아이콘: 국가 국기 SVG URL / 다운로드
- 계산기: 고시환율 기준 환전 계산(매매기준율·현찰·송금, 환전 우대율)

사용 예
    async with NaverExchange() as fx:
        usd = await fx.rate("USD")                         # 하나은행 USD 현재
        rows = await fx.history("JPY", start=date(2026, 9, 1), end=date(2026, 9, 30))
        news = await fx.news()
        calc = await fx.calculator()
        print(calc.convert(1_000_000, "KRW", "JPY", mode=ConvertMode.CASH, preferential=0.9))
"""

from __future__ import annotations

import asyncio
import base64
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, AsyncIterator, Iterable, Optional

import httpx

BASE_URL = "https://stock.naver.com"
RATES_PATH = "/api/stockSecurity/exchange-rates/v2"
MARKET_PATH = "/api/securityService/marketindex"
INDICATOR_PATH = "/api/securityService/integration/indicators"
NEWS_PATH = "/api/domestic/news/focus"
FLAG_URL = "https://ssl.pstatic.net/imgstock/fn/real/logo/flag/Nation{code}.svg"
NEWS_SECTION_ID = 429  # 뉴스 > 환율 섹션
MAX_PAGE_SIZE = 50  # 일별·회차 API 의 size 상한(51 이상은 400)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


# ──────────────────────────── Enums ────────────────────────────


class Bank(Enum):
    """고시 은행. API 가 받는 값은 hana / shinhan 두 가지뿐(그 외 400)."""

    HANA = "hana"
    SHINHAN = "shinhan"

    @property
    def label(self) -> str:
        return {Bank.HANA: "하나은행", Bank.SHINHAN: "신한은행"}[self]

    @property
    def code(self) -> str:
        """banksExchanges API 의 bankType 값."""
        return {Bank.HANA: "HNB", Bank.SHINHAN: "SHB"}[self]

    @property
    def suffix(self) -> str:
        """시장지표 코드 접미사 (FX_USDKRW / FX_USDKRW_SHB)."""
        return {Bank.HANA: "", Bank.SHINHAN: "_SHB"}[self]

    def market_index_code(self, currency: str) -> str:
        return f"FX_{currency.upper()}KRW{self.suffix}"

    @classmethod
    def from_value(cls, value: "str | Bank") -> "Bank":
        if isinstance(value, cls):
            return value
        v = str(value).strip()
        for b in cls:
            if v.lower() in (b.value, b.name.lower(), b.code.lower()) or v in (b.label, b.label[:2]):
                return b
        raise ValueError(f"알 수 없는 은행: {value!r} (hana / shinhan)")


class RateType(Enum):
    """고시환율 종류. value = API 필드명."""

    BASE = "saleBaseRate"  # 매매기준율
    CASH_BUY = "cashBuyingValue"  # 현찰 살 때
    CASH_SELL = "cashSellingValue"  # 현찰 팔 때
    SEND = "sendMoneyValue"  # 송금 보낼 때
    RECEIVE = "receiveMoneyValue"  # 송금 받을 때

    @property
    def label(self) -> str:
        return {
            RateType.BASE: "매매기준율",
            RateType.CASH_BUY: "현찰 살 때",
            RateType.CASH_SELL: "현찰 팔 때",
            RateType.SEND: "송금 보낼 때",
            RateType.RECEIVE: "송금 받을 때",
        }[self]


class ConvertMode(Enum):
    """계산기 적용 환율. CASH/REMIT 는 원화→외화면 '살 때/보낼 때', 외화→원화면 '팔 때/받을 때'."""

    BASE = "base"
    CASH = "cash"
    REMIT = "remit"

    @property
    def label(self) -> str:
        return {ConvertMode.BASE: "매매기준율", ConvertMode.CASH: "현찰", ConvertMode.REMIT: "송금"}[self]

    def rate_type(self, buying_foreign: bool) -> RateType:
        if self is ConvertMode.BASE:
            return RateType.BASE
        if self is ConvertMode.CASH:
            return RateType.CASH_BUY if buying_foreign else RateType.CASH_SELL
        return RateType.SEND if buying_foreign else RateType.RECEIVE


class ChartPeriod(Enum):
    """차트 기간. value = (경로, range)."""

    ROUND = ("round", None)  # 당일·전일 회차별
    MONTH_1 = ("month", 1)
    MONTH_3 = ("month", 3)
    MONTH_6 = ("month", 6)
    YEAR_1 = ("year", 1)
    YEAR_3 = ("year", 3)
    YEAR_5 = ("year", 5)
    YEAR_10 = ("year", 10)

    @property
    def label(self) -> str:
        kind, rng = self.value
        return "회차" if kind == "round" else f"{rng}{'개월' if kind == 'month' else '년'}"


class Fluctuation(Enum):
    RISING = "RISING"
    FALLING = "FALLING"
    UNCHANGED = "UNCHANGED"

    @property
    def symbol(self) -> str:
        return {Fluctuation.RISING: "▲", Fluctuation.FALLING: "▼", Fluctuation.UNCHANGED: "-"}[self]

    @classmethod
    def from_value(cls, value: Any) -> "Fluctuation":
        """'RISING' / {'name': 'FALLING'} / 숫자(부호) 모두 받는다."""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            value = value.get("name") or value.get("code")
        if isinstance(value, (int, float)):
            return cls.RISING if value > 0 else cls.FALLING if value < 0 else cls.UNCHANGED
        v = str(value or "").upper()
        if v in ("RISING", "UPPER_LIMIT", "1", "2"):
            return cls.RISING
        if v in ("FALLING", "LOWER_LIMIT", "4", "5"):
            return cls.FALLING
        return cls.UNCHANGED


class MarketStatus(Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    PREOPEN = "PREOPEN"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_value(cls, value: Any) -> "MarketStatus":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value or "").upper())
        except ValueError:
            return cls.UNKNOWN


class Indicator(Enum):
    """integration/indicators 에 넘기는 지표 코드."""

    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"
    KOSPI200 = "KPI200"
    DOW = ".DJI"
    SP500 = ".INX"
    NASDAQ = ".IXIC"
    USD_KRW = "FX_USDKRW"
    GOLD = "GCcv1"
    WTI = "CLcv1"


# ──────────────────────────── helpers ────────────────────────────


def _g(d: dict, snake: str, camel: Optional[str] = None, default: Any = None) -> Any:
    """to_dict() 결과(snake_case)와 API 원본(camelCase)을 모두 읽는다."""
    if snake in d:
        return d[snake]
    if camel and camel in d:
        return d[camel]
    return default


def _f(v: Any) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _i(v: Any) -> Optional[int]:
    x = _f(v)
    return int(x) if x is not None else None


def _dt(v: Any) -> Optional[datetime]:
    """ISO8601 / 'YYYYMMDDHHMMSS' / 'YYYYMMDD' 를 datetime 으로."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    s = str(v)
    if s.isdigit():
        fmt = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(s))
        return datetime.strptime(s, fmt) if fmt else None
    return datetime.fromisoformat(s)


def _d(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)
    return datetime.strptime(s[:8], "%Y%m%d").date() if s[:8].isdigit() else date.fromisoformat(s[:10])


def _date_cursor(d: date) -> str:
    """일별 조회 커서 = base64('YYYYMMDD') (패딩 제거). 이 날짜 '이전' 데이터부터 내려온다."""
    return base64.b64encode(d.strftime("%Y%m%d").encode()).decode().rstrip("=")


def flag_url(nation_code: str) -> str:
    """국기 아이콘(SVG) URL. 유로는 'EUR'."""
    return FLAG_URL.format(code=nation_code or "-")


def _jsonable(v: Any) -> Any:
    if isinstance(v, Enum):
        return v.value if not isinstance(v.value, tuple) else v.name
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


class _Model:
    def to_dict(self) -> dict:
        return _jsonable(asdict(self))  # type: ignore[call-overload]


# ──────────────────────────── 모델: 고시환율 ────────────────────────────


@dataclass
class ExchangeRate(_Model):
    """은행 고시환율 한 건(현재 · 일별 · 회차별 공통)."""

    currency: str
    market_index_code: str
    nation_name: str
    nation_code: str
    currency_name: str
    currency_name_en: Optional[str]
    basis: int  # 고시 단위 (JPY·VND·IDR 은 100)
    bank_name: str
    date: Optional[date]
    round: Optional[int]
    base_rate: Optional[float]
    change: Optional[float]
    change_rate: Optional[float]
    cash_buy: Optional[float]
    cash_sell: Optional[float]
    send: Optional[float]
    receive: Optional[float]
    announced_at: Optional[datetime]
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    market_status: MarketStatus = MarketStatus.UNKNOWN
    chart_image_url: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "ExchangeRate":
        return cls(
            currency=_g(d, "currency", "currencyCode"),
            market_index_code=_g(d, "market_index_code", "marketIndexCode", ""),
            nation_name=_g(d, "nation_name", "nationName", ""),
            nation_code=_g(d, "nation_code", "nationCode", ""),
            currency_name=_g(d, "currency_name", "currencyKoreanName", ""),
            currency_name_en=_g(d, "currency_name_en", "currencyEnglishName"),
            basis=_i(_g(d, "basis", "currencyBasis", 1)) or 1,
            bank_name=_g(d, "bank_name", "providerKoreanName", ""),
            date=_d(_g(d, "date")),
            round=_i(_g(d, "round")),
            base_rate=_f(_g(d, "base_rate", "saleBaseRate")),
            change=_f(_g(d, "change", "changePrice")),
            change_rate=_f(_g(d, "change_rate", "changeRate")),
            cash_buy=_f(_g(d, "cash_buy", "cashBuyingValue")),
            cash_sell=_f(_g(d, "cash_sell", "cashSellingValue")),
            send=_f(_g(d, "send", "sendMoneyValue")),
            receive=_f(_g(d, "receive", "receiveMoneyValue")),
            announced_at=_dt(_g(d, "announced_at", "announcedAt")),
            open=_f(_g(d, "open", "openingPrice")),
            high=_f(_g(d, "high", "highPriceOfDay")),
            low=_f(_g(d, "low", "lowPriceOfDay")),
            high_52w=_f(_g(d, "high_52w", "highPriceOf52Weeks")),
            low_52w=_f(_g(d, "low_52w", "lowPriceOf52Weeks")),
            market_status=MarketStatus.from_value(_g(d, "market_status", "marketStatus")),
            chart_image_url=_g(d, "chart_image_url", "month3ChartUrl") or _g(d, "chart_image_url", "homeChartImageUrl"),
        )

    @property
    def flag_url(self) -> str:
        return flag_url(self.nation_code)

    @property
    def fluctuation(self) -> Fluctuation:
        return Fluctuation.from_value(self.change or 0)

    def value(self, rate_type: RateType) -> Optional[float]:
        """고시 단위(basis) 기준 값. 예: JPY 는 100엔당 원."""
        return {
            RateType.BASE: self.base_rate,
            RateType.CASH_BUY: self.cash_buy,
            RateType.CASH_SELL: self.cash_sell,
            RateType.SEND: self.send,
            RateType.RECEIVE: self.receive,
        }[rate_type]

    def per_unit(self, rate_type: RateType = RateType.BASE) -> Optional[float]:
        """1 통화 단위당 원화. 예: JPY 845.33(100엔) → 8.4533."""
        v = self.value(rate_type)
        return v / self.basis if v is not None else None

    def spread_pct(self, rate_type: RateType) -> Optional[float]:
        """매매기준율 대비 스프레드(%)."""
        v = self.value(rate_type)
        if v is None or not self.base_rate:
            return None
        return (v - self.base_rate) / self.base_rate * 100

    def __str__(self) -> str:
        unit = f"{self.basis}{self.currency}" if self.basis != 1 else self.currency
        ch = f"{self.fluctuation.symbol}{abs(self.change or 0):,.2f} ({self.change_rate or 0:+.2f}%)"
        return (
            f"{self.nation_name} {self.currency_name}({unit}) {self.base_rate:,.2f}원 {ch} | "
            f"현찰 살때 {self.cash_buy:,.2f} · 팔때 {self.cash_sell:,.2f} | "
            f"송금 보낼때 {self.send:,.2f} · 받을때 {self.receive:,.2f} [{self.bank_name} {self.round}회차]"
        )


@dataclass
class RatePage(_Model):
    """커서 페이지(일별·회차별)."""

    items: list[ExchangeRate] = field(default_factory=list)
    has_next: bool = False
    cursor: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "RatePage":
        return cls(
            items=[ExchangeRate.from_dict(x) for x in d.get("items") or []],
            has_next=bool(_g(d, "has_next", "hasNext", False)),
            cursor=d.get("cursor"),
        )


# ──────────────────────────── 모델: 차트 ────────────────────────────


@dataclass
class ChartPoint(_Model):
    """기간 차트 한 점(일·주 단위)."""

    date: Optional[date]
    close: Optional[float]
    high: Optional[float]
    low: Optional[float]

    @classmethod
    def from_dict(cls, d: dict) -> "ChartPoint":
        return cls(
            date=_d(_g(d, "date", "localDate")),
            close=_f(_g(d, "close", "closingPrice")),
            high=_f(_g(d, "high", "highPrice")),
            low=_f(_g(d, "low", "lowPrice")),
        )


@dataclass
class RoundPoint(_Model):
    """회차 차트 한 점."""

    traded_at: Optional[datetime]
    announced_at: Optional[datetime]
    price: Optional[float]
    round: Optional[int]

    @classmethod
    def from_dict(cls, d: dict) -> "RoundPoint":
        return cls(
            traded_at=_dt(_g(d, "traded_at", "tradeBaseAt")),
            announced_at=_dt(_g(d, "announced_at", "announcedAt")),
            price=_f(_g(d, "price", "currentPrice")),
            round=_i(_g(d, "round")),
        )


@dataclass
class RateChart(_Model):
    """1·3·6개월 / 1·3·5·10년 차트."""

    currency: str
    bank: Bank
    period: ChartPeriod
    start: Optional[date]
    end: Optional[date]
    high: Optional[float]
    low: Optional[float]
    points: list[ChartPoint] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict, currency: str = "", bank: "Bank | str" = Bank.HANA, period: "ChartPeriod | str" = ChartPeriod.MONTH_1) -> "RateChart":
        p = _g(d, "period", default=period)
        return cls(
            currency=_g(d, "currency", default=currency),
            bank=Bank.from_value(_g(d, "bank", default=bank)),
            period=p if isinstance(p, ChartPeriod) else ChartPeriod[p],
            start=_d(_g(d, "start", "startDate")),
            end=_d(_g(d, "end", "endDate")),
            high=_f(_g(d, "high", "highPrice")),
            low=_f(_g(d, "low", "lowPrice")),
            points=[ChartPoint.from_dict(x) for x in _g(d, "points", "priceInfos", []) or []],
        )


@dataclass
class RoundChart(_Model):
    """당일(+전일) 회차별 차트."""

    currency: str
    bank: Bank
    trade_date: Optional[date]
    prev_trade_date: Optional[date]
    open: Optional[float]
    prev_close: Optional[float]
    points: list[RoundPoint] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict, currency: str = "", bank: "Bank | str" = Bank.HANA) -> "RoundChart":
        return cls(
            currency=_g(d, "currency", default=currency),
            bank=Bank.from_value(_g(d, "bank", default=bank)),
            trade_date=_d(_g(d, "trade_date", "tradeBaseAt")),
            prev_trade_date=_d(_g(d, "prev_trade_date", "lastTradeBaseAt")),
            open=_f(_g(d, "open", "openingPrice")),
            prev_close=_f(_g(d, "prev_close", "lastClosingPrice")),
            points=[RoundPoint.from_dict(x) for x in _g(d, "points", "priceInfos", []) or []],
        )

    @property
    def today(self) -> list[RoundPoint]:
        return [p for p in self.points if p.traded_at and self.trade_date and p.traded_at.date() == self.trade_date]


# ──────────────────────────── 모델: 시장지표 ────────────────────────────


@dataclass
class MarketItem(_Model):
    """주요 환율(달러인덱스 등) · 국제시장 환율(EUR/USD 등) · 은행 고시 목록 한 건."""

    code: str
    symbol: str
    name: str
    category: str
    close: Optional[float]
    change: Optional[float]
    change_rate: Optional[float]
    fluctuation: Fluctuation
    traded_at: Optional[datetime]
    market_status: MarketStatus = MarketStatus.UNKNOWN
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    high_52w: Optional[float] = None
    unit: Optional[str] = None
    delay: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None
    chart_urls: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketItem":
        charts = _g(d, "chart_urls", default=None)
        if charts is None:
            charts = ((d.get("imageChartUrlInfo") or {}).get("line")) or {}
        return cls(
            code=_g(d, "code", "reutersCode", ""),
            symbol=_g(d, "symbol", "symbolCode", ""),
            name=_g(d, "name", default=""),
            category=_g(d, "category", "categoryType", ""),
            close=_f(_g(d, "close", "closePrice")),
            change=_f(_g(d, "change", "fluctuations")),
            change_rate=_f(_g(d, "change_rate", "fluctuationsRatio")),
            fluctuation=Fluctuation.from_value(_g(d, "fluctuation", "fluctuationsType")),
            traded_at=_dt(_g(d, "traded_at", "localTradedAt")),
            market_status=MarketStatus.from_value(_g(d, "market_status", "marketStatus")),
            open=_f(_g(d, "open", "openPrice")),
            high=_f(_g(d, "high", "highPrice")),
            low=_f(_g(d, "low", "lowPrice")),
            high_52w=_f(_g(d, "high_52w", "highPriceOf52Weeks")),
            unit=_g(d, "unit"),
            delay=_g(d, "delay", "delayTimeName"),
            source=_g(d, "source", "statementSource") or _g(d, "source", "description"),
            url=_g(d, "url", "newPcUrl") or _g(d, "url", "endUrl"),
            chart_urls=dict(charts),
        )


@dataclass
class IndicatorQuote(_Model):
    """코스피·다우·USD/KRW·금·WTI 등 통합 지표."""

    code: str
    name: str
    price: Optional[float]
    change: Optional[float]
    change_rate: Optional[float]
    fluctuation: Fluctuation
    prev_close: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    high_52w: Optional[float]
    low_52w: Optional[float]
    traded_at: Optional[datetime]
    market_status: MarketStatus = MarketStatus.UNKNOWN
    nation_name: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "IndicatorQuote":
        return cls(
            code=_g(d, "code", "reutersCode") or _g(d, "code", "itemCode", ""),
            name=_g(d, "name", "stockName") or _g(d, "name", "indexName", ""),
            price=_f(_g(d, "price", "currentPrice")) if _g(d, "price", "currentPrice") is not None else _f(_g(d, "price", "closePrice")),
            change=_f(_g(d, "change", "fluctuations")),
            change_rate=_f(_g(d, "change_rate", "fluctuationsRatio")),
            fluctuation=Fluctuation.from_value(_g(d, "fluctuation", "fluctuationsType")),
            prev_close=_f(_g(d, "prev_close", "lastClosePrice")),
            open=_f(_g(d, "open", "openPrice")),
            high=_f(_g(d, "high", "highPrice")),
            low=_f(_g(d, "low", "lowPrice")),
            high_52w=_f(_g(d, "high_52w", "highPriceOf52Weeks")),
            low_52w=_f(_g(d, "low_52w", "lowPriceOf52Weeks")),
            traded_at=_dt(_g(d, "traded_at", "localTradedAt")),
            market_status=MarketStatus.from_value(_g(d, "market_status", "marketStatus")),
            nation_name=_g(d, "nation_name", "nationName"),
        )


# ──────────────────────────── 모델: 뉴스 ────────────────────────────


@dataclass
class NewsArticle(_Model):
    office_id: str
    office_name: str
    article_id: str
    title: str
    summary: str
    published_at: Optional[datetime]
    url: str
    thumbnails: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "NewsArticle":
        return cls(
            office_id=_g(d, "office_id", "officeID", ""),
            office_name=_g(d, "office_name", "officeHName", ""),
            article_id=_g(d, "article_id", "articleID", ""),
            title=_g(d, "title", default=""),
            summary=_g(d, "summary", "subcontent", ""),
            published_at=_dt(_g(d, "published_at", "date")),
            url=_g(d, "url", default=""),
            thumbnails=dict(_g(d, "thumbnails", default={}) or {}),
        )

    @property
    def thumbnail(self) -> Optional[str]:
        """가장 큰 썸네일."""
        if not self.thumbnails:
            return None
        return self.thumbnails[max(self.thumbnails, key=lambda k: int("".join(c for c in k if c.isdigit()) or 0))]


@dataclass
class NewsPage(_Model):
    date: Optional[date]
    page: int
    page_size: int
    articles: list[NewsArticle] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "NewsPage":
        return cls(
            date=_d(_g(d, "date")),
            page=_i(_g(d, "page")) or 1,
            page_size=_i(_g(d, "page_size", "pageSize")) or 0,
            articles=[NewsArticle.from_dict(x) for x in d.get("articles") or []],
        )


# ──────────────────────────── 계산기 ────────────────────────────


@dataclass
class Conversion(_Model):
    amount: float
    source: str
    target: str
    result: float
    mode: ConvertMode
    preferential: float  # 환전 우대율 0~1 (0.9 = 90% 우대)
    legs: list[dict] = field(default_factory=list)  # 통화별 적용 환율 내역

    @classmethod
    def from_dict(cls, d: dict) -> "Conversion":
        return cls(
            amount=float(d["amount"]),
            source=d["source"],
            target=d["target"],
            result=float(d["result"]),
            mode=ConvertMode(d["mode"]),
            preferential=float(d.get("preferential", 0)),
            legs=list(d.get("legs") or []),
        )

    def __str__(self) -> str:
        pref = f", 우대 {self.preferential:.0%}" if self.preferential else ""
        # 1 단위당 환율: 100원 이상은 소수 2자리, 미만(JPY 8.6017·VND 0.0516 등)은 4자리
        legs = " / ".join(
            f"{x['currency']} {x['rate_type']} {x['applied']:,.{2 if x['applied'] >= 100 else 4}f}원" for x in self.legs
        )
        return f"{self.amount:,.2f} {self.source} → {self.result:,.2f} {self.target} ({self.mode.label}{pref}; {legs})"


def _round_sig(x: float, sig: int) -> float:
    if x == 0:
        return 0.0
    from math import floor, log10

    return round(x, -int(floor(log10(abs(x)))) + (sig - 1))


def _num_text(x: float) -> str:
    """암산식용 숫자 표기: 1340 → '1,340', 8.5 → '8.5'."""
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,g}"


def mental_rule(krw_per_unit: float, unit_name: str, max_error: float = 0.02) -> tuple[str, float]:
    """1 단위당 원화 환율로 암산식을 만든다. 오차 max_error(기본 2%) 안에서 가장 짧은 숫자.

    - 1원 이상(달러·엔 등):  '달러 × 1,340 ≈ 원'
    - 1원 미만(동·루피아 등): '동에서 0 두 개 떼고 × 5.2 ≈ 원' (여행지에서 흔히 쓰는 방식)
    반환: (식, 실제 환율 대비 오차 % — 양수면 식이 원화를 조금 크게 셈)
    """
    from math import ceil, log10

    zeros = ceil(-log10(krw_per_unit)) if krw_per_unit < 1 else 0  # 0.0516 → 2
    target = krw_per_unit * 10**zeros  # 5.16
    n = target
    for sig in (1, 2, 3, 4):
        n = _round_sig(target, sig)
        if abs(n - target) / target <= max_error:
            break
    err = (n - target) / target * 100
    if zeros:
        count = {1: "하나", 2: "두 개", 3: "세 개", 4: "네 개"}.get(zeros, f"{zeros}개")
        return f"{unit_name}에서 0 {count} 떼고 × {_num_text(n)} ≈ 원", err
    return f"{unit_name} × {_num_text(n)} ≈ 원", err


@dataclass
class QuickRate(_Model):
    """원화 일정액(기본 1,000원)이 외화로 얼마인지 + 암산식."""

    currency: str
    nation_name: str
    unit_name: str
    krw: float
    foreign_amount: float
    krw_per_unit: float
    rule: str
    rule_error_pct: float
    rate_type: RateType = RateType.BASE

    @classmethod
    def from_dict(cls, d: dict) -> "QuickRate":
        rt = d.get("rate_type", RateType.BASE.value)
        return cls(
            currency=d["currency"],
            nation_name=d.get("nation_name", ""),
            unit_name=d.get("unit_name", ""),
            krw=float(d["krw"]),
            foreign_amount=float(d["foreign_amount"]),
            krw_per_unit=float(d["krw_per_unit"]),
            rule=d.get("rule", ""),
            rule_error_pct=float(d.get("rule_error_pct", 0)),
            rate_type=rt if isinstance(rt, RateType) else RateType(rt),
        )

    @property
    def amount_text(self) -> str:
        a = self.foreign_amount
        digits = 0 if a >= 100 else 2 if a >= 1 else 3
        return f"{a:,.{digits}f} {self.currency}"


class ExchangeCalculator:
    """고시환율로 환전 금액을 계산한다(네트워크 없음).

    - 원화→외화: 살 때(현찰) / 보낼 때(송금) 적용, 외화→원화: 팔 때 / 받을 때 적용
    - 외화→외화: 원화를 거쳐 계산
    - 우대율 p: 적용환율 = 매매기준율 ± 스프레드 × (1 - p)
    """

    def __init__(self, rates: Iterable[ExchangeRate]):
        self.rates = {r.currency: r for r in rates}

    def __contains__(self, currency: str) -> bool:
        return currency.upper() in self.rates or currency.upper() == "KRW"

    def applied_rate(self, currency: str, rate_type: RateType, preferential: float = 0.0) -> float:
        """1 통화 단위당 적용 원화 환율."""
        r = self.rates.get(currency.upper())
        if r is None:
            raise KeyError(f"환율 없음: {currency}")
        base = r.per_unit(RateType.BASE)
        val = r.per_unit(rate_type)
        if base is None or val is None:
            raise ValueError(f"{currency} {rate_type.label} 고시값 없음")
        return base + (val - base) * (1 - preferential)

    def convert(
        self,
        amount: float,
        source: str,
        target: str,
        *,
        mode: ConvertMode = ConvertMode.BASE,
        preferential: float = 0.0,
    ) -> Conversion:
        src, dst = source.upper(), target.upper()
        if not 0 <= preferential <= 1:
            raise ValueError("preferential 은 0~1 (예: 0.9 = 90% 우대)")
        legs: list[dict] = []
        krw = float(amount)
        if src != "KRW":  # 외화 → 원화 (팔 때)
            rt = mode.rate_type(buying_foreign=False)
            rate = self.applied_rate(src, rt, preferential)
            krw = amount * rate
            legs.append({"currency": src, "rate_type": rt.label, "applied": rate})
        result = krw
        if dst != "KRW":  # 원화 → 외화 (살 때)
            rt = mode.rate_type(buying_foreign=True)
            rate = self.applied_rate(dst, rt, preferential)
            result = krw / rate
            legs.append({"currency": dst, "rate_type": rt.label, "applied": rate})
        return Conversion(amount, src, dst, round(result, 4), mode, preferential, legs)

    def quick(self, currency: str, krw: float = 1000, rate_type: RateType = RateType.BASE) -> QuickRate:
        """krw 원이 외화로 얼마인지 + 암산식."""
        r = self.rates.get(currency.upper())
        if r is None:
            raise KeyError(f"환율 없음: {currency}")
        per_unit = self.applied_rate(currency, rate_type)
        rule, err = mental_rule(per_unit, r.currency_name or r.currency)
        return QuickRate(r.currency, r.nation_name, r.currency_name, krw, krw / per_unit, per_unit, rule, err, rate_type)

    def quick_table(
        self, currencies: Optional[Iterable[str]] = None, krw: float = 1000, rate_type: RateType = RateType.BASE
    ) -> list[QuickRate]:
        """여러 통화 한 번에. currencies 를 생략하면 고시된 전체 통화."""
        codes = [c.upper() for c in currencies] if currencies else list(self.rates)
        return [self.quick(c, krw, rate_type) for c in codes if c in self.rates]

    def pretty_quick(
        self, currencies: Optional[Iterable[str]] = None, krw: float = 1000, rate_type: RateType = RateType.BASE
    ) -> str:
        rows = self.quick_table(currencies, krw, rate_type)
        head = ["통화", f"{krw:,.0f}원 =", "암산식(외화 → 원, 대략)", "식 오차"]
        body = [[f"{q.nation_name} {q.unit_name}", q.amount_text, q.rule, f"{q.rule_error_pct:+.1f}%"] for q in rows]
        return _text_table(head, body, right=(1, 3))


def _width(s: str) -> int:
    """터미널 표시 폭(한글 등 전각 문자는 2칸)."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _text_table(headers: list[str], rows: list[list[str]], right: Iterable[int] = (), indent: str = "  ") -> str:
    right = set(right)
    widths = [max(_width(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]

    def line(r: list[str]) -> str:
        cells = []
        for i, c in enumerate(r):
            gap = " " * (widths[i] - _width(str(c)))
            cells.append(gap + str(c) if i in right else str(c) + gap)
        return indent + "  ".join(cells).rstrip()

    sep = indent + "  ".join("─" * w for w in widths)
    return "\n".join([line(headers), sep, *(line(r) for r in rows)])


# ──────────────────────────── 클라이언트 ────────────────────────────


class NaverExchange:
    """네이버 증권 환율 비동기 클라이언트. `async with` 로 사용하거나 close() 호출."""

    def __init__(
        self,
        *,
        bank: "Bank | str" = Bank.HANA,
        timeout: float = 15.0,
        concurrency: int = 5,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.bank = Bank.from_value(bank)
        self._own = client is None
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Referer": BASE_URL + "/market/marketindex/exchangeRate/exchange"},
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(concurrency)

    async def __aenter__(self) -> "NaverExchange":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own:
            await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict] = None, retries: int = 2) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        async with self._sem:
            for attempt in range(retries + 1):
                try:
                    r = await self._client.get(path, params=params)
                    if r.status_code == 400:
                        raise ValueError(f"잘못된 요청 {path} {params}: {r.text[:200]}")
                    r.raise_for_status()
                    return r.json()
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if attempt == retries:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError("unreachable")

    def _bank(self, bank: "Bank | str | None") -> Bank:
        return Bank.from_value(bank) if bank is not None else self.bank

    # ── 현재 ──

    async def rates(self, bank: "Bank | str | None" = None) -> list[ExchangeRate]:
        """전체 통화(57개) 현재 고시환율."""
        data = await self._get(f"{RATES_PATH}/latest", {"bankType": self._bank(bank).value})
        return [ExchangeRate.from_dict(x) for x in data.get("items") or []]

    async def rate(self, currency: str, bank: "Bank | str | None" = None) -> ExchangeRate:
        """통화 하나 현재 고시환율 + 당일 시가·고가·저가·52주 고저."""
        b = self._bank(bank)
        data = await self._get(f"{RATES_PATH}/market-index/{b.market_index_code(currency)}/latest")
        return ExchangeRate.from_dict(data)

    async def bank_board(self, bank: "Bank | str | None" = None) -> list[MarketItem]:
        """은행 고시 전광판(통화별 현재가·등락·고시회차)."""
        data = await self._get(f"{MARKET_PATH}/exchange/banksExchanges", {"bankType": self._bank(bank).code})
        return [MarketItem.from_dict(x) for x in data]

    async def majors(self) -> list[MarketItem]:
        """주요 환율(달러인덱스 · USD/KRW 등)."""
        return [MarketItem.from_dict(x) for x in await self._get(f"{MARKET_PATH}/majors/exchange")]

    async def world_rates(self) -> list[MarketItem]:
        """국제시장 환율(EUR/USD · USD/JPY 등, 레피니티브 기준)."""
        return [MarketItem.from_dict(x) for x in await self._get(f"{MARKET_PATH}/majors/exchangeWorld")]

    async def indicators(self, codes: Iterable["Indicator | str"] = tuple(Indicator)) -> list[IndicatorQuote]:
        """코스피·다우·USD/KRW·금·WTI 등 통합 지표."""
        joined = ",".join(c.value if isinstance(c, Indicator) else str(c) for c in codes)
        data = await self._get(INDICATOR_PATH, {"indicatorCodes": joined})
        return [IndicatorQuote.from_dict(x) for x in data]

    # ── 과거 ──

    async def rounds_page(
        self, currency: str, bank: "Bank | str | None" = None, size: int = 20, cursor: Optional[str] = None
    ) -> RatePage:
        """당일 회차별 고시(최신 회차부터)."""
        params = {"bankType": self._bank(bank).value, "size": min(size, MAX_PAGE_SIZE), "cursor": cursor}
        return RatePage.from_dict(await self._get(f"{RATES_PATH}/{currency.upper()}", params))

    async def rounds(self, currency: str, bank: "Bank | str | None" = None) -> list[ExchangeRate]:
        """당일 전체 회차."""
        out: list[ExchangeRate] = []
        cursor = None
        while True:
            page = await self.rounds_page(currency, bank, size=MAX_PAGE_SIZE, cursor=cursor)
            out += page.items
            if not page.has_next or not page.cursor:
                return out
            cursor = page.cursor

    async def daily_page(
        self,
        currency: str,
        bank: "Bank | str | None" = None,
        size: int = 20,
        cursor: Optional[str] = None,
        before: Optional[date] = None,
    ) -> RatePage:
        """일별 고시(최신일부터). before 를 주면 그 날짜 '이전' 데이터부터."""
        if before and not cursor:
            cursor = _date_cursor(before)
        params = {"bankType": self._bank(bank).value, "size": min(size, MAX_PAGE_SIZE), "cursor": cursor}
        return RatePage.from_dict(await self._get(f"{RATES_PATH}/{currency.upper()}/daily", params))

    async def iter_daily(
        self, currency: str, bank: "Bank | str | None" = None, *, start: Optional[date] = None, end: Optional[date] = None
    ) -> AsyncIterator[ExchangeRate]:
        """end(포함)부터 start(포함)까지 일별 고시를 최신순으로."""
        before = (end + timedelta(days=1)) if end else None
        cursor = None
        while True:
            page = await self.daily_page(currency, bank, size=MAX_PAGE_SIZE, cursor=cursor, before=None if cursor else before)
            for r in page.items:
                if start and r.date and r.date < start:
                    return
                yield r
            if not page.has_next or not page.cursor:
                return
            cursor = page.cursor

    async def history(
        self,
        currency: str,
        bank: "Bank | str | None" = None,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
        days: Optional[int] = None,
    ) -> list[ExchangeRate]:
        """기간 일별 고시(과거→최신 순). days=30 처럼 최근 N일도 가능."""
        if days and not start:
            start = (end or date.today()) - timedelta(days=days)
        rows = [r async for r in self.iter_daily(currency, bank, start=start, end=end)]
        return list(reversed(rows))

    async def rate_on(self, currency: str, day: date, bank: "Bank | str | None" = None) -> Optional[ExchangeRate]:
        """특정 날짜의 고시(그날 고시가 없으면 직전 영업일)."""
        page = await self.daily_page(currency, bank, size=1, before=day + timedelta(days=1))
        return page.items[0] if page.items else None

    async def chart(
        self, currency: str, period: ChartPeriod = ChartPeriod.MONTH_3, bank: "Bank | str | None" = None
    ) -> "RateChart | RoundChart":
        b = self._bank(bank)
        kind, rng = period.value
        data = await self._get(f"{RATES_PATH}/{currency.upper()}/charts/{kind}", {"bankType": b.value, "range": rng})
        if kind == "round":
            return RoundChart.from_dict(data, currency=currency.upper(), bank=b)
        return RateChart.from_dict(data, currency=currency.upper(), bank=b, period=period)

    # ── 뉴스 ──

    async def news(self, day: Optional[date] = None, page: int = 1, page_size: int = 15) -> NewsPage:
        """환율 뉴스(날짜별). 그날 기사가 없으면 서버가 가까운 날짜로 대체(enableFallback)."""
        day = day or date.today()
        params = {
            "sid": NEWS_SECTION_ID,
            "page": page,
            "pageSize": page_size,
            "date": day.strftime("%Y%m%d"),
            "enableFallback": "true",
        }
        return NewsPage.from_dict(await self._get(NEWS_PATH, params))

    async def news_all(self, day: Optional[date] = None, page_size: int = 15, max_pages: int = 10) -> list[NewsArticle]:
        """그날 기사 전부(페이지를 끝까지)."""
        out: list[NewsArticle] = []
        seen: set[str] = set()
        for p in range(1, max_pages + 1):
            page = await self.news(day, p, page_size)
            new = [a for a in page.articles if a.article_id not in seen]
            seen.update(a.article_id for a in new)
            out += new
            if len(page.articles) < page_size or not new:
                break
        return out

    # ── 아이콘 ──

    @staticmethod
    def flag_url(nation_code: str) -> str:
        return flag_url(nation_code)

    async def flag_svg(self, nation_code: str) -> bytes:
        """국기 SVG 바이트."""
        async with self._sem:
            r = await self._client.get(flag_url(nation_code))
            r.raise_for_status()
            return r.content

    # ── 계산기 ──

    async def calculator(self, bank: "Bank | str | None" = None) -> ExchangeCalculator:
        """현재 고시환율로 만든 계산기."""
        return ExchangeCalculator(await self.rates(bank))

    async def convert(
        self,
        amount: float,
        source: str,
        target: str,
        *,
        mode: ConvertMode = ConvertMode.BASE,
        preferential: float = 0.0,
        bank: "Bank | str | None" = None,
    ) -> Conversion:
        return (await self.calculator(bank)).convert(amount, source, target, mode=mode, preferential=preferential)


# ──────────────────────────── 실행 예시 ────────────────────────────


async def _demo() -> None:
    getattr(sys.stdout, "reconfigure", lambda **_: None)(encoding="utf-8")
    async with NaverExchange() as fx:
        usd, jpy = await asyncio.gather(fx.rate("USD"), fx.rate("JPY"))
        print("💱 현재")
        print(" ", usd)
        print(" ", jpy, jpy.flag_url)

        print("\n🧮 계산기")
        calc = await fx.calculator()
        print(" ", calc.convert(1_000_000, "KRW", "JPY", mode=ConvertMode.CASH))
        print(" ", calc.convert(1_000_000, "KRW", "JPY", mode=ConvertMode.CASH, preferential=0.9))
        print(" ", calc.convert(500, "USD", "THB", mode=ConvertMode.REMIT))

        print("\n💴 1,000원이면? (매매기준율)")
        travel = ["USD", "JPY", "EUR", "CNY", "TWD", "HKD", "THB", "VND", "PHP", "SGD", "MYR", "IDR", "AUD", "GBP"]
        print(calc.pretty_quick(travel))

        print("\n📅 USD 최근 5영업일")
        for r in (await fx.history("USD", days=7))[-5:]:
            print(f"  {r.date} {r.base_rate:,.2f} ({r.change_rate:+.2f}%)")
        past = await fx.rate_on("USD", date(2020, 3, 19))
        print(f"  2020-03-19 → {past.date} {past.base_rate:,.2f}" if past else "  2020-03-19 없음")

        chart = await fx.chart("USD", ChartPeriod.YEAR_1)
        assert isinstance(chart, RateChart)
        print(f"\n📈 USD 1년 차트 {chart.start}~{chart.end} 고 {chart.high} 저 {chart.low} ({len(chart.points)}점)")

        print("\n🌐 주요 지표")
        for m in await fx.majors():
            print(f"  {m.name} {m.close:,} {m.fluctuation.symbol}{abs(m.change_rate or 0):.2f}%")

        print("\n📰 환율 뉴스")
        for a in (await fx.news(page_size=5)).articles:
            print(f"  [{a.office_name}] {a.title} ({a.published_at:%m-%d %H:%M})")


if __name__ == "__main__":
    asyncio.run(_demo())
