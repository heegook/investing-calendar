#!/usr/bin/env python3
"""업비트 KRW 마켓 일봉을 받아 주간 스냅샷을 누적 저장함

사용법
  python3 scripts/fetch_upbit.py            # 실행일(한국시간) 09:00 기준
  python3 scripts/fetch_upbit.py 2026-11-18 # 특정 기준일 09:00 기준

산출 기준
  · 업비트 일봉은 매일 09:00 KST에 시작·마감하므로, 기준일 D의 '현재 가격'은
    D-1일 일봉의 종가(= D일 09:00 KST 직전 마지막 체결가)임
  · '7일 전 가격'은 D-8일 일봉의 종가(= D-7일 09:00 KST 직전 마지막 체결가)임
  · 주간 등락률 = (현재 가격 / 7일 전 가격 - 1) × 100
"""
import csv, json, os, sys, time, datetime as dt, urllib.request

MARKETS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-DOGE", "KRW-USDT"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KST = dt.timezone(dt.timedelta(hours=9))


def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    basis = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.datetime.now(KST).date()
    raw_dir = os.path.join(ROOT, "data", "raw", basis.isoformat())
    os.makedirs(raw_dir, exist_ok=True)
    to_utc = dt.datetime(basis.year, basis.month, basis.day, 0, 0, tzinfo=dt.timezone.utc)  # = 09:00 KST
    rows = []
    for m in MARKETS:
        url = f"https://api.upbit.com/v1/candles/days?market={m}&count=10&to={to_utc:%Y-%m-%dT%H:%M:%SZ}"
        data = get(url)
        json.dump(data, open(os.path.join(raw_dir, f"day_{m}.json"), "w"), ensure_ascii=False, indent=1)
        by = {c["candle_date_time_kst"][:10]: c for c in data}
        cur_key = (basis - dt.timedelta(days=1)).isoformat()
        base_key = (basis - dt.timedelta(days=8)).isoformat()
        if cur_key not in by or base_key not in by:
            print(f"[경고] {m}: 기준 일봉 누락 → 기록하지 않음 (0으로 채우지 않음)")
            continue
        cur, base = by[cur_key]["trade_price"], by[base_key]["trade_price"]
        rows.append({
            "basis_kst": f"{basis.isoformat()} 09:00",
            "market": m,
            "price_now": cur,
            "price_7d_ago": base,
            "weekly_return_pct": round((cur / base - 1) * 100, 2),
            "fetched_at_kst": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        })
        time.sleep(0.25)
    ticker = get("https://api.upbit.com/v1/ticker?markets=" + ",".join(MARKETS))
    json.dump(ticker, open(os.path.join(raw_dir, "ticker.json"), "w"), ensure_ascii=False, indent=1)

    path = os.path.join(ROOT, "data", "snapshots.csv")
    old = []
    if os.path.exists(path):
        old = [r for r in csv.DictReader(open(path, encoding="utf-8")) if r["basis_kst"] != rows[0]["basis_kst"]] if rows else []
    allrows = sorted(old + rows, key=lambda r: (r["basis_kst"], MARKETS.index(r["market"])))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(allrows)
    for r in rows:
        print(f'{r["market"]:9} 현재 {r["price_now"]:>14,.1f}  7일 전 {r["price_7d_ago"]:>14,.1f}  주간 {r["weekly_return_pct"]:+.2f}%')
    print("저장:", raw_dir, "/", path)


if __name__ == "__main__":
    main()
