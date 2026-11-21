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


def run_daily(codes: list[str] | None = None, limit: int | None = None, price_date: date | None = None) -> dict:
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
        df = df[df["market"].isin(config.settings()["universe"]["markets"])]
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
    except Exception as e:  # noqa: BLE001
        status = "FAILED"
        log.error("일일 분석 실패: %s\n%s", e, traceback.format_exc())
        db.finish_run(run_id, status, stages, now_kst().isoformat(timespec="seconds"), str(e), summary)
        return {"run_id": run_id, "status": status, "error": str(e), "stages": stages}
    db.finish_run(run_id, status, stages, now_kst().isoformat(timespec="seconds"), None, summary)
    return {"run_id": run_id, "status": status, "stages": stages, "summary": summary}


def _print_summary(results, summary) -> None:
    print("\n=== 재무지표 계산 결과 (상위 30개 표시) ===")
    for r, cf, rr in results[:30]:
        cells = []
        for m in rr.checklist:
            mark = {"통과": "O", "미통과": "X", "확인 불가": "?"}[m.check.value]
            cells.append(f"{CHECK_LABEL[_key(m.name)]}={m.display}{mark}")
        print(f"{r.stock_code} {r.name} [{cf.status}] 통과 {rr.pass_count}/10 확인불가 {rr.na_count} | " + ", ".join(cells))
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


def _key(name: str) -> str:
    return {v: k for k, v in CHECK_LABEL.items()}[name]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Korea Value Stock Analyzer 일일 분석")
    ap.add_argument("--run-now", action="store_true", help="즉시 1회 실행")
    ap.add_argument("--codes", help="쉼표로 구분한 종목코드만 실행(검증용)")
    ap.add_argument("--limit", type=int, help="대상 종목 수 제한(검증용)")
    ap.add_argument("--price-date", help="주가 기준일 강제 지정 YYYY-MM-DD (재현용)")
    a = ap.parse_args(argv)
    if not a.run_now:
        print("STEP 11(07:30 자동 실행)은 아직 구현 전임. --run-now 로 1회 실행 가능함")
        return 2
    res = run_daily(a.codes.split(",") if a.codes else None, a.limit,
                    date.fromisoformat(a.price_date) if a.price_date else None)
    return 0 if res["status"] in ("SUCCESS", "PARTIAL") else 1


if __name__ == "__main__":
    sys.exit(main())
