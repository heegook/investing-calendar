"""10개 재무지표 계산 엔진 (STEP 4)

모든 결과는 Metric(값·공식·입력값·원천·기준일·상태·통과여부)으로 반환함
입력값이 하나라도 없으면 값을 만들지 않고 None 으로 둠
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from analyzers.financial_analyzer import CompanyFinancials
from core import config
from core.models import CheckResult, DataStatus, Metric, Reason, SourceRef, Value


@dataclass
class PriceInfo:
    stock_code: str
    price_date: str | None
    close: float | None
    listed_shares: int | None
    market_cap: float | None
    source: str
    source_ref: str
    status: DataStatus
    collected_at: str | None = None

    def src(self, detail: str) -> SourceRef:
        return SourceRef(self.source, self.source_ref, self.price_date, self.collected_at, detail)


CHECKLIST = ["psr", "per", "pbr", "operating_margin", "net_margin", "debt_ratio", "reserve_ratio",
             "listed_shares", "market_cap", "revenue_trend"]
CHECK_LABEL = {
    "psr": "PSR", "per": "PER", "pbr": "PBR", "operating_margin": "영업이익률", "net_margin": "순이익률",
    "debt_ratio": "부채비율", "reserve_ratio": "유보율", "listed_shares": "상장주식수",
    "market_cap": "시가총액", "revenue_trend": "최근 3개년 매출 추이",
}


@dataclass
class RatioResult:
    stock_code: str
    metrics: dict[str, Metric] = field(default_factory=dict)
    valuation: Any = None   # STEP 5 ValuationResult

    @property
    def checklist(self) -> list[Metric]:
        return [self.metrics[k] for k in CHECKLIST if k in self.metrics]

    @property
    def pass_count(self) -> int:
        return sum(m.check == CheckResult.PASS for m in self.checklist)

    @property
    def na_count(self) -> int:
        return sum(m.check == CheckResult.NA for m in self.checklist)

    @property
    def all_pass(self) -> bool:
        return self.pass_count == len(CHECKLIST)

    @property
    def non_verified_core(self) -> list[str]:
        return [CHECK_LABEL[k] for k in CHECKLIST if k in self.metrics
                and self.metrics[k].status != DataStatus.VERIFIED]


def _src(v: Value | None) -> list[SourceRef]:
    return [v.source] if v is not None and v.source else []


def _missing(name: str, unit: str, formula: str, threshold: str, inputs: dict[str, Value | Any],
             sources: list[SourceRef], as_of: str | None) -> Metric:
    reasons = [v.reason for v in inputs.values() if isinstance(v, Value) and v.value is None and v.reason]
    statuses = [v.status for v in inputs.values() if isinstance(v, Value)]
    reason = reasons[0] if reasons else Reason.INPUT_MISSING
    status = DataStatus.FAILED if reason in (Reason.NO_API_KEY, Reason.COLLECTION_FAILED) else \
        DataStatus.worst(*statuses, DataStatus.UNVERIFIED)
    notes = [n for v in inputs.values() if isinstance(v, Value) for n in v.notes]
    return Metric(name, None, unit, formula, status,
                  inputs={k: (v.value if isinstance(v, Value) else v) for k, v in inputs.items()},
                  sources=sources, as_of=as_of, reason=reason, check=CheckResult.NA, threshold=threshold,
                  notes=notes, display="확인 불가")


class RatioCalculator:
    def __init__(self):
        s = config.settings()
        self.t = s["thresholds"]
        self.v = s["validation"]

    # ------------------------------------------------------------------
    def calculate(self, cf: CompanyFinancials, price: PriceInfo) -> RatioResult:
        res = RatioResult(cf.stock_code)
        m = res.metrics
        price_v = Value(price.close, price.status, price.src("종가(실제 종가, 수정주가 아님)"),
                        reason=None if price.close else Reason.COLLECTION_FAILED)
        latest_fy = cf.annual_periods[0] if cf.annual_periods else None
        fy = cf.annual.get(latest_fy, {}) if latest_fy else {}
        no_fin = cf.status in ("NO_API_KEY", "FAILED", "CFS_NOT_AVAILABLE", "NOT_FOUND")
        fin_reason = {"NO_API_KEY": Reason.NO_API_KEY, "FAILED": Reason.COLLECTION_FAILED,
                      "CFS_NOT_AVAILABLE": Reason.CFS_NOT_AVAILABLE,
                      "NOT_FOUND": Reason.STATEMENT_NOT_FOUND}.get(cf.status)

        def fin(v: Value | None, label: str) -> Value:
            if v is not None:
                return v
            if no_fin:
                st = DataStatus.FAILED if fin_reason in (Reason.NO_API_KEY, Reason.COLLECTION_FAILED) else DataStatus.UNVERIFIED
                return Value(None, st, reason=fin_reason, notes=[f"{label}: {cf.message or cf.status}"])
            return Value(None, DataStatus.UNVERIFIED, reason=Reason.STATEMENT_NOT_FOUND, notes=[f"{label} 미확인"])

        revenue = fin(fy.get("revenue"), "직전 사업연도 매출액")
        op = fin(fy.get("operating_income"), "직전 사업연도 영업이익")
        ni_parent = fin(fy.get("net_income_parent"), "직전 사업연도 지배기업 소유주 귀속 순이익")
        ni = fin(fy.get("net_income"), "직전 사업연도 연결 당기순이익")
        ttm_eps = fin(cf.ttm.get("eps"), "TTM EPS")
        ttm_nip = fin(cf.ttm.get("net_income_parent"), "TTM 지배순이익")
        eq_parent = fin(cf.balance.get("equity_parent"), "최근 분기 지배기업 소유주지분")
        liab = fin(cf.balance.get("total_liabilities"), "최근 분기 부채총계")
        equity = fin(cf.balance.get("total_equity"), "최근 분기 자본총계")
        cap = fin(cf.balance.get("issued_capital"), "자본금")
        csur = fin(cf.balance.get("capital_surplus"), "자본잉여금")
        reta = fin(cf.balance.get("retained_earnings"), "이익잉여금")

        # ---------------- 시가총액 ----------------
        mcap_v = Value(price.market_cap, price.status, price.src("종가 × 상장주식수"),
                       reason=None if price.market_cap else Reason.COLLECTION_FAILED)
        if price.close and price.listed_shares:
            mc = price.close * price.listed_shares
            m["market_cap"] = Metric(
                "시가총액", mc, "원", "전일 종가 × 상장주식수", price.status,
                inputs={"close": price.close, "listed_shares": price.listed_shares},
                sources=[price.src("종가·상장주식수")], as_of=price.price_date,
                check=CheckResult.PASS if mc >= self.t["market_cap_min_krw"] else CheckResult.FAIL,
                threshold=f"≥ {self.t['market_cap_min_krw'] / 1e8:,.0f}억원", display=f"{mc / 1e8:,.0f}억원")
        else:
            m["market_cap"] = _missing("시가총액", "원", "전일 종가 × 상장주식수", "≥ 500억원",
                                       {"close": price_v}, [], price.price_date)

        # ---------------- 상장주식수 ----------------
        if price.listed_shares:
            n = price.listed_shares
            ok = self.t["listed_shares_min"] <= n <= self.t["listed_shares_max"]
            notes = []
            if cf.shares.get("treasury_shares") is not None:
                notes.append(f"자기주식수 {cf.shares['treasury_shares']:,.0f}주 (DART 결산기준일 {cf.shares.get('stlm_dt')}) 별도 보관")
                if cf.shares.get("note"):
                    notes.append(cf.shares["note"])
            else:
                notes.append("자기주식수 미확인")
            m["listed_shares"] = Metric(
                "상장주식수", float(n), "주", "KRX 상장 보통주식수", price.status,
                inputs={"listed_shares": n, "treasury_shares": cf.shares.get("treasury_shares")},
                sources=[price.src("상장주식수")], as_of=price.price_date,
                check=CheckResult.PASS if ok else CheckResult.FAIL,
                threshold="1,000만 주 이상 8,000만 주 이하", notes=notes, display=f"{n:,.0f}주")
        else:
            m["listed_shares"] = _missing("상장주식수", "주", "KRX 상장 보통주식수", "1,000만~8,000만 주",
                                          {"listed_shares": price_v}, [], price.price_date)

        # ---------------- 주식수 조정(액면분할·병합·무상증자) ----------------
        split_factor, split_note = self._split_factor(cf, price)
        outstanding = cf.shares.get("outstanding_shares")

        # ---------------- PSR ----------------
        f = "시가총액 / 직전 사업연도 매출액"
        if m["market_cap"].value and revenue.value:
            if revenue.value <= 0:
                m["psr"] = Metric("PSR", None, "배", f, revenue.status, inputs={"revenue": revenue.value},
                                  reason=Reason.NOT_MEANINGFUL, check=CheckResult.FAIL, display="N/M",
                                  threshold=f"< {self.t['psr_max']}", as_of=price.price_date)
            else:
                v = m["market_cap"].value / revenue.value
                m["psr"] = Metric("PSR", v, "배", f, DataStatus.worst(price.status, revenue.status),
                                  inputs={"market_cap": m["market_cap"].value, "revenue": revenue.value,
                                          "fiscal_period": latest_fy},
                                  sources=[price.src("시가총액"), *_src(revenue)], as_of=price.price_date,
                                  check=CheckResult.PASS if v < self.t["psr_max"] else CheckResult.FAIL,
                                  threshold=f"< {self.t['psr_max']}", notes=revenue.notes, display=f"{v:.2f}배")
        else:
            m["psr"] = _missing("PSR", "배", f, f"< {self.t['psr_max']}", {"market_cap": mcap_v, "revenue": revenue},
                                _src(revenue), price.price_date)

        # ---------------- PER ----------------
        f = "전일 종가 / TTM EPS  (TTM EPS = 최근 연간 EPS + 당기 누적 EPS − 전년 동기 누적 EPS)"
        thr = f"< {self.t['per_max']}"
        other_cls0 = cf.shares.get("other_class_outstanding") or 0
        if ttm_eps.value is None and ttm_nip.value is not None and outstanding and cf.status == "OK":
            # 보고 EPS 계정이 없을 때: TTM 지배순이익 / (유통보통주 + 종류주식) 로 산출(PARTIAL)
            calc_eps = ttm_nip.value / (outstanding + other_cls0)
            ttm_eps = Value(calc_eps, DataStatus.worst(ttm_nip.status, DataStatus.PARTIAL), ttm_nip.source,
                            notes=[f"보고 EPS 계정 미확인 → TTM 지배순이익 {ttm_nip.value:,.0f} ÷ 유통주식수 "
                                   f"{outstanding + other_cls0:,.0f} 로 산출(결산일 기준, 가중평균 아님)"],
                            inputs={"method": "net_income_parent / outstanding"})
        if price.close and ttm_eps.value is not None:
            eps_adj = ttm_eps.value / split_factor
            notes = list(ttm_eps.notes) + ([split_note] if split_note else []) + [cf.ttm_basis]
            status = DataStatus.worst(price.status, ttm_eps.status)
            # 교차검증: TTM 지배순이익 / 유통보통주식수
            cross = None
            other_cls = cf.shares.get("other_class_outstanding") or 0
            if ttm_nip.value is not None and outstanding:
                cross = ttm_nip.value / ((outstanding + other_cls) * split_factor)
                if eps_adj != 0:
                    dev = abs(cross / eps_adj - 1) * 100
                    base = "보통주+우선주 등 유통주식수" if other_cls else "유통주식수"
                    notes.append(f"EPS 교차검증: TTM 지배순이익/{base} = {cross:,.1f}원 (보고 EPS 대비 {dev:.1f}% 차이, "
                                 f"유통주식수는 가중평균이 아닌 결산일 기준 근사)")
                    if dev > self.v["eps_cross_check_tolerance_pct"]:
                        status = DataStatus.worst(status, DataStatus.PARTIAL)
            else:
                notes.append("EPS 교차검증 불가(지배순이익 또는 유통주식수 미확인)")
            if split_factor != 1.0:
                status = DataStatus.worst(status, DataStatus.PARTIAL)
            inputs = {"close": price.close, "ttm_eps": ttm_eps.value, "split_factor": split_factor,
                      "ttm_eps_adjusted": eps_adj, "eps_cross_check": cross, "ttm_detail": ttm_eps.inputs}
            if eps_adj <= 0:
                m["per"] = Metric("PER", None, "배", f, status, inputs=inputs,
                                  sources=[price.src("종가"), *_src(ttm_eps)], as_of=price.price_date,
                                  reason=Reason.NOT_MEANINGFUL, check=CheckResult.FAIL, threshold=thr,
                                  notes=notes + ["TTM EPS ≤ 0 → PER N/M (저평가 조건 미충족으로 처리)"], display="N/M")
            else:
                v = price.close / eps_adj
                m["per"] = Metric("PER", v, "배", f, status, inputs=inputs,
                                  sources=[price.src("종가"), *_src(ttm_eps)], as_of=price.price_date,
                                  check=CheckResult.PASS if v < self.t["per_max"] else CheckResult.FAIL,
                                  threshold=thr, notes=notes, display=f"{v:.2f}배")
            m["ttm_eps"] = Metric("TTM EPS", ttm_eps.value, "원", "최근 연간 EPS + 당기 누적 EPS − 전년 동기 누적 EPS",
                                  ttm_eps.status, inputs=ttm_eps.inputs, sources=_src(ttm_eps),
                                  notes=[cf.ttm_basis], display=f"{ttm_eps.value:,.0f}원")
        else:
            m["per"] = _missing("PER", "배", f, thr, {"close": price_v, "ttm_eps": ttm_eps}, _src(ttm_eps),
                                price.price_date)

        # ---------------- PBR ----------------
        f = "전일 종가 / BPS  (BPS = 지배기업 소유주지분 / 기말 유통보통주식수)"
        thr = f"< {self.t['pbr_max']}"
        if price.close and eq_parent.value is not None and outstanding:
            shares_adj = outstanding * split_factor
            bps = eq_parent.value / shares_adj
            status = DataStatus.worst(price.status, eq_parent.status)
            if split_factor != 1.0:
                status = DataStatus.worst(status, DataStatus.PARTIAL)
            notes = list(eq_parent.notes) + ([split_note] if split_note else [])
            if cf.shares.get("other_class_outstanding"):
                notes.append(f"우선주 등 종류주식 {cf.shares['other_class_outstanding']:,.0f}주 존재 → 지배지분 전체를 "
                             f"보통주로 나눈 지시 공식이므로 BPS 과대·PBR 과소 가능")
            if cf.shares.get("note"):
                notes.append(cf.shares["note"])
                if "변동 미반영" in cf.shares["note"]:
                    status = DataStatus.worst(status, DataStatus.PARTIAL)
            notes.append(f"기말 유통보통주식수 {outstanding:,.0f}주 = 발행 {cf.shares.get('issued_shares'):,.0f} − "
                         f"자기 {cf.shares.get('treasury_shares'):,.0f} (DART {cf.shares.get('stlm_dt')})"
                         if cf.shares.get("issued_shares") is not None and cf.shares.get("treasury_shares") is not None
                         else f"기말 유통보통주식수 {outstanding:,.0f}주")
            inputs = {"close": price.close, "equity_parent": eq_parent.value, "outstanding_shares": outstanding,
                      "split_factor": split_factor, "bps": bps, "balance_period": cf.balance_period}
            if bps <= 0:
                m["pbr"] = Metric("PBR", None, "배", f, status, inputs=inputs, reason=Reason.NOT_MEANINGFUL,
                                  sources=[price.src("종가"), *_src(eq_parent)], as_of=price.price_date,
                                  check=CheckResult.FAIL, threshold=thr, notes=notes + ["자본잠식(BPS ≤ 0)"], display="N/M")
            else:
                v = price.close / bps
                m["pbr"] = Metric("PBR", v, "배", f, status, inputs=inputs,
                                  sources=[price.src("종가"), *_src(eq_parent)], as_of=price.price_date,
                                  check=CheckResult.PASS if v < self.t["pbr_max"] else CheckResult.FAIL,
                                  threshold=thr, notes=notes, display=f"{v:.2f}배")
            m["bps"] = Metric("BPS", bps, "원", "지배기업 소유주지분 / 기말 유통보통주식수", status,
                              inputs=inputs, sources=_src(eq_parent), as_of=cf.balance_period, display=f"{bps:,.0f}원")
        else:
            sh = Value(outstanding, DataStatus.VERIFIED if outstanding else DataStatus.UNVERIFIED,
                       reason=None if outstanding else (fin_reason or Reason.INPUT_MISSING),
                       notes=[] if outstanding else ["기말 유통보통주식수(DART 주식총수 현황) 미확인"])
            m["pbr"] = _missing("PBR", "배", f, thr, {"close": price_v, "equity_parent": eq_parent,
                                                      "outstanding_shares": sh}, _src(eq_parent), price.price_date)

        # ---------------- 영업이익률 / 순이익률 ----------------
        for key, label, num, thr_v in (("operating_margin", "영업이익률", op, self.t["operating_margin_min"]),
                                       ("net_margin", "순이익률", ni_parent, self.t["net_margin_min"])):
            numer = "영업이익" if key == "operating_margin" else "지배기업 소유주 귀속 순이익"
            f = f"직전 사업연도 {numer} / 직전 사업연도 매출액 × 100"
            if num.value is not None and revenue.value:
                v = num.value / revenue.value * 100
                notes = [*num.notes, *revenue.notes]
                if key == "net_margin" and ni.value is not None:
                    notes.append(f"연결 당기순이익 {ni.value:,.0f}원 / 지배기업 소유주 귀속 {num.value:,.0f}원 구분 적용")
                m[key] = Metric(label, v, "%", f, DataStatus.worst(num.status, revenue.status),
                                inputs={"numerator": num.value, "revenue": revenue.value, "fiscal_period": latest_fy,
                                        **({"net_income_consolidated": ni.value} if key == "net_margin" else {})},
                                sources=[*_src(num), *_src(revenue)], as_of=latest_fy,
                                check=CheckResult.PASS if v >= thr_v else CheckResult.FAIL,
                                threshold=f"≥ {thr_v}%", notes=notes, display=f"{v:.2f}%")
            else:
                m[key] = _missing(label, "%", f, f"≥ {thr_v}%", {"numerator": num, "revenue": revenue},
                                  [*_src(num), *_src(revenue)], latest_fy)

        # ---------------- 부채비율 ----------------
        f = "최근 분기 부채총계 / 최근 분기 자본총계 × 100"
        thr = f"≤ {self.t['debt_ratio_max']}%"
        if liab.value is not None and equity.value is not None:
            st = DataStatus.worst(liab.status, equity.status)
            bs_ok = cf.check_ok("자산총계 = 부채총계 + 자본총계")
            if bs_ok is False:
                st = DataStatus.worst(st, DataStatus.PARTIAL)
            if equity.value <= 0:
                m["debt_ratio"] = Metric("부채비율", None, "%", f, st, inputs={"liabilities": liab.value, "equity": equity.value},
                                         sources=[*_src(liab), *_src(equity)], as_of=cf.balance_period,
                                         reason=Reason.NOT_MEANINGFUL, check=CheckResult.FAIL, threshold=thr,
                                         notes=["자본총계 ≤ 0 (완전자본잠식)"], display="N/M")
            else:
                v = liab.value / equity.value * 100
                m["debt_ratio"] = Metric("부채비율", v, "%", f, st,
                                         inputs={"liabilities": liab.value, "equity": equity.value,
                                                 "balance_period": cf.balance_period},
                                         sources=[*_src(liab), *_src(equity)], as_of=cf.balance_period,
                                         check=CheckResult.PASS if v <= self.t["debt_ratio_max"] else CheckResult.FAIL,
                                         threshold=thr, notes=["자본총계는 비지배지분 포함 연결 자본총계"], display=f"{v:.1f}%")
        else:
            m["debt_ratio"] = _missing("부채비율", "%", f, thr, {"liabilities": liab, "equity": equity},
                                       [*_src(liab), *_src(equity)], cf.balance_period)

        # ---------------- 유보율 ----------------
        f = "(자본잉여금 + 이익잉여금) / 자본금 × 100"
        thr = f"≥ {self.t['reserve_ratio_min']:,.0f}%"
        basis = ("연결재무상태표의 자본금·자본잉여금·이익잉여금 계정 사용. K-IFRS 연결재무상태표에서 이 계정들은 "
                 "지배기업 소유주지분 내 구성항목으로 표시되므로 지배기업 소유주지분 기준임")
        if None not in (cap.value, csur.value, reta.value) and cap.value:
            v = (csur.value + reta.value) / cap.value * 100
            m["reserve_ratio"] = Metric("유보율", v, "%", f, DataStatus.worst(cap.status, csur.status, reta.status),
                                        inputs={"capital_surplus": csur.value, "retained_earnings": reta.value,
                                                "issued_capital": cap.value, "balance_period": cf.balance_period},
                                        sources=[*_src(csur), *_src(reta), *_src(cap)], as_of=cf.balance_period,
                                        check=CheckResult.PASS if v >= self.t["reserve_ratio_min"] else CheckResult.FAIL,
                                        threshold=thr, notes=[basis], display=f"{v:,.1f}%")
        else:
            m["reserve_ratio"] = _missing("유보율", "%", f, thr,
                                          {"capital_surplus": csur, "retained_earnings": reta, "issued_capital": cap},
                                          [*_src(csur), *_src(reta), *_src(cap)], cf.balance_period)
            m["reserve_ratio"].notes.append(basis)

        # ---------------- 최근 3개년 매출 추이 ----------------
        m["revenue_trend"] = self.revenue_trend(cf, no_fin, fin_reason)
        return res

    # ------------------------------------------------------------------
    def _split_factor(self, cf: CompanyFinancials, price: PriceInfo) -> tuple[float, str | None]:
        """DART 결산일 발행주식수 대비 현재 KRX 상장주식수 비율로 액면분할·병합·무상증자 감지

        비율이 임계값 이상(또는 역수 이상)일 때만 조정계수로 사용하며, 소폭 차이(유상증자·전환 등)는 조정하지 않고 표시만 함
        """
        iss = cf.shares.get("issued_shares")
        if not iss or not price.listed_shares:
            return 1.0, None
        r = price.listed_shares / iss
        th = self.v["split_ratio_detection"]
        if r >= th or r <= 1 / th:
            return r, (f"주식수 변동 감지: DART 결산일 발행주식 {iss:,.0f}주 → KRX 현재 상장 {price.listed_shares:,.0f}주 "
                       f"(×{r:.4f}). 액면분할·병합·무상증자 가능성으로 주당지표를 조정함 - 공시 확인 필요")
        if abs(r - 1) > 0.05:
            return 1.0, f"DART 결산일 발행주식수 대비 현재 상장주식수 {(r - 1) * 100:+.1f}% 차이(조정 안 함)"
        return 1.0, None

    def revenue_trend(self, cf: CompanyFinancials, no_fin: bool = False, fin_reason: Reason | None = None) -> Metric:
        f = "최근 3개 사업연도 연결 매출액 비교 (전전기 → 전기 → 당기)"
        periods = list(reversed(cf.annual_periods[:3]))  # 과거 → 최신
        vals = [cf.annual[p]["revenue"] for p in periods] if len(periods) == 3 else []
        if len(vals) != 3 or any(v.value is None for v in vals):
            st = DataStatus.FAILED if fin_reason in (Reason.NO_API_KEY, Reason.COLLECTION_FAILED) else DataStatus.UNVERIFIED
            return Metric("최근 3개년 매출 추이", None, "", f, st, reason=fin_reason or Reason.INPUT_MISSING,
                          check=CheckResult.NA, threshold="3년 연속 감소 아님", display="확인 불가",
                          inputs={p: cf.annual[p]["revenue"].value for p in periods})
        y1, y2, y3 = (v.value for v in vals)
        label = classify_revenue_trend(y1, y2, y3, self.t["revenue_flat_band_pct"])
        declining = label == "지속 감소"
        g1 = (y2 / y1 - 1) * 100 if y1 else None
        g2 = (y3 / y2 - 1) * 100 if y2 else None
        return Metric("최근 3개년 매출 추이", g2, "%", f, DataStatus.worst(*(v.status for v in vals)),
                      inputs={"periods": periods, "revenues": [y1, y2, y3], "growth_pct": [g1, g2]},
                      sources=[s for v in vals for s in _src(v)], as_of=periods[-1],
                      check=CheckResult.FAIL if declining else CheckResult.PASS, threshold="3년 연속 감소 아님",
                      notes=["지속 감소 → 저평가 제외 대상" if declining else "",
                             "매출 추이만으로 산업 경쟁력을 판단하지 않음"],
                      display=label)


def classify_revenue_trend(y1: float | None, y2: float | None, y3: float | None, flat_band_pct: float = 3.0) -> str:
    """y1(전전기) → y2(전기) → y3(당기)"""
    if None in (y1, y2, y3) or not y1 or not y2:
        return "확인 불가"
    g1, g2 = y2 / y1 - 1, y3 / y2 - 1
    band = flat_band_pct / 100
    if g1 < 0 and g2 < 0:
        return "지속 감소"
    if abs(g1) <= band and abs(g2) <= band:
        return "횡보"
    if g1 >= 0 and g2 >= 0:
        return "지속 성장"
    if g1 > 0 and g2 < 0:
        return "성장 후 감소"
    return "감소 후 회복"
