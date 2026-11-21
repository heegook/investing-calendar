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
    (r"화재|폭발|중대재해|산업재해|사망사고|유출|환경오염", "중대한 환경·안전 사고"),
    (r"생산중단|영업정지", "생산중단·영업정지(사유 원문 확인)"),
]

# (정규식, 진행 단계)
STAGE_RULES: list[tuple[str, str]] = [
    (r"미지정|지정유예|무혐의|무죄|불기소|취하|기각|지정취소|해제|대상\s*제외|사유\s*해소",
     "무혐의 및 무죄(미지정·취하·기각·해제·해소 포함)"),
    (r"풍문|보도", "의혹 제기"),
    (r"확정|최종", "최종 판결"),
    (r"판결|선고|결정\(1심\)|1심", "1심 판결"),
    (r"기소", "기소"),
    (r"지정예고|예고", "조사 착수"),
    (r"지정|과징금|시정명령|조치|부과", "행정제재"),
    (r"사실확인", "기소 또는 판결(사실확인 공시, 원문 확인 필요)"),
    (r"혐의발생|고소|고발|조사|수사|압수수색", "조사 착수"),
]

# 뉴스 검색어 → 기사 제목 분류 규칙
#  - 검색어는 넓게, 분류는 '제목'에 위반·사고를 뜻하는 단어가 있을 때만 (계약 체결·제품 홍보 기사 제외)
NEWS_QUERIES = ["횡령", "배임", "분식회계", "공정위", "하도급", "갑질", "오너 리스크", "경영권 분쟁",
                "불매", "리콜", "화재", "중대재해", "압수수색"]
NEWS_TYPE_RULES: list[tuple[str, str]] = [
    (r"횡령|배임", "횡령·배임"),
    (r"분식|회계부정|회계처리기준\s*위반|감리", "회계부정"),
    (r"(공정위|공정거래|하도급).*(과징금|시정명령|제재|위반|고발|불공정|부당)|"
     r"(과징금|시정명령|제재|불공정|부당).*(공정위|공정거래|하도급)", "공정거래법 및 하도급법 위반"),
    (r"오너\s*리스크|갑질|경영권\s*분쟁|압수수색|구속|기소", "오너리스크"),
    (r"불매|리콜|사과문|대국민\s*사과", "중대한 사회적 논란"),
    (r"(화재|폭발|중대재해|사망|누출|유출)", "중대한 환경·안전 사고"),
]
# 회사명·업종 용어에 포함된 '화재' 등으로 인한 오탐 제거
NEWS_TITLE_EXCLUDE = (r"삼성화재|현대해상|DB손해|DB손보|메리츠화재|흥국화재|롯데손해|한화손해|화재보험|해상화재|"
                      r"화재\s*(안심|예방|감지|보험|안전\s*인증)|방화문|소화기|개인정보\s*유출\s*방지")
# 안전 설비 기부·봉사·캠페인 등 사고가 아닌 기사
NEWS_NON_EVENT = r"봉사|기부|후원|캠페인|훈련|경보기|차단장치|설치\s*지원|예방\s*교육|출시|인증\s*획득"
NEWS_URL_EXCLUDE = r"sports|/star|starnews|entertain|/volleyball/|/baseball/|/football/|/golf/"


def name_in_title(name: str, title: str) -> bool:
    """회사명이 다른 회사명의 일부로만 쓰인 경우(예: '케이씨' ⊂ '케이씨코에너지') 제외"""
    t = title
    n = re.escape(name)
    return bool(re.search(rf"(?<![가-힣A-Za-z0-9]){n}(?![가-힣A-Za-z0-9])|{n}(?=(은|는|이|가|의|을|를|에|와|과|도|,|·|\)|'|\"|…|$))", t))


def classify_news(title: str) -> str | None:
    if re.search(NEWS_NON_EVENT, title):
        return None
    if re.search(NEWS_TITLE_EXCLUDE, title):
        title = re.sub(NEWS_TITLE_EXCLUDE, "", title)
    return next((t for p, t in NEWS_TYPE_RULES if re.search(p, title)), None)


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


def _bump(txt: str) -> str:
    m = re.search(r"동일 보도 (\d+)건", txt)
    n = int(m.group(1)) + 1 if m else 2
    return re.sub(r"동일 보도 \d+건", f"동일 보도 {n}건", txt) if m else f"{txt} · 동일 보도 {n}건"


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
            rep.scope["news"] = (f"네이버 뉴스 검색 API(검색어 {len(NEWS_QUERIES)}개, 관련도순 상위, 기간 내 기사만) - "
                                 "제목에 회사명과 위반·사고 단어가 함께 있는 기사만 분류, 동일일·동일유형 기사는 1건으로 묶음")
            try:
                clusters: dict[tuple, RiskEvent] = {}
                for q in NEWS_QUERIES:
                    for n in self.news.search(f"{name} {q}", must_contain=[name]):
                        if n["date"] and n["date"] < start.isoformat():
                            continue
                        if re.search(NEWS_URL_EXCLUDE, n["link"] or "", re.I):
                            continue
                        if not name_in_title(name, n["title"]):
                            continue
                        etype = classify_news(n["title"])
                        if etype is None:
                            continue
                        key = (etype, n["date"])
                        if key in clusters:
                            clusters[key].final_result = _bump(clusters[key].final_result)
                            continue
                        clusters[key] = RiskEvent(
                            stock_code, n["year"], etype, n["title"], None, "의혹 제기(언론 보도, 사실 미확정)",
                            "미확정(언론 보도) · 동일 보도 1건", n["link"], "NEWS", checked, n["date"])
                rep.events += list(clusters.values())
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
