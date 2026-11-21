"""10개 재무지표 계산·검증 테스트"""
import pytest

from analyzers.financial_analyzer import CompanyFinancials, FinancialAnalyzer
from analyzers.ratio_calculator import PriceInfo, RatioCalculator, classify_revenue_trend
from collectors.dart_collector import (bsns_year_candidates, parse_amount, parse_report_name,
                                       reprt_code_for)
from core.models import CheckResult, DataStatus, Reason
from tests.fixtures.sample_company import E8, SHARES, reports


def price(close=10_000.0, shares=10_500_000, status=DataStatus.VERIFIED):
    return PriceInfo("TEST01", "2026-11-20", close, shares, close * shares, "KRX_OFFICIAL", "test", status)


@pytest.fixture
def rr():
    cf = FinancialAnalyzer().analyze("TEST01", reports(), SHARES)
    return RatioCalculator().calculate(cf, price())


def test_market_cap_and_shares(rr):
    m = rr.metrics
    assert m["market_cap"].value == pytest.approx(1050 * E8)
    assert m["market_cap"].check == CheckResult.PASS
    assert m["listed_shares"].check == CheckResult.PASS
    assert m["listed_shares"].inputs["treasury_shares"] == 500_000


def test_psr(rr):
    # 1050억 / 1000억 = 1.05 → 미통과
    assert rr.metrics["psr"].value == pytest.approx(1.05)
    assert rr.metrics["psr"].check == CheckResult.FAIL


def test_per_with_cross_check(rr):
    m = rr.metrics["per"]
    assert m.value == pytest.approx(10_000 / 900)
    assert m.check == CheckResult.PASS
    assert m.inputs["eps_cross_check"] == pytest.approx(900)  # 90억 / 1,000만주
    assert m.status == DataStatus.VERIFIED


def test_pbr_uses_parent_equity_and_outstanding(rr):
    m = rr.metrics["pbr"]
    bps = 1350 * E8 / 10_000_000
    assert rr.metrics["bps"].value == pytest.approx(bps)
    assert m.value == pytest.approx(10_000 / bps)
    assert m.check == CheckResult.PASS


def test_margins_and_debt_reserve(rr):
    m = rr.metrics
    assert m["operating_margin"].value == pytest.approx(12.0)
    assert m["net_margin"].value == pytest.approx(8.0)          # 지배귀속 80 / 1000
    assert m["net_margin"].inputs["net_income_consolidated"] == pytest.approx(90 * E8)
    assert m["debt_ratio"].value == pytest.approx(600 / 1400 * 100)
    assert m["reserve_ratio"].value == pytest.approx((50 + 1200) / 5 * 100)
    assert rr.pass_count == 9 and rr.na_count == 0


def test_revenue_trend_classification():
    assert classify_revenue_trend(950, 900, 1000) == "감소 후 회복"
    assert classify_revenue_trend(100, 110, 120) == "지속 성장"
    assert classify_revenue_trend(120, 110, 100) == "지속 감소"
    assert classify_revenue_trend(100, 99.5, 99) == "지속 감소"   # 소폭이라도 연속 감소면 제외 대상
    assert classify_revenue_trend(100, 120, 110) == "성장 후 감소"
    assert classify_revenue_trend(100, 101, 100.5) == "횡보"
    assert classify_revenue_trend(None, 1, 1) == "확인 불가"


def test_negative_eps_is_nm():
    cf = FinancialAnalyzer().analyze("TEST01", reports(), SHARES)
    cf.ttm["eps"].value = -150.0
    m = RatioCalculator().calculate(cf, price()).metrics["per"]
    assert m.value is None and m.display == "N/M" and m.reason == Reason.NOT_MEANINGFUL


def test_no_api_key_gives_null_failed_not_fake():
    cf = CompanyFinancials("TEST01", "NO_API_KEY", "DART_API_KEY 미설정")
    rr = RatioCalculator().calculate(cf, price())
    for k in ("psr", "per", "pbr", "operating_margin", "net_margin", "debt_ratio", "reserve_ratio", "revenue_trend"):
        assert rr.metrics[k].value is None
        assert rr.metrics[k].check == CheckResult.NA
        assert rr.metrics[k].status == DataStatus.FAILED
    # 주가 기반 항목은 계산됨
    assert rr.metrics["market_cap"].value is not None


def test_cfs_not_available_is_distinguished_from_failure():
    cf = CompanyFinancials("TEST01", "CFS_NOT_AVAILABLE", "연결재무제표 확인 불가")
    rr = RatioCalculator().calculate(cf, price())
    assert rr.metrics["per"].reason == Reason.CFS_NOT_AVAILABLE
    assert rr.metrics["per"].status == DataStatus.UNVERIFIED


def test_balance_sheet_mismatch_flags_partial():
    cf = FinancialAnalyzer().analyze("TEST01", reports(bs_kwargs={"assets": 2100}), SHARES)
    chk = [v for v in cf.validations if v["check"] == "자산총계 = 부채총계 + 자본총계"][0]
    assert chk["result"] == "FAIL"
    rr = RatioCalculator().calculate(cf, price())
    assert rr.metrics["debt_ratio"].status == DataStatus.PARTIAL


def test_investment_assets_from_real_dart_layout():
    cf = FinancialAnalyzer().analyze("TEST01", reports(), SHARES)
    # 관계기업 100 + FVOCI 30 + 장기금융상품 20 + 투자부동산 50 = 200억, 단기 FVPL(유동)·파생상품 제외
    assert cf.investment_assets.value == pytest.approx(200 * E8)
    assert cf.investment_assets.status == DataStatus.VERIFIED
    names = {i["account_nm"] for i in cf.investment_breakdown}
    assert "단기당기손익-공정가치금융자산" not in names
    assert not any("파생" in n for n in names)
    assert "합계 = 비유동자산" in cf.investment_assets.notes[0]


def test_investment_subtotal_row_not_double_counted():
    reps = reports()
    for r in reps:
        r.rows.append({"sj_div": "BS", "account_id": "-표준계정코드 미사용-", "account_nm": "장기투자자산", "ord": "18",
                       "currency": "KRW", "thstrm_amount": f"{150 * E8:,.0f}"})
    cf = FinancialAnalyzer().analyze("TEST01", reps, SHARES)
    assert cf.investment_assets.value == pytest.approx(200 * E8)
    sub = [i for i in cf.investment_breakdown if i["account_nm"] == "장기투자자산"][0]
    assert sub["included"] is False


def test_split_detection_adjusts_per_share():
    cf = FinancialAnalyzer().analyze("TEST01", reports(), SHARES)
    # 결산일 이후 1:5 액면분할 → 상장주식수 5배, 주가 1/5
    rr = RatioCalculator().calculate(cf, price(close=2_000.0, shares=52_500_000))
    per = rr.metrics["per"]
    assert per.inputs["split_factor"] == pytest.approx(5.0)
    assert per.value == pytest.approx(2_000 / (900 / 5))
    assert per.status == DataStatus.PARTIAL


def test_unverified_price_propagates():
    cf = FinancialAnalyzer().analyze("TEST01", reports(), SHARES)
    rr = RatioCalculator().calculate(cf, price(status=DataStatus.UNVERIFIED))
    assert rr.metrics["per"].status == DataStatus.UNVERIFIED
    assert rr.metrics["operating_margin"].status == DataStatus.VERIFIED
    assert "PER" in rr.non_verified_core


def test_balance_units_and_cfs_checks():
    cf = FinancialAnalyzer().analyze("TEST01", reports(), SHARES)
    res = {v["check"]: v["result"] for v in cf.validations}
    assert res["연결재무제표(CFS) 사용"] == "PASS"
    assert res["단위 검증(원 단위)"] == "PASS"
    assert res["주식수·자기주식수 구분"] == "PASS"


def test_dart_report_parsing():
    assert parse_report_name("[기재정정]사업보고서 (2025.12)") == ("사업보고서", "2025-12")
    assert parse_report_name("분기보고서 (2026.09)") == ("분기보고서", "2026-09")
    assert reprt_code_for("분기보고서", 9, 12) == "11014"
    assert reprt_code_for("분기보고서", 6, 3) == "11013"   # 3월 결산 1분기(6월말)
    assert reprt_code_for("반기보고서", 9, 3) == "11012"
    assert bsns_year_candidates(2026, 9, 12) == [2026]
    assert bsns_year_candidates(2026, 6, 3) == [2027, 2026]
    assert parse_amount("-1,234") == -1234 and parse_amount("") is None and parse_amount("-") is None


def test_share_premium_used_when_capital_surplus_row_absent():
    reps = reports()
    for r in reps:
        for row in r.rows:
            if row["account_nm"] == "자본잉여금":
                row["account_nm"], row["account_id"] = "주식발행초과금", "ifrs-full_SharePremium"
    cf = FinancialAnalyzer().analyze("TEST01", reps, SHARES)
    cs = cf.balance["capital_surplus"]
    assert cs.value == pytest.approx(50 * E8)
    assert cs.status == DataStatus.PARTIAL and "주식발행초과금" in cs.notes[0]


def test_only_common_class_detection():
    from collectors.dart_collector import only_common_class
    hd = [{"se": "보통주식", "istc_totqy": "36,047,135"}, {"se": "배당우선주식\n(1종 종류주식)", "istc_totqy": "-"},
          {"se": "합계", "istc_totqy": "36,047,135"}, {"se": "비고", "istc_totqy": ""}]
    ss = [{"se": "보통주", "istc_totqy": "5,846,278,608"}, {"se": "우선주", "istc_totqy": "815,974,664"},
          {"se": "합계", "istc_totqy": "6,662,253,272"}]
    assert only_common_class(hd) is True
    assert only_common_class(ss) is False


def test_investment_section_when_total_row_comes_last():
    """합계 행이 구간 끝에 오는 형식도 합계 일치로 식별"""
    reps = reports()
    for r in reps:
        for row in r.rows:
            if row["account_nm"] == "비유동자산":
                row["ord"] = "26"
            if row["account_nm"] == "유동자산":
                row["ord"] = "12"
    cf = FinancialAnalyzer().analyze("TEST01", reps, SHARES)
    assert cf.investment_assets.value == pytest.approx(200 * E8)
    assert "앞 구간" in cf.investment_assets.notes[0]


def test_preferred_shares_used_in_eps_cross_check():
    sh = dict(SHARES, other_class_outstanding=2_000_000.0)
    cf = FinancialAnalyzer().analyze("TEST01", reports(), sh)
    rr = RatioCalculator().calculate(cf, price())
    assert rr.metrics["per"].inputs["eps_cross_check"] == pytest.approx(90 * E8 / 12_000_000)
    assert any("우선주" in n for n in rr.metrics["pbr"].notes)



def test_eps_fallback_when_reported_eps_missing():
    reps = reports()
    for r in reps:
        r.rows = [x for x in r.rows if x["account_nm"] != "기본주당이익"]
    cf = FinancialAnalyzer().analyze("TEST01", reps, SHARES)
    rr = RatioCalculator().calculate(cf, price())
    per = rr.metrics["per"]
    assert per.value == pytest.approx(10_000 / 900)        # 90억 / 1,000만주
    assert per.status == DataStatus.PARTIAL


def test_share_class_parsing():
    from collectors.dart_collector import is_common_class, share_row
    assert is_common_class("의결권 있는 주식") and not is_common_class("의결권 없는 주식")
    assert is_common_class("보통주식") and not is_common_class("우선주")
    r = share_row({"istc_totqy": "13,000,000", "tesstk_co": "-", "distb_stock_co": "13,000,000"})
    assert r["treasury_shares"] == 0 and r["outstanding_shares"] == 13_000_000


def test_account_name_normalization():
    from analyzers.financial_analyzer import _norm
    assert _norm("Ⅰ. 매출액") == "매출액" and _norm("1. 영업이익") == "영업이익" and _norm("(1) 자본금") == "자본금"


def test_eps_from_continuing_operations_when_total_missing():
    """효성 사례: 사업보고서에 계속영업·중단영업 주당이익만 공시"""
    reps = reports()
    for r in reps:
        if r.reprt_code == "11011":
            for x in r.rows:
                if x["account_nm"] == "기본주당이익":
                    x["account_id"] = "ifrs-full_BasicEarningsLossPerShareFromContinuingOperations"
                    x["account_nm"] = "계속영업기본주당이익(손실)"
            r.rows.append({"sj_div": "IS", "account_id": "ifrs-full_BasicEarningsLossPerShareFromDiscontinuedOperations",
                           "account_nm": "중단영업기본주당이익(손실)", "ord": "13", "currency": "KRW",
                           "thstrm_amount": None})
    cf = FinancialAnalyzer().analyze("TEST01", reps, SHARES)
    v = cf.annual["2025-12"]["eps"]
    assert v.value == pytest.approx(800) and v.status == DataStatus.PARTIAL
    assert cf.ttm["eps"].value == pytest.approx(900)
