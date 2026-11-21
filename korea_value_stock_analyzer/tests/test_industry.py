"""특이사항 판정 규칙 테스트 (PART 5)"""
from analyzers.industry_analyzer import IndustryAnalyzer, split_sections

DOC = """§I. 회사의 개요
§1. 회사의 개요
당사는 전자부품 제조업을 영위함.
연결대상 종속회사 | 설립일 | 주소 | 주요사업
Test Electronics (Suzhou) Co., Ltd. | 2010.01 | 중국 소주 | 전자부품 제조
§II. 사업의 내용
§1. 사업의 개요
당사는 스마트폰 부품을 생산함.
§2. 주요 제품 및 서비스
카메라모듈 70%, 기타 30%
§4. 매출 및 수주상황
지역별 매출은 국내 40%, 중국 35%, 기타 25% 입니다.
§7. 기타 참고사항
산업은 성숙 단계임.
§III. 재무에 관한 사항
"""


class _Dart:
    available = True

    def __init__(self, doc, code="26299", inv=None):
        self.doc, self.code, self.inv = doc, code, inv or []

    def company_info(self, c):
        return {"induty_code": self.code}

    def document_text(self, c, r):
        return self.doc

    def other_investments(self, c, y):
        return self.inv


ANNUAL = {"report_nm": "사업보고서 (2025.12)", "rcept_no": "20260310000001", "bsns_year": 2025}


def test_china_yes_with_evidence():
    rep = IndustryAnalyzer(_Dart(DOC)).analyze("T", "테스트", "0001", ANNUAL)
    j = rep.special["중국 관련주"]
    assert j.result == "Yes"
    assert any("현지법인" in e for e in j.evidence) and any("매출비중" in e for e in j.evidence)
    assert rep.special["지주회사"].result == "No" and rep.special["금융주"].result == "No"
    assert "카메라모듈" in rep.business["주요 매출 품목 및 부문별 매출 비중"]


def test_export_only_is_not_china_related():
    doc = DOC.replace("Test Electronics (Suzhou) Co., Ltd. | 2010.01 | 중국 소주 | 전자부품 제조", "") \
             .replace("지역별 매출은 국내 40%, 중국 35%, 기타 25% 입니다.", "제품 일부를 중국에 수출하고 있음.")
    j = IndustryAnalyzer(_Dart(doc)).analyze("T", "테스트", "0001", ANNUAL).special["중국 관련주"]
    assert j.result == "No" and any("수출만으로는" in e for e in j.evidence)


def test_holding_and_financial_by_ksic():
    rep = IndustryAnalyzer(_Dart(DOC, code="64992")).analyze("T", "테스트홀딩스", "0001", ANNUAL)
    assert rep.special["지주회사"].result == "Yes" and rep.special["금융주"].result == "No"
    rep2 = IndustryAnalyzer(_Dart(DOC, code="65121")).analyze("T", "테스트보험", "0001", ANNUAL)
    assert rep2.special["금융주"].result == "Yes"


def test_unavailable_is_not_no():
    class NoKey:
        available = False
    rep = IndustryAnalyzer(NoKey()).analyze("T", "테스트", None, None)
    assert all(j.result == "확인 불가" for j in rep.special.values())


def test_split_sections():
    secs = split_sections(DOC)
    assert any(t.startswith("II. 사업의 내용") for t, _ in secs)
