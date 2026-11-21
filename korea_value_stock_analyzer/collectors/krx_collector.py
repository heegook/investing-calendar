"""KRX 상장종목 마스터 및 전종목 시세 스냅샷 수집

수집 경로 (우선순위)
 1) KRX_OFFICIAL : KRX_ID/KRX_PW 가 설정된 경우 pykrx 를 통해 KRX 정보데이터시스템에서 직접 수집
 2) KRX_FDR_MIRROR : FinanceDataReader 가 KRX 전종목 시세(MDCSTAT01501)를 일자별로 보관한 캐시 파일
    - 파일명이 거래일이므로 기준일이 명시적이고 재현 가능함
    - 2025년 이후 data.krx.co.kr 가 비로그인 조회를 차단하여 FinanceDataReader.StockListing('KRX') 도 이 캐시를 사용함

주의: 네이버 증권 시세는 KRX+NXT(넥스트레이드) 통합 시세라 KRX 공식 종가와 다를 수 있으므로 종가 원천으로 사용하지 않음
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import requests

from core import config
from core.models import DataStatus
from core.trading_calendar import is_trading_day, now_kst

FDR_CACHE_BASE = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/listing"
MARKET_ID = {"STK": "KOSPI", "KSQ": "KOSDAQ", "KNX": "KONEX"}


@dataclass
class Snapshot:
    df: pd.DataFrame | None
    requested_date: date
    actual_date: date | None
    source: str
    source_ref: str
    status: DataStatus
    collected_at: str
    message: str = ""

    @property
    def is_stale(self) -> bool:
        return self.actual_date is not None and self.actual_date != self.requested_date


class KRXCollector:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.raw_dir = config.path("raw_dir") / "krx"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.log = config.setup_logging("krx_collector")

    # ------------------------------------------------------------------
    def _get_csv(self, url: str) -> str | None:
        r = requests.get(url, timeout=self.timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content.decode("utf-8-sig")

    def _mirror(self, kind: str, d: date) -> tuple[pd.DataFrame | None, str]:
        url = f"{FDR_CACHE_BASE}/{kind}/{d.isoformat()}.csv"
        cache = self.raw_dir / f"{kind}_{d.isoformat()}.csv"
        if cache.exists():
            text = cache.read_text(encoding="utf-8")
        else:
            text = self._get_csv(url)
            if text is None:
                return None, url
            cache.write_text(text, encoding="utf-8")
        df = pd.read_csv(io.StringIO(text), index_col=0, dtype=str)
        return df, url

    # ------------------------------------------------------------------
    def price_snapshot(self, price_date: date, max_back_days: int = 10) -> Snapshot:
        """요청 거래일의 전종목 종가·상장주식수·시가총액

        요청일 데이터가 없으면 직전 데이터를 찾되 is_stale=True 로 표시하여
        과거 데이터를 최신인 것처럼 사용하지 않도록 함
        """
        collected_at = now_kst().isoformat(timespec="seconds")
        if config.env("KRX_ID") and config.env("KRX_PW"):
            snap = self._official_snapshot(price_date, collected_at)
            if snap.df is not None:
                return snap
            self.log.warning("KRX 공식 수집 실패 → 미러 사용: %s", snap.message)

        d, tried = price_date, []
        while (price_date - d).days <= max_back_days:
            if is_trading_day(d):
                try:
                    df, url = self._mirror("krx", d)
                except requests.RequestException as e:
                    return Snapshot(None, price_date, None, "KRX_FDR_MIRROR", f"{FDR_CACHE_BASE}/krx",
                                    DataStatus.FAILED, collected_at, f"수집 실패: {e}")
                tried.append(d.isoformat())
                if df is not None:
                    out = self._normalize_mirror(df)
                    msg = "" if d == price_date else (
                        f"요청 기준일 {price_date} 데이터 미존재 → 직전 보관일 {d} (최신 아님)")
                    return Snapshot(out, price_date, d, "KRX_FDR_MIRROR", url,
                                    DataStatus.UNVERIFIED, collected_at, msg)
            d -= timedelta(days=1)
        return Snapshot(None, price_date, None, "KRX_FDR_MIRROR", f"{FDR_CACHE_BASE}/krx",
                        DataStatus.FAILED, collected_at, f"스냅샷 미존재: {tried}")

    @staticmethod
    def _normalize_mirror(df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame({
            "stock_code": df["Code"].str.zfill(6),
            "isin": df["ISU_CD"],
            "name": df["Name"],
            "market": df["MarketId"].map(MARKET_ID),
            "dept": df["Dept"],
            "close": pd.to_numeric(df["Close"], errors="coerce"),
            "listed_shares": pd.to_numeric(df["Stocks"], errors="coerce").astype("Int64"),
            "source_market_cap": pd.to_numeric(df["Marcap"], errors="coerce"),
            "volume": pd.to_numeric(df["Volume"], errors="coerce"),
        })
        return out.reset_index(drop=True)

    def _official_snapshot(self, price_date: date, collected_at: str) -> Snapshot:
        try:
            from pykrx import stock
            ymd = price_date.strftime("%Y%m%d")
            frames = []
            for mkt in ("KOSPI", "KOSDAQ"):
                cap = stock.get_market_cap(ymd, market=mkt)
                if cap is None or cap.empty:
                    raise RuntimeError(f"{mkt} 시가총액 데이터 없음({ymd})")
                cap = cap.reset_index().rename(columns={"티커": "stock_code"})
                cap["market"] = mkt
                cap["name"] = [stock.get_market_ticker_name(t) for t in cap["stock_code"]]
                frames.append(cap)
            df = pd.concat(frames, ignore_index=True)
            out = pd.DataFrame({
                "stock_code": df["stock_code"], "isin": None, "name": df["name"], "market": df["market"],
                "dept": None, "close": df["종가"].astype(float),
                "listed_shares": df["상장주식수"].astype("Int64"),
                "source_market_cap": df["시가총액"].astype(float), "volume": df["거래량"],
            })
            return Snapshot(out, price_date, price_date, "KRX_OFFICIAL",
                            f"pykrx.get_market_cap({ymd})", DataStatus.VERIFIED, collected_at)
        except Exception as e:  # noqa: BLE001
            return Snapshot(None, price_date, None, "KRX_OFFICIAL", "pykrx", DataStatus.FAILED,
                            collected_at, str(e))

    # ------------------------------------------------------------------
    def company_descriptions(self, ref_date: date, max_back_days: int = 10) -> tuple[pd.DataFrame | None, str, date | None]:
        """업종·주요제품·결산월·상장일 (KRX 상장법인 상세 목록)"""
        d = ref_date
        while (ref_date - d).days <= max_back_days:
            if is_trading_day(d):
                df, url = self._mirror("desc", d)
                if df is not None:
                    out = pd.DataFrame({
                        "stock_code": df["Code"].str.zfill(6),
                        "industry": df["Industry"],
                        "products": df["Products"],
                        "settle_month": df["SettleMonth"].str.extract(r"(\d+)")[0].astype(float).astype("Int64"),
                        "listing_date": df["ListingDate"],
                    })
                    return out, url, d
            d -= timedelta(days=1)
        return None, "", None


# ----------------------------------------------------------------------
# 유니버스 분류
# ----------------------------------------------------------------------
def classify_listing(df: pd.DataFrame) -> pd.DataFrame:
    """종목 유형 플래그 및 제외 사유 계산 (설정 기반)"""
    s = config.settings()["universe"]
    pat = s["patterns"]
    ex = s["exclude"]
    df = df.copy()
    name = df["name"].fillna("")
    industry = df.get("industry", pd.Series("", index=df.index)).fillna("")
    dept = df["dept"].fillna("")

    # 보통주: KRX 단축코드 끝자리 0 (우선주는 5/7/9/K 등) + 종목명 '우'/'우B' 접미사 보조 판정
    df["is_preferred"] = (df["stock_code"].str[-1] != "0") | name.str.contains(r"\d?우[A-C]?$|\(전환\)$", regex=True)
    df["is_common"] = ~df["is_preferred"]
    df["is_spac"] = name.str.contains(pat["spac_name"], regex=True) | dept.str.contains("SPAC")
    df["is_reit"] = name.str.contains(pat["reit_name"], regex=True)
    df["is_etf_etn"] = name.str.contains(pat["etf_etn_name"], regex=True)
    df["is_investment_company"] = name.str.contains(pat["investment_company_name"], regex=True)
    if pat.get("investment_company_industry"):
        df["is_investment_company"] |= industry.str.contains(pat["investment_company_industry"], regex=True)
    df["is_administrative"] = dept.str.contains("관리종목")
    # 상장폐지 예정 여부는 KIND(정리매매 종목) 조회가 필요하며 현재 접근 불가 → '확인 불가'
    df["delisting_status"] = "확인 불가"

    reasons = []
    for _, r in df.iterrows():
        rs = []
        if r["market"] not in s["markets"]:
            rs.append(f"시장 제외({r['market']})")
        if ex["preferred"] and r["is_preferred"]:
            rs.append("우선주")
        if ex["spac"] and r["is_spac"]:
            rs.append("스팩")
        if ex["reit"] and r["is_reit"]:
            rs.append("리츠")
        if ex["etf"] and r["is_etf_etn"]:
            rs.append("ETF/ETN")
        if ex["investment_company"] and r["is_investment_company"]:
            rs.append("투자회사")
        if ex.get("administrative_issue") and r["is_administrative"]:
            rs.append("관리종목")
        reasons.append(rs)
    df["exclusion_reasons"] = reasons
    return df


def apply_size_filter(df: pd.DataFrame) -> pd.Series:
    """상장주식수·시가총액 기준 (설정값)"""
    u = config.settings()["universe"]
    return ((df["listed_shares"] >= u["min_listed_shares"]) & (df["listed_shares"] <= u["max_listed_shares"])
            & (df["market_cap"] >= u["min_market_cap_krw"]))


def normalize_name(s: str) -> str:
    return re.sub(r"\s+", "", s or "")
