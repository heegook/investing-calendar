"""스크리닝·선정 규칙 테스트 (PART 4)"""
from types import SimpleNamespace

import pytest

from analyzers.financial_analyzer import CompanyFinancials, FinancialAnalyzer
from analyzers.ratio_calculator import RatioCalculator
from analyzers.screening_engine import ScreeningEngine, _ratio
from core import config
from tests.fixtures.sample_company import SHARES, reports
from tests.test_financial_ratios import price


def entry(code, close):
    cf = FinancialAnalyzer().analyze(code, reports(), SHARES)
    rr = RatioCalculator().calculate(cf, price(close=close))
    return SimpleNamespace(stock_code=code, name=f"가상{code}", market="KOSPI", industry="테스트"), cf, rr


def test_weights_sum_100():
    assert sum(config.scoring_weights()["weights"].values()) == 100


def test_fixture_passes_all_at_9000():
    _, cf, rr = entry("A", 9_000)
    assert rr.all_pass, {k: m.check for k, m in rr.metrics.items()}


def test_less_than_10_no_relaxation():
    ents = [entry("A", 9_000), entry("B", 8_000), entry("C", 12_000)]   # C 는 PSR ≥ 1 로 미통과
    rows = ScreeningEngine().run(ents)
    sel = [r.stock_code for r in rows if r.selected]
    assert sorted(sel) == ["A", "B"]
    assert not any(r.selected for r in rows if not r.passed_all)


def test_more_than_10_top10_by_score():
    ents = [entry(f"S{i:02d}", 6_000 + i * 200) for i in range(13)]  # 모두 통과, 낮은 가격일수록 PER·PBR 우수
    rows = ScreeningEngine().run(ents)
    sel = [r for r in rows if r.selected]
    assert len(sel) == 10
    assert [r.stock_code for r in sel] == [f"S{i:02d}" for i in range(10)]
    assert all(sel[i].score >= sel[i + 1].score for i in range(9))


def test_score_components_transparent():
    _, cf, rr = entry("A", 9_000)
    total, comp = ScreeningEngine().score(cf, rr)
    assert total == pytest.approx(sum(c["points"] for c in comp.values()), abs=0.01)
    assert set(comp) == set(config.scoring_weights()["weights"])
    assert comp["criteria_pass_count"]["points"] == pytest.approx(30)
    # 최근 분기 영업이익 35 vs 전년 동기 30 → +16.7%
    assert comp["recent_quarter_direction"]["value"] == pytest.approx(16.6667, rel=1e-3)
    assert 0 < total <= 100


def test_low_per_alone_not_enough():
    """PER 만 극단적으로 낮고 다른 지표 확인 불가인 종목은 상위가 될 수 없음"""
    _, cf_good, rr_good = entry("G", 9_000)
    cf_bad = CompanyFinancials("X", "NO_API_KEY")
    rr_bad = RatioCalculator().calculate(cf_bad, price(close=9_000))
    eng = ScreeningEngine()
    assert eng.score(cf_good, rr_good)[0] > eng.score(cf_bad, rr_bad)[0]


def test_ratio_scale():
    assert _ratio("lower_better", 15, 15, 5) == 0 and _ratio("lower_better", 5, 15, 5) == 1
    assert _ratio("higher_better", 30, 5, 25) == 1 and _ratio("higher_better", 0, 5, 25) == 0
