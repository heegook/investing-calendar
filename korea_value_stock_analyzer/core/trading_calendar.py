"""KRX 거래일 계산 (Asia/Seoul 기준)

주말, 한국 공휴일(선거일·대체공휴일 포함), 근로자의 날, 연말 휴장일을 휴장일로 처리함
holidays 패키지의 XKRX(한국거래소) 금융 휴일 달력을 사용함
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


@lru_cache(maxsize=None)
def _krx_holidays(year: int) -> dict:
    return dict(holidays.financial_holidays("XKRX", years=year))


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in _krx_holidays(d.year)


def holiday_name(d: date) -> str | None:
    if d.weekday() >= 5:
        return "주말"
    return _krx_holidays(d.year).get(d)


def previous_trading_day(ref: date) -> date:
    """ref 이전(ref 미포함) 가장 최근 거래일"""
    d = ref - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def analysis_price_date(run_at: datetime | None = None) -> date:
    """분석 기준 주가일: 실행일 기준 전일 거래일 종가

    매일 07:30 장 시작 전 실행하므로 실행일 직전 거래일의 종가를 기준으로 함
    토·일·공휴일 실행 시에도 직전 거래일이 됨
    """
    run_at = run_at or now_kst()
    return previous_trading_day(run_at.astimezone(KST).date())


def recent_trading_days(ref: date, n: int) -> list[date]:
    out, d = [], ref
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out
