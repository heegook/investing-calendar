"""경영층 모럴해저드 및 기업 리스크 조사 (STEP 7, PART 6)

조사 방법
 1) DART 공시 검색 (최근 10년): 주요사항보고(B)·기타공시(E)·거래소공시(I)의 보고서명을 규칙으로 분류
    - 횡령·배임 혐의발생/사실확인, 불성실공시법인 지정(예고), 공시번복·불이행, 회계처리기준 위반·감리,
      공정위 과징금·시정명령, 상장적격성 실질심사, 경영권 분쟁, 화재·중대재해 등
 2) 뉴스 검색 (선택): NAVER_CLIENT_ID/SECRET 이 있으면 네이버 뉴스 검색 API 로 키워드 보도 수집
    - 언론 보도는 '의혹 제기(언론 보도)' 단계로만 기록하며 사실로 표현하지 않음

원칙
 - 확정되지 않은 의혹을 범죄 사실로 표현하지 않음: 공시 보고서명과 단계만 기록하고 원문 링크를 남김
 - 사건이 없더라도 '문제 없음'으로 단정하지 않고 '검색 범위 내 확인된 사건 없음'으로 표시
 - 조사 범위(기간·공시 유형·뉴스 수집 여부)를 결과와 함께 저장
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from collectors.dart_collector import DartCollector, DartError
from collectors.news_collector import NewsCollector
from core.trading_calendar import now_kst

DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

# (정규식, 사건 유형) — 위에서부터 먼저 일치하는 규칙 적용
TYPE_RULES: list[tuple[str, str]] = [
    (r"횡령|배임", "횡령·배임"),
    (r"불성실공시법인", "불성실공시법인 지정"),
    (r"공시번복|공시불이행|공시변경|공시위반", "공시 위반"),
    (r"회계처리기준|감리|분식|증권선물위원회|감사의견|의견거절|감사범위제한", "회계부정"),
    (r"공정거래위원회|공정위|하도급|담합|시정명령|과징금", "공정거래법 및 하도급법 위반"),
    (r"상장적격성|실질심사|상장폐지사유|개선기간", "상장 관련 중대 사유"),
    (r"경영권분쟁|경영권\s*분쟁|최대주주.*소송|주주총회.*소송|직무집행정지", "오너리스크"),
    (r"화재|폭발|중대재해|산업재해|사망사고|유출|환경오염|생산중단|영업정지", "중대한 환경·안전 사고"),
]

# (정규식, 진행 단계)
STAGE_RULES: list[tuple[str, str]] = [
    (r"무혐의|무죄|불기소|취하|기각|지정취소|해제", "무혐의 및 무죄(취하·기각·해제 포함)"),
    (r"확정|최종", "최종 판결"),
    (r"판결|선고|결정\(1심\)|1심", "1심 판결"),
    (r"기소", "기소"),
    (r"지정예고|예고", "조사 착수"),
    (r"지정|과징금|시정명령|조치|부과", "행정제재"),
    (r"사실확인", "기소"),            # 횡령·배임 사실확인: 공소 제기·판결 등으로 사실이 확인된 경우 공시됨(원문 확인 필요)
    (r"혐의발생|고소|고발|조사|수사|압수수색", "조사 착수"),
]

NEWS_KEYWORDS = {
    "횡령·배임": ["횡령", "배임"],
    "회계부정": ["분식회계", "회계부정"],
    "공정거래법 및 하도급법 위반": ["공정위 과징금", "하도급"],
    "오너리스크": ["오너 리스크", "갑질", "경영권 분쟁"],
    "중대한 사회적 논란": ["불매", "논란"],
    "중대한 환경·안전 사고": ["화재", "중대재해"],
}


@dataclass
class RiskEvent:
    stock_code: str
    event_year: int | None
    event_type: str
    summary: str
    related_party: str | None
    stage: str
    final_result: str
    source_url: str
    source: str
    checked_date: str
    event_date: str | None = None


@dataclass
class RiskReport:
    stock_code: str
    name: str
    events: list[RiskEvent] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"          # OK / PARTIAL / FAILED
    message: str = ""

    @property
    def conclusion(self) -> str:
        if self.status == "FAILED":
            return f"조사 실패: {self.message}"
        if not self.events:
            return "검색 범위 내 확인된 사건 없음"
        types = sorted({e.event_type for e in self.events})
        return f"검색 범위 내 {len(self.events)}건 확인 ({', '.join(types)}) - 원문 확인 필요"


def classify(report_nm: str) -> tuple[str | None, str]:
    nm = re.sub(r"\s+", "", report_nm or "")
    etype = next((t for p, t in TYPE_RULES if re.search(p, nm)), None)
    stage = next((s for p, s in STAGE_RULES if re.search(p, nm)), "공시(진행 단계는 원문 확인 필요)")
    return etype, stage


def final_result_of(stage: str) -> str:
    if stage.startswith("무혐의"):
        return "무혐의 및 무죄(공시 기준)"
    if stage in ("최종 판결",):
        return "최종 판결(내용은 원문 확인)"
    return "미확정(진행 중 또는 원문 확인 필요)"


class RiskAnalyzer:
    PBLNTF_TYPES = ("B", "E", "I")

    def __init__(self, dart: DartCollector | None = None, news: NewsCollector | None = None, years: int = 10):
        self.dart = dart or DartCollector()
        self.news = news or NewsCollector()
        self.years = years

    def analyze(self, stock_code: str, name: str, corp_code: str | None, ref: date) -> RiskReport:
        checked = now_kst().date().isoformat()
        start = date(ref.year - self.years, ref.month, 1)
        rep = RiskReport(stock_code, name, scope={
            "period": f"{start} ~ {ref}", "dart_types": "주요사항보고(B)·기타공시(E)·거래소공시(I) 보고서명",
            "news": "미실시", "checked_date": checked})
        # ---------- DART ----------
        if not self.dart.available or not corp_code:
            rep.status, rep.message = "FAILED", "DART_API_KEY 미설정 또는 고유번호 없음 - 공시 조사 미실시"
        else:
            try:
                for ty in self.PBLNTF_TYPES:
                    for it in self.dart.disclosures(corp_code, start, ref, ty):
                        etype, stage = classify(it.get("report_nm", ""))
                        if etype is None:
                            continue
                        dt = it.get("rcept_dt")
                        rep.events.append(RiskEvent(
                            stock_code, int(dt[:4]) if dt else None, etype, it.get("report_nm", "").strip(),
                            it.get("flr_nm"), stage, final_result_of(stage), DART_VIEW.format(it["rcept_no"]),
                            f"DART({ty})", checked, f"{dt[:4]}-{dt[4:6]}-{dt[6:]}" if dt else None))
            except DartError as e:
                rep.status, rep.message = "FAILED", f"DART 공시 조사 실패: {e}"
        # ---------- 뉴스 ----------
        if self.news.available:
            rep.scope["news"] = f"네이버 뉴스 검색 API(키워드 {sum(len(v) for v in NEWS_KEYWORDS.values())}개, 최근 보도 위주)"
            try:
                for etype, kws in NEWS_KEYWORDS.items():
                    for kw in kws:
                        for n in self.news.search(f"{name} {kw}", must_contain=[name, kw.split()[0]]):
                            rep.events.append(RiskEvent(
                                stock_code, n["year"], etype, n["title"], None, "의혹 제기(언론 보도, 사실 미확정)",
                                "미확정(언론 보도)", n["link"], "NEWS", checked, n["date"]))
            except Exception as e:  # noqa: BLE001
                rep.status = "PARTIAL" if rep.status == "OK" else rep.status
                rep.message = (rep.message + f" / 뉴스 수집 실패: {e}").strip(" /")
        else:
            rep.scope["news"] = "미실시(NAVER_CLIENT_ID/SECRET 미설정) - 언론 보도 기반 사건은 조사되지 않음"
            if rep.status == "OK":
                rep.status = "PARTIAL"
                rep.message = "뉴스 조사 미실시"
        # 중복 제거(같은 원문 URL)
        seen, uniq = set(), []
        for e in sorted(rep.events, key=lambda x: (x.event_date or ""), reverse=True):
            if e.source_url in seen:
                continue
            seen.add(e.source_url)
            uniq.append(e)
        rep.events = uniq
        return rep
