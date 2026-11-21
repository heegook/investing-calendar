# Korea Value Stock Analyzer

KOSPI·KOSDAQ 상장기업의 연결재무제표와 KRX 시세를 기반으로 저평가 종목을 발굴하는 분석 시스템임
실제 확인된 데이터만 사용하며, 확인되지 않은 값은 추정하지 않고 null(확인 불가)로 처리함

## 개발 진행 현황

| STEP | 내용 | 상태 |
|---|---|---|
| 1 | 프로젝트 폴더·가상환경 구성 | 완료 |
| 2 | DART API·KRX 데이터 수집 | 완료 (DART는 API 키 등록 후 실데이터 검증 필요) |
| 3 | 연결재무제표 수집 및 정규화 | 완료 (단위테스트 검증, 실데이터 검증은 키 등록 후) |
| 4 | 10개 재무지표 계산 엔진 | 완료 |
| 5~12 | 적정주가, 스크리닝, 리스크, 대시보드, 보고서, 자동실행 | 예정 |

## 1. 설치

```bash
cd korea_value_stock_analyzer
python3 -m venv .venv          # Python 3.12 이상
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Windows 에서는 한글 출력 오류 방지를 위해 실행 전 PowerShell 에서 `$env:PYTHONUTF8=1` 을 설정함

## 2. API 키 설정

```bash
cp .env.example .env
```

`.env` 파일에 다음 값을 입력함 (`.env` 는 git 에 커밋되지 않음)

| 변수 | 필수 | 용도 |
|---|---|---|
| `DART_API_KEY` | 필수 | 재무제표·주식총수·정기보고서 목록. https://opendart.fss.or.kr 에서 무료 발급 |
| `KRX_ID`, `KRX_PW` | 선택 | KRX 정보데이터시스템 회원 계정. 설정 시 pykrx 로 KRX 공식 시세 수집(신뢰도 HIGH) |
| `NAVER_CLIENT_ID/SECRET` | 선택 | STEP 7 뉴스 검색용 |

API 키가 없으면
 - 주가·상장주식수·시가총액·종목분류는 실제 공개 데이터로 정상 작동함
 - 재무제표 기반 지표(PSR, PER, PBR, 이익률, 부채비율, 유보율, 매출추이)는 `FAILED / NO_API_KEY` 로 표시되며 숫자를 채우지 않음

## 3. 실행

```bash
# 일일 분석 즉시 실행 (현재: 주가 → 재무제표 → 10개 지표)
python -m scheduler.daily_scheduler --run-now

# 검증용: 특정 종목만 / 대상 수 제한 / 주가 기준일 지정(재현)
python -m scheduler.daily_scheduler --run-now --codes 005930,000660
python -m scheduler.daily_scheduler --run-now --limit 20
python -m scheduler.daily_scheduler --run-now --price-date 2026-11-20

# 실데이터 검증 리포트(사용 계정·접수번호·검증 결과) → logs/verify_<run_id>.txt
python -m scheduler.daily_scheduler --run-now --codes 005930,009150,267260 --dump

# 테스트
python -m pytest -q
```

대시보드(`streamlit run app.py`)와 07:30 자동 실행은 STEP 9·11 에서 추가 예정임

## 4. 데이터 출처와 기준

| 데이터 | 출처(우선순위) | 신뢰도 / 상태 |
|---|---|---|
| 연결재무제표, 자기주식수 | DART OpenAPI `fnlttSinglAcntAll(CFS)`, `stockTotqySttus`, `list.json` | HIGH, 검증 통과 시 VERIFIED |
| 전일 종가·상장주식수 | ① KRX 공식(pykrx, 로그인) ② KRX 전종목 시세 스냅샷(FinanceDataReader 캐시) | ① VERIFIED ② UNVERIFIED |
| 업종·주요제품·결산월 | KRX 상장법인 상세 목록(FinanceDataReader 캐시) | MEDIUM |

 - KRX 정보데이터시스템은 2025년부터 비로그인 조회를 차단하여 계정 미설정 시 ② 경로를 사용하며, 파일명이 거래일이므로 기준일이 명시적임
 - 네이버 증권 시세는 KRX+NXT(넥스트레이드) 통합 시세라 KRX 공식 종가와 다를 수 있으므로 종가 원천으로 사용하지 않음 (2026-11-20 삼성전자: KRX 183,100원 / 네이버 통합 180,900원 확인)
 - 주가 기준일은 실행일 직전 거래일이며 주말·공휴일·근로자의 날·연말 휴장일은 KRX 휴장일 달력으로 처리함
 - 요청 기준일의 시세가 없으면 과거 데이터를 최신으로 쓰지 않고 실행을 실패 처리함

## 5. 지표 계산 기준 (STEP 4)

| 지표 | 공식 | 통과 기준 |
|---|---|---|
| PSR | 시가총액 / 직전 사업연도 매출액 | < 1.0 |
| PER | 전일 종가 / TTM EPS | < 15 (EPS ≤ 0 이면 N/M, 미통과) |
| PBR | 전일 종가 / BPS, BPS = 지배기업 소유주지분 / 기말 유통보통주식수 | < 1.5 |
| 영업이익률 | 직전 사업연도 영업이익 / 매출액 × 100 | ≥ 5% |
| 순이익률 | 직전 사업연도 지배기업 소유주 귀속 순이익 / 매출액 × 100 | ≥ 5% |
| 부채비율 | 최근 분기 부채총계 / 자본총계 × 100 | ≤ 200% |
| 유보율 | (자본잉여금 + 이익잉여금) / 자본금 × 100 | ≥ 2,000% |
| 상장주식수 | KRX 상장 보통주식수 | 1,000만~8,000만 주 |
| 시가총액 | 전일 종가 × 상장주식수 | ≥ 500억원 |
| 매출 추이 | 최근 3개 사업연도 연결 매출액 | 지속 감소가 아닐 것 |

※ TTM EPS = 최근 연간 EPS + 당기 누적 EPS − 전년 동기 누적 EPS, 교차검증으로 TTM 지배순이익 / 유통주식수 를 함께 계산함
※ 유보율은 연결재무상태표의 자본금·자본잉여금·이익잉여금(지배기업 소유주지분 구성항목) 기준임
※ 투자자산 = 비유동 금융자산 + 관계기업·공동기업 투자 + 투자부동산 (비유동자산 구간 내 계정만, 소계 중복 제외, 파생상품·대여금·보증금 제외)

## 6. 데이터 상태

| 상태 | 의미 |
|---|---|
| VERIFIED | 원천자료 확인 및 검증 완료 |
| PARTIAL | 일부 자료만 확인 또는 검증 일부 불일치 |
| UNVERIFIED | 원천자료 검증 미완료, 또는 원천에 자료 없음(예: 연결재무제표 확인 불가) |
| FAILED | 데이터 수집 실패(API 키 미설정, 네트워크·API 오류) |

값이 없을 때는 사유 코드(`NO_API_KEY`, `COLLECTION_FAILED`, `CFS_NOT_AVAILABLE`, `STATEMENT_NOT_FOUND`, `ACCOUNT_NOT_FOUND`, `NOT_MEANINGFUL` 등)를 함께 저장하여 수집 실패와 재무제표 미확인을 구분함

## 7. 구현된 검증 항목

1. 연결(CFS)/별도(OFS) 구분: CFS 만 저장, CFS 부재 시 OFS 존재 여부만 확인하여 '연결재무제표 확인 불가' 표시
2. 회계연도·분기 구분: 정기보고서 명칭의 보고기간과 결산월로 보고서 코드 판정, 응답 접수번호 대조
3. 누적/분기 구분: 3개월 값과 누적 값을 별도 열로 관리
4. TTM 검증: 공식 결과와 최근 4개 분기 합계 비교
5. 주식수·자기주식수 구분: 발행 − 자기 = 유통 검증
6. 액면분할·무상증자: DART 결산일 발행주식수 대비 KRX 현재 상장주식수 비율로 감지·조정
7. 종가 기준일 검증
8. 단위 검증(원 단위, 원화 통화)
9. 자산총계 = 부채총계 + 자본총계, 유동/비유동 합계 검증
10. 적정주가 공식 검증 - STEP 5 예정

## 8. 폴더 구조

```
config/        settings.yaml(임계값·제외조건·출처), scoring_weights.yaml
core/          공통 모델(Metric, DataStatus), 설정, KRX 거래일 달력
collectors/    krx_collector, price_collector, dart_collector, news_collector(예정)
analyzers/     financial_analyzer(정규화·검증), financial_pipeline, ratio_calculator
database/      db_manager (SQLite 스키마)
scheduler/     daily_scheduler (--run-now)
tests/         단위테스트 (tests/fixtures 는 테스트 전용 가상 데이터)
data/          raw(원천 응답 보관), processed, database
logs/          실행 로그
```
