"""단위 테스트 전용 가상 회사(DART 응답 형식)

※ 계산 로직 검증용 고정 입력값이며 실제 기업 데이터가 아님. 분석 파이프라인·대시보드에서는 절대 사용하지 않음
금액 단위: 원 (DART 와 동일). 억 = 1e8
"""
from analyzers.financial_analyzer import ReportData

E8 = 100_000_000


def row(sj, aid, nm, ord_, th=None, add=None, frm=None, frm_q=None, frm_add=None, bfe=None, cur="KRW"):
    f = lambda v: None if v is None else f"{v:,.0f}" if abs(v) >= 1000 else str(v)  # noqa: E731
    return {"sj_div": sj, "account_id": aid, "account_nm": nm, "ord": str(ord_), "currency": cur,
            "thstrm_amount": f(th), "thstrm_add_amount": f(add), "frmtrm_amount": f(frm),
            "frmtrm_q_amount": f(frm_q), "frmtrm_add_amount": f(frm_add), "bfefrmtrm_amount": f(bfe)}


def bs_rows(*, assets=2000, ca=800, nca=1200, liab=600, cl=400, ncl=200, equity=1400, parent=1350, nci=50,
            cap=5, csur=50, reta=1200, invest=True):
    """실제 DART 응답 형식: 합계 행이 구간 머리에 오고 하위 항목이 뒤따름"""
    rows = [
        row("BS", "ifrs-full_Assets", "자산총계", 7, assets * E8),
        row("BS", "ifrs-full_CurrentAssets", "유동자산", 8, ca * E8),
        row("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", 9, 500 * E8),
        row("BS", "ifrs-full_CurrentFinancialAssetsAtFairValueThroughProfitOrLoss", "단기당기손익-공정가치금융자산", 10,
            100 * E8),
        row("BS", "ifrs-full_Inventories", "재고자산", 11, 200 * E8),
        row("BS", "ifrs-full_NoncurrentAssets", "비유동자산", 17, nca * E8),
        row("BS", "ifrs-full_DeferredTaxAssets", "이연법인세자산", 18, 100 * E8),
    ]
    if invest:
        rows += [
            row("BS", "ifrs-full_InvestmentAccountedForUsingEquityMethod", "관계기업투자", 19, 100 * E8),
            row("BS", "-표준계정코드 미사용-", "기타포괄손익-공정가치측정금융자산", 20, 30 * E8),
            row("BS", "-표준계정코드 미사용-", "장기금융상품", 21, 20 * E8),
            row("BS", "ifrs-full_InvestmentProperty", "투자부동산", 22, 50 * E8),
            row("BS", "ifrs-full_NoncurrentDerivativeFinancialAssets", "비유동파생상품자산", 23, 0),
        ]
    rows += [
        row("BS", "ifrs-full_PropertyPlantAndEquipment", "유형자산", 25, (900 if invest else 1100) * E8),
        row("BS", "ifrs-full_Equity", "자본총계", 27, equity * E8),
        row("BS", "ifrs-full_EquityAttributableToOwnersOfParent", "지배기업 소유주지분", 28, parent * E8),
        row("BS", "ifrs-full_IssuedCapital", "자본금", 29, cap * E8),
        row("BS", "dart_CapitalSurplus", "자본잉여금", 30, csur * E8),
        row("BS", "ifrs-full_RetainedEarnings", "이익잉여금", 31, reta * E8),
        row("BS", "ifrs-full_NoncontrollingInterests", "비지배지분", 32, nci * E8),
        row("BS", "ifrs-full_EquityAndLiabilities", "부채와자본총계", 33, assets * E8),
        row("BS", "ifrs-full_CurrentLiabilities", "유동부채", 35, cl * E8),
        row("BS", "ifrs-full_Liabilities", "부채총계", 40, liab * E8),
        row("BS", "ifrs-full_NoncurrentLiabilities", "비유동부채", 41, ncl * E8),
    ]
    return rows


def is_rows(rev, op, ni, nip, eps):
    """각 인자는 dict(th=, add=, frm=, frm_q=, frm_add=, bfe=) — 금액은 억원, EPS 는 원"""
    def amt(d, scale):
        return {k: (v * scale if v is not None else None) for k, v in d.items()}
    return [
        row("IS", "ifrs-full_Revenue", "매출액", 1, **amt(rev, E8)),
        row("IS", "dart_OperatingIncomeLoss", "영업이익", 5, **amt(op, E8)),
        row("IS", "ifrs-full_ProfitLoss", "당기순이익", 9, **amt(ni, E8)),
        row("IS", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업의 소유주", 10, **amt(nip, E8)),
        row("IS", "ifrs-full_ProfitLossAttributableToNoncontrollingInterests", "비지배지분", 11,
            **{k: (v * 0.1 * E8 if v is not None else None) for k, v in ni.items()}),
        row("IS", "ifrs-full_BasicEarningsLossPerShare", "기본주당이익", 12, **amt(eps, 1)),
        row("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업활동현금흐름", 1,
            **amt({k: v for k, v in rev.items() if k in ("th", "frm", "bfe")}, E8 * 0.1)),
    ]


def reports(bs_kwargs=None, include_q3_2025=True, latest="3Q"):
    bs_kwargs = bs_kwargs or {}
    out = [
        ReportData("20260310000001", "20260310", "사업보고서 (2025.12)", "2025-12", "11011", 2025,
                   is_rows(rev=dict(th=1000, frm=900, bfe=950), op=dict(th=120, frm=100, bfe=110),
                           ni=dict(th=90, frm=70, bfe=75), nip=dict(th=80, frm=60, bfe=70),
                           eps=dict(th=800, frm=600, bfe=700)) + bs_rows(**bs_kwargs)),
        ReportData("20260515000001", "20260515", "분기보고서 (2026.03)", "2026-03", "11013", 2026,
                   is_rows(rev=dict(th=250, frm=230), op=dict(th=30, frm=28), ni=dict(th=22, frm=20),
                           nip=dict(th=20, frm=18), eps=dict(th=200, frm=180)) + bs_rows(**bs_kwargs)),
        ReportData("20260814000001", "20260814", "반기보고서 (2026.06)", "2026-06", "11012", 2026,
                   is_rows(rev=dict(th=270, add=520, frm_q=250, frm_add=480), op=dict(th=32, add=62, frm_add=58),
                           ni=dict(th=28, add=50, frm_add=44), nip=dict(th=25, add=45, frm_add=40),
                           eps=dict(th=250, add=450, frm_add=400)) + bs_rows(**bs_kwargs)),
    ]
    if latest == "3Q":
        out.append(ReportData(
            "20261113000001", "20261113", "분기보고서 (2026.09)", "2026-09", "11014", 2026,
            is_rows(rev=dict(th=280, add=800, frm_q=260, frm_add=740), op=dict(th=35, add=97, frm_q=30, frm_add=88),
                    ni=dict(th=28, add=78, frm_add=66), nip=dict(th=25, add=70, frm_add=60),
                    eps=dict(th=250, add=700, frm_add=600)) + bs_rows(**bs_kwargs)))
    if include_q3_2025:
        out.append(ReportData(
            "20251114000001", "20251114", "분기보고서 (2025.09)", "2025-09", "11014", 2025,
            is_rows(rev=dict(th=260, add=740), op=dict(th=30, add=88), ni=dict(th=22, add=66),
                    nip=dict(th=20, add=60), eps=dict(th=200, add=600)) + bs_rows(**bs_kwargs)))
    return out


SHARES = {"issued_shares": 10_500_000.0, "treasury_shares": 500_000.0, "outstanding_shares": 10_000_000.0,
          "stlm_dt": "2026-09-30", "rcept_no": "20261113000001"}
