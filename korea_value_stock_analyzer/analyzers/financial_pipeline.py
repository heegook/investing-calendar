"""DART 수집 → 연결재무제표 정규화 → DB 저장 연결 로직 (STEP 2~3)"""
from __future__ import annotations

from datetime import date

from analyzers.financial_analyzer import CompanyFinancials, FinancialAnalyzer, ReportData
from collectors.dart_collector import (DartCollector, DartError, NoApiKey, only_common_class, parse_amount,
                                       share_row)
from core import config
from core.trading_calendar import now_kst
from database.db_manager import DBManager

AMOUNT_COLS = ("thstrm_amount", "thstrm_add_amount", "frmtrm_amount", "frmtrm_q_amount",
               "frmtrm_add_amount", "bfefrmtrm_amount")


class FinancialPipeline:
    def __init__(self, db: DBManager, dart: DartCollector | None = None):
        self.db = db
        self.dart = dart or DartCollector()
        self.analyzer = FinancialAnalyzer()
        self.max_reports = config.settings()["dart"]["max_reports_per_company"]
        self.log = config.setup_logging("financial_pipeline")
        self._corp_map: dict | None = None

    def corp_map(self) -> dict:
        if self._corp_map is None:
            self._corp_map = self.dart.corp_codes()
        return self._corp_map

    def collect(self, stock_code: str, settle_month: int | None, ref: date,
                listed_shares: int | None = None) -> CompanyFinancials:
        if not self.dart.available:
            return CompanyFinancials(stock_code, "NO_API_KEY", "DART_API_KEY 미설정 - 재무제표 수집 불가(값을 채우지 않음)")
        try:
            cmap = self.corp_map()
        except DartError as e:
            return CompanyFinancials(stock_code, "FAILED", f"DART 고유번호 수집 실패: {e}")
        info = cmap.get(stock_code)
        if not info:
            return CompanyFinancials(stock_code, "NOT_FOUND", "DART 고유번호 미확인(공시대상 아님 또는 코드 불일치)")
        corp = info["corp_code"]
        self.db.execute("UPDATE companies SET corp_code=? WHERE stock_code=?", (corp, stock_code))
        sm = settle_month or 12
        try:
            reports = self.dart.periodic_reports(corp, ref)
            if not reports:
                return CompanyFinancials(stock_code, "NOT_FOUND", "최근 정기보고서 미확인")
            latest = self.dart.latest_per_period(reports)[: self.max_reports]
            rcepts = {r.rcept_no for r in reports}
            fetched: list[ReportData] = []
            statuses = []
            for rep in latest:
                fx = self.dart.fetch_statement(corp, rep, sm, rcepts)
                statuses.append(fx.status)
                self.db.upsert("dart_reports", [{
                    "stock_code": stock_code, "corp_code": corp, "rcept_no": rep.rcept_no, "rcept_dt": rep.rcept_dt,
                    "report_nm": rep.report_nm, "period": rep.period, "bsns_year": fx.bsns_year,
                    "reprt_code": fx.reprt_code, "fs_div": "CFS" if fx.status == "OK" else "NONE",
                    "fetch_status": fx.status, "message": fx.message, "collected_at": fx.collected_at}])
                if fx.status == "OK":
                    rd = ReportData(rep.rcept_no, rep.rcept_dt, rep.report_nm, rep.period, fx.reprt_code,
                                    fx.bsns_year, fx.rows, fx.collected_at, "CFS")
                    fetched.append(rd)
                    self._save_rows(stock_code, corp, rd)
            if not fetched:
                if "FAILED" in statuses:
                    return CompanyFinancials(stock_code, "FAILED", "재무제표 수집 실패(API 오류)")
                if "CFS_NOT_AVAILABLE" in statuses:
                    return CompanyFinancials(stock_code, "CFS_NOT_AVAILABLE",
                                             "연결재무제표 확인 불가(별도재무제표만 공시) - 별도 대체하지 않음")
                return CompanyFinancials(stock_code, "NOT_FOUND", "연결재무제표 미확인")
            shares, diags, latest_total = {}, [], None
            ordered = sorted(fetched, key=lambda r: (r.period, r.is_annual), reverse=True)[:3]
            for idx, rep in enumerate(ordered):
                shares = self.dart.stock_totals(corp, rep.bsns_year, rep.reprt_code) or {}
                diags.append(self.dart.last_stock_diag)
                rows = self.dart.last_stock_rows
                if idx == 0 and not shares:
                    latest_total = next((it for it in rows if "합계" in (it.get("se") or "")), None)
                if shares.get("outstanding_shares"):
                    if (idx > 0 and latest_total is not None and only_common_class(rows)
                            and share_row(latest_total).get("outstanding_shares") is not None):
                        # 직전 보고서에서 보통주 단일 종류가 확인되면 최신 보고서의 '합계' 행 = 보통주
                        shares = share_row(latest_total)
                        shares["note"] = (f"최신 보고서({ordered[0].period})는 합계 행만 공시, "
                                          f"{rep.period} 보고서에서 우선주 등 종류주식 0주 확인 → 합계를 보통주로 사용")
                    elif idx > 0:
                        # 최신 보고서의 자기주식 취득·처분 현황(보통주 총계 기말수량)으로 보완
                        top = ordered[0]
                        tr = self.dart.treasury_period_end(corp, top.bsns_year, top.reprt_code)
                        diags.append(self.dart.last_stock_diag)
                        same_issued = listed_shares is not None and shares.get("issued_shares") is not None and \
                            abs(listed_shares - shares["issued_shares"]) < 1
                        if tr is not None and tr["treasury_shares"] is not None and same_issued:
                            shares["treasury_shares"] = tr["treasury_shares"]
                            shares["outstanding_shares"] = shares["issued_shares"] - tr["treasury_shares"]
                            shares["stlm_dt"] = tr.get("stlm_dt") or shares.get("stlm_dt")
                            shares["note"] = (f"자기주식: {top.period} 자기주식 취득·처분 현황 보통주 기말수량 / "
                                              f"발행주식: {rep.period} 주식총수(현재 KRX 상장주식수와 동일 확인)")
                        else:
                            shares["note"] = (shares.get("note", "") + f" 최신 보고서({top.period})는 보통주 행 미공시 → "
                                              f"{rep.period} 기준 자기주식수 사용(이후 변동 미반영)").strip()
                    break
            shares["_diag"] = diags
            if shares.get("outstanding_shares") is not None:
                self.db.upsert("share_counts", [{
                    "stock_code": stock_code, "rcept_no": shares.get("rcept_no") or ordered[0].rcept_no,
                    "stlm_dt": shares.get("stlm_dt"), "issued_shares": shares.get("issued_shares"),
                    "treasury_shares": shares.get("treasury_shares"),
                    "outstanding_shares": shares.get("outstanding_shares"), "source": "DART_OPENAPI",
                    "collected_at": now_kst().isoformat(timespec="seconds")}])
            cf = self.analyzer.analyze(stock_code, fetched, shares, sm)
            if "CFS_NOT_AVAILABLE" in statuses:
                cf.message = (cf.message + " / 일부 기간 연결재무제표 확인 불가").strip(" /")
            return cf
        except NoApiKey:
            return CompanyFinancials(stock_code, "NO_API_KEY", "DART_API_KEY 미설정")
        except DartError as e:
            self.log.error("%s DART 오류: %s", stock_code, e)
            return CompanyFinancials(stock_code, "FAILED", f"DART 수집 실패: {e}")

    def _save_rows(self, stock_code: str, corp: str, rd: ReportData) -> None:
        self.db.upsert("financial_statements", [{
            "stock_code": stock_code, "corp_code": corp, "rcept_no": rd.rcept_no, "bsns_year": rd.bsns_year,
            "reprt_code": rd.reprt_code, "period": rd.period, "fs_div": "CFS", "sj_div": r.get("sj_div"),
            "account_id": r.get("account_id"), "account_nm": r.get("account_nm"),
            "account_detail": r.get("account_detail"), "ord": int(r.get("ord") or 0), "currency": r.get("currency"),
            **{c: parse_amount(r.get(c)) for c in AMOUNT_COLS}, "collected_at": rd.collected_at,
        } for r in rd.rows])
