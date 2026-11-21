"""전체 종목 스크리닝 및 최종 선정 (STEP 6)

 1) 1차 필터: 10개 재무지표 모두 '통과'인 종목만 우선 후보군 (확인 불가는 통과로 보지 않음)
 2) 후보가 10개 미만이면 조건을 완화하지 않고 충족 종목만 선정
 3) 10개 이상이면 100점 만점 점수(가중치 공개, config/scoring_weights.yaml)로 순위 결정
 4) 최종 선정 종목에 VERIFIED 가 아닌 핵심 지표가 있으면 경고
 5) 직전 실행과 비교하여 신규 편입·제외 종목 및 주요 지표 변화 기록
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from analyzers.financial_analyzer import CompanyFinancials
from analyzers.ratio_calculator import CHECK_LABEL, CHECKLIST, RatioResult
from core import config
from core.models import CheckResult, DataStatus


@dataclass
class ScreeningRow:
    stock_code: str
    name: str
    market: str | None
    industry: str | None
    pass_count: int
    na_count: int
    passed_all: bool
    score: float
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    rank: int | None = None
    selected: bool = False
    fin_status: str = ""

    def details(self) -> dict[str, Any]:
        return {"name": self.name, "market": self.market, "industry": self.industry, "checks": self.checks,
                "components": self.components, "warnings": self.warnings, "fin_status": self.fin_status}


def _ratio(direction: str, value: float, worst: float, best: float) -> float:
    if direction == "lower_better":
        r = (worst - value) / (worst - best)
    else:
        r = (value - worst) / (best - worst)
    return max(0.0, min(1.0, r))


class ScreeningEngine:
    def __init__(self):
        sw = config.scoring_weights()
        self.weights: dict[str, float] = sw["weights"]
        self.scales: dict[str, dict] = sw["scales"]
        self.max_selected = sw.get("selection", {}).get("max_selected", 10)
        total = sum(self.weights.values())
        if abs(total - 100) > 1e-9:
            raise ValueError(f"scoring_weights.yaml 가중치 합계가 100이 아님: {total}")

    # ------------------------------------------------------------------
    def score(self, cf: CompanyFinancials, rr: RatioResult) -> tuple[float, dict[str, dict[str, Any]]]:
        m = rr.metrics
        comp: dict[str, dict[str, Any]] = {}

        def add(key: str, value: float | None, note: str = "", basis: str = ""):
            w = self.weights[key]
            if value is None:
                comp[key] = {"weight": w, "value": None, "ratio": 0.0, "points": 0.0, "note": note or "값 없음 → 0점",
                             "basis": basis}
                return
            sc = self.scales.get(key)
            r = _ratio(sc["direction"], value, sc["worst"], sc["best"]) if sc else value
            comp[key] = {"weight": w, "value": value, "ratio": round(r, 4), "points": round(w * r, 4),
                         "note": note, "basis": basis}

        add("criteria_pass_count", rr.pass_count / len(CHECKLIST), basis=f"{rr.pass_count}/{len(CHECKLIST)} 충족")
        for k in ("per", "pbr", "operating_margin", "net_margin", "debt_ratio", "reserve_ratio"):
            mt = m.get(k)
            v = mt.value if mt is not None else None
            add(k, v, "" if v is not None else f"{mt.display if mt else '확인 불가'} → 0점",
                basis=f"{CHECK_LABEL[k]} {mt.display if mt else ''}")
        # 매출 성장률: 직전 사업연도 vs 전기
        rt = m.get("revenue_trend")
        add("revenue_growth", rt.value if rt is not None else None, basis="직전 사업연도 연결 매출 성장률(%)")
        # 영업현금흐름: 직전 사업연도 영업CF / 지배순이익
        fy = cf.annual_periods[0] if cf.annual_periods else None
        ocf = cf.annual.get(fy, {}).get("operating_cf") if fy else None
        nip = cf.annual.get(fy, {}).get("net_income_parent") if fy else None
        if ocf is None or ocf.value is None:
            add("operating_cash_flow", None, "영업현금흐름 미확인 → 0점")
        elif ocf.value <= 0:
            add("operating_cash_flow", 0.0, f"영업현금흐름 {ocf.value:,.0f}원 ≤ 0 → 0점", basis=f"{fy} 영업CF")
        elif nip is None or nip.value is None or nip.value <= 0:
            add("operating_cash_flow", 1.0, "영업CF > 0, 지배순이익 ≤ 0 또는 미확인 → 영업CF 양수만 반영",
                basis=f"{fy} 영업CF {ocf.value:,.0f}")
        else:
            add("operating_cash_flow", ocf.value / nip.value,
                basis=f"{fy} 영업CF {ocf.value:,.0f} / 지배순이익 {nip.value:,.0f}")
        # 최근 분기 방향성
        q = cf.latest_q_yoy
        add("recent_quarter_direction", q.get("yoy_pct") if q else None,
            "" if q.get("yoy_pct") is not None else "최근 분기 전년 동기 비교 불가 → 0점",
            basis=(f"{q.get('period')} 영업이익 {q.get('op_current', 0):,.0f} vs 전년 동기 {q.get('op_prior_year', 0):,.0f}"
                   if q else ""))
        total = round(sum(c["points"] for c in comp.values()), 2)
        return total, comp

    # ------------------------------------------------------------------
    def run(self, entries: list[tuple[Any, CompanyFinancials, RatioResult]]) -> list[ScreeningRow]:
        rows: list[ScreeningRow] = []
        for r, cf, rr in entries:
            score, comp = self.score(cf, rr)
            checks = {CHECK_LABEL[k]: rr.metrics[k].check.value for k in CHECKLIST if k in rr.metrics}
            sr = ScreeningRow(r.stock_code, r.name, getattr(r, "market", None),
                              r.industry if isinstance(getattr(r, "industry", None), str) else None,
                              rr.pass_count, rr.na_count, rr.all_pass, score, comp, checks, fin_status=cf.status)
            nv = rr.non_verified_core
            if nv:
                sr.warnings.append(f"VERIFIED 아닌 핵심 지표: {', '.join(nv)}")
            vr = getattr(rr, "valuation", None)
            if vr is not None and vr.cautions:
                sr.warnings += vr.cautions
            rows.append(sr)

        def key(s: ScreeningRow):
            m = {k: v["value"] for k, v in s.components.items()}
            big = float("inf")
            return (-s.score, m.get("per") if m.get("per") is not None else big,
                    m.get("pbr") if m.get("pbr") is not None else big,
                    -(m.get("operating_margin") or -big))

        cands = sorted([s for s in rows if s.passed_all], key=key)
        for i, s in enumerate(cands, 1):
            s.rank = i
            s.selected = i <= self.max_selected
        others = sorted([s for s in rows if not s.passed_all], key=key)
        for i, s in enumerate(others, len(cands) + 1):
            s.rank = i
        return cands + others


# ----------------------------------------------------------------------
def compare_with_previous(db, run_id: str) -> dict[str, Any]:
    """직전 실행 대비 신규 편입·제외 종목 및 지표 변화 (PART 12)"""
    prev = db.query("SELECT run_id FROM screening_results WHERE run_id < ? ORDER BY run_id DESC LIMIT 1", (run_id,))
    if not prev:
        return {"previous_run": None, "message": "비교할 직전 실행 없음"}
    p = prev[0]["run_id"]
    cur_sel = {r["stock_code"] for r in db.query("SELECT stock_code FROM screening_results WHERE run_id=? AND selected=1",
                                                  (run_id,))}
    prev_sel = {r["stock_code"] for r in db.query("SELECT stock_code FROM screening_results WHERE run_id=? AND selected=1",
                                                   (p,))}
    changes = []
    for metric in ("per", "pbr", "psr"):
        rows = db.query(
            "SELECT a.stock_code, a.value AS cur, b.value AS prev FROM financial_ratios a JOIN financial_ratios b "
            "ON a.stock_code=b.stock_code AND a.metric=b.metric WHERE a.run_id=? AND b.run_id=? AND a.metric=? "
            "AND a.value IS NOT NULL AND b.value IS NOT NULL AND abs(a.value-b.value) > 1e-9", (run_id, p, metric))
        changes += [{"stock_code": x["stock_code"], "metric": metric, "prev": x["prev"], "cur": x["cur"]} for x in rows]
    vrows = db.query(
        "SELECT a.stock_code, a.fair_value AS cur_fv, b.fair_value AS prev_fv, a.safety_margin_pct AS cur_sm, "
        "b.safety_margin_pct AS prev_sm FROM valuation_results a JOIN valuation_results b ON a.stock_code=b.stock_code "
        "WHERE a.run_id=? AND b.run_id=?", (run_id, p))
    for x in vrows:
        if x["cur_fv"] != x["prev_fv"]:
            changes.append({"stock_code": x["stock_code"], "metric": "fair_value", "prev": x["prev_fv"], "cur": x["cur_fv"]})
        if x["cur_sm"] != x["prev_sm"]:
            changes.append({"stock_code": x["stock_code"], "metric": "safety_margin_pct", "prev": x["prev_sm"],
                            "cur": x["cur_sm"]})
    return {"previous_run": p, "new_entries": sorted(cur_sel - prev_sel), "removed": sorted(prev_sel - cur_sel),
            "metric_changes": changes}


def to_db_rows(run_id: str, analysis_date: str, rows: list[ScreeningRow]) -> list[dict[str, Any]]:
    return [{"run_id": run_id, "analysis_date": analysis_date, "stock_code": s.stock_code, "in_universe": 1,
             "pass_count": s.pass_count, "na_count": s.na_count, "passed_all": int(s.passed_all), "score": s.score,
             "rank": s.rank, "selected": int(s.selected),
             "details_json": json.dumps(s.details(), ensure_ascii=False, default=str)} for s in rows]
