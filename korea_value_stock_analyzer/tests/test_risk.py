"""리스크 공시 분류 규칙 테스트 (PART 6)"""
from datetime import date

from analyzers.risk_analyzer import RiskAnalyzer, classify, final_result_of


def test_classification_and_stage():
    assert classify("횡령ㆍ배임혐의발생") == ("횡령·배임", "조사 착수")
    assert classify("횡령ㆍ배임사실확인")[1].startswith("기소 또는 판결")
    assert classify("[유가]불성실공시법인지정예고")[0] == "불성실공시법인 지정"
    assert classify("[유가]불성실공시법인지정예고")[1] == "조사 착수"
    assert classify("[코]불성실공시법인지정")[1] == "행정제재"
    assert classify("횡령ㆍ배임혐의발생(무혐의)")[1].startswith("무혐의")
    assert classify("기타경영사항(자율공시)(공정거래위원회 과징금 부과)")[0] == "공정거래법 및 하도급법 위반"
    assert classify("주요사항보고서(유상증자결정)")[0] is None


def test_final_result_never_asserts_guilt():
    assert "미확정" in final_result_of("조사 착수")
    assert "무혐의" in final_result_of("무혐의 및 무죄(취하·기각·해제 포함)")


class _FakeDart:
    available = True

    def __init__(self, items):
        self.items = items

    def disclosures(self, corp, s, e, ty):
        return self.items if ty == "I" else []


class _NoNews:
    available = False


def test_no_events_is_not_declared_clean():
    rep = RiskAnalyzer(_FakeDart([]), _NoNews()).analyze("T", "테스트", "00000001", date(2026, 11, 21))
    assert rep.conclusion == "검색 범위 내 확인된 사건 없음"
    assert rep.status == "PARTIAL" and "미실시" in rep.scope["news"]


def test_events_recorded_with_source():
    items = [{"report_nm": "[코]불성실공시법인지정", "rcept_no": "20200101000001", "rcept_dt": "20200101",
              "flr_nm": "코스닥시장본부"}]
    rep = RiskAnalyzer(_FakeDart(items), _NoNews()).analyze("T", "테스트", "00000001", date(2026, 11, 21))
    e = rep.events[0]
    assert e.event_year == 2020 and e.stage == "행정제재" and "rcpNo=20200101000001" in e.source_url
    assert "원문 확인 필요" in rep.conclusion


def test_news_false_positive_filters():
    from analyzers.risk_analyzer import classify_news
    assert classify_news("GS, SOOP 4연패 몰고 2연승…'호키 트리플크라운' 삼성화재 연패 탈출") is None
    assert classify_news("와이엔텍, 코오롱건설과 16억 하도급계약 체결") is None
    assert classify_news("'단선·화재 안심' 쿠쿠홈시스, DC 카본매트 출시") is None
    assert classify_news("공정위, KH바텍 불공정 하도급 시정명령") == "공정거래법 및 하도급법 위반"
    assert classify_news("여수산단 와이엔텍 폐기물 야적장 화재 발생") == "중대한 환경·안전 사고"
    assert classify_news("효성, 분식회계 재판 마침표") == "회계부정"


def test_non_designation_and_resolution():
    assert classify("불성실공시법인미지정 (지정유예)")[1].startswith("무혐의")
    assert classify("기타시장안내(상장적격성 실질심사 대상제외 결정)")[1].startswith("무혐의")
    assert classify("조회공시요구(풍문또는보도)(감사의견 비적정설)") == ("회계부정", "의혹 제기")
    assert classify("생산중단(자회사의 주요경영사항)")[0].startswith("생산중단")
