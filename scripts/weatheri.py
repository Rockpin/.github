"""웨더아이(weatheri.co.kr) 세계 날씨 비동기 조회 모듈.

페이지(HAR 기준)
- 세계 날씨: /forecast/world/asia_n.php?selO=<대륙>&selN=<국가>&selC=<도시>
  · selO/selN/selC 세 값이 모두 맞아야 해당 도시가 나온다(아니면 기본 도시 쿠알라룸푸르로 대체).
  · 10일 일별 요약 + 일자별 3시간 간격 상세 + 좌표 + 월별 기후값.
  · 주의: 웨더아이 월별 기후값은 최신 공식 평년값(1991–2020)과 다르다. 일본 기상청과 대조한 10월 강수량:
    도쿄 197.8 vs 234.8mm, 오사카 112.3 vs 136.0mm, 후쿠오카 73.7 vs 94.5mm (도쿄·오사카 값은 1981–2010 평년값과 일치).
    도시 간 상대 비교에는 쓸 만하지만 절대값은 공식 기관 자료로 확인할 것.
- 세계기후(월별 평년값): /bygone/bygone12.php?selL=<권역> | ?selN=<국가> | ?selC=<도시>
  · 예보와 별개인 자체 도시 ID(333곳). 월별 평균/최고/최저기온 · 강수량 + 연간값.

사용 예
    async with WeatheriClient() as wx:
        cities = await wx.get_cities(Continent.ASIA, "JP")
        tokyo = await wx.get_city_weather(Continent.ASIA, "JP", cities[0].city_id)
        print(tokyo.pretty(hourly=True))

        normals = await wx.get_climate_normals_by_name("도쿄")
        print(normals.pretty())
"""

from __future__ import annotations

import asyncio
import html
import os
import sys
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Optional
from urllib.parse import quote, unquote

import httpx

BASE_URL = "https://www.weatheri.co.kr"
WORLD_PATH = "/forecast/world/asia_n.php"
CLIMATE_PATH = "/bygone/bygone12.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


# ──────────────────────────── Enums ────────────────────────────


class Continent(Enum):
    """세계 날씨 대륙 선택값(selO). value 는 사이트 파라미터 그대로(공백 포함)."""

    ASIA = "Asia"
    MIDDLE_EAST = "Middle"
    AFRICA = "AFRICA"
    EUROPE = "Europe"
    SOUTH_AMERICA = "AMERICA"
    NORTH_AMERICA = "NORTH  AMERICA"
    OCEANIA = "Australia"
    ANTARCTICA = "STATIONS IN THE ANTARCTIC"

    @property
    def label(self) -> str:
        return _CONTINENT_LABELS[self]

    @classmethod
    def from_value(cls, value: "str | Continent") -> "Continent":
        if isinstance(value, cls):
            return value
        v = str(value).strip()
        for c in cls:
            if v in (c.value, c.name, c.label) or v.upper() == c.value.upper():
                return c
        raise ValueError(f"알 수 없는 대륙: {value!r}")


_CONTINENT_LABELS = {
    Continent.ASIA: "아시아",
    Continent.MIDDLE_EAST: "중동",
    Continent.AFRICA: "아프리카",
    Continent.EUROPE: "유럽",
    Continent.SOUTH_AMERICA: "남아메리카",
    Continent.NORTH_AMERICA: "북아메리카",
    Continent.OCEANIA: "오세아니아",
    Continent.ANTARCTICA: "남극",
}


class ClimateRegion(Enum):
    """세계기후(월별 평년값) 권역 선택값(selL). 예보의 Continent 와 구분이 다르다."""

    FAR_EAST_ASIA = "Far East Asia"
    SOUTH_EAST_ASIA = "South East Asia"
    EASTERN_EUROPE = "Eastern Europe"
    RUSSIA = "Russia"
    NORTH_AMERICA = "North America"
    SCANDINAVIA = "Scandinavian Europe"
    WESTERN_EUROPE = "Western Europe"
    AFRICA = "Africa"
    OCEANIA = "Australia & New Zealand"
    LATIN_AMERICA = "Latin America"
    MIDDLE_EAST = "Middle East"
    PACIFIC_ISLANDS = "Pacific Islands"

    @property
    def label(self) -> str:
        return _REGION_LABELS[self]

    @classmethod
    def from_value(cls, value: "str | ClimateRegion") -> "ClimateRegion":
        if isinstance(value, cls):
            return value
        v = unquote(str(value)).strip()
        for r in cls:
            if v in (r.value, r.name, r.label) or v.lower() == r.value.lower():
                return r
        raise ValueError(f"알 수 없는 권역: {value!r}")


_REGION_LABELS = {
    ClimateRegion.FAR_EAST_ASIA: "극동아시아",
    ClimateRegion.SOUTH_EAST_ASIA: "동남아시아",
    ClimateRegion.EASTERN_EUROPE: "동유럽",
    ClimateRegion.RUSSIA: "러시아",
    ClimateRegion.NORTH_AMERICA: "북미",
    ClimateRegion.SCANDINAVIA: "북유럽",
    ClimateRegion.WESTERN_EUROPE: "서유럽",
    ClimateRegion.AFRICA: "아프리카",
    ClimateRegion.OCEANIA: "오세아니아",
    ClimateRegion.LATIN_AMERICA: "중남미",
    ClimateRegion.MIDDLE_EAST: "중동",
    ClimateRegion.PACIFIC_ISLANDS: "태평양제도",
}


class WeatherIcon(Enum):
    """웨더아이 날씨 아이콘(/images/icon_2013_01/NN.png).

    01·03·04·21 은 사이트 alt 문구로 확인, 나머지는 아이콘 그림 기준 해석.
    11~20 은 '앞 날씨 → 뒤 날씨' 변화형 아이콘.
    """

    CLEAR = "01"
    PARTLY_CLOUDY = "02"
    OVERCAST = "03"
    RAIN = "04"
    SNOW = "05"
    RAIN_SNOW = "06"
    SHOWER = "07"
    SNOW_SHOWER = "08"
    FOG = "09"
    THUNDERSTORM = "10"
    CLEAR_THEN_CLOUDY = "11"
    CLEAR_THEN_THUNDER = "12"
    CLEAR_THEN_RAIN = "13"
    CLEAR_THEN_SNOW = "14"
    CLEAR_THEN_RAIN_SNOW = "15"
    CLOUDY_THEN_CLEAR = "16"
    THUNDER_THEN_CLEAR = "17"
    RAIN_THEN_CLEAR = "18"
    SNOW_THEN_CLEAR = "19"
    RAIN_SNOW_THEN_CLEAR = "20"
    MOSTLY_CLOUDY = "21"
    YELLOW_DUST = "22"
    UNKNOWN = "00"

    @property
    def label(self) -> str:
        return _ICON_LABELS.get(self, "알 수 없음")

    @property
    def url(self) -> str:
        return f"{BASE_URL}/images/icon_2013_01/{self.value}.png"

    @property
    def emoji(self) -> str:
        return _ICON_EMOJI.get(self, "❔")

    @property
    def display(self) -> str:
        """이모지 + 한글 (표 출력용)."""
        return f"{self.emoji} {self.label}"

    @classmethod
    def from_value(cls, value: "str | int | WeatherIcon | None") -> "WeatherIcon":
        if isinstance(value, cls):
            return value
        if value is None or value == "":
            return cls.UNKNOWN
        m = re.search(r"(\d+)", str(value))
        if not m:
            try:
                return cls[str(value)]
            except KeyError:
                return cls.UNKNOWN
        try:
            return cls(m.group(1).zfill(2))
        except ValueError:
            return cls.UNKNOWN

    @classmethod
    def from_src(cls, src: str) -> "WeatherIcon":
        m = re.search(r"icon_2013_01/(\d+)\.png", src or "")
        return cls.from_value(m.group(1) if m else None)


_ICON_EMOJI = {
    WeatherIcon.CLEAR: "☀️",
    WeatherIcon.PARTLY_CLOUDY: "🌤️",
    WeatherIcon.OVERCAST: "☁️",
    WeatherIcon.RAIN: "🌧️",
    WeatherIcon.SNOW: "❄️",
    WeatherIcon.RAIN_SNOW: "🌨️",
    WeatherIcon.SHOWER: "🌦️",
    WeatherIcon.SNOW_SHOWER: "🌨️",
    WeatherIcon.FOG: "🌫️",
    WeatherIcon.THUNDERSTORM: "⛈️",
    WeatherIcon.CLEAR_THEN_CLOUDY: "🌤️",
    WeatherIcon.CLEAR_THEN_THUNDER: "🌩️",
    WeatherIcon.CLEAR_THEN_RAIN: "🌦️",
    WeatherIcon.CLEAR_THEN_SNOW: "🌨️",
    WeatherIcon.CLEAR_THEN_RAIN_SNOW: "🌨️",
    WeatherIcon.CLOUDY_THEN_CLEAR: "🌤️",
    WeatherIcon.THUNDER_THEN_CLEAR: "🌤️",
    WeatherIcon.RAIN_THEN_CLEAR: "🌤️",
    WeatherIcon.SNOW_THEN_CLEAR: "🌤️",
    WeatherIcon.RAIN_SNOW_THEN_CLEAR: "🌤️",
    WeatherIcon.MOSTLY_CLOUDY: "🌥️",
    WeatherIcon.YELLOW_DUST: "😷",
    WeatherIcon.UNKNOWN: "❔",
}

_ICON_LABELS = {
    WeatherIcon.CLEAR: "맑음",
    WeatherIcon.PARTLY_CLOUDY: "구름조금",
    WeatherIcon.OVERCAST: "흐림",
    WeatherIcon.RAIN: "비",
    WeatherIcon.SNOW: "눈",
    WeatherIcon.RAIN_SNOW: "비/눈",
    WeatherIcon.SHOWER: "소나기",
    WeatherIcon.SNOW_SHOWER: "한때 눈",
    WeatherIcon.FOG: "안개",
    WeatherIcon.THUNDERSTORM: "뇌우",
    WeatherIcon.CLEAR_THEN_CLOUDY: "맑은 후 흐림",
    WeatherIcon.CLEAR_THEN_THUNDER: "맑은 후 뇌우",
    WeatherIcon.CLEAR_THEN_RAIN: "맑은 후 비",
    WeatherIcon.CLEAR_THEN_SNOW: "맑은 후 눈",
    WeatherIcon.CLEAR_THEN_RAIN_SNOW: "맑은 후 비/눈",
    WeatherIcon.CLOUDY_THEN_CLEAR: "흐린 후 맑음",
    WeatherIcon.THUNDER_THEN_CLEAR: "뇌우 후 맑음",
    WeatherIcon.RAIN_THEN_CLEAR: "비 온 후 맑음",
    WeatherIcon.SNOW_THEN_CLEAR: "눈 온 후 맑음",
    WeatherIcon.RAIN_SNOW_THEN_CLEAR: "비/눈 후 맑음",
    WeatherIcon.MOSTLY_CLOUDY: "구름많음",
    WeatherIcon.YELLOW_DUST: "황사",
}


class WindDirection(Enum):
    """16방위 풍향. value = 한글 표기."""

    N = "북"
    NNE = "북북동"
    NE = "북동"
    ENE = "동북동"
    E = "동"
    ESE = "동남동"
    SE = "남동"
    SSE = "남남동"
    S = "남"
    SSW = "남남서"
    SW = "남서"
    WSW = "서남서"
    W = "서"
    WNW = "서북서"
    NW = "북서"
    NNW = "북북서"
    CALM = "정온"
    VARIABLE = "가변"

    @property
    def degrees(self) -> Optional[float]:
        order = list(WindDirection)[:16]
        return order.index(self) * 22.5 if self in order else None

    @classmethod
    def from_degrees(cls, deg: Optional[float]) -> Optional["WindDirection"]:
        if deg is None:
            return None
        return list(cls)[:16][int(((deg % 360) + 11.25) // 22.5) % 16]

    @classmethod
    def from_value(cls, value: "str | WindDirection | None") -> Optional["WindDirection"]:
        if value is None or isinstance(value, cls):
            return value
        v = str(value).strip()
        if not v:
            return None
        for d in cls:
            if v in (d.value, d.name):
                return d
        if v.upper() in ("VRB", "VAR"):
            return cls.VARIABLE
        return None


# ──────────────────────────── helpers ────────────────────────────


def _text(fragment: str) -> str:
    """태그 제거 + 엔티티 디코드 + 공백 정리."""
    t = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def _num(value: Any, cast=float) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return cast(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return cast(float(m.group(0))) if m else None


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _infer_date(month: Optional[int], day: int, today: Optional[date] = None) -> date:
    """연도(필요 시 월)가 없는 날짜를 오늘 기준 가장 가까운 날짜로 보정."""
    today = today or date.today()
    if month is None:
        month = today.month
        if day < today.day - 15:  # 월이 바뀐 경우(예: 31일 → 1일)
            month = month % 12 + 1
    year = today.year
    if month < today.month - 6:  # 12월 → 1월
        year += 1
    elif month > today.month + 6:
        year -= 1
    return date(year, month, day)


def _enum_dict(d: dict) -> Any:
    """asdict 결과의 Enum/date 를 JSON 친화 값으로 변환."""

    def conv(v):
        if isinstance(v, Enum):
            return v.value
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    return conv(d)


class _Model:
    def to_dict(self) -> dict:
        return _enum_dict(asdict(self))  # type: ignore[call-overload]


# ── 출력 서식 ──

_WEEKDAYS = "월화수목금토일"


# 강조 색상. None = 터미널(tty)일 때만 자동 사용, NO_COLOR 환경변수가 있으면 끔.
COLOR: Optional[bool] = None
_HL_ON, _HL_OFF = "\x1b[1;33m", "\x1b[0m"  # 굵은 노랑


def _use_color(color: Optional[bool]) -> bool:
    if color is None:
        color = COLOR
    if color is None:
        return sys.stdout.isatty() and "NO_COLOR" not in os.environ
    return color


def _width(s: str) -> int:
    """터미널 표시 폭. 한글 등 전각 문자와 이모지는 2칸, 이모지 변형 선택자(FE0F)·ZWJ 는 0칸."""
    w = 0
    for i, ch in enumerate(s):
        if ch in "\ufe0f\u200d" or unicodedata.combining(ch):
            continue
        wide = (
            ord(ch) >= 0x1F000
            or unicodedata.east_asian_width(ch) in "WF"
            or (i + 1 < len(s) and s[i + 1] == "\ufe0f")
        )
        w += 2 if wide else 1
    return w


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _width(s))
    return gap + s if right else s + gap


def _table(
    headers: list[str],
    rows: list[list[str]],
    right: Iterable[int] = (),
    indent: str = "  ",
    highlight: Iterable[int] = (),
    color: Optional[bool] = None,
) -> str:
    """전각 문자·이모지 폭을 맞춘 텍스트 표.

    right = 오른쪽 정렬할 열 번호, highlight = 강조할 행 번호(👉 표시 + 터미널이면 굵은 노랑).
    """
    right, highlight = set(right), set(highlight)
    use_color = _use_color(color)
    widths = [max(_width(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]

    def line(r: list[str], mark: bool = False) -> str:
        body = "  ".join(_pad(str(c), widths[i], i in right) for i, c in enumerate(r)).rstrip()
        if not mark:
            return indent + body
        text = indent[:-2] + "👉" + body
        return f"{_HL_ON}{text}{_HL_OFF}" if use_color else text

    sep = indent + "  ".join("─" * w for w in widths)
    return "\n".join([line(headers), sep, *(line(r, i in highlight) for i, r in enumerate(rows))])


def _f(v: Optional[float], unit: str = "", empty: str = "-", digits: Optional[int] = None) -> str:
    """숫자 서식: digits 를 주면 고정 소수 자리, 아니면 정수는 소수점 없이. None 이면 '-'."""
    if v is None:
        return empty
    if digits is not None:
        return f"{v:.{digits}f}{unit}"
    return f"{v:.0f}{unit}" if float(v).is_integer() else f"{v:.1f}{unit}"


# ──────────────────────────── 세계 날씨 모델 ────────────────────────────


@dataclass
class Country(_Model):
    code: str
    name: str
    continent: Continent

    @classmethod
    def from_dict(cls, d: dict) -> "Country":
        return cls(code=d["code"], name=d["name"], continent=Continent.from_value(d["continent"]))


@dataclass
class City(_Model):
    city_id: str
    name: str
    country_code: str
    continent: Continent

    @classmethod
    def from_dict(cls, d: dict) -> "City":
        return cls(
            city_id=str(d["city_id"]),
            name=d["name"],
            country_code=d["country_code"],
            continent=Continent.from_value(d["continent"]),
        )


@dataclass
class HourlyForecast(_Model):
    time: datetime
    icon: WeatherIcon
    temp_c: Optional[float]
    precip_mm: Optional[float]
    wind_speed_ms: Optional[float]
    wind_dir_deg: Optional[float]
    humidity_pct: Optional[int]

    @property
    def wind_direction(self) -> Optional[WindDirection]:
        return WindDirection.from_degrees(self.wind_dir_deg)

    @classmethod
    def from_dict(cls, d: dict) -> "HourlyForecast":
        t = d["time"]
        return cls(
            time=t if isinstance(t, datetime) else datetime.fromisoformat(t),
            icon=WeatherIcon.from_value(d.get("icon")),
            temp_c=_num(d.get("temp_c")),
            precip_mm=_num(d.get("precip_mm")),
            wind_speed_ms=_num(d.get("wind_speed_ms")),
            wind_dir_deg=_num(d.get("wind_dir_deg")),
            humidity_pct=_num(d.get("humidity_pct"), int),
        )

    def row(self) -> list[str]:
        wd = self.wind_direction
        wind = _f(self.wind_speed_ms, "m/s") + (f" {wd.value}" if wd else "")
        return [f"{self.time:%H}시", self.icon.display, _f(self.temp_c, "°"), _f(self.precip_mm, "mm"), wind, _f(self.humidity_pct, "%")]


@dataclass
class DailyForecast(_Model):
    date: date
    icon: WeatherIcon
    temp_max_c: Optional[float]
    temp_min_c: Optional[float]
    wind_speed_ms: Optional[float]
    precip_mm: Optional[float]
    hourly: list[HourlyForecast] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "DailyForecast":
        return cls(
            date=_parse_date(d["date"]),
            icon=WeatherIcon.from_value(d.get("icon")),
            temp_max_c=_num(d.get("temp_max_c")),
            temp_min_c=_num(d.get("temp_min_c")),
            wind_speed_ms=_num(d.get("wind_speed_ms")),
            precip_mm=_num(d.get("precip_mm")),
            hourly=[HourlyForecast.from_dict(h) for h in d.get("hourly") or []],
        )

    @property
    def label(self) -> str:
        return f"{self.date:%m/%d}({_WEEKDAYS[self.date.weekday()]})"

    def row(self) -> list[str]:
        return [
            self.label,
            self.icon.display,
            _f(self.temp_max_c, "°"),
            _f(self.temp_min_c, "°"),
            _f(self.precip_mm, "mm"),
            _f(self.wind_speed_ms, "m/s"),
        ]

    def pretty_hourly(self) -> str:
        rows = [h.row() for h in self.hourly]
        return _table(["시각", "날씨", "기온", "강수", "바람", "습도"], rows, right=(2, 3, 5), indent="    ")


@dataclass
class MonthlyClimate(_Model):
    month: int
    temp_min_c: Optional[float]
    temp_max_c: Optional[float]
    rain_days: Optional[float]
    precip_mm: Optional[float]

    @classmethod
    def from_dict(cls, d: dict) -> "MonthlyClimate":
        return cls(
            month=int(d["month"]),
            temp_min_c=_num(d.get("temp_min_c")),
            temp_max_c=_num(d.get("temp_max_c")),
            rain_days=_num(d.get("rain_days")),
            precip_mm=_num(d.get("precip_mm")),
        )

    def row(self) -> list[str]:
        return [
            f"{self.month}월",
            _f(self.temp_max_c, "°", digits=1),
            _f(self.temp_min_c, "°", digits=1),
            _f(self.rain_days, "일", digits=1),
            _f(self.precip_mm, "mm", digits=1),
        ]


@dataclass
class CityWeather(_Model):
    continent: Continent
    country_code: str
    country_name: str
    city_id: str
    city_name: str
    city_name_en: str
    local_time: str
    lat: Optional[float]
    lon: Optional[float]
    current_icon: WeatherIcon
    daily: list[DailyForecast] = field(default_factory=list)
    climate: list[MonthlyClimate] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def from_dict(cls, d: dict) -> "CityWeather":
        fa = d.get("fetched_at")
        return cls(
            continent=Continent.from_value(d["continent"]),
            country_code=d["country_code"],
            country_name=d.get("country_name", ""),
            city_id=str(d["city_id"]),
            city_name=d.get("city_name", ""),
            city_name_en=d.get("city_name_en", ""),
            local_time=d.get("local_time", ""),
            lat=_num(d.get("lat")),
            lon=_num(d.get("lon")),
            current_icon=WeatherIcon.from_value(d.get("current_icon")),
            daily=[DailyForecast.from_dict(x) for x in d.get("daily") or []],
            climate=[MonthlyClimate.from_dict(x) for x in d.get("climate") or []],
            fetched_at=datetime.fromisoformat(fa) if isinstance(fa, str) else (fa or datetime.now()),
        )

    @property
    def title(self) -> str:
        name = self.city_name or self.city_name_en
        en = f" ({self.city_name_en})" if self.city_name and self.city_name_en else ""
        return f"{self.country_name} {name}{en}".strip()

    def pretty(self, hourly: bool = False, climate: bool = False, color: Optional[bool] = None) -> str:
        """읽기 좋은 텍스트. hourly=True 면 일자별 3시간 예보, climate=True 면 월별 기후값(이번 달 강조) 포함."""
        head = f"📍 {self.title}"
        if self.lat is not None and self.lon is not None:
            head += f"   🧭 {self.lat:.2f}, {self.lon:.2f}"
        if self.local_time:
            head += f"   🕘 현지 {self.local_time}"
        out = [head, "", "  📅 10일 예보"]
        out.append(_table(["날짜", "날씨", "최고", "최저", "강수", "바람"], [d.row() for d in self.daily], right=(2, 3, 4, 5)))
        if hourly:
            for d in self.daily:
                temps = f"{_f(d.temp_max_c, '°')} / {_f(d.temp_min_c, '°')}"
                out += ["", f"  ⏱️ {d.label}  {d.icon.display}  🌡️ {temps}", d.pretty_hourly()]
        if climate and self.climate:
            # 현지 기준 이번 달
            this_month = self.daily[0].date.month if self.daily else date.today().month
            rows = [m.row() for m in self.climate]
            hl = [i for i, m in enumerate(self.climate) if m.month == this_month]
            out += ["", "  📊 월별 기후"]
            cur = next((m for m in self.climate if m.month == this_month), None)
            if cur:
                out.append(
                    f"  🗓️ 이번 달({this_month}월): 최고 {_f(cur.temp_max_c, '°', digits=1)} · "
                    f"최저 {_f(cur.temp_min_c, '°', digits=1)} · 비 오는 날 {_f(cur.rain_days, '일', digits=1)} · "
                    f"강수량 {_f(cur.precip_mm, 'mm', digits=1)}"
                )
            out.append(
                _table(["월", "최고", "최저", "강수일수", "강수량"], rows, right=(1, 2, 3, 4), highlight=hl, color=color)
            )
        return "\n".join(out)

    def __str__(self) -> str:
        return self.pretty()


# ──────────────────────────── 세계기후(월별 평년값) 모델 ────────────────────────────


@dataclass
class ClimateCountry(_Model):
    code: str
    name: str

    @classmethod
    def from_dict(cls, d: dict) -> "ClimateCountry":
        return cls(code=d["code"], name=d["name"])


@dataclass
class ClimateCity(_Model):
    """세계기후 도시. city_id 는 예보(City.city_id)와 다른 세계기후 전용 ID."""

    city_id: str
    name: str
    country_code: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ClimateCity":
        return cls(city_id=str(d["city_id"]), name=d["name"], country_code=d.get("country_code", ""))


@dataclass
class ClimateNormalMonth(_Model):
    """월 평년값. month=0 은 연간(전년) 값."""

    month: int
    temp_avg_c: Optional[float]
    temp_max_c: Optional[float]
    temp_min_c: Optional[float]
    precip_mm: Optional[float]

    @classmethod
    def from_dict(cls, d: dict) -> "ClimateNormalMonth":
        return cls(
            month=int(d["month"]),
            temp_avg_c=_num(d.get("temp_avg_c")),
            temp_max_c=_num(d.get("temp_max_c")),
            temp_min_c=_num(d.get("temp_min_c")),
            precip_mm=_num(d.get("precip_mm")),
        )

    def is_empty(self) -> bool:
        return all(v is None for v in (self.temp_avg_c, self.temp_max_c, self.temp_min_c, self.precip_mm))

    def row(self) -> list[str]:
        return [
            f"{self.month}월" if self.month else "연간",
            _f(self.temp_avg_c, "°", digits=1),
            _f(self.temp_max_c, "°", digits=1),
            _f(self.temp_min_c, "°", digits=1),
            _f(self.precip_mm, "mm", digits=1),
        ]


@dataclass
class ClimateNormals(_Model):
    city_id: str
    city_name: str
    city_name_en: str
    country_name: str
    months: list[ClimateNormalMonth] = field(default_factory=list)
    annual: Optional[ClimateNormalMonth] = None

    @classmethod
    def from_dict(cls, d: dict) -> "ClimateNormals":
        return cls(
            city_id=str(d["city_id"]),
            city_name=d.get("city_name", ""),
            city_name_en=d.get("city_name_en", ""),
            country_name=d.get("country_name", ""),
            months=[ClimateNormalMonth.from_dict(x) for x in d.get("months") or []],
            annual=ClimateNormalMonth.from_dict(d["annual"]) if d.get("annual") else None,
        )

    def month(self, m: int) -> Optional[ClimateNormalMonth]:
        return next((x for x in self.months if x.month == m), None)

    def pretty(self, month: Optional[int] = None, color: Optional[bool] = None) -> str:
        """월별 평년값 표. month(기본: 이번 달) 행을 👉 로 강조하고 위에 요약 한 줄."""
        month = month or date.today().month
        en = f" ({self.city_name_en})" if self.city_name_en else ""
        rows = [m.row() for m in self.months] + ([self.annual.row()] if self.annual else [])
        hl = [i for i, m in enumerate(self.months) if m.month == month]
        out = [f"📊 {self.country_name} {self.city_name}{en} 월별 평년값"]
        cur = self.month(month)
        if cur:
            parts = [
                ("평균", _f(cur.temp_avg_c, "°", digits=1)),
                ("최고", _f(cur.temp_max_c, "°", digits=1)),
                ("최저", _f(cur.temp_min_c, "°", digits=1)),
                ("강수량", _f(cur.precip_mm, "mm", digits=1)),
            ]
            out.append(f"  🗓️ 이번 달({month}월): " + " · ".join(f"{k} {v}" for k, v in parts if v != "-"))
        out.append(_table(["월", "평균", "최고", "최저", "강수량"], rows, right=(1, 2, 3, 4), highlight=hl, color=color))
        return "\n".join(out)

    def __str__(self) -> str:
        return self.pretty()


# ──────────────────────────── 파서 ────────────────────────────


def parse_countries(page: str, continent: Continent) -> list[Country]:
    sel = re.search(r'name="selnation".*?</select>', page, re.S | re.I)
    if not sel:
        return []
    return [
        Country(code=c, name=_text(n), continent=continent)
        for c, n in re.findall(r"<option value=([A-Z0-9]+)\s*(?:selected)?\s*>([^<]+)", sel.group(0))
    ]


def parse_cities(page: str, continent: Continent, country_code: str) -> list[City]:
    i = page.find("name=selcity")
    if i < 0:
        return []
    end = page.find("</td>", i)
    block = page[i:end]
    return [
        City(city_id=cid, name=_text(n), country_code=country_code, continent=continent)
        for cid, n in re.findall(r"<option value=(\d+)\s*(?:selected)?\s*>([^<\n]+)", block)
    ]


def _row_values(table: str, label: str) -> list[str]:
    """'기온(˚C)' 같은 라벨 행의 칸 값들. 풍향 행은 HTML 주석(<!--tr ... tr-->) 안에 있어 같이 처리."""
    m = re.search(
        r"<(?:!--)?tr[^>]*>\s*<td>(?:<font[^>]*>)?" + re.escape(label) + r"(?:</font>)?</td>(.*?)</tr",
        table,
        re.S,
    )
    if not m:
        return []
    return [c for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)]


def parse_city_weather(
    page: str, continent: Continent, country_code: str, city_id: str, today: Optional[date] = None
) -> CityWeather:
    title = re.search(r'color="#ff6600">([^<]+)</font>', page)
    country_name, city_en, city_ko = "", "", ""
    if title:
        t = _text(title.group(1))
        country_name, _, rest = t.partition(" - ")
        m = re.match(r"(.*?)\s+([^\sA-Za-z,.()'-][^A-Za-z]*)$", rest)
        city_en, city_ko = (m.group(1), m.group(2)) if m else (rest, "")
    local = re.search(r"현지시간\s*:.*?</font>\s*([^<]+)</td>", page, re.S)
    geo = re.search(r"googlemap\.php\?lon=([-\d.]+)&lat=([-\d.]+)&icon=(\d+)", page)

    # 일별 요약: 날짜 헤더(EBF1DE) + showthree 셀
    headers = re.findall(r'bgcolor="#EBF1DE"><b>(\d+)월\s*(\d+)일', page)
    cells = re.findall(r"onclick='showthree\(\"(\d+)\"\)'(.*?)</table>", page, re.S)
    daily: list[DailyForecast] = []
    for idx, (num, body) in enumerate(cells):
        if idx >= len(headers):
            break
        mon, day = int(headers[idx][0]), int(headers[idx][1])
        temps = re.findall(r"(-?\d+(?:\.\d+)?)\s*˚C", body)
        greys = [_text(x) for x in re.findall(r'<font color="7F7F7F">(.*?)</font>', body)]
        d = DailyForecast(
            date=_infer_date(mon, day, today),
            icon=WeatherIcon.from_src(body),
            temp_max_c=_num(temps[0]) if temps else None,
            temp_min_c=_num(temps[1]) if len(temps) > 1 else None,
            wind_speed_ms=_num(greys[0]) if greys else None,
            precip_mm=_num(greys[1]) if len(greys) > 1 else None,
        )
        tbl = re.search(r'id="t' + num + r'" name="t' + num + r'".*?(?=id="t\d+" name=|<!--br>|위치정보)', page, re.S)
        if tbl:
            d.hourly = _parse_hourly(tbl.group(0), d.date)
        daily.append(d)

    climate: list[MonthlyClimate] = []
    ci = page.find("기후정보")
    if ci >= 0:
        block = page[ci:]
        rows = {}
        for label in ("최저기온", "최고기온", "강수일수", "월강수량"):
            m = re.search(r">\s*" + label + r"\s*<.*?</tr>", block, re.S)
            rows[label] = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(0), re.S)] if m else []
        for mth in range(12):
            def g(lbl):
                vals = rows.get(lbl) or []
                return _num(vals[mth]) if mth < len(vals) else None
            if any(g(l) is not None for l in rows):
                climate.append(MonthlyClimate(mth + 1, g("최저기온"), g("최고기온"), g("강수일수"), g("월강수량")))

    return CityWeather(
        continent=continent,
        country_code=country_code,
        country_name=country_name.strip(),
        city_id=str(city_id),
        city_name=city_ko.strip(),
        city_name_en=city_en.strip(),
        local_time=_text(local.group(1)) if local else "",
        lat=_num(geo.group(2)) if geo else None,
        lon=_num(geo.group(1)) if geo else None,
        current_icon=WeatherIcon.from_value(geo.group(3)) if geo else WeatherIcon.UNKNOWN,
        daily=daily,
        climate=climate,
    )


def _parse_hourly(table: str, day: date) -> list[HourlyForecast]:
    hours = [int(h) for h in re.findall(r"<b>(\d{2})h</td>", table)]
    icons = [WeatherIcon.from_src(c) for c in _row_values(table, "날씨")]
    temps = [_num(_text(c)) for c in _row_values(table, "기온(˚C)")]
    precs = [_num(_text(c)) for c in _row_values(table, "강수량(mm)")]
    winds = [_num(_text(c)) for c in _row_values(table, "풍속(m/s)")]
    dirs = [_num(_text(c)) for c in _row_values(table, "풍향")]
    hums = [_num(_text(c), int) for c in _row_values(table, "습도(%)")]
    out = []
    for i, h in enumerate(hours):
        # 00h 는 다음 날 자정
        t = datetime(day.year, day.month, day.day, h % 24)
        if h == 0 and i > 0:
            t = datetime.fromordinal(day.toordinal() + 1)
        at = lambda arr: arr[i] if i < len(arr) else None  # noqa: E731
        out.append(
            HourlyForecast(
                time=t,
                icon=at(icons) or WeatherIcon.UNKNOWN,
                temp_c=at(temps),
                precip_mm=at(precs),
                wind_speed_ms=at(winds),
                wind_dir_deg=at(dirs),
                humidity_pct=at(hums),
            )
        )
    return out


def _options(block: str) -> list[tuple[str, str]]:
    found = re.findall(r"<option\s+value\s*=\s*[\"']?([^\"' >]+)[^>]*>([^<\n]*)", block)
    return [(unquote(v), _text(n)) for v, n in found]


def _select_block(page: str, name: str) -> str:
    """<select name=...> 블록. 도시 select 는 </select> 가 없어 </td> 까지 자른다."""
    m = re.search(r"<select[^>]*name\s*=\s*[\"']?" + name + r"\b", page, re.I)
    if not m:
        return ""
    ends = [page.lower().find("</select>", m.end()), page.find("</td>", m.end())]
    return page[m.end() : min((i for i in ends if i > 0), default=len(page))]


def parse_climate_countries(page: str) -> list[ClimateCountry]:
    return [ClimateCountry(code=v, name=n) for v, n in _options(_select_block(page, "selnation"))]


def parse_climate_cities(page: str, country_code: str = "") -> list[ClimateCity]:
    opts = _options(_select_block(page, "selcity"))
    return [ClimateCity(city_id=v, name=n, country_code=country_code) for v, n in opts if v.isdigit()]


def parse_climate_normals(page: str, city_id: str) -> ClimateNormals:
    i = page.find("해당도시를")
    block = page[i:] if i >= 0 else page
    names = re.findall(r'<font color="#124d79"><strong>([^<]*)</strong></font>', block)
    title = _text(names[0]) if names else ""
    m = re.match(r"(.*?)\s+([^A-Za-z]+)$", title)
    name_en, name_ko = (m.group(1), m.group(2)) if m else (title, "")
    country = _text(names[1]).split(" - ")[0] if len(names) > 1 else ""

    # 행 라벨: 평균 기온 / 최고 기온 / 최저 기온 / 강수량 — 도시마다 일부 행이 없을 수 있음
    rows: dict[str, list[Optional[float]]] = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", block, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 2:
            continue
        label = _text(tds[0]).replace(" ", "")
        key = next((k for k in ("평균", "최고", "최저", "강수량") if label.startswith(k)), None)
        if key:
            vals = [_num(_text(x)) for x in tds[1:14]]  # 1~12월 + 전년
            # 자료 없는 도시는 사이트가 13칸 모두 0 으로 채워 보여주므로 없는 행으로 처리
            if any(v not in (None, 0) for v in vals):
                rows[key] = vals

    def mk(idx: int, month: int) -> ClimateNormalMonth:
        def g(key: str) -> Optional[float]:
            vals = rows.get(key) or []
            return vals[idx] if idx < len(vals) else None

        return ClimateNormalMonth(month, g("평균"), g("최고"), g("최저"), g("강수량"))

    months = [x for x in (mk(i, i + 1) for i in range(12)) if not x.is_empty()]
    annual = mk(12, 0)
    return ClimateNormals(
        city_id=str(city_id),
        city_name=name_ko.strip(),
        city_name_en=name_en.strip(),
        country_name=country.strip(),
        months=months,
        annual=None if annual.is_empty() else annual,
    )


# ──────────────────────────── 클라이언트 ────────────────────────────


class WeatheriClient:
    """웨더아이 비동기 클라이언트. `async with` 로 사용하거나 close() 호출."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        concurrency: int = 5,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._own = client is None
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._climate_index: Optional[list[ClimateCity]] = None

    async def __aenter__(self) -> "WeatheriClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own:
            await self._client.aclose()

    async def _get(self, path: str, query: str, referer: str = "", retries: int = 2) -> str:
        url = f"{path}?{query}" if query else path
        headers = {"Referer": BASE_URL + referer} if referer else {}
        async with self._sem:
            for attempt in range(retries + 1):
                try:
                    r = await self._client.get(url, headers=headers)
                    r.raise_for_status()
                    return r.content.decode("utf-8", "replace")
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if attempt == retries:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError("unreachable")

    # ── 세계 날씨 ──

    @staticmethod
    def _world_query(continent: Continent, country: str = "", city: str = "") -> str:
        q = f"selO={quote(continent.value)}"
        if city:
            q += f"&selC={city}"
        if country:
            q += f"&selN={country}"
        return q

    async def get_countries(self, continent: "Continent | str") -> list[Country]:
        c = Continent.from_value(continent)
        page = await self._get(WORLD_PATH, self._world_query(c), referer=WORLD_PATH)
        return parse_countries(page, c)

    async def get_cities(self, continent: "Continent | str", country_code: str) -> list[City]:
        c = Continent.from_value(continent)
        page = await self._get(WORLD_PATH, self._world_query(c, country_code), referer=WORLD_PATH)
        return parse_cities(page, c, country_code)

    async def get_city_weather(
        self, continent: "Continent | str", country_code: str, city_id: "str | int"
    ) -> CityWeather:
        c = Continent.from_value(continent)
        page = await self._get(WORLD_PATH, self._world_query(c, country_code, str(city_id)), referer=WORLD_PATH)
        # 잘못된 조합이면 사이트가 다른 도시(기본 쿠알라룸푸르 등)를 보여주므로 목록에 있는지 확인
        if str(city_id) not in {x.city_id for x in parse_cities(page, c, country_code)}:
            raise LookupError(f"도시 없음: {c.value}/{country_code}/{city_id}")
        w = parse_city_weather(page, c, country_code, str(city_id))
        if not w.daily:
            raise LookupError(f"예보 없음: {c.value}/{country_code}/{city_id}")
        return w

    async def get_weather(self, city: City) -> CityWeather:
        return await self.get_city_weather(city.continent, city.country_code, city.city_id)

    async def get_many(self, cities: Iterable[City]) -> list["CityWeather | BaseException"]:
        """여러 도시 동시 조회. 실패한 도시는 예외 객체로 자리 유지."""
        return await asyncio.gather(*(self.get_weather(c) for c in cities), return_exceptions=True)

    async def find_cities(
        self, keyword: str, continents: Optional[Iterable["Continent | str"]] = None
    ) -> list[City]:
        """도시명(한글) 부분 일치 검색. 대륙 → 국가 목록을 순회하므로 요청이 많다."""
        conts = [Continent.from_value(c) for c in continents] if continents else list(Continent)
        countries = [x for lst in await asyncio.gather(*(self.get_countries(c) for c in conts)) for x in lst]
        lists = await asyncio.gather(*(self.get_cities(x.continent, x.code) for x in countries))
        return [city for lst in lists for city in lst if keyword in city.name]

    # ── 세계기후(월별 평년값) ──

    async def get_climate_countries(self, region: "ClimateRegion | str | None" = None) -> list[ClimateCountry]:
        """권역을 주면 그 권역 국가만, 없으면 전체 국가."""
        q = f"selL={quote(ClimateRegion.from_value(region).value)}" if region else ""
        return parse_climate_countries(await self._get(CLIMATE_PATH, q, referer=CLIMATE_PATH))

    async def get_climate_cities(self, country_code: Optional[str] = None) -> list[ClimateCity]:
        """국가 코드를 주면 그 나라 도시만, 없으면 전체 도시(333곳, 국가 코드 없음)."""
        q = f"selN={country_code}" if country_code else ""
        page = await self._get(CLIMATE_PATH, q, referer=CLIMATE_PATH)
        return parse_climate_cities(page, country_code or "")

    async def get_climate_normals(self, city: "ClimateCity | str | int") -> ClimateNormals:
        city_id = city.city_id if isinstance(city, ClimateCity) else str(city)
        page = await self._get(CLIMATE_PATH, f"selC={city_id}", referer=CLIMATE_PATH)
        normals = parse_climate_normals(page, city_id)
        if not normals.months:  # 없는 ID면 값이 빈 표가 나온다
            raise LookupError(f"평년값 없음: 세계기후 도시 {city_id}")
        return normals

    async def get_all_climate_cities(self) -> list[ClimateCity]:
        """전체 국가를 돌며 국가 코드가 붙은 세계기후 도시 목록을 만든다(국가 수만큼 요청, 결과 캐시).

        기본 목록(get_climate_cities())에는 도쿄 등 일부 도시가 빠져 있어 국가별 목록이 더 완전하다.
        """
        if self._climate_index is None:
            countries = await self.get_climate_countries()
            lists = await asyncio.gather(*(self.get_climate_cities(c.code) for c in countries))
            self._climate_index = [city for lst in lists for city in lst]
        return self._climate_index

    async def find_climate_cities(self, keyword: str) -> list[ClimateCity]:
        """도시명(한글) 부분 일치 검색."""
        return [c for c in await self.get_all_climate_cities() if keyword in c.name]

    async def get_climate_normals_by_name(self, name: str) -> ClimateNormals:
        """도시명(한글)으로 평년값 조회. 정확히 일치 → 부분 일치 순으로 첫 도시."""

        def pick(cities: list[ClimateCity]) -> Optional[ClimateCity]:
            return next((c for c in cities if c.name == name), None) or next((c for c in cities if name in c.name), None)

        hit = pick(await self.get_climate_cities()) or pick(await self.get_all_climate_cities())
        if not hit:
            raise LookupError(f"세계기후 도시 없음: {name}")
        return await self.get_climate_normals(hit)


# ──────────────────────────── 실행 예시 ────────────────────────────


async def _demo() -> None:
    getattr(sys.stdout, "reconfigure", lambda **_: None)(encoding="utf-8")
    async with WeatheriClient() as wx:
        jp = await wx.get_cities(Continent.ASIA, "JP")
        tokyo = next(c for c in jp if c.name == "도쿄")
        weather = await wx.get_weather(tokyo)
        print(weather.pretty(climate=True))
        print()
        print(f"  ⏱️ {weather.daily[0].label} 3시간 예보")
        print(weather.daily[0].pretty_hourly())
        print()
        print(await wx.get_climate_normals_by_name("도쿄"))


if __name__ == "__main__":
    asyncio.run(_demo())
