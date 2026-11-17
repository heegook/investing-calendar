# 업비트 코인 주간 브리핑

BTC·ETH·XRP·DOGE 업비트 KRW 마켓 기준 주간 브리핑을 매주 수·금 07:00(KST) 열람용으로 누적 관리하는 폴더임

## 바로 열기
- 최신 보고서(아티팩트, 같은 주소로 계속 갱신): https://claude.ai/artifact/C4tWEvf7WxhB3xL4R7fzHk
- 오프라인 단일 파일: `index.html` (외부 라이브러리·폰트 없음, 더블클릭으로 엣지에서 열림)
- 날짜별 보관본: `reports/YYYY-MM-DD.html` · 보고서 안 **보관함·달력** 섹션(툴바 버튼)에서 날짜를 눌러 지난 회차를 열 수 있음
- 달력 일정: `data/events.json` (한국시간 기준, 회차 발행 때 갱신)

## 엣지 즐겨찾기 등록
1. 가장 간단한 방법: 위 아티팩트 주소를 엣지에서 열고 `Ctrl+D` → 즐겨찾기 모음 선택
2. 폴더째 등록: 엣지 `설정 → 즐겨찾기 → 즐겨찾기 가져오기 → 즐겨찾기 또는 책갈피 HTML 파일` 에서 `edge-favorites.html` 선택
3. 로컬 파일로 보려면 저장소를 내려받아 `index.html`을 엣지로 연 뒤 `Ctrl+D`

## 누적 관리 방식
| 단계 | 내용 |
|---|---|
| 1. 가격 수집 | `python3 scripts/fetch_upbit.py YYYY-MM-DD` → `data/raw/날짜/` 원자료 저장, `data/snapshots.csv`에 주간 등락률 한 줄씩 누적 |
| 2. 본문 작성 | `reports/src/YYYY-MM-DD.body.html` (뉴스·해석, 사실/언론/해석 구분) |
| 3. 빌드 | `python3 scripts/build_report.py` → 전 회차를 다시 만들어 보관함·달력을 맞춤: `index.html`(최신), `reports/날짜.html`(보관), `dist/artifact.html`(게시용) |
| 4. 게시 | `dist/artifact.html`을 같은 아티팩트 주소로 재게시하면서 `reports/*.html`을 함께 올림 → 즐겨찾기 주소 그대로 최신본과 지난 회차 열람 |

※ 업비트 일봉은 09:00 KST 마감이므로 07:00 열람용 보고서는 전일 09:00 마감 일봉을 ‘현재 가격’으로 사용함 (예: 11/18(수) 07:00 보고서 → 기준일 인자 `2026-11-17`)
※ 수집 실패 코인은 0으로 채우지 않고 기록에서 제외하며 보고서에는 ‘미보고’로 표시함
