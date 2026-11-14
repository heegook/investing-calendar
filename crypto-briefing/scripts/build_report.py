#!/usr/bin/env python3
"""보고서 빌드: 업비트 원자료(JSON) + 본문(src/DATE.body.html) → 단일 HTML

사용법: python3 scripts/build_report.py 2026-11-14
산출물
  · index.html               최신 보고서 (엣지 즐겨찾기 대상, 외부 리소스 없음)
  · reports/DATE.html        날짜별 보관본
  · reports/index.html       보관 목록
  · dist/artifact.html       Claude 아티팩트 게시용 (doctype/head 제외본)
"""
import csv, datetime as dt, glob, html, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from indicators import compute

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KST = dt.timezone(dt.timedelta(hours=9))
COINS = [("BTC", "KRW-BTC", "비트코인", "btc"), ("ETH", "KRW-ETH", "이더리움", "eth"),
         ("XRP", "KRW-XRP", "리플", "xrp"), ("DOGE", "KRW-DOGE", "도지코인", "doge")]
WD = "월화수목금토일"


def fmt(v):
    if v >= 1000: return f"{v:,.0f}"
    if v >= 100: return f"{v:,.1f}".rstrip("0").rstrip(".") if v % 1 else f"{v:,.0f}"
    return f"{v:,.1f}"


def pct(v):
    return f"{'+' if v > 0 else '−' if v < 0 else '±'}{abs(v):.2f}%"


def cls(v): return "up" if v > 0 else "down" if v < 0 else ""
def arrow(v): return "▲" if v > 0 else "▼" if v < 0 else "■"


def load(basis):
    raw = os.path.join(ROOT, "data", "raw", basis.isoformat())
    out = {}
    for m in [c[1] for c in COINS] + ["KRW-USDT"]:
        out[m] = {c["candle_date_time_kst"][:10]: c for c in json.load(open(os.path.join(raw, f"day_{m}.json")))}
    return out


def build(basis):
    d = load(basis)
    keys = [(basis - dt.timedelta(days=8 - i)).isoformat() for i in range(8)]  # 8개 일봉: D-8 .. D-1
    pts = [(basis - dt.timedelta(days=7 - i)) for i in range(8)]                # 각 일봉이 마감되는 날짜(09:00)
    stats = {}
    for sym, m, *_ in COINS:
        closes = [d[m][k]["trade_price"] for k in keys]
        hi = max(d[m][k]["high_price"] for k in keys[1:]); lo = min(d[m][k]["low_price"] for k in keys[1:])
        stats[sym] = dict(closes=closes, base=closes[0], cur=closes[-1], wk=(closes[-1] / closes[0] - 1) * 100,
                          d1=(closes[-1] / closes[-2] - 1) * 100, hi=hi, lo=lo)

    # 카드
    cards = []
    for sym, m, name, k in COINS:
        s = stats[sym]
        cards.append(f'''<article class="card" data-coin="{sym}">
  <div class="card-top"><span class="sym"><i class="f-{k}" style="background:var(--c-{k})"></i>{sym}</span><span class="mk">{name} · {m}</span></div>
  <div><div class="vs">현재 가격 ({basis.month}/{basis.day} 09:00)</div><div class="price num">{fmt(s["cur"])}원</div></div>
  <span class="chg {cls(s["wk"])} num">{arrow(s["wk"])} {pct(s["wk"])} <small>주간 {"상승" if s["wk"]>0 else "하락"}</small></span>
  <div class="row num"><span>7일 전 가격</span><span>{fmt(s["base"])}원</span></div>
  <div class="row num"><span>기간 고가 / 저가</span><span>{fmt(s["hi"])} / {fmt(s["lo"])}</span></div>
  <div class="row num"><span>최근 1일 (참고)</span><span class="{cls(s["d1"])}" style="color:var(--{cls(s["d1"]) or "ink"})">{pct(s["d1"])}</span></div>
</article>''')

    # 막대그래프 (가로)
    W, H, L, R, T, B = 460, 200, 52, 70, 10, 30
    vals = [stats[c[0]]["wk"] for c in COINS]
    lo_v = min(-4, (int(min(vals) / 4) - 1) * 4); hi_v = max(0, (int(max(vals) / 4) + (1 if max(vals) > 0 else 0)) * 4)
    sx = lambda v: L + (v - lo_v) / (hi_v - lo_v) * (W - L - R)
    rowh = (H - T - B) / len(COINS)
    g = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="4종 주간 수익률 막대그래프"><rect class="bg-rect" x="0" y="0" width="{W}" height="{H}"/>']
    for t in range(lo_v, hi_v + 1, 4):
        x = sx(t); g.append(f'<line class="{"ax0" if t==0 else "ax"}" x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{H-B+4}"/>')
        g.append(f'<text class="tick" x="{x:.1f}" y="{H-B+18}" text-anchor="middle">{"+" if t>0 else "−" if t<0 else ""}{abs(t)}%</text>')
    for i, (sym, *_r) in enumerate(COINS):
        v = stats[sym]["wk"]; y = T + i * rowh + rowh * 0.2; h = rowh * 0.6
        x0, x1 = sorted([sx(0), sx(v)])
        g.append(f'<g class="cc c-{sym}"><text class="lab" x="{L-10}" y="{y+h/2+4:.1f}" text-anchor="end">{sym}</text>')
        g.append(f'<rect class="bar-{cls(v) or "down"}" x="{x0:.1f}" y="{y:.1f}" width="{max(x1-x0,1):.1f}" height="{h:.1f}" rx="2"/>')
        tx = max(sx(0), x1) + 6  # 값 라벨은 0축 오른쪽(또는 양수 막대 끝)에 배치해 코인명과 겹치지 않게 함
        g.append(f'<text class="val {cls(v)}" x="{tx:.1f}" y="{y+h/2+4:.1f}" text-anchor="start">{pct(v)}</text></g>')
    g.append("</svg>")
    bar = "".join(g)

    # 정규화 추이
    W, H, L, R, T, B = 560, 280, 40, 96, 14, 30
    idx = {c[0]: [p / stats[c[0]]["base"] * 100 for p in stats[c[0]]["closes"]] for c in COINS}
    allv = [v for a in idx.values() for v in a]
    ymin = int((min(allv) - 1.5) // 5) * 5; ymax = int(max(allv) // 5 + 1) * 5
    sx = lambda i: L + i / 7 * (W - L - R); sy = lambda v: T + (ymax - v) / (ymax - ymin) * (H - T - B)
    g = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="7일 정규화 가격 추이"><rect class="bg-rect" x="0" y="0" width="{W}" height="{H}"/>']
    for v in range(ymin, ymax + 1, 5):
        g.append(f'<line class="{"ax0" if v==100 else "ax"}" x1="{L}" y1="{sy(v):.1f}" x2="{W-R}" y2="{sy(v):.1f}"/><text class="tick" x="{L-6}" y="{sy(v)+4:.1f}" text-anchor="end">{v}</text>')
    for i, p in enumerate(pts):
        g.append(f'<text class="tick" x="{sx(i):.1f}" y="{H-B+18}" text-anchor="middle">{p.month}/{p.day}</text>')
    ends = sorted([(idx[c[0]][-1], c) for c in COINS], reverse=True)
    ys = [sy(v) for v, c in ends]
    for i in range(1, len(ys)): ys[i] = max(ys[i], ys[i-1] + 15)          # 위→아래로 간격 확보
    ys[-1] = min(ys[-1], H - B - 4)
    for i in range(len(ys) - 2, -1, -1): ys[i] = min(ys[i], ys[i+1] - 15)  # 아래 경계에서 위로 밀어 올림
    lab_y = {c[0]: ys[i] for i, (v, c) in enumerate(ends)}
    for sym, m, name, k in COINS:
        path = " ".join(f'{"M" if i==0 else "L"}{sx(i):.1f},{sy(v):.1f}' for i, v in enumerate(idx[sym]))
        g.append(f'<g class="cc c-{sym}"><path class="line s-{k}" d="{path}"/>')
        g.append(f'<circle class="dot f-{k}" cx="{sx(7):.1f}" cy="{sy(idx[sym][-1]):.1f}" r="3.6"/>')
        g.append(f'<text class="lab f-{k}" x="{sx(7)+8:.1f}" y="{lab_y[sym]+4:.1f}">{sym} {idx[sym][-1]:.1f}</text></g>')
    g.append("</svg>")
    line = "".join(g)

    # 일별 표
    rows = []
    for i, k in enumerate(keys):
        p = pts[i]; cells = []
        for m in [c[1] for c in COINS] + ["KRW-USDT"]:
            v = d[m][k]["trade_price"]; cc = f' cc c-{m[4:]}' if m != "KRW-USDT" else ""
            if i == 0: cells.append(f'<td class="r{cc}">{fmt(v)}</td>')
            else:
                ch = (v / d[m][keys[i-1]]["trade_price"] - 1) * 100
                cells.append(f'<td class="r{cc}">{fmt(v)} <span class="{cls(ch)}" style="font-weight:600;font-size:12px">({pct(ch)})</span></td>')
        tag = " · 7일 전" if i == 0 else " · 현재" if i == 7 else ""
        rows.append(f'<tr><td>{p.month}/{p.day}({WD[p.weekday()]}){tag}</td>{"".join(cells)}</tr>')
    daily = "".join(rows)

    # 누적 기록
    hist = {}
    path = os.path.join(ROOT, "data", "snapshots.csv")
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            hist.setdefault(r["basis_kst"], {})[r["market"]] = r
    hrows = []
    for b in sorted(hist, reverse=True):
        cells = []
        for sym, m, *_ in COINS:
            r = hist[b].get(m)
            if not r: cells.append(f'<td class="r cc c-{sym}"><span class="na">미보고</span></td>'); continue
            v = float(r["weekly_return_pct"])
            cells.append(f'<td class="r cc c-{sym}"><span class="{cls(v)}">{arrow(v)} {pct(v)}</span><br><span class="small">{fmt(float(r["price_now"]))}원</span></td>')
        fn = b[:10] + ".html"
        link = f'<a href="reports/{fn}">{fn}</a>' if os.path.exists(os.path.join(ROOT, "reports", fn)) or b[:10] == basis.isoformat() else '<span class="na">없음</span>'
        hrows.append(f'<tr><td>{b}</td>{"".join(cells)}<td>{link}</td></tr>')
    history = "".join(hrows) or '<tr><td colspan="6">기록 없음</td></tr>'

    # 기간별 기술적 지표 표 (원자료가 있을 때만)
    raw = os.path.join(ROOT, "data", "raw", basis.isoformat())
    tech = {"{{TECH_SHORT}}": "", "{{TECH_MID}}": "", "{{TECH_LONG}}": ""}
    if os.path.exists(os.path.join(raw, "day200_KRW-BTC.json")):
        T = {c[0]: compute(raw, c[1]) for c in COINS}
        def gap(r, k): return (r["last"] / r[k] - 1) * 100
        def cell_gap(r, k):
            v = gap(r, k); word = "상회" if v > 0 else "하회"
            return f'<td class="r"><span class="{cls(v)}">{("+" if v > 0 else "−")}{abs(v):.1f}%</span><br><span class="small">{word} · {fmt(r[k])}</span></td>'
        def cell_rsi(v):
            lab = "과매도(30↓)" if v < 30 else "과매수(70↑)" if v > 70 else "약세권(30~50)" if v < 50 else "강세권(50~70)"
            return f'<td class="r"><b>{v:.1f}</b><br><span class="small">{lab}</span></td>'
        def cell(v, sub=""): return f'<td class="r"><b>{v}</b>{"<br><span class=small>" + sub + "</span>" if sub else ""}</td>'
        def table(rows):
            head = "".join(f'<th class="r cc c-{c[0]}">{c[0]}</th>' for c in COINS)
            body = "".join(f'<tr><td>{name}<br><span class="small">{basis_}</span></td>{"".join(fn(T[c[0]]).replace(chr(60) + "td class=" + chr(34) + "r", chr(60) + "td class=" + chr(34) + "r cc c-" + c[0], 1) for c in COINS)}</tr>' for name, basis_, fn in rows)
            return f'<div class="tbl-wrap"><table class="num"><thead><tr><th>지표</th>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        tech["{{TECH_SHORT}}"] = table([
            ("1시간봉 RSI(14)", "11/14 08:00~09:00 봉까지", lambda r: cell_rsi(r["h1_rsi"])),
            ("4시간봉 RSI(14)", "11/14 05:00~09:00 봉까지", lambda r: cell_rsi(r["h4_rsi"])),
            ("1시간봉 60이평 대비", "약 2.5일 평균", lambda r: cell_gap(r, "h1_ma60")),
            ("최근 24시간 고가 / 저가", "11/13 09:00~11/14 09:00", lambda r: cell(f'{fmt(r["h24_hi"])} / {fmt(r["h24_lo"])}')),
            ("일봉 ATR(14) ÷ 종가", "하루 평균 진폭", lambda r: cell(f'{r["atr_d_pct"]:.1f}%', "목표가 아님, 변동성 참고")),
        ])
        tech["{{TECH_MID}}"] = table([
            ("일봉 RSI(14)", "11/13 일봉까지", lambda r: cell_rsi(r["d_rsi"])),
            ("볼린저밴드 %B (20, 2σ)", "0 미만 = 하단 밴드 아래", lambda r: cell(f'{r["pctb"]:+.2f}'.replace("-", "−"), "하단 이탈" if r["pctb"] < 0 else "하단 부근" if r["pctb"] < 0.2 else "밴드 안")),
            ("MACD 히스토그램 (12,26,9)", "전일 대비", lambda r: cell("음(−)" if r["macd_h"] < 0 else "양(+)", "음(−) 확대" if r["macd_h"] < r["macd_h_prev"] else "음(−) 축소" if r["macd_h"] < 0 else "양(+)")),
            ("5일 이평 대비", "단기 추세", lambda r: cell_gap(r, "ma5")),
            ("20일 이평 대비", "약 1개월 추세", lambda r: cell_gap(r, "ma20")),
            ("20일 고가 / 저가", "최근 20개 일봉", lambda r: cell(f'{fmt(r["hi20"])} / {fmt(r["lo20"])}')),
            ("11/13 거래대금 ÷ 직전 20일 평균", "업비트 KRW 마켓", lambda r: cell(f'{r["vol_ratio"]:.2f}배', "급증" if r["vol_ratio"] >= 2 else "증가" if r["vol_ratio"] >= 1.2 else "평균 수준")),
            ("연속 하락 마감 일수", "일봉 종가 기준", lambda r: cell(f'{r["down_streak"]}일')),
        ])
        tech["{{TECH_LONG}}"] = table([
            ("200주 이평 대비", "약 4년 평균", lambda r: cell_gap(r, "w_ma200")),
            ("20주 / 50주 이평 배열", "중장기 추세", lambda r: cell("20주 < 50주" if r["w_ma20"] < r["w_ma50"] else "20주 > 50주", "역배열(약세)" if r["w_ma20"] < r["w_ma50"] else "정배열")),
            ("50주 이평 대비", "약 1년 평균", lambda r: cell_gap(r, "w_ma50")),
            ("주봉 RSI(14)", "11/9 주 진행 중 봉 포함", lambda r: cell_rsi(r["w_rsi"])),
            ("52주 고점 대비", "주봉 고가 기준", lambda r: cell(pct((r["last"] / r["hi52"] - 1) * 100), f'고점 {fmt(r["hi52"])}')),
            ("52주 저가 대비", "주봉 저가 기준", lambda r: cell(pct((r["last"] / r["lo52"] - 1) * 100), f'저가 {fmt(r["lo52"])}')),
        ])

    now = dt.datetime.now(KST)
    gen = f"{now:%Y-%m-%d}({WD[now.weekday()]}) {now:%H:%M} KST"
    # 작성 시각은 본문 내용을 마지막으로 고친 시각으로 고정함 (화면 수정만으로 바뀌지 않게)
    gt = os.path.join(ROOT, "reports", "src", f"{basis.isoformat()}.gentime")
    if os.path.exists(gt): gen = open(gt, encoding="utf-8").read().strip()
    else: open(gt, "w", encoding="utf-8").write(gen + "\n")
    body = open(os.path.join(ROOT, "reports", "src", f"{basis.isoformat()}.body.html"), encoding="utf-8").read()
    for k, v in {"{{CARDS}}": "".join(cards), "{{BARCHART}}": bar, "{{LINECHART}}": line, "{{DAILYTABLE}}": daily,
                 "{{HISTORY}}": history, "{{GEN_TIME}}": gen, **tech}.items():
        body = body.replace(k, v)
    tpl = open(os.path.join(ROOT, "scripts", "template.html"), encoding="utf-8").read()
    fragment = tpl.replace("{{BODY}}", body)
    assert "{{" not in fragment, "치환되지 않은 자리표시자 있음"

    head_bits = ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')
    title_end = fragment.index("</style>") + len("</style>")
    full = head_bits + fragment[:title_end] + "\n</head>\n<body>\n" + fragment[title_end:] + "\n</body>\n</html>\n"

    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    open(os.path.join(ROOT, "dist", "artifact.html"), "w", encoding="utf-8").write(fragment)
    open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8").write(full)
    # 보관본은 reports/ 하위에 있으므로 상대 링크 보정
    open(os.path.join(ROOT, "reports", f"{basis.isoformat()}.html"), "w", encoding="utf-8").write(full.replace('href="reports/', 'href="'))

    # 보관 목록
    items = sorted([os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "reports", "????-??-??.html"))], reverse=True)
    lis = "".join(f'<li><a href="{f}">{f[:-5]} 브리핑</a></li>' for f in items)
    open(os.path.join(ROOT, "reports", "index.html"), "w", encoding="utf-8").write(
        f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>업비트 브리핑 보관함</title><style>body{{font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#121a2b;background:#fff;line-height:1.8}}
h1{{font-size:24px;border-bottom:2px solid #13294b;padding-bottom:8px}}a{{color:#1f4f9a}}</style></head>
<body><h1>업비트 코인 주간 브리핑 보관함</h1><p><a href="../index.html">← 최신 보고서</a></p><ul>{lis}</ul></body></html>''')
    for sym, *_ in COINS:
        s = stats[sym]; print(f'{sym:5} {fmt(s["base"]):>13} → {fmt(s["cur"]):>13}  주간 {pct(s["wk"])}  1일 {pct(s["d1"])}')
    print("완료:", gen)


if __name__ == "__main__":
    build(dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.datetime.now(KST).date())
