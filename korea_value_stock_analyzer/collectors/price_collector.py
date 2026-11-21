"""전일 거래일 종가 수집 및 검증 → daily_prices / companies 저장

검증 항목
 - 종가 기준일 검증: 요청 기준일(전일 거래일)과 실제 데이터 기준일 일치 여부
 - 시가총액 재계산 검증: 종가 × 상장주식수 vs 원천 시가총액
 - 실제 종가만 사용 (수정주가 미사용)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from collectors.krx_collector import KRXCollector, Snapshot, classify_listing
from core import config
from core.models import DataStatus
from core.trading_calendar import analysis_price_date
from database.db_manager import DBManager


@dataclass
class PriceResult:
    snapshot: Snapshot
    frame: pd.DataFrame | None
    checks: dict = field(default_factory=dict)


class PriceCollector:
    def __init__(self, db: DBManager | None = None, krx: KRXCollector | None = None):
        self.db = db or DBManager()
        self.krx = krx or KRXCollector()
        self.log = config.setup_logging("price_collector")

    def collect(self, price_date: date | None = None, save: bool = True) -> PriceResult:
        price_date = price_date or analysis_price_date()
        snap = self.krx.price_snapshot(price_date)
        if snap.df is None:
            self.log.error("주가 수집 실패: %s", snap.message)
            return PriceResult(snap, None, {"collected": False, "message": snap.message})

        df = snap.df.copy()
        df["market_cap"] = df["close"] * df["listed_shares"].astype(float)
        tol = config.settings()["validation"]["market_cap_tolerance_pct"] / 100
        diff = (df["market_cap"] - df["source_market_cap"]).abs() / df["source_market_cap"]
        df["marcap_check_ok"] = diff <= tol

        status = snap.status
        if snap.is_stale:
            status = DataStatus.FAILED  # 최신 데이터 아님 → 당일 분석용으로는 실패 처리
        df["status"] = status.value
        df.loc[~df["marcap_check_ok"], "status"] = DataStatus.worst(status, DataStatus.PARTIAL).value

        desc, desc_url, desc_date = self.krx.company_descriptions(snap.actual_date or price_date)
        if desc is not None:
            df = df.merge(desc, on="stock_code", how="left")
        else:
            for c in ("industry", "products", "settle_month", "listing_date"):
                df[c] = None
        df = classify_listing(df)

        checks = {
            "collected": True,
            "requested_price_date": price_date.isoformat(),
            "actual_price_date": snap.actual_date.isoformat() if snap.actual_date else None,
            "price_date_check": "OK" if not snap.is_stale else "STALE",
            "rows": int(len(df)),
            "marcap_recalc_mismatch": int((~df["marcap_check_ok"]).sum()),
            "nonpositive_close": int((df["close"] <= 0).sum()),
            "source": snap.source, "source_ref": snap.source_ref,
            "desc_source_ref": desc_url, "desc_as_of": desc_date.isoformat() if desc_date else None,
            "message": snap.message,
        }
        if save and not snap.is_stale:
            self._save(df, snap, desc_url, desc_date)
        elif snap.is_stale:
            self.log.error("주가 기준일 불일치(요청 %s / 실제 %s) → 저장하지 않음", price_date, snap.actual_date)
        return PriceResult(snap, df, checks)

    def _save(self, df: pd.DataFrame, snap: Snapshot, desc_url: str, desc_date: date | None) -> None:
        pd_str = snap.actual_date.isoformat()
        self.db.upsert("daily_prices", [{
            "price_date": pd_str, "stock_code": r.stock_code, "close": _f(r.close),
            "listed_shares": _i(r.listed_shares), "market_cap": _f(r.market_cap),
            "source_market_cap": _f(r.source_market_cap), "source": snap.source,
            "source_ref": snap.source_ref, "status": r.status, "collected_at": snap.collected_at,
        } for r in df.itertuples()])
        existing = {r["stock_code"]: r["corp_code"] for r in self.db.query("SELECT stock_code, corp_code FROM companies")}
        self.db.upsert("companies", [{
            "stock_code": r.stock_code, "corp_code": existing.get(r.stock_code), "isin": r.isin,
            "name": r.name, "market": r.market, "dept": r.dept if isinstance(r.dept, str) else None,
            "industry": r.industry if isinstance(r.industry, str) else None,
            "products": r.products if isinstance(r.products, str) else None,
            "settle_month": _i(r.settle_month), "listing_date": r.listing_date if isinstance(r.listing_date, str) else None,
            "is_common": int(r.is_common), "is_preferred": int(r.is_preferred), "is_spac": int(r.is_spac),
            "is_reit": int(r.is_reit), "is_etf_etn": int(r.is_etf_etn),
            "is_investment_company": int(r.is_investment_company), "is_administrative": int(r.is_administrative),
            "delisting_status": r.delisting_status, "exclusion_reasons": list(r.exclusion_reasons),
            "master_source": f"{snap.source}; desc={desc_url}", "master_as_of": pd_str,
            "updated_at": snap.collected_at,
        } for r in df.itertuples()])


def _f(v):
    return None if v is None or pd.isna(v) else float(v)


def _i(v):
    return None if v is None or pd.isna(v) else int(v)
