"""분석 결과 공통 데이터 모델

모든 계산 결과는 Metric 으로 표현하며 원천 데이터, 기준일, 수집 일시, 적용 공식,
신뢰도, 검증 상태를 함께 보관함. 확인되지 않은 값은 숫자를 채우지 않고 None 으로 둠
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DataStatus(str, Enum):
    VERIFIED = "VERIFIED"      # 원천자료 확인 및 검증 완료
    PARTIAL = "PARTIAL"        # 일부 자료만 확인
    UNVERIFIED = "UNVERIFIED"  # 원천자료 검증 미완료 / 원천에서 자료 미확인
    FAILED = "FAILED"          # 데이터 수집 실패

    @staticmethod
    def worst(*statuses: "DataStatus") -> "DataStatus":
        order = [DataStatus.FAILED, DataStatus.UNVERIFIED, DataStatus.PARTIAL, DataStatus.VERIFIED]
        present = [s for s in statuses if s is not None]
        if not present:
            return DataStatus.UNVERIFIED
        return min(present, key=order.index)


class Reason(str, Enum):
    """값이 없는(None) 이유. 수집 실패와 재무제표 미확인을 구분하기 위함"""
    NO_API_KEY = "NO_API_KEY"                    # API 키 미설정 → 수집 불가
    COLLECTION_FAILED = "COLLECTION_FAILED"      # 네트워크/API 오류 등 수집 실패
    CFS_NOT_AVAILABLE = "CFS_NOT_AVAILABLE"      # 연결재무제표 확인 불가(별도만 존재 또는 미공시)
    STATEMENT_NOT_FOUND = "STATEMENT_NOT_FOUND"  # 해당 기간 재무제표 미확인
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"      # 재무제표는 있으나 해당 계정 미확인
    INPUT_MISSING = "INPUT_MISSING"              # 계산 입력값 중 일부가 없음
    NOT_MEANINGFUL = "NOT_MEANINGFUL"            # 적자 등으로 배수 의미 없음(N/M)
    VALIDATION_FAILED = "VALIDATION_FAILED"      # 검증 실패로 사용 보류
    STALE_DATA = "STALE_DATA"                    # 최신 데이터가 아님


class CheckResult(str, Enum):
    PASS = "통과"
    FAIL = "미통과"
    NA = "확인 불가"


@dataclass
class SourceRef:
    source: str                 # source_registry 키 (예: DART_OPENAPI)
    reference: str              # 원문 식별자(접수번호, URL, 파일명 등)
    as_of: str | None = None    # 원천 데이터 기준일(결산기준일/주가 기준일)
    collected_at: str | None = None
    detail: str | None = None   # 계정명, 보고서명 등


@dataclass
class Metric:
    name: str
    value: float | None
    unit: str
    formula: str
    status: DataStatus
    inputs: dict[str, Any] = field(default_factory=dict)
    sources: list[SourceRef] = field(default_factory=list)
    as_of: str | None = None
    reason: Reason | None = None
    check: CheckResult | None = None
    threshold: str | None = None
    notes: list[str] = field(default_factory=list)
    display: str | None = None   # 'N/M' 등 표시용 문자열

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["reason"] = self.reason.value if self.reason else None
        d["check"] = self.check.value if self.check else None
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


@dataclass
class Value:
    """원천 데이터 단일 값(계정 금액 등) + 추적정보"""
    value: float | None
    status: DataStatus
    source: SourceRef | None = None
    reason: Reason | None = None
    notes: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.value is not None
