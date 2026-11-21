"""금융감독원 OpenDART 수집기

 - corpCode.xml       : 종목코드 → 고유번호(corp_code)
 - list.json          : 정기보고서(사업/반기/분기) 목록 → 접수번호·보고기간 확인
 - fnlttSinglAcntAll  : 단일회사 전체 재무제표 (fs_div=CFS 연결만 저장)
 - stockTotqySttus    : 주식의 총수 현황 (발행주식총수·자기주식수·유통주식수)

원칙
 - 연결재무제표(CFS)가 없으면 별도재무제표(OFS)로 대체하지 않음. OFS 는 '존재 여부 확인'에만 사용하고 저장하지 않음
 - API 키 미설정·네트워크 오류(수집 실패)와 '조회된 데이터 없음'(재무제표 미확인)을 구분함
 - 원천 응답(JSON)은 data/raw/dart 에 그대로 보관하여 재현성을 확보함
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from xml.etree import ElementTree

import requests

from core import config
from core.trading_calendar import now_kst

BASE = "https://opendart.fss.or.kr/api"

REPRT_ANNUAL, REPRT_H1, REPRT_Q1, REPRT_Q3 = "11011", "11012", "11013", "11014"
REPRT_NAME = {REPRT_ANNUAL: "사업보고서", REPRT_H1: "반기보고서", REPRT_Q1: "1분기보고서", REPRT_Q3: "3분기보고서"}

DART_STATUS = {
    "000": "정상", "010": "등록되지 않은 키", "011": "사용할 수 없는 키", "012": "접근할 수 없는 IP",
    "013": "조회된 데이터 없음", "014": "파일이 존재하지 않음", "020": "요청 제한 초과",
    "021": "조회 가능한 회사 개수 초과", "100": "필드의 부적절한 값", "101": "부적절한 접근",
    "800": "시스템 점검", "900": "정의되지 않은 오류", "901": "개인정보 보유기간 만료 키",
}
FATAL = {"010", "011", "012", "020", "101", "800", "901"}  # 이후 호출도 실패할 상태


class DartError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class NoApiKey(DartError):
    def __init__(self):
        super().__init__("NO_KEY", "DART_API_KEY 미설정")


@dataclass
class PeriodicReport:
    rcept_no: str
    rcept_dt: str
    report_nm: str
    kind: str          # 사업보고서/반기보고서/분기보고서
    period: str        # YYYY-MM (보고 대상 기간 말)
    is_correction: bool

    @property
    def period_date(self) -> tuple[int, int]:
        y, m = self.period.split("-")
        return int(y), int(m)


@dataclass
class StatementFetch:
    report: PeriodicReport
    reprt_code: str
    bsns_year: int | None
    status: str                  # OK / CFS_NOT_AVAILABLE / NOT_FOUND / FAILED
    rows: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    rcept_no_matched: bool = False
    collected_at: str = ""


_REPORT_RE = re.compile(r"(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)")


def parse_report_name(nm: str) -> tuple[str, str] | None:
    m = _REPORT_RE.search(nm or "")
    if not m:
        return None
    return m.group(1), f"{m.group(2)}-{m.group(3)}"


def reprt_code_for(kind: str, period_month: int, settle_month: int) -> str | None:
    """보고기간 말 월과 결산월로 DART 보고서 코드 결정"""
    if kind == "사업보고서":
        return REPRT_ANNUAL
    months_into = (period_month - settle_month) % 12
    return {3: REPRT_Q1, 6: REPRT_H1, 9: REPRT_Q3, 0: REPRT_ANNUAL}.get(months_into)


def bsns_year_candidates(period_year: int, period_month: int, settle_month: int) -> list[int]:
    """DART bsns_year 후보. 12월 결산은 보고기간 연도, 비12월 결산은 회계연도 종료연도와 그 전년을 시도"""
    if settle_month == 12:
        return [period_year]
    fy_end_year = period_year if period_month <= settle_month else period_year + 1
    return [fy_end_year, fy_end_year - 1]


def parse_amount(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N/A", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class DartCollector:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or config.env("DART_API_KEY")
        cfg = config.settings()["dart"]
        self.interval = cfg["request_interval_sec"]
        self.timeout = cfg["timeout_sec"]
        self.retries = cfg["max_retries"]
        self.lookback_months = cfg["lookback_months"]
        self.raw_dir = config.path("raw_dir") / "dart"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.log = config.setup_logging("dart_collector")
        self._last_call = 0.0
        self.call_count = 0
        self.fatal: DartError | None = None
        self.last_stock_diag = ""
        self.last_stock_rows: list[dict[str, Any]] = []

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    def _request(self, endpoint: str, params: dict[str, Any], binary: bool = False):
        if not self.api_key:
            raise NoApiKey()
        if self.fatal:
            raise self.fatal
        p = {"crtfc_key": self.api_key, **params}
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            wait = self.interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                self._last_call = time.time()
                self.call_count += 1
                r = requests.get(f"{BASE}/{endpoint}", params=p, timeout=self.timeout)
                r.raise_for_status()
                if binary:
                    if r.content[:2] == b"PK":
                        return r.content
                    j = r.json()
                    raise DartError(j.get("status", "900"), j.get("message", ""))
                j = r.json()
                code = j.get("status", "900")
                if code in FATAL:
                    self.fatal = DartError(code, j.get("message") or DART_STATUS.get(code, ""))
                    raise self.fatal
                return j
            except DartError:
                raise
            except (requests.RequestException, ValueError) as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
        raise DartError("NETWORK", f"{endpoint} 호출 실패: {last_exc}")

    def _save_raw(self, name: str, payload: Any) -> None:
        f = self.raw_dir / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    def corp_codes(self, refresh: bool = False) -> dict[str, dict[str, str]]:
        """{stock_code: {corp_code, corp_name, modify_date}}"""
        cache = self.raw_dir / "corpCode.json"
        if cache.exists() and not refresh and (time.time() - cache.stat().st_mtime) < 86400 * 7:
            return json.loads(cache.read_text(encoding="utf-8"))
        content = self._request("corpCode.xml", {}, binary=True)
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            xml = z.read(z.namelist()[0])
        root = ElementTree.fromstring(xml)
        out = {}
        for el in root.iter("list"):
            sc = (el.findtext("stock_code") or "").strip()
            if sc:
                out[sc] = {"corp_code": el.findtext("corp_code").strip(),
                           "corp_name": (el.findtext("corp_name") or "").strip(),
                           "modify_date": (el.findtext("modify_date") or "").strip()}
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out

    def periodic_reports(self, corp_code: str, ref: date) -> list[PeriodicReport]:
        m = ref.year * 12 + ref.month - 1 - self.lookback_months
        bgn = date(m // 12, m % 12 + 1, 1)
        out, page = [], 1
        while True:
            j = self._request("list.json", {
                "corp_code": corp_code, "bgn_de": bgn.strftime("%Y%m%d"), "end_de": ref.strftime("%Y%m%d"),
                "pblntf_ty": "A", "page_no": page, "page_count": 100, "last_reprt_at": "N"})
            if j.get("status") == "013":
                break
            if j.get("status") != "000":
                raise DartError(j.get("status"), j.get("message", ""))
            for it in j.get("list", []):
                parsed = parse_report_name(it.get("report_nm", ""))
                if not parsed:
                    continue
                kind, period = parsed
                out.append(PeriodicReport(it["rcept_no"], it["rcept_dt"], it["report_nm"].strip(), kind, period,
                                          "정정" in it["report_nm"]))
            if page >= int(j.get("total_page", 1)):
                break
            page += 1
        self._save_raw(f"{corp_code}/list_{ref:%Y%m%d}.json", [r.__dict__ for r in out])
        return out

    @staticmethod
    def latest_per_period(reports: list[PeriodicReport]) -> list[PeriodicReport]:
        """보고기간별 최종(정정 포함 가장 마지막 접수) 보고서, 최신 기간 순"""
        best: dict[tuple[str, str], PeriodicReport] = {}
        for r in reports:
            k = (r.kind, r.period)
            if k not in best or r.rcept_no > best[k].rcept_no:
                best[k] = r
        return sorted(best.values(), key=lambda r: (r.period, r.kind != "사업보고서"), reverse=True)

    # ------------------------------------------------------------------
    def fetch_statement(self, corp_code: str, report: PeriodicReport, settle_month: int,
                        all_rcept_nos: set[str]) -> StatementFetch:
        py, pm = report.period_date
        reprt = reprt_code_for(report.kind, pm, settle_month)
        collected_at = now_kst().isoformat(timespec="seconds")
        if reprt is None:
            return StatementFetch(report, "", None, "FAILED", message=f"보고서 코드 판정 불가(결산월 {settle_month})",
                                  collected_at=collected_at)
        first_data: StatementFetch | None = None
        any_ofs = False
        # 캐시: 같은 접수번호의 원천 응답이 이미 보관되어 있으면 재사용(정정공시는 접수번호가 달라 재수집됨)
        for by in bsns_year_candidates(py, pm, settle_month):
            f = self.raw_dir / corp_code / f"fnltt_{by}_{reprt}_CFS.json"
            if f.exists():
                try:
                    j = json.loads(f.read_text(encoding="utf-8"))
                    rows = j.get("list", [])
                    if rows and rows[0].get("rcept_no") == report.rcept_no:
                        return StatementFetch(report, reprt, by, "OK", rows, message="원천 보관본 재사용",
                                              rcept_no_matched=True, collected_at=collected_at)
                except (OSError, ValueError):
                    pass
        for by in bsns_year_candidates(py, pm, settle_month):
            j = self._request("fnlttSinglAcntAll.json", {"corp_code": corp_code, "bsns_year": by,
                                                          "reprt_code": reprt, "fs_div": "CFS"})
            if j.get("status") == "000":
                rows = j.get("list", [])
                self._save_raw(f"{corp_code}/fnltt_{by}_{reprt}_CFS.json", j)
                matched = bool(rows) and rows[0].get("rcept_no") in all_rcept_nos
                fetch = StatementFetch(report, reprt, by, "OK", rows, rcept_no_matched=matched,
                                       collected_at=collected_at)
                if matched:
                    return fetch
                first_data = first_data or fetch
            elif j.get("status") == "013":
                jo = self._request("fnlttSinglAcntAll.json", {"corp_code": corp_code, "bsns_year": by,
                                                               "reprt_code": reprt, "fs_div": "OFS"})
                if jo.get("status") == "000" and jo.get("list") and jo["list"][0].get("rcept_no") in all_rcept_nos:
                    any_ofs = True
            else:
                return StatementFetch(report, reprt, by, "FAILED",
                                      message=f"[{j.get('status')}] {j.get('message')}", collected_at=collected_at)
        if first_data:
            first_data.message = "응답 접수번호가 정기보고서 목록과 불일치 - 확인 필요"
            return first_data
        if any_ofs:
            return StatementFetch(report, reprt, None, "CFS_NOT_AVAILABLE",
                                  message="연결재무제표 확인 불가(별도재무제표만 공시)", collected_at=collected_at)
        return StatementFetch(report, reprt, None, "NOT_FOUND", message="재무제표 미확인(조회된 데이터 없음)",
                              collected_at=collected_at)

    def stock_totals(self, corp_code: str, bsns_year: int, reprt_code: str) -> dict[str, Any] | None:
        """보통주 기준 발행주식총수·자기주식수·유통주식수 + 결산기준일

        진단 정보는 self.last_stock_diag 에 남김 (응답 상태, 구분값 목록)
        """
        j = self._request("stockTotqySttus.json", {"corp_code": corp_code, "bsns_year": bsns_year,
                                                    "reprt_code": reprt_code})
        self._save_raw(f"{corp_code}/stockTotqy_{bsns_year}_{reprt_code}.json", j)
        self.last_stock_rows = j.get("list", []) if j.get("status") == "000" else []
        ses = [(it.get("se") or "") for it in j.get("list", [])]
        self.last_stock_diag = f"stockTotqySttus({bsns_year},{reprt_code}) status={j.get('status')} se={ses}"
        if j.get("status") != "000":
            return None
        for it in j.get("list", []):
            if is_common_class(it.get("se")):
                out = share_row(it)
                others = [parse_amount(x.get("distb_stock_co")) or 0 for x in j.get("list", [])
                          if not is_common_class(x.get("se")) and not _is_total_or_note(x.get("se"))]
                out["other_class_outstanding"] = float(sum(others))
                self.last_stock_diag += f" → 사용: {it}"
                return out
        return None

    def treasury_events(self, corp_code: str, after: date, until: date) -> list[dict[str, str]] | None:
        """기준일 이후 자기주식 취득·처분·소각·신탁 관련 주요사항보고서 등 공시 목록 (None = 조회 실패)"""
        out, page = [], 1
        while True:
            j = self._request("list.json", {"corp_code": corp_code, "bgn_de": after.strftime("%Y%m%d"),
                                            "end_de": until.strftime("%Y%m%d"), "page_no": page, "page_count": 100})
            if j.get("status") == "013":
                break
            if j.get("status") != "000":
                return None
            for it in j.get("list", []):
                nm = re.sub(r"\s+", "", it.get("report_nm") or "")
                if re.search(r"자기주식|자사주|주식소각|이익소각", nm):
                    out.append({"rcept_no": it["rcept_no"], "rcept_dt": it["rcept_dt"], "report_nm": it["report_nm"].strip()})
            if page >= int(j.get("total_page", 1)):
                break
            page += 1
        self._save_raw(f"{corp_code}/treasury_events_{after:%Y%m%d}_{until:%Y%m%d}.json", out)
        self.last_stock_diag = f"자기주식 관련 공시({after}~{until}) {len(out)}건: " + "; ".join(
            f"{e['rcept_dt']} {e['report_nm']}" for e in out[:5])
        return out

    def treasury_period_end(self, corp_code: str, bsns_year: int, reprt_code: str) -> dict[str, Any] | None:
        """자기주식 취득 및 처분 현황(tesstkAcqsDspsSttus)의 '총계' 행 보통주 기말수량"""
        j = self._request("tesstkAcqsDspsSttus.json", {"corp_code": corp_code, "bsns_year": bsns_year,
                                                        "reprt_code": reprt_code})
        self._save_raw(f"{corp_code}/tesstk_{bsns_year}_{reprt_code}.json", j)
        rows = j.get("list", []) if j.get("status") == "000" else []
        self.last_stock_diag = (f"tesstkAcqsDspsSttus({bsns_year},{reprt_code}) status={j.get('status')} "
                                f"rows={len(rows)}")
        tot = [r for r in rows if "총계" in re.sub(r"\s+", "", (r.get("acqs_mth1") or "") + (r.get("acqs_mth2") or ""))
               and is_common_class(r.get("stock_knd"))]
        if len(tot) != 1:
            sample = {k: rows[0].get(k) for k in ("acqs_mth1", "acqs_mth2", "acqs_mth3", "stock_knd", "bsis_qy",
                                                  "change_qy_acqs", "change_qy_dsps", "trmend_qy", "stlm_dt")} if rows else {}
            self.last_stock_diag += f" 보통주 총계 행 {len(tot)}개 → 사용 안 함 | 첫 행: {sample}"
            return None
        q = parse_amount(tot[0].get("trmend_qy"))
        if q is None and (tot[0].get("trmend_qy") or "").strip() == "-":
            q = 0.0
        self.last_stock_diag += f" → 보통주 기말 자기주식 {q}"
        return {"treasury_shares": q, "stlm_dt": tot[0].get("stlm_dt"), "rcept_no": tot[0].get("rcept_no")}


def is_common_class(se: str | None) -> bool:
    t = re.sub(r"\s+", "", se or "")
    return "보통" in t or ("의결권있" in t and "없" not in t.replace("있는", ""))


def _is_total_or_note(se: str | None) -> bool:
    t = re.sub(r"\s+", "", se or "")
    return "합계" in t or "비고" in t or "총계" in t


def share_row(it: dict[str, Any]) -> dict[str, Any]:
    out = {"issued_shares": parse_amount(it.get("istc_totqy")), "treasury_shares": parse_amount(it.get("tesstk_co")),
           "outstanding_shares": parse_amount(it.get("distb_stock_co")), "stlm_dt": it.get("stlm_dt"),
           "rcept_no": it.get("rcept_no")}
    raw_t = (it.get("tesstk_co") or "").strip()
    if out["treasury_shares"] is None and raw_t == "-" and out["issued_shares"] is not None:
        # DART 는 0 을 '-' 로 표기함: 발행 = 유통 이 확인될 때만 0 으로 해석
        if out["outstanding_shares"] is None or abs(out["outstanding_shares"] - out["issued_shares"]) < 1:
            out["treasury_shares"] = 0.0
            out["note"] = "자기주식 '-' 표기 → 발행주식수 = 유통주식수 확인되어 0주로 해석"
    if out["outstanding_shares"] is None and None not in (out["issued_shares"], out["treasury_shares"]):
        out["outstanding_shares"] = out["issued_shares"] - out["treasury_shares"]
        out["note"] = (out.get("note", "") + " 유통주식수 = 발행주식총수 − 자기주식수(원천 공란)").strip()
    return out


def only_common_class(rows: list[dict[str, Any]]) -> bool:
    """보통주 외 종류주식(우선주 등) 발행주식수가 모두 0/공란이면 True"""
    for it in rows:
        if is_common_class(it.get("se")) or _is_total_or_note(it.get("se")):
            continue
        if (parse_amount(it.get("istc_totqy")) or 0) > 0:
            return False
    return True
