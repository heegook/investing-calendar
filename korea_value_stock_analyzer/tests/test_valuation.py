"""적정주가 공식 검증 (PART 8, PART 13-10)"""
import pytest

from analyzers.financial_analyzer import CompanyFinancials, FinancialAnalyzer
from analyzers.valuation_engine import DISCLAIMER, ValuationEngine
from core.models import DataStatus, Reason
from tests.fixtures.sample_company import E8, SHARES, reports
from tests.test_financial_ratios import price


@pytest.fixture
def cf():
    return FinancialAnalyzer().analyze("TEST01", reports(), SHARES)


def test_default_formula_exact(cf):
    # {(120×9) + (800 + 200 − 400×1.2) − 200}억 = 1,400억 ÷ 1,050만주
    v = ValuationEngine().calculate(cf, price())
    assert v.is_default_formula
    assert v.fair_value == pytest.approx(1400 * E8 / 10_500_000)
    assert v.steps[0]["result"] == pytest.approx(1080 * E8)
    assert v.steps[1]["result"] == pytest.approx(520 * E8)
    assert v.status == DataStatus.VERIFIED
    assert all(c["result"] == "PASS" for c in v.validations)
    assert v.disclaimer == DISCLAIMER


def test_margin_metrics(cf):
    v = ValuationEngine().calculate(cf, price())
    fair = 1400 * E8 / 10_500_000
    assert v.safety_margin_pct == pytest.approx((fair - 10_000) / 10_000 * 100)
    assert v.upside_pct == pytest.approx(v.safety_margin_pct)
    assert v.discount_pct == pytest.approx((fair - 10_000) / fair * 100)
    assert v.price_20pct_off == pytest.approx(fair * 0.8)
    assert v.price_30pct_off == pytest.approx(fair * 0.7)


def test_inputs_table_traceable(cf):
    v = ValuationEngine().calculate(cf, price())
    keys = [i["key"] for i in v.inputs]
    assert keys == ["operating_income", "current_assets", "investment_assets", "current_liabilities",
                    "noncurrent_liabilities", "listed_shares"]
    assert all(i["value"] is not None for i in v.inputs)
    assert v.inputs[0]["rcept_no"] == "20260310000001"        # 사업보고서 영업이익
    assert "2026-09" in v.inputs[1]["basis"]                   # 최근 분기
    assert sum(i["amount"] for i in v.investment_breakdown if i["included"]) == pytest.approx(200 * E8)


def test_simulation_is_flagged(cf):
    v = ValuationEngine().calculate(cf, price(), op_multiple=7, cl_weight=1.0)
    assert not v.is_default_formula
    assert "시뮬레이션" in v.formula and "시뮬레이션" in v.cautions[0]
    assert v.fair_value == pytest.approx((120 * 7 + 800 + 200 - 400 - 200) * E8 / 10_500_000)


def test_missing_inputs_no_value():
    v = ValuationEngine().calculate(CompanyFinancials("TEST01", "NO_API_KEY"), price())
    assert v.fair_value is None and v.reason == Reason.NO_API_KEY and v.status == DataStatus.FAILED


def test_holding_and_financial_caution(cf):
    # 지주회사(KSIC 상 '기타 금융업')는 지주 주의만, 금융업 주의는 붙이지 않음
    v = ValuationEngine().calculate(cf, price(), name="테스트홀딩스", industry="기타 금융업")
    assert any("지주회사" in c for c in v.cautions) and not any("금융업 추정" in c for c in v.cautions)
    v2 = ValuationEngine().calculate(cf, price(), name="테스트은행", industry="은행 및 저축기관")
    assert any("금융업 추정" in c for c in v2.cautions)


def test_negative_fair_value_no_discount_prices():
    cf = FinancialAnalyzer().analyze("TEST01", reports(bs_kwargs={"ncl": 3000, "liab": 3400, "equity": -1400,
                                                                  "assets": 2000}), SHARES)
    v = ValuationEngine().calculate(cf, price())
    assert v.fair_value < 0
    assert v.price_20pct_off is None and v.discount_pct is None
    assert any("적정주가 ≤ 0" in c for c in v.cautions)
