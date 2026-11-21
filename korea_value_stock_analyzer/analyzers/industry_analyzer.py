"""특이사항(PART 5) 판정 및 사업·산업 원문 추출(PART 7) - STEP 8

특이사항 판정 (Yes / No / 확인 불가, 근거 필수)
 - 지주회사: DART 기업개황 업종코드 64992(지주회사) 또는 사업보고서 '회사의 개요'의 공정거래법상 지주회사 기재
 - 금융주  : DART 기업개황 업종코드 64~66(금융·보험업, 지주회사 64992 제외) 또는 금융지주회사 기재
 - 중국 관련주: 아래 중 하나 이상이 원문에서 확인될 때만 Yes
     · 중국 현지법인(연결 종속회사 소재지 또는 타법인 출자 중 지분율 20% 이상 중국 법인)
     · 중국 생산시설(사업의 내용에 중국 + 공장/생산 기재)
     · 중국 매출 비중 10% 이상 기재
     · 중국 주요 고객사 기재
   단순 수출 언급만 있으면 No (근거에 그 사실을 남김)

사업·산업 분석
 - 사업보고서 'II. 사업의 내용' 하위 항목을 원문 그대로 발췌하여 PART 7 항목에 대응시킴
 - 산업 분류(구조적 성장/성숙/사양/복합)와 성장동력 판단은 정성 판단이므로 원문 근거 없이 자동 판정하지 않음
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from collectors.dart_collector import DartCollector, DartError

CHINA_NAME = (r"중국|China|CHINA|上海|Shanghai|SHANGHAI|北京|Beijing|天津|Tianjin|蘇州|苏州|Suzhou|青岛|青島|Qingdao|"
              r"深圳|Shenzhen|广州|廣州|Guangzhou|大连|大連|Dalian|无锡|無錫|Wuxi|烟台|煙台|Yantai|威海|Weihai|Hangzhou|"
              r"杭州|Nanjing|南京|Chongqing|重庆|Chengdu|成都|Shenyang|沈阳|Xi'?an|西安|Dongguan|东莞|Huizhou|惠州|"
              r"Kunshan|昆山|Changzhou|常州|Nantong|南通|Hebei|河北|Shandong|山东|Jiangsu|江苏|Zhejiang|浙江|"
              r"Guangdong|广东|Liaoning|辽宁|Jilin|吉林|有限公司|유한공사")
HK_NAME = r"홍콩|Hong\s*Kong|HONG\s*KONG|香港"

# PART 7 항목 ↔ 사업의 내용 하위 목차
BUSINESS_SECTIONS = {
    "주요 사업 부문": r"사업의\s*개요",
    "주요 매출 품목 및 부문별 매출 비중": r"주요\s*제품|주요\s*서비스",
    "원재료 및 생산설비": r"원재료",
    "매출·수주 및 주요 고객": r"매출\s*및\s*수주|매출에\s*관한|판매경로",
    "위험관리(환율 민감도 등)": r"위험관리|파생",
    "주요 계약 및 연구개발": r"주요계약|연구개발",
    "산업의 특성·성장성·경쟁(기타 참고사항)": r"기타\s*참고사항",
}


@dataclass
class Judgement:
    item: str
    result: str                 # Yes / No / 확인 불가
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass
class IndustryReport:
    stock_code: str
    name: str
    special: dict[str, Judgement] = field(default_factory=dict)
    business: dict[str, str] = field(default_factory=dict)       # PART 7 항목 → 원문 발췌
    classification: str = "판정 보류(정성 분석 필요)"
    rationale: str = ("사업보고서 '사업의 내용' 원문 발췌를 근거로 정성 판단이 필요함. "
                      "산업 성장성과 기업 성장성은 구분하여 판단해야 하므로 원문 근거 없이 자동 분류하지 않음")
    source_report: str | None = None
    status: str = "OK"
    message: str = ""


def split_sections(text: str) -> list[tuple[str, str]]:
    """'§제목' 줄 기준으로 (제목, 본문) 목록"""
    out, cur_title, buf = [], "", []
    for line in text.split("\n"):
        if line.startswith("§"):
            if cur_title or buf:
                out.append((cur_title, "\n".join(buf).strip()))
            cur_title, buf = line[1:].strip(), []
        else:
            buf.append(line)
    out.append((cur_title, "\n".join(buf).strip()))
    return out


def section_range(sections: list[tuple[str, str]], start_pat: str, end_pat: str) -> list[tuple[str, str]]:
    i = next((k for k, (t, _) in enumerate(sections) if re.search(start_pat, t)), None)
    if i is None:
        return []
    j = next((k for k in range(i + 1, len(sections)) if re.search(end_pat, sections[k][0])), len(sections))
    return sections[i:j]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.다음함됨])\s+|\n", text) if s.strip()]


class IndustryAnalyzer:
    def __init__(self, dart: DartCollector | None = None, excerpt_chars: int = 2500):
        self.dart = dart or DartCollector()
        self.excerpt_chars = excerpt_chars

    # ------------------------------------------------------------------
    def analyze(self, stock_code: str, name: str, corp_code: str | None, annual: dict[str, Any] | None) -> IndustryReport:
        rep = IndustryReport(stock_code, name)
        if not self.dart.available or not corp_code or not annual:
            rep.status, rep.message = "FAILED", "DART 키·고유번호·사업보고서 정보 없음"
            for k in ("지주회사", "중국 관련주", "금융주"):
                rep.special[k] = Judgement(k, "확인 불가", [rep.message])
            return rep
        rep.source_report = f"{annual['report_nm']} ({annual['rcept_no']})"
        url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={annual['rcept_no']}"
        try:
            info = self.dart.company_info(corp_code) or {}
        except DartError as e:
            info, rep.message = {}, f"기업개황 조회 실패: {e}"
        try:
            text = self.dart.document_text(corp_code, annual["rcept_no"])
        except (DartError, Exception) as e:  # noqa: BLE001
            text, rep.message = None, (rep.message + f" / 사업보고서 원문 조회 실패: {e}").strip(" /")
        try:
            inv = self.dart.other_investments(corp_code, annual["bsns_year"])
        except DartError:
            inv = None

        sections = split_sections(text) if text else []
        overview = "\n".join(b for _, b in section_range(sections, r"회사의\s*개요", r"사업의\s*내용"))
        business_secs = section_range(sections, r"사업의\s*내용", r"재무에\s*관한\s*사항")
        business = "\n".join(b for _, b in business_secs)
        code = str(info.get("induty_code") or "")
        code_src = f"DART 기업개황 업종코드 {code or '미확인'}"

        rep.special["지주회사"] = self._holding(code, code_src, overview, text is not None, url)
        rep.special["금융주"] = self._financial(code, code_src, overview, text is not None, url)
        rep.special["중국 관련주"] = self._china(overview, business, inv, text is not None, url)

        # PART 7: 사업의 내용 하위 항목 원문 발췌
        for item, pat in BUSINESS_SECTIONS.items():
            parts = [b for t, b in business_secs if re.search(pat, t)]
            rep.business[item] = (("\n".join(parts))[: self.excerpt_chars] if parts else "원문에서 해당 항목 미확인")
        if text is None:
            rep.status = "FAILED"
        elif not business_secs:
            rep.status = "PARTIAL"
            rep.message = (rep.message + " / 사업의 내용 목차 식별 실패").strip(" /")
        return rep

    # ------------------------------------------------------------------
    @staticmethod
    def _holding(code: str, code_src: str, overview: str, has_doc: bool, url: str) -> Judgement:
        ev = []
        if code.startswith("64992"):
            ev.append(f"{code_src} = 지주회사(KSIC 64992)")
        m = re.search(r"[^.\n]{0,80}(독점규제\s*및\s*공정거래에\s*관한\s*법률|공정거래법)[^.\n]{0,40}지주회사[^.\n]{0,60}", overview)
        if m:
            ev.append(f"회사의 개요: “{m.group(0).strip()}”")
        if ev:
            return Judgement("지주회사", "Yes", ev, [url])
        if not code and not has_doc:
            return Judgement("지주회사", "확인 불가", ["업종코드·사업보고서 원문 모두 미확인"], [url])
        return Judgement("지주회사", "No", [f"{code_src}(64992 아님)",
                                          "사업보고서 회사의 개요에 공정거래법상 지주회사 기재 없음" if has_doc
                                          else "사업보고서 원문 미확인"], [url])

    @staticmethod
    def _financial(code: str, code_src: str, overview: str, has_doc: bool, url: str) -> Judgement:
        ev = []
        if code[:2] in ("64", "65", "66") and not code.startswith("64992"):
            ev.append(f"{code_src} = 금융 및 보험업(KSIC 64~66)")
        if re.search(r"금융지주회사", overview):
            ev.append("회사의 개요에 '금융지주회사' 기재")
        if ev:
            return Judgement("금융주", "Yes", ev, [url])
        if not code:
            return Judgement("금융주", "확인 불가", ["업종코드 미확인"], [url])
        return Judgement("금융주", "No", [f"{code_src}(금융·보험업 아님)"], [url])

    @staticmethod
    def _china(overview: str, business: str, inv: list[dict] | None, has_doc: bool, url: str) -> Judgement:
        ev, criteria = [], set()
        # 1) 연결 종속회사 소재지(회사의 개요 표)
        for line in overview.split("\n"):
            if re.search(CHINA_NAME, line) and re.search(r"\|", line):
                ev.append(f"[현지법인] 회사의 개요 종속회사 표: {line.strip()[:160]}")
                criteria.add("현지법인")
                if len([e for e in ev if e.startswith("[현지법인]")]) >= 3:
                    break
        # 2) 타법인 출자 현황(지분율 20% 이상 중국 법인)
        for it in inv or []:
            nm = it.get("inv_prm") or ""
            rt = it.get("trmend_blce_qota_rt") or ""
            try:
                rate = float(str(rt).replace(",", "").replace("%", ""))
            except ValueError:
                rate = None
            if re.search(CHINA_NAME, nm) and (rate is None or rate >= 20):
                ev.append(f"[지분관계] 타법인 출자현황: {nm} 지분율 {rt}%")
                criteria.add("지분관계")
        # 3) 사업의 내용 문장
        for snt in _sentences(business):
            if "중국" not in snt and not re.search(r"China|CHINA", snt):
                continue
            s = snt[:200]
            if re.search(r"공장|생산법인|생산기지|생산시설|생산\s*거점|제조\s*법인", snt):
                ev.append(f"[생산시설] {s}"); criteria.add("생산시설")
            m = re.search(r"중국[^.\n%]{0,40}?(\d{1,3}(?:\.\d+)?)\s*%", snt)
            if m and float(m.group(1)) >= 10:
                ev.append(f"[매출비중] {s}"); criteria.add("매출비중")
            if re.search(r"(주요|핵심)\s*(고객|거래처|매출처)", snt):
                ev.append(f"[주요고객] {s}"); criteria.add("주요고객")
            if len(ev) >= 10:
                break
        if criteria:
            return Judgement("중국 관련주", "Yes", [f"충족 기준: {', '.join(sorted(criteria))}"] + ev[:8], [url])
        if not has_doc:
            return Judgement("중국 관련주", "확인 불가", ["사업보고서 원문 미확인"], [url])
        export_only = re.search(r"중국[^.\n]{0,30}수출|수출[^.\n]{0,30}중국", business)
        hk = re.search(HK_NAME, overview)
        notes = ["사업보고서 원문에서 중국 현지법인·생산시설·매출비중(10% 이상)·주요 고객 기재 미확인"]
        if export_only:
            notes.append("중국 수출 언급은 있으나 수출만으로는 중국 관련주로 분류하지 않음")
        if hk:
            notes.append("홍콩 소재 법인 기재 있음(중국 본토 법인과 구분, 원문 확인 필요)")
        return Judgement("중국 관련주", "No", notes, [url])
