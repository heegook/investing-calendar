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
from analyzers.screening_engine import ScreeningEngine, compare_with_previous, to_db_rows
from analyzers.valuation_engine import ValuationEngine
from analyzers.financial_analyzer import CompanyFinancials
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
        veng = ValuationEngine()
        val_rows = []
        fin_status: dict[str, int] = {}
        ratio_rows, results = [], []
        for i, r in enumerate(target.itertuples(), 1):
            sm = int(r.settle_month) if pd.notna(r.settle_month) else None
            try:
                cf = fp.collect(r.stock_code, sm, started.date(), int(r.listed_shares))
            except Exception as e:  # noqa: BLE001 - 한 종목 오류가 전체 실행을 멈추지 않도록 기록 후 계속
                log.error("%s 재무 처리 오류: %s\n%s", r.stock_code, e, traceback.format_exc())
                cf = CompanyFinancials(r.stock_code, "FAILED", f"처리 오류: {e}")
            fin_status[cf.status] = fin_status.get(cf.status, 0) + 1
            price = PriceInfo(r.stock_code, pr.snapshot.actual_date.isoformat(), float(r.close),
                              int(r.listed_shares), float(r.market_cap), pr.snapshot.source,
                              pr.snapshot.source_ref, DataStatus(r.status), pr.snapshot.collected_at)
            rr = calc.calculate(cf, price)
            vr = veng.calculate(cf, price, name=r.name,
                                industry=r.industry if isinstance(r.industry, str) else None)
            rr.valuation = vr
            results.append((r, cf, rr))
            val_rows.append({
                "run_id": run_id, "analysis_date": started.date().isoformat(), "stock_code": r.stock_code,
                "fair_value": vr.fair_value, "current_price": vr.current_price,
                "safety_margin_pct": vr.safety_margin_pct, "discount_pct": vr.discount_pct,
                "upside_pct": vr.upside_pct, "price_20pct_off": vr.price_20pct_off,
                "price_30pct_off": vr.price_30pct_off, "status": vr.status.value,
                "caution": " / ".join(vr.cautions) or None, "metadata_json": vr.to_dict()})
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
        db.upsert("valuation_results", val_rows)
        stages["valuation"] = {"computed": sum(v["fair_value"] is not None for v in val_rows), "total": len(val_rows)}
        stages["financials"] = {"status_counts": fin_status, "dart_calls": fp.dart.call_count,
                                "dart_fatal": str(fp.dart.fatal) if fp.dart.fatal else None}
        stages["ratios"] = {"companies": len(results), "metric_rows": len(ratio_rows)}
        summary["financial_status"] = fin_status
        if fin_status.get("NO_API_KEY") or fin_status.get("FAILED"):
            status = "PARTIAL"
        # ---------------- 4. 스크리닝·선정 ----------------
        log.info("[4] 저평가 후보 선정")
        srows = ScreeningEngine().run(results)
        db.upsert("screening_results", to_db_rows(run_id, started.date().isoformat(), srows))
        cands = [x for x in srows if x.passed_all]
        selected = [x for x in srows if x.selected]
        diff = compare_with_previous(db, run_id)
        stages["screening"] = {"candidates": len(cands), "selected": len(selected),
                               "selected_codes": [x.stock_code for x in selected],
                               "vs_previous": {k: v for k, v in diff.items() if k != "metric_changes"},
                               "metric_changes": len(diff.get("metric_changes", []))}
        summary["candidates_all_10_pass"] = len(cands)
        summary["selected"] = [f"{x.rank}. {x.stock_code} {x.name} {x.score:.1f}점" for x in selected]
        _print_selection(srows, diff)
        stages["not_implemented"] = ["STEP 7 리스크",
                                     "STEP 10 보고서", "STEP 11 07:30 자동 실행"]
        _print_summary(results, summary)
        if dump:
            path = config.path("log_dir") / f"verify_{run_id}.log"
            srow_map = {x.stock_code: x for x in srows}
            text = verification_report(results, pr.checks, run_id)
            text += "\n\n" + "=" * 100 + "\n## 스크리닝 점수 (가중치·환산 기준: config/scoring_weights.yaml)\n"
            for x in srows[:200]:
                text += (f"- {x.rank}. {x.stock_code} {x.name} | 선정={'Y' if x.selected else 'N'} | 통과 {x.pass_count}/10 | "
                         f"{x.score:.2f}점 | " + "; ".join(
                             f"{k}={'-' if c['value'] is None else round(c['value'], 4)}→{c['points']:.2f}/{c['weight']}"
                             for k, c in x.components.items()) + (f" | 경고: {' / '.join(x.warnings)}" if x.warnings else "") + "\n")
            path.write_text(text, encoding="utf-8")
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
        L.append("### 주당이익 관련 원천 행 (보고서 | 재무제표 | account_id | 계정명 | 당기)")
        L += [f"- {a} | {b} | {c} | {d} | {e}" for a, b, c, d, e in cf.eps_rows] or ["- (해당 행 없음)"]
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
        vr = getattr(rr, "valuation", None)
        if vr is not None:
            L.append(f"### 적정주가 [{vr.status.value}] 공식: {vr.formula}")
            for i in vr.inputs:
                L.append(f"- 입력 {i['item']} = {_fmt(i['value'])} {i['unit']} | {i['basis']} | {i['status']} | {i['source']}")
            for st in vr.steps:
                L.append(f"- {st['step']}: {st.get('formula', '')} = {_fmt(st['result']) if isinstance(st['result'], (int, float)) else st['result']}")
            if vr.fair_value is not None:
                L.append(f"- 적정주가 {vr.fair_value:,.0f}원 / 현재가 {vr.current_price:,.0f}원 / 안전마진율 "
                         f"{vr.safety_margin_pct:,.1f}% / 할인율 {'-' if vr.discount_pct is None else f'{vr.discount_pct:,.1f}%'}"
                         f" / 20% 할인가 {_fmt(vr.price_20pct_off)} / 30% 할인가 {_fmt(vr.price_30pct_off)}")
            L += [f"- [{c['result']}] {c['check']}: {c['detail']}" for c in vr.validations]
            L += [f"- 주의: {c}" for c in vr.cautions]
            L.append(f"- ※ {vr.disclaimer}")
        L.append("### 10개 지표")
        for key, m in rr.metrics.items():
            L.append(f"- {m.name}: {m.display} | {m.check.value if m.check else '-'} | {m.status.value} | "
                     f"{m.reason.value if m.reason else ''} | {' / '.join(n for n in m.notes if n)}")
    return "\n".join(L)


def _print_selection(srows, diff) -> None:
    cands = [x for x in srows if x.passed_all]
    print(f"\n=== 스크리닝: 10개 지표 모두 통과 {len(cands)}개 / 최종 선정 {sum(x.selected for x in srows)}개 ===")
    if len(cands) < 10:
        print("  ※ 충족 종목이 10개 미만이므로 조건을 완화하지 않고 충족 종목만 선정함")
    for x in [x for x in srows if x.selected]:
        comp = ", ".join(f"{k}:{v['points']:.1f}" for k, v in x.components.items())
        print(f"  {x.rank:>2}. {x.stock_code} {x.name} {x.score:.1f}점 | {comp}")
        for w in x.warnings:
            print(f"      ⚠ {w}")
    near = [x for x in srows if not x.passed_all and x.pass_count >= 8][:10]
    if near:
        print("  참고(8~9개 충족, 선정 대상 아님): " + ", ".join(
            f"{x.stock_code} {x.name}({x.pass_count}/10, 미충족: "
            f"{'/'.join(k for k, v in x.checks.items() if v != '통과')})" for x in near))
    if diff.get("previous_run"):
        print(f"  직전 실행({diff['previous_run']}) 대비 신규 편입 {diff['new_entries']} / 제외 {diff['removed']} / "
              f"지표 변화 {len(diff['metric_changes'])}건")


def _print_summary(results, summary) -> None:
    if len(results) > 30:
        print(f"\n(종목별 상세 출력 생략: {len(results)}개 - DB 및 --dump 리포트 참조)")
        print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
        return
    print("\n=== 재무지표 계산 결과 ===")
    for r, cf, rr in results:
        cells = []
        for m in rr.checklist:
            mark = {"통과": "O", "미통과": "X", "확인 불가": "?"}[m.check.value]
            cells.append(f"{CHECK_LABEL[_key(m.name)]}={m.display}{mark}")
        print(f"{r.stock_code} {r.name} [{cf.status}] 통과 {rr.pass_count}/10 확인불가 {rr.na_count} | " + ", ".join(cells))
        vr = getattr(rr, "valuation", None)
        if vr is not None:
            if vr.fair_value is not None:
                print(f"    적정주가 {vr.fair_value:,.0f}원 (현재가 {vr.current_price:,.0f}원, 안전마진율 "
                      f"{vr.safety_margin_pct:+.1f}%) [{vr.status.value}]")
            else:
                print(f"    적정주가 산출 불가: {vr.steps[0]['result'] if vr.steps else vr.reason}")
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
