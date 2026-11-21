"""SQLite 데이터베이스 관리

PART 12 의 10개 테이블과 수집 상태 추적용 보조 테이블(dart_reports)을 관리함
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from core import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    stock_code TEXT PRIMARY KEY,
    corp_code TEXT,
    isin TEXT,
    name TEXT NOT NULL,
    market TEXT,
    dept TEXT,
    industry TEXT,
    products TEXT,
    settle_month INTEGER,
    listing_date TEXT,
    is_common INTEGER,
    is_preferred INTEGER,
    is_spac INTEGER,
    is_reit INTEGER,
    is_etf_etn INTEGER,
    is_investment_company INTEGER,
    is_administrative INTEGER,
    delisting_status TEXT,          -- '해당 없음' / '상장폐지 예정' / '확인 불가'
    exclusion_reasons TEXT,         -- JSON list
    master_source TEXT,
    master_as_of TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS daily_prices (
    price_date TEXT NOT NULL,       -- 실제 주가 기준일 (YYYY-MM-DD)
    stock_code TEXT NOT NULL,
    close REAL,                     -- 실제 종가(수정주가 아님)
    listed_shares INTEGER,          -- 상장주식수
    market_cap REAL,                -- 종가 × 상장주식수 (재계산)
    source_market_cap REAL,         -- 원천 제공 시가총액(검증용)
    source TEXT,
    source_ref TEXT,
    status TEXT,
    collected_at TEXT,
    PRIMARY KEY (price_date, stock_code)
);

CREATE TABLE IF NOT EXISTS dart_reports (
    stock_code TEXT NOT NULL,
    corp_code TEXT,
    rcept_no TEXT NOT NULL,
    rcept_dt TEXT,
    report_nm TEXT,
    period TEXT,                    -- 보고 대상 기간 말 (YYYY-MM)
    bsns_year INTEGER,
    reprt_code TEXT,
    fs_div TEXT,                    -- CFS / NONE
    fetch_status TEXT,              -- OK / CFS_NOT_AVAILABLE / NOT_FOUND / FAILED
    message TEXT,
    collected_at TEXT,
    PRIMARY KEY (stock_code, rcept_no)
);

CREATE TABLE IF NOT EXISTS share_counts (
    stock_code TEXT NOT NULL,
    rcept_no TEXT NOT NULL,
    stlm_dt TEXT,                   -- 결산기준일
    issued_shares REAL,             -- 발행주식총수(보통주)
    treasury_shares REAL,           -- 자기주식수(보통주)
    outstanding_shares REAL,        -- 유통주식수(보통주)
    source TEXT,
    collected_at TEXT,
    PRIMARY KEY (stock_code, rcept_no)
);

CREATE TABLE IF NOT EXISTS financial_statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    corp_code TEXT,
    rcept_no TEXT NOT NULL,
    bsns_year INTEGER,
    reprt_code TEXT,
    period TEXT,
    fs_div TEXT NOT NULL,           -- CFS(연결) 만 저장. OFS 는 저장하지 않음
    sj_div TEXT,                    -- BS/IS/CIS/CF/SCE
    account_id TEXT,
    account_nm TEXT,
    account_detail TEXT,
    ord INTEGER,
    currency TEXT,
    thstrm_amount REAL,
    thstrm_add_amount REAL,
    frmtrm_amount REAL,
    frmtrm_q_amount REAL,
    frmtrm_add_amount REAL,
    bfefrmtrm_amount REAL,
    collected_at TEXT,
    UNIQUE (rcept_no, fs_div, sj_div, ord, account_id, account_nm)
);
CREATE INDEX IF NOT EXISTS ix_fs_stock ON financial_statements(stock_code, rcept_no);

CREATE TABLE IF NOT EXISTS financial_ratios (
    run_id TEXT NOT NULL,
    analysis_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    unit TEXT,
    display TEXT,
    status TEXT,
    check_result TEXT,
    reason TEXT,
    formula TEXT,
    as_of TEXT,
    metadata_json TEXT,
    PRIMARY KEY (run_id, stock_code, metric)
);

CREATE TABLE IF NOT EXISTS valuation_results (
    run_id TEXT NOT NULL,
    analysis_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    fair_value REAL,
    current_price REAL,
    safety_margin_pct REAL,
    discount_pct REAL,
    upside_pct REAL,
    price_20pct_off REAL,
    price_30pct_off REAL,
    status TEXT,
    caution TEXT,
    metadata_json TEXT,
    PRIMARY KEY (run_id, stock_code)
);

CREATE TABLE IF NOT EXISTS corporate_risks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    event_year INTEGER,
    event_type TEXT,
    summary TEXT,
    related_party TEXT,
    stage TEXT,                     -- 의혹 제기/조사 착수/기소/1심 판결/최종 판결/행정제재/무혐의 및 무죄
    final_result TEXT,
    source_url TEXT,
    checked_date TEXT,
    status TEXT,
    UNIQUE (stock_code, source_url, event_type)
);

CREATE TABLE IF NOT EXISTS industry_analysis (
    stock_code TEXT NOT NULL,
    analysis_date TEXT NOT NULL,
    classification TEXT,
    rationale TEXT,
    metadata_json TEXT,
    PRIMARY KEY (stock_code, analysis_date)
);

CREATE TABLE IF NOT EXISTS screening_results (
    run_id TEXT NOT NULL,
    analysis_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    in_universe INTEGER,
    pass_count INTEGER,
    na_count INTEGER,
    passed_all INTEGER,
    score REAL,
    rank INTEGER,
    selected INTEGER,
    details_json TEXT,
    PRIMARY KEY (run_id, stock_code)
);

CREATE TABLE IF NOT EXISTS source_registry (
    source_key TEXT PRIMARY KEY,
    name TEXT,
    priority INTEGER,
    reliability TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS analysis_history (
    run_id TEXT PRIMARY KEY,
    analysis_date TEXT,
    price_date TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,                    -- RUNNING / SUCCESS / PARTIAL / FAILED
    stages_json TEXT,
    error TEXT,
    summary_json TEXT
);
"""


class DBManager:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = str(db_path or config.path("database"))
        with self.connect() as con:
            con.executescript(SCHEMA)
        self.sync_source_registry()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---------- generic helpers ----------
    def upsert(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        with self.connect() as con:
            con.executemany(sql, [tuple(_ser(r.get(c)) for c in cols) for r in rows])
        return len(rows)

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.connect() as con:
            con.execute(sql, params)

    def sync_source_registry(self) -> None:
        from datetime import datetime
        srcs = config.settings().get("sources", {})
        self.upsert("source_registry", [
            {"source_key": k, "name": v["name"], "priority": v["priority"],
             "reliability": v["reliability"], "updated_at": datetime.now().isoformat(timespec="seconds")}
            for k, v in srcs.items()
        ])

    # ---------- analysis history ----------
    def start_run(self, run_id: str, analysis_date: str, price_date: str, started_at: str) -> None:
        self.upsert("analysis_history", [{
            "run_id": run_id, "analysis_date": analysis_date, "price_date": price_date,
            "started_at": started_at, "finished_at": None, "status": "RUNNING",
            "stages_json": "{}", "error": None, "summary_json": None}])

    def finish_run(self, run_id: str, status: str, stages: dict, finished_at: str,
                   error: str | None = None, summary: dict | None = None) -> None:
        self.execute(
            "UPDATE analysis_history SET status=?, stages_json=?, finished_at=?, error=?, summary_json=? WHERE run_id=?",
            (status, json.dumps(stages, ensure_ascii=False, default=str), finished_at, error,
             json.dumps(summary or {}, ensure_ascii=False, default=str), run_id))


def _ser(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    if hasattr(v, "value") and not isinstance(v, (int, float, str)):
        return v.value  # Enum
    return v
