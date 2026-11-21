"""TTM·분기 실적·누적/분기 구분 검증"""
import pytest

from analyzers.financial_analyzer import FinancialAnalyzer, _prior_fy_end, _shift_month
from tests.fixtures.sample_company import E8, SHARES, reports


@pytest.fixture
def cf():
    return FinancialAnalyzer().analyze("TEST01", reports(), SHARES)


def test_ttm_formula_from_3q(cf):
    # 매출 TTM = 연간 1000 + 당기누적 800 − 전년동기누적 740 = 1060억
    assert cf.ttm["revenue"].value == pytest.approx(1060 * E8)
    assert cf.ttm["net_income_parent"].value == pytest.approx(90 * E8)
    # EPS TTM = 800 + 700 − 600 = 900원
    assert cf.ttm["eps"].value == pytest.approx(900)
    assert "TTM = 연간(2025-12" in cf.ttm_basis


def test_last_four_quarters_and_q4_derivation(cf):
    labels = [q["period"] for q in cf.quarters]
    assert labels == ["2026-09", "2026-06", "2026-03", "2025-12"]
    q4 = cf.quarters[3]
    assert q4["derived"] is True
    # 4Q = 연간 1000 − 3Q 누적 740 = 260억
    assert q4["revenue"].value == pytest.approx(260 * E8)
    assert [q["revenue"].value / E8 for q in cf.quarters] == pytest.approx([280, 270, 250, 260])


def test_ttm_cross_check_passes(cf):
    rev = [v for v in cf.validations if v["check"] == "TTM 검증(매출액)"][0]
    assert rev["result"] == "PASS"


def test_cumulative_vs_quarter_distinguished(cf):
    chk = [v for v in cf.validations if v["check"] == "누적/분기 실적 구분"][0]
    assert chk["result"] == "PASS"


def test_ttm_missing_prior_annual_is_null():
    reps = [r for r in reports() if r.reprt_code != "11011"]
    cf = FinancialAnalyzer().analyze("TEST01", reps, SHARES)
    assert cf.ttm["eps"].value is None
    assert "미확인" in cf.ttm_basis


def test_q1_latest_uses_3month_columns():
    reps = [r for r in reports(latest="1Q") if r.period != "2026-06"]
    cf = FinancialAnalyzer().analyze("TEST01", reps, SHARES)
    # 1Q 최신: TTM = 1000 + 250 − 230 = 1020억
    assert cf.ttm["revenue"].value == pytest.approx(1020 * E8)


def test_period_helpers():
    assert _shift_month("2026-09", -3) == "2026-06"
    assert _shift_month("2026-01", -3) == "2025-10"
    assert _prior_fy_end("2026-09", 3) == "2025-12"
    assert _prior_fy_end("2026-12", 3) == "2026-03"  # 3월 결산 3분기(12월말)
