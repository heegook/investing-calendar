from datetime import date, datetime

from core.trading_calendar import KST, analysis_price_date, is_trading_day, previous_trading_day


def test_weekend_uses_friday():
    assert analysis_price_date(datetime(2026, 11, 21, 7, 30, tzinfo=KST)) == date(2026, 11, 20)  # 토
    assert analysis_price_date(datetime(2026, 11, 23, 7, 30, tzinfo=KST)) == date(2026, 11, 20)  # 월 → 금


def test_krx_holidays():
    assert not is_trading_day(date(2026, 12, 31))   # 연말 휴장일
    assert not is_trading_day(date(2026, 5, 1))     # 근로자의 날
    assert not is_trading_day(date(2026, 6, 3))     # 지방선거일
    assert previous_trading_day(date(2027, 1, 4)) == date(2026, 12, 30)
