"""일일 분석 실행기

현재 구현 범위: STEP 1~4
  1) 주가 데이터 업데이트 (전일 거래일 종가)
  2) 기업 공시 및 연결재무제표 업데이트 (DART)
  3) 전체 기업 재무비율 재계산 (10개 지표)
이후 단계(후보 선정, 리스크, 적정주가, 보고서, 07:30 자동 실행)는 STEP 5~11 에서 추가함

사용법
  python -m scheduler.daily_scheduler --run-now
  python -m scheduler.daily_scheduler --run-now --codes 005930,000660
  python -m scheduler.daily_scheduler --run-now --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date

import pandas as pd

from analyzers.financial_pipeline import FinancialPipeline
from analyzers.ratio_calculator import CHECK_LABEL, PriceInfo, RatioCalculator
from collectors.krx_collector import apply_size_filter
from collectors.price_collector import PriceCollector
from core import config
from core.models import DataStatus
from core.trading_calendar import analysis_price_date, now_kst
from database.db_manager import DBManager


def run_daily(codes: list[str] | None = None, limit: int | None = None, price_date: date | None = None,
              dump: bool = False) -> dict:
    log = config.setup_logging("daily")
    db = DBManager()
    started = now_kst()
    pdate = price_date or analysis_price_date(started)
    run_id = started.strftime("%Y%m%d-%H%M%S")
    db.start_run(run_id, started.date().isoformat(), pdate.isoformat(), started.isoformat(timespec="seconds"))
    stages: dict[str, dict] = {}
    summary: dict = {}
    status = "SUCCESS"
    try:
        # ---------------- 1. 주가 ----------------
        log.info("[1/3] 주가 데이터 업데이트 - 기준일 %s", pdate)
        pr = PriceCollector(db).collect(pdate)
        stages["prices"] = pr.checks
        if pr.frame is None or pr.snapshot.is_stale:
            raise RuntimeError(f"주가 데이터 미갱신: {pr.checks.get('message')}")
        df = pr.frame
        df = df[df["market"].isin(config.settings()["universe"]["markets"])].copy()
        df["in_type_universe"] = df["exclusion_reasons"].apply(lambda x: len(x) == 0)
        df["in_size_universe"] = apply_size_filter(df)
        scope = config.settings()["dart"]["collect_scope"]
        target = df if scope == "all" else df[df["in_type_universe"] & df["in_size_universe"]]
        if codes:
            target = df[df["stock_code"].isin(codes)]
        if limit:
            target = target.head(limit)
        summary["listed_kospi_kosdaq"] = int(len(df))
        summary["type_universe"] = int(df["in_type_universe"].sum())
        summary["size_type_universe"] = int((df["in_type_universe"] & df["in_size_universe"]).sum())
        summary["dart_targets"] = int(len(target))
        log.info("전체 %d / 유형통과 %d / 유형+규모 통과 %d / 재무 수집 대상 %d",
                 summary["listed_kospi_kosdaq"], summary["type_universe"], summary["size_type_universe"], len(target))

        # ---------------- 2~3. 재무제표 + 비율 ----------------
        fp = FinancialPipeline(db)
        calc = RatioCalculator()
        fin_status: dict[str, int] = {}
        ratio_rows, results = [], []
        for i, r in enumerate(target.itertuples(), 1):
            sm = int(r.settle_month) if pd.notna(r.settle_month) else None
            cf = fp.collect(r.stock_code, sm, started.date())
            fin_status[cf.status] = fin_status.get(cf.status, 0) + 1
            price = PriceInfo(r.stock_code, pr.snapshot.actual_date.isoformat(), float(r.close),
                              int(r.listed_shares), float(r.market_cap), pr.snapshot.source,
                              pr.snapshot.source_ref, DataStatus(r.status), pr.snapshot.collected_at)
            rr = calc.calculate(cf, price)
            results.append((r, cf, rr))
            for key, mt in rr.metrics.items():
                ratio_rows.append({
                    "run_id": run_id, "analysis_date": started.date().isoformat(), "stock_code": r.stock_code,
                    "metric": key, "value": mt.value, "unit": mt.unit, "display": mt.display,
                    "status": mt.status.value, "check_result": mt.check.value if mt.check else None,
                    "reason": mt.reason.value if mt.reason else None, "formula": mt.formula, "as_of": mt.as_of,
                    "metadata_json": mt.to_json()})
            if i % 25 == 0:
                log.info("  진행 %d/%d (DART 호출 %d건)", i, len(target), fp.dart.call_count)
            if fp.dart.fatal:
                log.error("DART 치명 오류로 이후 수집 중단: %s", fp.dart.fatal)
        db.upsert("financial_ratios", ratio_rows)
        stages["financials"] = {"status_counts": fin_status, "dart_calls": fp.dart.call_count,
                                "dart_fatal": str(fp.dart.fatal) if fp.dart.fatal else None}
        stages["ratios"] = {"companies": len(results), "metric_rows": len(ratio_rows)}
        summary["financial_status"] = fin_status
        summary["all_10_pass"] = [r.stock_code for r, cf, rr in results if rr.all_pass]
        if fin_status.get("NO_API_KEY") or fin_status.get("FAILED"):
            status = "PARTIAL"
        stages["not_implemented"] = ["STEP 5 적정주가", "STEP 6 스크리닝·선정", "STEP 7 리스크",
                                     "STEP 10 보고서", "STEP 11 07:30 자동 실행"]
        _print_summary(results, summary)
        if dump:
            path = config.path("log_dir") / f"verify_{run_id}.log"
            path.write_text(verification_report(results, pr.checks, run_id), encoding="utf-8")
            summary["verification_report"] = str(path)
            print(f"\n검증 리포트 저장: {path}")
    except Exception as e:  # noqa: BLE001
        status = "FAILED"
        log.error("일일 분석 실패: %s\n%s", e, traceback.format_exc())
        db.finish_run(run_id, status, stages, now_kst().isoformat(timespec="seconds"), str(e), summary)
        return {"run_id": run_id, "status": status, "error": str(e), "stages": stages}
    db.finish_run(run_id, status, stages, now_kst().isoformat(timespec="seconds"), None, summary)
    return {"run_id": run_id, "status": status, "stages": stages, "summary": summary}


def _fmt(v) -> str:
    if v is None:
        return "null"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.4f}"


def verification_report(results, price_checks: dict, run_id: str) -> str:
    """실데이터 검증용 상세 리포트 (사용 계정·원천 접수번호·검증 결과). 비밀값은 포함하지 않음"""
    L = [f"# 검증 리포트 run_id={run_id}", "## 주가 수집", json.dumps(price_checks, ensure_ascii=False, indent=1)]
    for r, cf, rr in results:
        L += ["", "=" * 100, f"## {r.stock_code} {r.name} | 재무상태={cf.status} {cf.message}",
              f"종가 {r.close:,.0f} / 상장주식수 {int(r.listed_shares):,} / 시총 {r.market_cap:,.0f} / 결산월 {r.settle_month}"]
        L.append("### 사용 보고서")
        L += [f"- {x['rcept_no']} {x['report_nm']} period={x['period']} reprt={x['reprt_code']} "
              f"bsns_year={x['bsns_year']} fs={x['fs_div']}" for x in cf.reports]
        L.append(f"### 주식총수(DART): { {k: v for k, v in cf.shares.items() if k != '_diag'} }")
        L += [f"- 진단: {d}" for d in cf.shares.get("_diag", [])]
        L.append("### 검증")
        L += [f"- [{v['result']}] {v['check']}: {v['detail']}" for v in cf.validations]
        L.append("### 연간(3개년)")
        for per in cf.annual_periods:
            for k, v in cf.annual[per].items():
                L.append(f"- {per} {k} = {_fmt(v.value)} [{v.status.value}] "
                         f"{v.source.detail if v.source else ''} {'; '.join(v.notes)}")
        L.append(f"### 최근 재무상태표 ({cf.balance_period})")
        for k, v in cf.balance.items():
            L.append(f"- {k} = {_fmt(v.value)} [{v.status.value}] {v.source.detail if v.source else ''} "
                     f"{'; '.join(v.notes)}")
        ia = cf.investment_assets
        L.append(f"### 투자자산 합계 = {_fmt(ia.value) if ia else 'null'}")
        L += [f"- {'포함' if i['included'] else '제외'} {i['category']} | {i['account_id']} | {i['account_nm']} | "
              f"{_fmt(i['amount'])} {i['note']}" for i in cf.investment_breakdown]
        L.append("### 재무상태표 원천 행 (ord | account_id | 계정명 | 당기말)")
        L += [f"- {o} | {a} | {n} | {v}" for o, a, n, v in cf.bs_dump]
        if ia is not None:
            L.append(f"- 투자자산 주석: {'; '.join(ia.notes)}")
        L.append("### 최근 4개 분기")
        for q in cf.quarters:
            L.append(f"- {q['label']}: " + ", ".join(f"{k}={_fmt(q[k].value)}" for k in
                                                       ("revenue", "operating_income", "net_income_parent", "eps")))
        L.append(f"### TTM: {cf.ttm_basis}")
        L += [f"- {k} = {_fmt(v.value)} [{v.status.value}] {'; '.join(v.notes)}" for k, v in cf.ttm.items()]
        L.append("### 10개 지표")
        for key, m in rr.metrics.items():
            L.append(f"- {m.name}: {m.display} | {m.check.value if m.check else '-'} | {m.status.value} | "
                     f"{m.reason.value if m.reason else ''} | {' / '.join(n for n in m.notes if n)}")
    return "\n".join(L)


def _print_summary(results, summary) -> None:
    print("\n=== 재무지표 계산 결과 (상위 30개 표시) ===")
    for r, cf, rr in results[:30]:
        cells = []
        for m in rr.checklist:
            mark = {"통과": "O", "미통과": "X", "확인 불가": "?"}[m.check.value]
            cells.append(f"{CHECK_LABEL[_key(m.name)]}={m.display}{mark}")
        print(f"{r.stock_code} {r.name} [{cf.status}] 통과 {rr.pass_count}/10 확인불가 {rr.na_count} | " + ", ".join(cells))
        if len(results) <= 5:
            for v in cf.validations:
                print(f"    [{v['result']}] {v['check']}: {v['detail']}")
            for d in cf.shares.get("_diag", []):
                print(f"    주식총수 진단: {d[:300]}")
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


def _key(name: str) -> str:
    return {v: k for k, v in CHECK_LABEL.items()}[name]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Korea Value Stock Analyzer 일일 분석")
    ap.add_argument("--run-now", action="store_true", help="즉시 1회 실행")
    ap.add_argument("--codes", nargs="+", help="종목코드(쉼표 또는 공백 구분, 예: \"005930,009150\")")
    ap.add_argument("--limit", type=int, help="대상 종목 수 제한(검증용)")
    ap.add_argument("--price-date", help="주가 기준일 강제 지정 YYYY-MM-DD (재현용)")
    ap.add_argument("--dump", action="store_true", help="logs/verify_<run_id>.log 상세 검증 리포트 저장")
    a = ap.parse_args(argv)
    if not a.run_now:
        print("STEP 11(07:30 자동 실행)은 아직 구현 전임. --run-now 로 1회 실행 가능함")
        return 2
    codes = None
    if a.codes:
        # PowerShell 은 따옴표 없는 005930,009150 을 숫자 배열(5930 9150)로 넘기므로 6자리로 복원함
        codes = [c.strip().zfill(6) for part in a.codes for c in part.split(",") if c.strip()]
        print(f"대상 종목코드: {codes}")
    res = run_daily(codes, a.limit,
                    date.fromisoformat(a.price_date) if a.price_date else None, a.dump)
    return 0 if res["status"] in ("SUCCESS", "PARTIAL") else 1


if __name__ == "__main__":
    sys.exit(main())
