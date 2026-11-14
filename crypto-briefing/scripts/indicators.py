#!/usr/bin/env python3
"""업비트 캔들로 기술적 지표를 계산함 (외부 라이브러리 없음)

  · 이동평균(MA): 단순 이동평균
  · RSI(14): 와일더(Wilder) 평활 방식
  · 볼린저밴드(20, 2σ): %B = (종가 − 하단) ÷ (상단 − 하단)
  · ATR(14): 와일더 평활, 종가 대비 % 로 환산
  · MACD(12, 26, 9): 지수이동평균 차이와 시그널선의 차이(히스토그램)
"""
import json, os, statistics as st

def sma(a, n): return sum(a[-n:]) / n if len(a) >= n else None

def ema_series(a, n):
    k = 2 / (n + 1); out = [a[0]]
    for x in a[1:]: out.append(x * k + out[-1] * (1 - k))
    return out

def rsi(a, n=14):
    if len(a) <= n: return None
    g = [max(a[i] - a[i-1], 0) for i in range(1, len(a))]; l = [max(a[i-1] - a[i], 0) for i in range(1, len(a))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)): ag = (ag * (n-1) + g[i]) / n; al = (al * (n-1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def atr(c, n=14):
    tr = [c[0]["high_price"] - c[0]["low_price"]]
    for i in range(1, len(c)):
        h, l, pc = c[i]["high_price"], c[i]["low_price"], c[i-1]["trade_price"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(tr[:n]) / n
    for x in tr[n:]: a = (a * (n-1) + x) / n
    return a

def boll_b(a, n=20, k=2):
    w = a[-n:]; m = sum(w) / n; sd = st.pstdev(w)
    return (a[-1] - (m - k*sd)) / (2*k*sd) if sd else None, m - k*sd, m + k*sd

def macd_hist(a):
    e12, e26 = ema_series(a, 12), ema_series(a, 26)
    macd = [x - y for x, y in zip(e12, e26)]; sig = ema_series(macd, 9)
    return macd[-1] - sig[-1], macd[-2] - sig[-2]

def load(raw, kind, m):
    d = json.load(open(os.path.join(raw, f"{kind}_{m}.json")))
    return sorted(d, key=lambda c: c["candle_date_time_kst"])  # 과거 → 최근

def compute(raw, m):
    D = load(raw, "day200", m); W = load(raw, "week", m); H1 = load(raw, "h1", m); H4 = load(raw, "h4", m)
    dc = [c["trade_price"] for c in D]; wc = [c["trade_price"] for c in W]
    h1c = [c["trade_price"] for c in H1]; h4c = [c["trade_price"] for c in H4]
    last = dc[-1]
    pb, bl, bu = boll_b(dc)
    mh, mh_prev = macd_hist(dc)
    vol = [c["candle_acc_trade_price"] for c in D]
    h24 = H1[-24:]
    downs = 0
    for i in range(len(dc) - 1, 0, -1):
        if dc[i] < dc[i-1]: downs += 1
        else: break
    wk52 = W[-52:]
    ytd_base = next((c["trade_price"] for c in D if c["candle_date_time_kst"][:10] == "2025-12-31"), None)
    return {
        "last": last,
        # 단기(1일): 1시간봉·4시간봉
        "h1_rsi": rsi(h1c), "h1_ma20": sma(h1c, 20), "h1_ma60": sma(h1c, 60),
        "h4_rsi": rsi(h4c), "h4_ma20": sma(h4c, 20),
        "h24_hi": max(c["high_price"] for c in h24), "h24_lo": min(c["low_price"] for c in h24),
        "atr_d_pct": atr(D) / last * 100,
        "h1_last": h1c[-1], "h1_time": H1[-1]["candle_date_time_kst"],
        # 중기(7일): 일봉
        "ma5": sma(dc, 5), "ma20": sma(dc, 20), "ma60": sma(dc, 60), "ma120": sma(dc, 120), "ma200d": sma(dc, 200),
        "d_rsi": rsi(dc), "pctb": pb, "bb_lo": bl, "bb_up": bu, "macd_h": mh, "macd_h_prev": mh_prev,
        "lo20": min(c["low_price"] for c in D[-20:]), "hi20": max(c["high_price"] for c in D[-20:]),
        "vol_ratio": vol[-1] / (sum(vol[-21:-1]) / 20), "vol7_ratio": (sum(vol[-7:]) / 7) / (sum(vol[-37:-7]) / 30),
        "down_streak": downs,
        # 장기(1개월+): 주봉 (마지막 주봉은 11/9 주 진행 중 캔들, 11/14 09:00까지)
        "w_ma20": sma(wc, 20), "w_ma50": sma(wc, 50), "w_ma200": sma(wc, 200), "w_n": len(wc), "w_rsi": rsi(wc),
        "hi52": max(c["high_price"] for c in wk52), "lo52": min(c["low_price"] for c in wk52),
        "ytd": (last / ytd_base - 1) * 100 if ytd_base else None,
    }

if __name__ == "__main__":
    import sys
    raw = sys.argv[1]
    for m in ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-DOGE"]:
        r = compute(raw, m)
        print(m, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
