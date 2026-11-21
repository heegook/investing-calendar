"""적정주가 계산 엔진 (STEP 5)

기본 공식 (지시된 공식을 그대로 적용, 계수 고정)

    적정주가 = { (영업이익 × 9) + (유동자산 + 투자자산 − 유동부채 × 1.2) − 비유동부채 } ÷ 상장주식수

 - 영업이익   : 최근 사업연도 연결 영업이익 (사업보고서)
 - 유동자산·투자자산·유동부채·비유동부채 : 최근 분기 연결재무상태표
 - 상장주식수 : 현재 KRX 상장 보통주식수
 - 투자자산   : 비유동 금융자산 + 관계기업·공동기업 투자 + 투자부동산 (financial_analyzer 정의, 계정별 내역 공개)

※ 이 값은 자체 정의한 평가모형에 따른 값이며 시장에서 확정된 내재가치가 아님
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from analyzers.financial_analyzer import CompanyFinancials
from analyzers.ratio_calculator import PriceInfo
from core import config
from core.models import DataStatus, Reason, Value

DEFAULT_OP_MULTIPLE = 9.0
DEFAULT_CL_WEIGHT = 1.2
FORMULA = "{ (영업이익 × 9) + (유동자산 + 투자자산 − 유동부채 × 1.2) − 비유동부채 } ÷ 상장주식수"
DISCLAIMER = "자체 정의한 평가모형에 따른 값이며 시장에서 확정된 내재가치가 아님"


@dataclass
class ValuationResult:
    stock_code: str
    fair_value: float | None
    current_price: float | None
    status: DataStatus
    is_default_formula: bool
    op_multiple: float
    cl_weight: float
    formula: str
    inputs: list[dict[str, Any]] = field(default_factory=list)        # 입력값 표
    steps: list[dict[str, Any]] = field(default_factory=list)         # 계산 과정 표
    investment_breakdown: list[dict[str, Any]] = field(default_factory=list)
    safety_margin_pct: float | None = None     # (적정 − 현재) / 현재 × 100
    discount_pct: float | None = None          # (적정 − 현재) / 적정 × 100  : 적정주가 대비 현재 주가 할인율
    upside_pct: float | None = None            # (적정 − 현재) / 현재 × 100  : 현재 주가 대비 상승 여력
    price_20pct_off: float | None = None
    price_30pct_off: float | None = None
    cautions: list[str] = field(default_factory=list)
    validations: list[dict[str, Any]] = field(default_factory=list)
    reason: Reason | None = None
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        d["reason"] = self.reason.value if self.reason else None
        return d


class ValuationEngine:
    def __init__(self):
        self.vcfg = config.settings().get("valuation", {})

    # ------------------------------------------------------------------
    def calculate(self, cf: CompanyFinancials, price: PriceInfo, *, name: str = "", industry: str | None = None,
                  op_multiple: float = DEFAULT_OP_MULTIPLE, cl_weight: float = DEFAULT_CL_WEIGHT) -> ValuationResult:
        is_default = (op_multiple == DEFAULT_OP_MULTIPLE and cl_weight == DEFAULT_CL_WEIGHT)
        formula = FORMULA if is_default else (
            f"{{ (영업이익 × {op_multiple:g}) + (유동자산 + 투자자산 − 유동부채 × {cl_weight:g}) − 비유동부채 }} ÷ 상장주식수"
            " [시뮬레이션 - 기본 공식 아님]")
        fy = cf.annual_periods[0] if cf.annual_periods else None
        op = cf.annual.get(fy, {}).get("operating_income") if fy else None
        b = cf.balance
        inv = cf.investment_assets
        shares = float(price.listed_shares) if price.listed_shares else None

        def row(key, label, v: Value | None, basis, unit="원"):
            return {"key": key, "item": label, "value": v.value if v else None, "unit": unit, "basis": basis,
                    "status": (v.status.value if v else DataStatus.UNVERIFIED.value),
                    "source": (v.source.detail if v and v.source else None),
                    "rcept_no": (v.source.reference if v and v.source else None),
                    "notes": (v.notes if v else ["미확인"])}

        inputs = [
            row("operating_income", "영업이익", op, f"최근 사업연도({fy}) 연결 영업이익"),
            row("current_assets", "유동자산", b.get("current_assets"), f"최근 분기({cf.balance_period}) 연결"),
            row("investment_assets", "투자자산", inv, f"최근 분기({cf.balance_period}) 연결, 세부 계정 별도 표시"),
            row("current_liabilities", "유동부채", b.get("current_liabilities"), f"최근 분기({cf.balance_period}) 연결"),
            row("noncurrent_liabilities", "비유동부채", b.get("noncurrent_liabilities"),
                f"최근 분기({cf.balance_period}) 연결"),
            {"key": "listed_shares", "item": "상장주식수", "value": shares, "unit": "주",
             "basis": f"KRX 현재 상장 보통주식수({price.price_date})", "status": price.status.value,
             "source": price.source, "rcept_no": price.source_ref, "notes": []},
        ]
        res = ValuationResult(cf.stock_code, None, price.close, DataStatus.UNVERIFIED, is_default, op_multiple,
                              cl_weight, formula, inputs, investment_breakdown=cf.investment_breakdown)
        res.cautions = self._cautions(name, industry)

        missing = [i["item"] for i in inputs if i["value"] is None]
        if missing:
            res.reason = (Reason.NO_API_KEY if cf.status == "NO_API_KEY" else
                          Reason.COLLECTION_FAILED if cf.status == "FAILED" else
                          Reason.CFS_NOT_AVAILABLE if cf.status == "CFS_NOT_AVAILABLE" else Reason.INPUT_MISSING)
            res.status = DataStatus.FAILED if res.reason in (Reason.NO_API_KEY, Reason.COLLECTION_FAILED) \
                else DataStatus.UNVERIFIED
            res.steps = [{"step": "입력값 확인", "result": f"미확인 입력값: {', '.join(missing)} → 적정주가 산출하지 않음"}]
            return res
        if shares <= 0:
            res.reason = Reason.VALIDATION_FAILED
            res.steps = [{"step": "입력값 확인", "result": "상장주식수 0 이하"}]
            return res

        OP, CA, INV, CL, NCL = (i["value"] for i in inputs[:5])
        op_part = OP * op_multiple
        asset_part = CA + INV - CL * cl_weight
        numerator = op_part + asset_part - NCL
        fair = numerator / shares
        res.steps = [
            {"step": "① 영업이익 × 배수", "formula": f"{OP:,.0f} × {op_multiple:g}", "result": op_part},
            {"step": "② 유동자산 + 투자자산 − 유동부채 × 가중치",
             "formula": f"{CA:,.0f} + {INV:,.0f} − {CL:,.0f} × {cl_weight:g}", "result": asset_part},
            {"step": "③ ① + ② − 비유동부채", "formula": f"{op_part:,.0f} + {asset_part:,.0f} − {NCL:,.0f}",
             "result": numerator},
            {"step": "④ ③ ÷ 상장주식수 = 적정주가", "formula": f"{numerator:,.0f} ÷ {shares:,.0f}", "result": fair},
        ]
        res.fair_value = fair
        p = price.close
        if p and p > 0:
            res.safety_margin_pct = (fair - p) / p * 100
            res.upside_pct = res.safety_margin_pct
            res.discount_pct = (fair - p) / fair * 100 if fair > 0 else None
        res.price_20pct_off = fair * 0.8 if fair > 0 else None
        res.price_30pct_off = fair * 0.7 if fair > 0 else None
        if fair <= 0:
            res.cautions.append("적정주가 ≤ 0: 부채가 영업가치·유동성 자산을 초과하는 구조로 할인율·할인가격 산출 안 함")

        # ---------- 검증 (PART 13-10) ----------
        res.validations = self._validate(res, OP, CA, INV, CL, NCL, shares, cf)
        statuses = [DataStatus(i["status"]) for i in inputs]
        res.status = DataStatus.worst(*statuses)
        if any(v["result"] == "FAIL" for v in res.validations):
            res.status = DataStatus.worst(res.status, DataStatus.PARTIAL)
        if not is_default:
            res.cautions.insert(0, "시뮬레이션 결과이며 기본 분석 결과(원래 공식)와 다름")
        return res

    # ------------------------------------------------------------------
    @staticmethod
    def _validate(res: ValuationResult, OP, CA, INV, CL, NCL, shares, cf: CompanyFinancials) -> list[dict]:
        out = []
        # 1) 독립 재계산 (Decimal 정밀 연산)
        D = Decimal
        fair_d = ((D(str(OP)) * D(str(res.op_multiple))) + (D(str(CA)) + D(str(INV)) - D(str(CL)) * D(str(res.cl_weight)))
                  - D(str(NCL))) / D(str(shares))
        diff = abs(float(fair_d) - res.fair_value)
        out.append({"check": "공식 독립 재계산(Decimal)", "result": "PASS" if diff < 0.01 else "FAIL",
                    "detail": f"재계산 {float(fair_d):,.4f} vs 엔진 {res.fair_value:,.4f}"})
        # 2) 투자자산 내역 합계 = 투자자산
        s = sum(i["amount"] for i in cf.investment_breakdown if i["included"])
        ok = abs(s - INV) < 1
        out.append({"check": "투자자산 = 포함 계정 합계", "result": "PASS" if ok else "FAIL",
                    "detail": f"계정 합계 {s:,.0f} / 투자자산 {INV:,.0f}"})
        # 3) 투자자산 ≤ 비유동자산 (중복 계산 방지)
        nca = cf.balance.get("noncurrent_assets").value if cf.balance.get("noncurrent_assets") else None
        if nca is not None:
            out.append({"check": "투자자산 ≤ 비유동자산(중복 방지)", "result": "PASS" if INV <= nca else "FAIL",
                        "detail": f"투자자산 {INV:,.0f} / 비유동자산 {nca:,.0f}"})
        # 4) 같은 기준일의 재무상태표 값 사용
        periods = {i["basis"].split("(")[1].split(")")[0] for i in res.inputs[1:5]}
        out.append({"check": "재무상태표 입력값 기준일 일치", "result": "PASS" if len(periods) == 1 else "FAIL",
                    "detail": ", ".join(sorted(periods))})
        # 5) 단위 검증: 재무 금액은 원 단위, 주당 값 크기 확인(유동자산 ≥ 1억원)
        out.append({"check": "단위 검증(원 단위)", "result": "PASS" if CA >= 1e8 else "FAIL",
                    "detail": f"유동자산 {CA:,.0f}원, 적정주가 {res.fair_value:,.0f}원/주"})
        return out

    def _cautions(self, name: str, industry: str | None) -> list[str]:
        out = []
        ind = industry or ""
        if re.search(self.vcfg.get("financial_industry_pattern", "$^"), ind):
            out.append(f"금융업 추정(업종: {ind}) - 유동/비유동 구분이 없는 금융회사 재무제표에 일반 제조업 공식을 적용한 결과이므로 해석 주의")
        if re.search(self.vcfg.get("holding_name_pattern", "$^"), name or "") or \
                re.search(self.vcfg.get("holding_industry_pattern", "$^"), ind):
            out.append("지주회사 추정 - 자회사 가치가 연결로 합산되어 일반 제조업과 동일 공식 적용 결과의 해석 주의")
        if out:
            out.append("※ 지주·금융 여부 정식 판정은 특이사항 분석(STEP 8)에서 근거와 함께 표시 예정")
        return out
