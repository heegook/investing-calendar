"""연결재무제표 정규화 및 검증 (STEP 3)

DART fnlttSinglAcntAll(CFS) 응답 행을 표준 항목으로 정규화함

 - 연간(직전 사업연도 포함 3개년): 최신 사업보고서의 당기/전기/전전기 열 사용 (동일 보고서 기준으로 비교 가능성 확보)
 - 최근 분기 재무상태표: 가장 최근 정기보고서의 당기말 값
 - 최근 4개 분기 실적: 분기보고서 3개월 값, 4분기 = 연간 − 3분기 누적
 - TTM = 최근 연간 + 당기 누적 − 전년 동기 누적

계정 매칭은 표준계정ID(account_id) 우선, 없으면 계정명 정규식을 사용하며
사용한 계정ID·계정명·접수번호·열 이름을 모두 원천정보로 기록함
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from collectors.dart_collector import REPRT_ANNUAL, REPRT_H1, REPRT_Q1, REPRT_Q3, parse_amount
from core import config
from core.models import DataStatus, Reason, SourceRef, Value

# ----------------------------------------------------------------------
# 계정 매핑 테이블
# ----------------------------------------------------------------------
IS_SJ = ("IS", "CIS")


@dataclass(frozen=True)
class AccountSpec:
    key: str
    label: str
    sj: tuple[str, ...]
    ids: tuple[str, ...]
    names: str  # 정규식(공백 제거 후 비교)


ACCOUNTS: dict[str, AccountSpec] = {a.key: a for a in [
    AccountSpec("revenue", "매출액", IS_SJ, ("ifrs-full_Revenue", "ifrs_Revenue",
                                          "ifrs-full_RevenueFromContractsWithCustomers"),
                r"^(매출액|매출|영업수익|수익|순매출액|매출액및지분법손익)(\(.*\))?$"),
    AccountSpec("operating_income", "영업이익", IS_SJ, ("dart_OperatingIncomeLoss",),
                r"^(영업이익|영업손익|영업손실)(\(손실\)|\(손익\))?$"),
    AccountSpec("net_income", "연결 당기순이익", IS_SJ, ("ifrs-full_ProfitLoss", "ifrs_ProfitLoss"),
                r"^(당기순이익|당기순이익\(손실\)|당기순손익|분기순이익|분기순이익\(손실\)|반기순이익|반기순이익\(손실\)|연결당기순이익)$"),
    AccountSpec("net_income_parent", "지배기업 소유주 귀속 순이익", IS_SJ,
                ("ifrs-full_ProfitLossAttributableToOwnersOfParent", "ifrs_ProfitLossAttributableToOwnersOfParent"),
                r"^(지배기업(의)?소유주(지분|에게귀속되는.*)?|지배기업소유주귀속.*|지배주주(지분|순이익)?)$"),
    AccountSpec("eps", "기본주당이익", IS_SJ,
                ("ifrs-full_BasicEarningsLossPerShare", "ifrs_BasicEarningsLossPerShare",
                 "ifrs-full_BasicAndDilutedEarningsLossPerShare"),
                r"^(보통주)?(기본|기본및희석|기본및희석)주당(순)?(이익|손익|손실|이익\(손실\)|순이익\(손실\))(\(.*\))?$"),
    AccountSpec("operating_cf", "영업활동현금흐름", ("CF",),
                ("ifrs-full_CashFlowsFromUsedInOperatingActivities", "ifrs_CashFlowsFromUsedInOperatingActivities"),
                r"^영업활동(으로인한|으로부터의)?(순)?현금흐름$"),
    AccountSpec("total_assets", "자산총계", ("BS",), ("ifrs-full_Assets", "ifrs_Assets"), r"^자산총계$"),
    AccountSpec("total_liabilities", "부채총계", ("BS",), ("ifrs-full_Liabilities", "ifrs_Liabilities"), r"^부채총계$"),
    AccountSpec("total_equity", "자본총계", ("BS",), ("ifrs-full_Equity", "ifrs_Equity"), r"^자본총계$"),
    AccountSpec("equity_parent", "지배기업 소유주지분", ("BS",),
                ("ifrs-full_EquityAttributableToOwnersOfParent", "ifrs_EquityAttributableToOwnersOfParent"),
                r"^(지배기업(의)?소유주(에게귀속되는)?(지분|자본)|지배기업소유지분|지배주주지분)$"),
    AccountSpec("current_assets", "유동자산", ("BS",), ("ifrs-full_CurrentAssets", "ifrs_CurrentAssets"), r"^유동자산$"),
    AccountSpec("noncurrent_assets", "비유동자산", ("BS",), ("ifrs-full_NoncurrentAssets", "ifrs_NoncurrentAssets"),
                r"^비유동자산$"),
    AccountSpec("current_liabilities", "유동부채", ("BS",), ("ifrs-full_CurrentLiabilities", "ifrs_CurrentLiabilities"),
                r"^유동부채$"),
    AccountSpec("noncurrent_liabilities", "비유동부채", ("BS",),
                ("ifrs-full_NoncurrentLiabilities", "ifrs_NoncurrentLiabilities"), r"^비유동부채$"),
    AccountSpec("noncontrolling", "비지배지분", ("BS",),
                ("ifrs-full_NoncontrollingInterests", "ifrs_NoncontrollingInterests"), r"^비지배(주주)?지분$"),
    AccountSpec("net_income_nci", "비지배지분 귀속 순이익", IS_SJ,
                ("ifrs-full_ProfitLossAttributableToNoncontrollingInterests",),
                r"^비지배(주주)?지분(에귀속되는.*|순이익)?$"),
    AccountSpec("issued_capital", "자본금", ("BS",), ("ifrs-full_IssuedCapital", "ifrs_IssuedCapital"), r"^자본금$"),
    # 자본잉여금은 표준계정ID가 회사별로 상이하여 계정명으로만 매칭함
    AccountSpec("capital_surplus", "자본잉여금", ("BS",), ("dart_CapitalSurplus",), r"^자본잉여금(\(.*\))?$"),
    AccountSpec("share_premium", "주식발행초과금", ("BS",), ("ifrs-full_SharePremium", "ifrs_SharePremium"),
                r"^주식발행초과금$"),
    AccountSpec("retained_earnings", "이익잉여금", ("BS",), ("ifrs-full_RetainedEarnings", "ifrs_RetainedEarnings"),
                r"^(이익잉여금|이익잉여금\(결손금\)|결손금|미처리결손금)$"),
]}

FLOW_KEYS = ("revenue", "operating_income", "net_income", "net_income_parent", "eps")
BS_KEYS = ("total_assets", "total_liabilities", "total_equity", "equity_parent", "current_assets",
           "noncurrent_assets", "current_liabilities", "noncurrent_liabilities", "issued_capital",
           "capital_surplus", "retained_earnings", "noncontrolling")

# ----------------------------------------------------------------------
# 투자자산 정의 (고정)
#  = 비유동 금융자산(장기금융상품·공정가치측정/상각후원가측정/매도가능/만기보유 금융자산 등)
#  + 관계기업·공동기업 투자(지분법 투자)
#  + 투자부동산
#  비유동자산 구간(비유동자산 행 ~ 자산총계 행) 안의 행만 대상으로 하며, 소계 행은 중복 계산하지 않음
#  제외: 파생상품자산, 대여금, 보증금, 매출채권, 이연법인세자산, 유·무형자산, 사용권자산
# ----------------------------------------------------------------------
INVESTMENT_ID = {
    "ifrs-full_InvestmentProperty": "투자부동산",
    "ifrs-full_InvestmentsInAssociates": "관계기업 투자",
    "ifrs-full_InvestmentsInJointVentures": "공동기업 투자",
    "ifrs-full_InvestmentAccountedForUsingEquityMethod": "관계기업·공동기업 투자",
    "ifrs-full_InvestmentsInAssociatesAndJointVentures": "관계기업·공동기업 투자",
    "ifrs-full_NoncurrentFinancialAssets": "비유동 금융자산",
    "ifrs-full_OtherNoncurrentFinancialAssets": "비유동 금융자산",
}
INVESTMENT_NAME = [
    ("투자부동산", r"투자부동산"),
    ("관계기업·공동기업 투자", r"(관계기업|공동기업|지분법).*(투자|주식)"),
    ("비유동 금융자산", r"(장기금융상품|장기투자증권|장기투자자산|매도가능(금융자산|증권)|만기보유(금융자산|증권)|"
                   r"공정가치.*(금융자산|지분상품|채무상품|증권)|기타포괄손익.*(금융자산|증권|지분상품)|"
                   r"당기손익.*(금융자산|증권)|상각후원가.*(금융자산|증권)|^(기타)?비유동금융자산$|^장기금융자산$|"
                   r"^기타장기금융자산$|^투자자산$|^기타투자자산$|^장기예금$)"),
]
INVESTMENT_AGGREGATE = (r"^(장기투자자산|투자자산|기타투자자산|비유동금융자산|기타비유동금융자산|장기금융자산|"
                        r"관계기업및공동기업투자|관계기업투자등)$")
BOUNDARY_IDS = {"ifrs-full_Assets", "ifrs-full_CurrentAssets", "ifrs-full_NoncurrentAssets", "ifrs-full_Equity",
                "ifrs-full_EquityAttributableToOwnersOfParent", "ifrs-full_EquityAndLiabilities",
                "ifrs-full_Liabilities", "ifrs-full_CurrentLiabilities", "ifrs-full_NoncurrentLiabilities"}
INVESTMENT_EXCLUDE = r"(파생|대여금|보증금|채권|이연법인세|유형자산|무형자산|사용권|선급|리스채권)"


def _norm(s: str | None) -> str:
    """공백 제거 + 앞머리 번호(Ⅰ. / 1. / (1) / 가.) 제거"""
    t = re.sub(r"\s+", "", s or "")
    return re.sub(r"^(\(?[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX0-9]{1,4}[.)]|\([0-9]{1,2}\)|[가-하]\.)", "", t)


# ----------------------------------------------------------------------
@dataclass
class ReportData:
    """DART 정기보고서 1건의 연결재무제표"""
    rcept_no: str
    rcept_dt: str
    report_nm: str
    period: str          # YYYY-MM
    reprt_code: str
    bsns_year: int
    rows: list[dict[str, Any]]
    collected_at: str = ""
    fs_div: str = "CFS"

    @property
    def is_annual(self) -> bool:
        return self.reprt_code == REPRT_ANNUAL

    @property
    def quarter_no(self) -> int:
        return {REPRT_Q1: 1, REPRT_H1: 2, REPRT_Q3: 3, REPRT_ANNUAL: 4}[self.reprt_code]


@dataclass
class CompanyFinancials:
    stock_code: str
    status: str                               # OK / CFS_NOT_AVAILABLE / NOT_FOUND / FAILED / NO_API_KEY
    message: str = ""
    annual: dict[str, dict[str, Value]] = field(default_factory=dict)      # {'2025-12': {...}}
    annual_periods: list[str] = field(default_factory=list)               # 최신 → 과거
    balance: dict[str, Value] = field(default_factory=dict)
    balance_period: str | None = None
    investment_assets: Value | None = None
    investment_breakdown: list[dict[str, Any]] = field(default_factory=list)
    quarters: list[dict[str, Any]] = field(default_factory=list)           # 최신 → 과거 4개
    ttm: dict[str, Value] = field(default_factory=dict)
    ttm_basis: str = ""
    latest_q_yoy: dict[str, Any] = field(default_factory=dict)
    restated: bool = False                                                 # 전기 재작성 감지            # 최근 분기 영업이익 전년 동기 대비
    shares: dict[str, Any] = field(default_factory=dict)                   # DART 주식총수
    validations: list[dict[str, Any]] = field(default_factory=list)
    bs_dump: list[tuple] = field(default_factory=list)
    eps_rows: list[tuple] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)

    def add_check(self, name: str, ok: bool | None, detail: str) -> None:
        self.validations.append({"check": name, "result": "PASS" if ok else ("N/A" if ok is None else "FAIL"),
                                 "detail": detail})

    def check_ok(self, name: str) -> bool | None:
        for v in self.validations:
            if v["check"] == name:
                return {"PASS": True, "FAIL": False}.get(v["result"])
        return None


# ----------------------------------------------------------------------
class FinancialAnalyzer:
    def __init__(self):
        self.tol_bs = config.settings()["validation"]["balance_sheet_tolerance_pct"] / 100

    # ---------------- 계정 탐색 ----------------
    @staticmethod
    def find_account(rows: list[dict[str, Any]], spec: AccountSpec) -> tuple[dict | None, list[str]]:
        notes: list[str] = []
        cands = [r for r in rows if r.get("sj_div") in spec.sj]
        # IS 가 있으면 IS 우선 (CIS 와 중복 방지)
        for sj in spec.sj:
            sub = [r for r in cands if r.get("sj_div") == sj]
            hits = [r for r in sub if r.get("account_id") in spec.ids]
            how = "account_id"
            if not hits:
                hits = [r for r in sub if re.search(spec.names, _norm(r.get("account_nm")))]
                how = "account_nm"
            if hits:
                hits.sort(key=lambda r: int(r.get("ord") or 0))
                vals = {parse_amount(h.get("thstrm_amount")) for h in hits}
                if len(hits) > 1 and len(vals) > 1:
                    notes.append(f"{spec.label}: 후보 {len(hits)}개 중 첫 행 사용({how})")
                return hits[0], notes
        return None, notes

    def _value(self, rep: ReportData, spec: AccountSpec, column: str) -> Value:
        row, notes = self.find_account(rep.rows, spec)
        if row is None and spec.key == "eps":
            return self._eps_from_components(rep, column)
        if row is None:
            return Value(None, DataStatus.UNVERIFIED, reason=Reason.ACCOUNT_NOT_FOUND,
                         notes=[f"{spec.label} 계정 미확인({rep.report_nm})"])
        cur = (row.get("currency") or "KRW").upper()
        amt = parse_amount(row.get(column))
        src = SourceRef("DART_OPENAPI", rep.rcept_no, rep.period, rep.collected_at,
                        f"{rep.report_nm} | {row.get('sj_div')} | {row.get('account_id')} | {row.get('account_nm')} | {column}")
        if cur != "KRW":
            return Value(None, DataStatus.UNVERIFIED, src, Reason.VALIDATION_FAILED, [f"통화 {cur} - 원화 아님"])
        if amt is None:
            return Value(None, DataStatus.UNVERIFIED, src, Reason.ACCOUNT_NOT_FOUND, [f"{column} 값 없음"] + notes)
        return Value(amt, DataStatus.VERIFIED if not notes else DataStatus.PARTIAL, src, notes=notes)

    def _eps_from_components(self, rep: ReportData, column: str) -> Value:
        """기본주당이익 합계 행이 없고 계속영업·중단영업 기본주당이익만 공시된 경우 합산(PARTIAL)

        중단영업 행이 없거나 공란이면 계속영업 값만 사용하고 그 사실을 표시함
        """
        rows = [r for r in rep.rows if r.get("sj_div") in IS_SJ]
        cont = next((r for r in rows if r.get("account_id") == "ifrs-full_BasicEarningsLossPerShareFromContinuingOperations"
                     or re.search(r"^계속영업(기본)?주당(순)?(이익|손익|손실)", _norm(r.get("account_nm")))), None)
        if cont is None or parse_amount(cont.get(column)) is None:
            return Value(None, DataStatus.UNVERIFIED, reason=Reason.ACCOUNT_NOT_FOUND,
                         notes=[f"기본주당이익 계정 미확인({rep.report_nm})"])
        disc = next((r for r in rows if r.get("account_id") == "ifrs-full_BasicEarningsLossPerShareFromDiscontinuedOperations"
                     or re.search(r"^중단영업(기본)?주당(순)?(이익|손익|손실)", _norm(r.get("account_nm")))), None)
        c = parse_amount(cont.get(column))
        d = parse_amount(disc.get(column)) if disc else None
        src = SourceRef("DART_OPENAPI", rep.rcept_no, rep.period, rep.collected_at,
                        f"{rep.report_nm} | 계속영업+중단영업 기본주당이익 | {column}")
        note = (f"기본주당이익 합계 행 미공시 → 계속영업 {c:,.0f}원 + 중단영업 {d:,.0f}원" if d is not None else
                f"기본주당이익 합계 행 미공시 → 계속영업 {c:,.0f}원 사용(중단영업 주당이익 공란)")
        return Value(c + (d or 0.0), DataStatus.PARTIAL, src, notes=[note])

    # ---------------- 투자자산 ----------------
    @staticmethod
    def _is_boundary(r: dict[str, Any]) -> bool:
        return (r.get("account_id") in BOUNDARY_IDS
                or bool(re.search(r"(총계|합계)$|^(유동|비유동)(자산|부채)$|^(자본|부채)$", _norm(r.get("account_nm")))))

    def _noncurrent_section(self, bs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str] | None:
        """비유동자산 합계 행을 기준으로 앞·뒤 연속 구간 중 합계가 일치하는 구간을 선택

        DART 응답은 '자산총계 → 유동자산 합계 → 유동 항목 → 비유동자산 합계 → 비유동 항목' 처럼
        합계 행이 구간 머리에 오는 형식이 일반적이나 회사별 차이가 있어 합계 일치로 확정함
        """
        h = next((i for i, r in enumerate(bs) if r.get("account_id") in ACCOUNTS["noncurrent_assets"].ids
                  or re.search(ACCOUNTS["noncurrent_assets"].names, _norm(r.get("account_nm")))), None)
        if h is None:
            return None
        total = parse_amount(bs[h].get("thstrm_amount"))
        after, j = [], h + 1
        while j < len(bs) and not self._is_boundary(bs[j]):
            after.append(bs[j]); j += 1
        before, j = [], h - 1
        while j >= 0 and not self._is_boundary(bs[j]):
            before.insert(0, bs[j]); j -= 1
        for label, rows in (("비유동자산 합계 행 뒤 구간", after), ("비유동자산 합계 행 앞 구간", before)):
            if not rows or total is None:
                continue
            amts = [(parse_amount(r.get("thstrm_amount")) or 0.0, r) for r in rows]
            s_all = sum(a for a, _ in amts)
            s_leaf = sum(a for a, r in amts if not re.search(INVESTMENT_AGGREGATE, _norm(r.get("account_nm"))))
            tol = max(1.0, abs(total) * 0.005)
            if abs(s_all - total) <= tol or abs(s_leaf - total) <= tol:
                return rows, f"{label}(항목 합계 = 비유동자산 {total:,.0f} 일치)"
        return None

    def investment_assets(self, rep: ReportData) -> tuple[Value, list[dict[str, Any]]]:
        """비유동자산 구간에서 투자자산 계정 합산

        1) ord 정렬 → 응답 순서 순으로 비유동자산 구간을 찾고 항목 합계가 비유동자산 합계와 일치해야 확정
        2) 실패 시 계정명 기준(단기·유동 표기 제외)으로 분류하고 PARTIAL 표시
        """
        bs_raw = [r for r in rep.rows if r.get("sj_div") == "BS"]
        found = None
        for rows in (sorted(bs_raw, key=lambda r: int(r.get("ord") or 0)), bs_raw):
            found = self._noncurrent_section(rows)
            if found:
                break
        if found:
            candidates, method = found
            fallback = False
        else:
            candidates = [r for r in bs_raw if not self._is_boundary(r)
                          and not re.match(r"^(단기|유동)", _norm(r.get("account_nm")))
                          and "Current" not in (r.get("account_id") or "").replace("Noncurrent", "")]
            method, fallback = "계정명 기준(구간 합계 불일치)", True
        start, end = 0, len(candidates)
        bs = candidates
        items = []
        for r in bs[start:end]:
            nm = _norm(r.get("account_nm"))
            cat = INVESTMENT_ID.get(r.get("account_id"))
            if cat is None and not re.search(INVESTMENT_EXCLUDE, nm):
                cat = next((c for c, p in INVESTMENT_NAME if re.search(p, nm)), None)
            if cat is None:
                continue
            amt = parse_amount(r.get("thstrm_amount"))
            if amt is None:
                continue
            items.append({"category": cat, "account_id": r.get("account_id"), "account_nm": r.get("account_nm"),
                          "ord": r.get("ord"), "amount": amt, "included": True, "note": ""})
        # 설정: 기타비유동금융자산 포함 여부
        inc_other = config.settings().get("valuation", {}).get("investment_assets", {}) \
            .get("include_other_noncurrent_financial_assets", True)
        if not inc_other:
            for it in items:
                if it["account_id"] == "ifrs-full_OtherNoncurrentFinancialAssets" or \
                        re.search(r"^(기타)?(비유동|장기)?기타?금융자산$", _norm(it["account_nm"])):
                    it["included"] = False
                    it["note"] = "설정에 따라 기타비유동금융자산 제외"
        # 소계 행 중복 제거: 집합형 계정명(장기투자자산 등) 행의 금액이 바로 뒤 연속 2개 이상 항목 합과 같으면 소계로 보고 제외
        for i, it in enumerate(items):
            if not (re.search(INVESTMENT_AGGREGATE, _norm(it["account_nm"]))
                    or it["account_id"] in ("ifrs-full_NoncurrentFinancialAssets",)):
                continue
            acc = 0.0
            for j in range(i + 1, len(items)):
                acc += items[j]["amount"]
                if j - i >= 2 and abs(acc - it["amount"]) <= max(1.0, abs(it["amount"]) * 1e-6):
                    it["included"] = False
                    it["note"] = f"하위 {j - i}개 항목의 소계로 판단하여 제외(중복 방지)"
                    break
        total = sum(it["amount"] for it in items if it["included"])
        src = SourceRef("DART_OPENAPI", rep.rcept_no, rep.period, rep.collected_at,
                        f"{rep.report_nm} | BS 비유동자산 구간 투자자산 {sum(i['included'] for i in items)}개 계정 합계")
        notes = [f"구간 식별: {method}"] + ([] if items else ["투자자산 해당 계정 없음 → 0원으로 처리"])
        status = DataStatus.VERIFIED if items and not fallback else DataStatus.PARTIAL
        if fallback:
            notes.append("비유동자산 구간 식별 실패 → 계정명 기준 분류(유동 항목 혼입 가능성 점검 필요)")
        return Value(total, status, src, notes=notes), items

    # ---------------- 메인 ----------------
    def analyze(self, stock_code: str, reports: list[ReportData], shares: dict[str, Any] | None = None,
                settle_month: int = 12) -> CompanyFinancials:
        cf = CompanyFinancials(stock_code, "OK", shares=shares or {})
        reports = sorted(reports, key=lambda r: (r.period, r.is_annual), reverse=True)
        cf.reports = [{"rcept_no": r.rcept_no, "report_nm": r.report_nm, "period": r.period,
                       "reprt_code": r.reprt_code, "bsns_year": r.bsns_year, "fs_div": r.fs_div} for r in reports]
        if not reports:
            cf.status, cf.message = "NOT_FOUND", "연결재무제표 미확인"
            return cf
        # 1) 연결/별도 구분 검증
        cf.add_check("연결재무제표(CFS) 사용", all(r.fs_div == "CFS" for r in reports),
                     ", ".join(f"{r.report_nm}:{r.fs_div}" for r in reports))

        latest = reports[0]
        annuals = [r for r in reports if r.is_annual]

        # 2) 연간 3개년 (최신 사업보고서의 당기/전기/전전기)
        if annuals:
            a = annuals[0]
            y, m = map(int, a.period.split("-"))
            cols = [("thstrm_amount", f"{y}-{m:02d}"), ("frmtrm_amount", f"{y - 1}-{m:02d}"),
                    ("bfefrmtrm_amount", f"{y - 2}-{m:02d}")]
            for col, per in cols:
                vals = {k: self._value(a, ACCOUNTS[k], col) for k in FLOW_KEYS + ("operating_cf",)}
                cf.annual[per] = vals
                cf.annual_periods.append(per)
            # 전전기 매출이 비어 있으면 이전 사업보고서로 보완
            if cf.annual[cols[2][1]]["revenue"].value is None and len(annuals) > 1:
                prev = annuals[1]
                cf.annual[cols[2][1]] = {k: self._value(prev, ACCOUNTS[k], "frmtrm_amount")
                                         for k in FLOW_KEYS + ("operating_cf",)}
        else:
            cf.message = "최근 사업보고서(연결) 미확인"

        # 3) 최근 분기 재무상태표
        cf.balance = {k: self._value(latest, ACCOUNTS[k], "thstrm_amount") for k in BS_KEYS}
        cf.balance_period = latest.period
        cf.investment_assets, cf.investment_breakdown = self.investment_assets(latest)
        cf.eps_rows = [(a.report_nm, r.get("sj_div"), r.get("account_id"), r.get("account_nm"), r.get("thstrm_amount"))
                       for a in (annuals[:1] + [latest]) for r in a.rows
                       if "주당" in (r.get("account_nm") or "") or "PerShare" in (r.get("account_id") or "")]
        cf.bs_dump = [(r.get("ord"), r.get("account_id"), r.get("account_nm"), r.get("thstrm_amount"))
                      for r in latest.rows if r.get("sj_div") == "BS"]

        self._parent_fallbacks(cf, latest, annuals)
        self._validate_balance(cf)
        self._quarters_and_ttm(cf, reports, annuals)
        self._validate_shares(cf)
        return cf

    # ---------------- 지배지분 계정 부재 시 처리 ----------------
    def _parent_fallbacks(self, cf: CompanyFinancials, latest: ReportData, annuals: list[ReportData]) -> None:
        """지배기업 소유주지분 행이 없고 비지배지분 행도 없는 경우에만 자본총계 = 지배지분으로 간주(PARTIAL)

        비지배지분이 존재하는데 지배지분 행이 없으면 값을 만들지 않고 null 유지
        """
        b = cf.balance
        if b["equity_parent"].value is None and b["total_equity"].value is not None:
            nci_row, _ = self.find_account(latest.rows, ACCOUNTS["noncontrolling"])
            if nci_row is None:
                te = b["total_equity"]
                b["equity_parent"] = Value(te.value, DataStatus.worst(te.status, DataStatus.PARTIAL), te.source,
                                           notes=["지배기업 소유주지분·비지배지분 행 미표시 → 자본총계를 지배지분으로 간주"])
        if b["capital_surplus"].value is None:
            sp = self._value(latest, ACCOUNTS["share_premium"], "thstrm_amount")
            if sp.value is not None:
                b["capital_surplus"] = Value(sp.value, DataStatus.worst(sp.status, DataStatus.PARTIAL), sp.source,
                                             notes=["자본잉여금 행 미표시 → 구성항목인 주식발행초과금만 반영"
                                                    "(기타자본잉여금은 기타자본항목에 포함되어 미반영 가능)"])
        if annuals:
            a = annuals[0]
            nci_row, _ = self.find_account(a.rows, ACCOUNTS["net_income_nci"])
            for per, vals in cf.annual.items():
                if vals["net_income_parent"].value is None and vals["net_income"].value is not None and nci_row is None:
                    ni = vals["net_income"]
                    vals["net_income_parent"] = Value(ni.value, DataStatus.worst(ni.status, DataStatus.PARTIAL),
                                                      ni.source, notes=["지배/비지배 귀속 구분 행 미표시 → 연결 당기순이익을 지배귀속으로 간주"])

    # ---------------- 검증: 자산 = 부채 + 자본 ----------------
    def _validate_balance(self, cf: CompanyFinancials) -> None:
        b = cf.balance
        A, L, E = (b[k].value for k in ("total_assets", "total_liabilities", "total_equity"))
        if None in (A, L, E):
            cf.add_check("자산총계 = 부채총계 + 자본총계", None, "입력 계정 일부 미확인")
        else:
            diff = abs(A - (L + E)) / abs(A) if A else 1
            ok = diff <= self.tol_bs
            cf.add_check("자산총계 = 부채총계 + 자본총계", ok, f"A={A:,.0f} L+E={L + E:,.0f} 차이 {diff * 100:.4f}%")
            if not ok:
                for k in BS_KEYS:
                    b[k].status = DataStatus.worst(b[k].status, DataStatus.PARTIAL)
        CA, NCA = b["current_assets"].value, b["noncurrent_assets"].value
        if None not in (A, CA, NCA):
            diff = abs(A - (CA + NCA)) / abs(A) if A else 1
            cf.add_check("자산총계 = 유동자산 + 비유동자산", diff <= self.tol_bs, f"차이 {diff * 100:.4f}%")
        CL, NCL = b["current_liabilities"].value, b["noncurrent_liabilities"].value
        if None not in (L, CL, NCL):
            diff = abs(L - (CL + NCL)) / abs(L) if L else 1
            cf.add_check("부채총계 = 유동부채 + 비유동부채", diff <= self.tol_bs, f"차이 {diff * 100:.4f}%")
        # 단위 검증: DART 금액은 원 단위. 자산총계가 1억원 미만이면 단위(천원/백만원) 오류 의심
        if A is not None:
            cf.add_check("단위 검증(원 단위)", A >= 1e8, f"자산총계 {A:,.0f}원")

    # ---------------- 분기 실적 및 TTM ----------------
    def _quarters_and_ttm(self, cf: CompanyFinancials, reports: list[ReportData], annuals: list[ReportData]) -> None:
        latest = reports[0]
        flow = ("revenue", "operating_income", "net_income", "net_income_parent", "eps")

        def q_vals(rep: ReportData, column: str) -> dict[str, Value]:
            return {k: self._value(rep, ACCOUNTS[k], column) for k in flow}

        # --- 최근 4개 분기 (개별 분기 3개월 실적) ---
        by_period = {r.period: r for r in reports}
        quarters: list[dict[str, Any]] = []
        for rep in reports:
            if len(quarters) >= 4:
                break
            if rep.is_annual:
                # 4분기 = 연간 − 3분기 누적 (같은 회계연도의 3분기 보고서 필요)
                y, m = map(int, rep.period.split("-"))
                q3_period = _shift_month(rep.period, -3)
                q3 = by_period.get(q3_period)
                nxt = by_period.get(_shift_month(rep.period, 9))
                vals = {}
                for k in flow:
                    av = self._value(rep, ACCOUNTS[k], "thstrm_amount")
                    # 우선: 익년 3분기 보고서의 전기 누적(재작성 반영, TTM 산식의 전년동기누적과 동일 기준)
                    qv_r = self._value(nxt, ACCOUNTS[k], "frmtrm_add_amount") if nxt else None
                    qv_o = self._value(q3, ACCOUNTS[k], "thstrm_add_amount") if q3 else None
                    if qv_r is not None and qv_r.value is not None:
                        v = _diff(av, qv_r, f"{k}: 연간({rep.rcept_no}) − 전년 3분기 누적(익년 3분기 보고서 {nxt.rcept_no} 전기누적)")
                        if qv_o is not None and qv_o.value is not None and qv_r.value and \
                                abs(qv_o.value - qv_r.value) / abs(qv_r.value) > 0.005:
                            v.notes.append(f"전기 재작성 감지: 3분기 누적 최초공시 {qv_o.value:,.0f} → 재작성 {qv_r.value:,.0f}")
                            cf.restated = True
                    elif qv_o is not None:
                        v = _diff(av, qv_o, f"{k}: 연간({rep.rcept_no}) − 3분기 누적({q3.rcept_no})")
                    else:
                        v = Value(None, DataStatus.UNVERIFIED, reason=Reason.STATEMENT_NOT_FOUND, notes=["3분기 누적 미확인"])
                    vals[k] = v
                if "eps" in vals and vals["eps"].value is not None:
                    vals["eps"].notes.append("4분기 EPS는 연간−누적 차감값(주식수 변동 시 근사치)")
                quarters.append({"period": rep.period, "label": f"{rep.period} (4Q, 산출)", "derived": True, **vals})
            else:
                quarters.append({"period": rep.period, "label": f"{rep.period} ({rep.quarter_no}Q)", "derived": False,
                                 **q_vals(rep, "thstrm_amount")})
        # 분기 보고서가 빠진 경우 전기 동분기 열로 보완하지 않고 누락으로 둠 (임의 보간 금지)
        cf.quarters = quarters

        # --- 최근 분기 영업이익 전년 동기 대비 (방향성) ---
        if not latest.is_annual:
            cur = self._value(latest, ACCOUNTS["operating_income"], "thstrm_amount")
            prev = self._value(latest, ACCOUNTS["operating_income"],
                               "frmtrm_amount" if latest.reprt_code == REPRT_Q1 else "frmtrm_q_amount")
            if cur.value is not None and prev.value is not None:
                yoy = (cur.value - prev.value) / abs(prev.value) * 100 if prev.value else None
                cf.latest_q_yoy = {"period": latest.period, "op_current": cur.value, "op_prior_year": prev.value,
                                   "yoy_pct": yoy, "rcept_no": latest.rcept_no}
        # --- TTM ---
        if latest.is_annual:
            cf.ttm_basis = f"최근 사업보고서({latest.period}) 연간 값"
            cf.ttm = {k: self._value(latest, ACCOUNTS[k], "thstrm_amount") for k in flow}
        else:
            prior_annual_period = _prior_fy_end(latest.period, latest.quarter_no)
            a = next((r for r in annuals if r.period == prior_annual_period), None)
            if a is None:
                cf.ttm_basis = f"직전 사업연도({prior_annual_period}) 사업보고서 미확인"
                cf.ttm = {k: Value(None, DataStatus.UNVERIFIED, reason=Reason.STATEMENT_NOT_FOUND,
                                   notes=[cf.ttm_basis]) for k in flow}
            else:
                cur_col = "thstrm_add_amount" if latest.reprt_code != REPRT_Q1 else "thstrm_amount"
                prev_col = "frmtrm_add_amount" if latest.reprt_code != REPRT_Q1 else "frmtrm_amount"
                cf.ttm_basis = (f"TTM = 연간({a.period}, {a.rcept_no}) + 당기누적({latest.period}, {latest.rcept_no}) "
                                f"− 전년동기누적({latest.rcept_no}.{prev_col})")
                for k in flow:
                    av = self._value(a, ACCOUNTS[k], "thstrm_amount")
                    cv = self._value(latest, ACCOUNTS[k], cur_col)
                    if latest.reprt_code == REPRT_Q1 and cv.value is None:
                        cv = self._value(latest, ACCOUNTS[k], "thstrm_add_amount")
                    pv = self._value(latest, ACCOUNTS[k], prev_col)
                    if latest.reprt_code == REPRT_Q1 and pv.value is None:
                        pv = self._value(latest, ACCOUNTS[k], "frmtrm_q_amount")
                    cf.ttm[k] = _ttm(av, cv, pv)

        # --- TTM 검증: 최근 4개 분기 합계와 비교 ---
        if len(cf.quarters) == 4 and not latest.is_annual:
            for k in ("revenue", "operating_income", "net_income_parent"):
                qs = [q[k].value for q in cf.quarters]
                t = cf.ttm.get(k)
                if None in qs or t is None or t.value is None:
                    cf.add_check(f"TTM 검증({ACCOUNTS[k].label})", None, "분기 값 일부 미확인")
                    continue
                s = sum(qs)
                diff = abs(s - t.value) / abs(t.value) if t.value else (0 if s == 0 else 1)
                ok = diff <= 0.005
                cf.add_check(f"TTM 검증({ACCOUNTS[k].label})", ok,
                             f"4개 분기 합 {s:,.0f} vs TTM {t.value:,.0f} (차이 {diff * 100:.3f}%)")
                if not ok:
                    t.status = DataStatus.worst(t.status, DataStatus.PARTIAL)
                    t.notes.append("4개 분기 합계와 불일치")
        # 누적/분기 구분 검증: 반기·3분기 보고서에서 누적 ≥ 3개월 (매출 기준)
        if latest.reprt_code in (REPRT_H1, REPRT_Q3):
            q = self._value(latest, ACCOUNTS["revenue"], "thstrm_amount").value
            c = self._value(latest, ACCOUNTS["revenue"], "thstrm_add_amount").value
            if q is not None and c is not None:
                cf.add_check("누적/분기 실적 구분", c >= q and c != q,
                             f"3개월 {q:,.0f} / 누적 {c:,.0f}")

    # ---------------- 주식수 검증 ----------------
    def _validate_shares(self, cf: CompanyFinancials) -> None:
        s = cf.shares
        if not s:
            cf.add_check("주식수·자기주식수 구분", None, "DART 주식총수 현황 미확인")
            return
        iss, tre, out = s.get("issued_shares"), s.get("treasury_shares"), s.get("outstanding_shares")
        if None not in (iss, tre, out):
            cf.add_check("주식수·자기주식수 구분", abs(iss - tre - out) <= 1,
                         f"발행 {iss:,.0f} − 자기 {tre:,.0f} = 유통 {out:,.0f}")
        else:
            cf.add_check("주식수·자기주식수 구분", None, f"발행 {iss} / 자기 {tre} / 유통 {out}")


# ----------------------------------------------------------------------
def _shift_month(period: str, months: int) -> str:
    y, m = map(int, period.split("-"))
    t = y * 12 + (m - 1) + months
    return f"{t // 12}-{t % 12 + 1:02d}"


def _prior_fy_end(period: str, quarter_no: int) -> str:
    """분기 보고기간 말 → 직전 회계연도 말"""
    return _shift_month(period, -3 * quarter_no)


def _diff(a: Value, b: Value, note: str) -> Value:
    if a.value is None or b.value is None:
        return Value(None, DataStatus.worst(a.status, b.status, DataStatus.UNVERIFIED),
                     reason=Reason.INPUT_MISSING, notes=[note, *a.notes, *b.notes])
    return Value(a.value - b.value, DataStatus.worst(a.status, b.status), a.source, notes=[note])


def _ttm(a: Value, c: Value, p: Value) -> Value:
    note = "연간 + 당기누적 − 전년동기누적"
    if None in (a.value, c.value, p.value):
        return Value(None, DataStatus.worst(a.status, c.status, p.status, DataStatus.UNVERIFIED),
                     reason=Reason.INPUT_MISSING, notes=[note, *a.notes, *c.notes, *p.notes])
    return Value(a.value + c.value - p.value, DataStatus.worst(a.status, c.status, p.status), c.source,
                 notes=[f"{note}: {a.value:,.2f} + {c.value:,.2f} − {p.value:,.2f}"],
                 inputs={"annual": a.value, "current_cum": c.value, "prior_cum": p.value,
                         "sources": [x.source.__dict__ for x in (a, c, p) if x.source]})
