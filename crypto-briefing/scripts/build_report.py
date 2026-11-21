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


def basis_hour(basis):
    """회차의 가격 기준 시각(시). data/raw/DATE/basis.json 이 있으면 그 시각(‘현시점’ 회차), 없으면 09시(일봉 마감)"""
    f = os.path.join(ROOT, "data", "raw", basis.isoformat(), "basis.json")
    return json.load(open(f))["hour"] if os.path.exists(f) else 9


def load_series(basis):
    """같은 시각 기준 8개 시점(7일 전 ~ 현재, 하루 간격)의 가격과 기간 고저가를 돌려줌
    · 09시 기준: 업비트 일봉 종가(09:00 마감) 사용
    · 그 외 시각: 해당 시각 직전 1시간봉 종가 사용 (예: 14:00 기준 = 13:00~14:00 봉 종가)"""
    raw = os.path.join(ROOT, "data", "raw", basis.isoformat()); hr = basis_hour(basis)
    end = dt.datetime(basis.year, basis.month, basis.day, hr)
    pts = [end - dt.timedelta(days=7 - i) for i in range(8)]
    closes, hilo = {}, {}
    for m in [c[1] for c in COINS] + ["KRW-USDT"]:
        if hr == 9:
            d = {c["candle_date_time_kst"][:10]: c for c in json.load(open(os.path.join(raw, f"day_{m}.json")))}
            keys = [(p - dt.timedelta(days=1)).date().isoformat() for p in pts]
            closes[m] = [d[k]["trade_price"] for k in keys]
            hilo[m] = (max(d[k]["high_price"] for k in keys[1:]), min(d[k]["low_price"] for k in keys[1:]))
        else:
            h = {c["candle_date_time_kst"][:16]: c for c in json.load(open(os.path.join(raw, f"h1basis_{m}.json")))}
            closes[m] = [h[(p - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")]["trade_price"] for p in pts]
            win = [c for k, c in h.items() if pts[0].strftime("%Y-%m-%dT%H:%M") <= k < end.strftime("%Y-%m-%dT%H:%M")]
            hilo[m] = (max(c["high_price"] for c in win), min(c["low_price"] for c in win))
    return pts, closes, hilo, hr


def build(basis, editions, latest):
    """editions: 발행된 회차 기준일 목록(오름차순), latest: 가장 최근 회차 기준일"""
    pts, closes_all, hilo, hr = load_series(basis)
    stats = {}
    for sym, m, *_ in COINS:
        closes = closes_all[m]; hi, lo = hilo[m]
        stats[sym] = dict(closes=closes, base=closes[0], cur=closes[-1], wk=(closes[-1] / closes[0] - 1) * 100,
                          d1=(closes[-1] / closes[-2] - 1) * 100, hi=hi, lo=lo)

    # 카드
    cards = []
    for sym, m, name, k in COINS:
        s = stats[sym]
        cards.append(f'''<article class="card" data-coin="{sym}">
  <div class="card-top"><span class="sym"><i class="f-{k}" style="background:var(--c-{k})"></i>{sym}</span><span class="mk">{name} · {m}</span></div>
  <div><div class="vs">현재 가격 ({basis.month}/{basis.day} {hr:02d}:00)</div><div class="price num">{fmt(s["cur"])}원</div></div>
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
    for i in range(8):
        p = pts[i]; cells = []
        for m in [c[1] for c in COINS] + ["KRW-USDT"]:
            v = closes_all[m][i]; cc = f' cc c-{m[4:]}' if m != "KRW-USDT" else ""
            if i == 0: cells.append(f'<td class="r{cc}">{fmt(v)}</td>')
            else:
                ch = (v / closes_all[m][i-1] - 1) * 100
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
    for b in sorted([b for b in hist if b[:10] <= basis.isoformat()], reverse=True):
        cells = []
        for sym, m, *_ in COINS:
            r = hist[b].get(m)
            if not r: cells.append(f'<td class="r cc c-{sym}"><span class="na">미보고</span></td>'); continue
            v = float(r["weekly_return_pct"])
            cells.append(f'<td class="r cc c-{sym}"><span class="{cls(v)}">{arrow(v)} {pct(v)}</span><br><span class="small">{fmt(float(r["price_now"]))}원</span></td>')
        fn = b[:10] + ".html"
        link = (('<b>현재 회차</b>' if b[:10] == basis.isoformat() else f'<a href="reports/{fn}">{b[5:7].lstrip("0")}/{b[8:10].lstrip("0")} 회차 열기</a>')
                if dt.date.fromisoformat(b[:10]) in editions else '<span class="na">없음</span>')
        hrows.append(f'<tr><td>{b}</td>{"".join(cells)}<td>{link}</td></tr>')
    history = "".join(hrows) or '<tr><td colspan="6">기록 없음</td></tr>'

    # 기간별 기술적 지표 표 (원자료가 있을 때만)
    raw = os.path.join(ROOT, "data", "raw", basis.isoformat())
    tech = {"{{TECH_SHORT}}": "", "{{TECH_MID}}": "", "{{TECH_LONG}}": ""}
    if os.path.exists(os.path.join(raw, "day200_KRW-BTC.json")):
        T = {c[0]: compute(raw, c[1]) for c in COINS}
        for c in COINS:  # 이평 대비 등은 회차 기준 가격으로 계산 (현시점 회차는 진행 중 일봉의 순간가 대신 기준 시각 가격)
            T[c[0]]["last"] = stats[c[0]]["cur"]
        def gap(r, k): return (r["last"] / r[k] - 1) * 100
        def cell_gap(r, k, base="last"):
            v = (r[base] / r[k] - 1) * 100; word = "상회" if v > 0 else "하회"
            return f'<td class="r"><span class="{cls(v)}">{("+" if v > 0 else "−")}{abs(v):.1f}%</span><br><span class="small">{word} · {fmt(r[k])}</span></td>'
        def cell_rsi(v):
            lab = "과매도(30↓)" if v < 30 else "과매수(70↑)" if v > 70 else "약세권(30~50)" if v < 50 else "강세권(50~70)"
            return f'<td class="r"><b>{v:.1f}</b><br><span class="small">{lab}</span></td>'
        def cell(v, sub=""): return f'<td class="r"><b>{v}</b>{"<br><span class=small>" + sub + "</span>" if sub else ""}</td>'
        def table(rows):
            head = "".join(f'<th class="r cc c-{c[0]}">{c[0]}</th>' for c in COINS)
            body = "".join(f'<tr><td>{name}<br><span class="small">{basis_}</span></td>{"".join(fn(T[c[0]]).replace(chr(60) + "td class=" + chr(34) + "r", chr(60) + "td class=" + chr(34) + "r cc c-" + c[0], 1) for c in COINS)}</tr>' for name, basis_, fn in rows)
            return f'<div class="tbl-wrap"><table class="num"><thead><tr><th>지표</th>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        def hlab(t, hrs):
            a = dt.datetime.fromisoformat(t); b = a + dt.timedelta(hours=hrs)
            return f"{a.month}/{a.day} {a:%H:%M}~{b:%H:%M} 봉까지"
        t1, t4 = T["BTC"]["h1_time"], T["BTC"]["h4_time"]
        e1 = dt.datetime.fromisoformat(t1) + dt.timedelta(hours=1); s1 = e1 - dt.timedelta(hours=24)
        tech["{{TECH_SHORT}}"] = table([
            ("1시간봉 RSI(14)", hlab(t1, 1), lambda r: cell_rsi(r["h1_rsi"])),
            ("4시간봉 RSI(14)", hlab(t4, 4), lambda r: cell_rsi(r["h4_rsi"])),
            ("1시간봉 60이평 대비", "최신 1시간봉 종가 기준", lambda r: cell_gap(r, "h1_ma60", "h1_last")),
            ("최근 24시간 고가 / 저가", f"{s1.month}/{s1.day} {s1:%H:%M}~{e1.month}/{e1.day} {e1:%H:%M}", lambda r: cell(f'{fmt(r["h24_hi"])} / {fmt(r["h24_lo"])}')),
            ("일봉 ATR(14) ÷ 종가", "하루 평균 진폭", lambda r: cell(f'{r["atr_d_pct"]:.1f}%', "목표가 아님, 변동성 참고")),
        ])
        lastd = basis - dt.timedelta(days=1)
        live = hr != 9   # 현시점 회차: 마지막 일봉은 진행 중(09:00~기준 시각)
        dlab = f"{basis.month}/{basis.day} 진행 중 일봉(09:00~{hr:02d}:00) 포함" if live else f"{lastd.month}/{lastd.day} 일봉까지"
        vk = "vol_ratio_prev" if live else "vol_ratio"
        vd = (basis - dt.timedelta(days=1)) if live else lastd
        tech["{{TECH_MID}}"] = table([
            ("일봉 RSI(14)", dlab, lambda r: cell_rsi(r["d_rsi"])),
            ("볼린저밴드 %B (20, 2σ)", "0 미만 = 하단 밴드 아래", lambda r: cell(f'{r["pctb"]:+.2f}'.replace("-", "−"), "하단 이탈" if r["pctb"] < 0 else "하단 부근" if r["pctb"] < 0.2 else "밴드 안")),
            ("MACD 히스토그램 (12,26,9)", "전일 대비", lambda r: cell("음(−)" if r["macd_h"] < 0 else "양(+)", "음(−) 확대" if r["macd_h"] < r["macd_h_prev"] else "음(−) 축소" if r["macd_h"] < 0 else "양(+)")),
            ("5일 이평 대비", "단기 추세", lambda r: cell_gap(r, "ma5")),
            ("20일 이평 대비", "약 1개월 추세", lambda r: cell_gap(r, "ma20")),
            ("20일 고가 / 저가", "최근 20개 일봉", lambda r: cell(f'{fmt(r["hi20"])} / {fmt(r["lo20"])}')),
            (f"{vd.month}/{vd.day} 거래대금 ÷ 직전 20일 평균", "업비트 KRW 마켓 · 마감된 일봉", lambda r: cell(f'{r[vk]:.2f}배', "급증" if r[vk] >= 2 else "증가" if r[vk] >= 1.2 else "평균 수준")),
            ("연속 하락 일수", "일봉 기준" + (" · 진행 중 봉 포함" if live else ""), lambda r: cell(f'{r["down_streak"]}일')),
        ])
        tech["{{TECH_LONG}}"] = table([
            ("200주 이평 대비", "약 4년 평균", lambda r: cell_gap(r, "w_ma200")),
            ("20주 / 50주 이평 배열", "중장기 추세", lambda r: cell("20주 < 50주" if r["w_ma20"] < r["w_ma50"] else "20주 > 50주", "역배열(약세)" if r["w_ma20"] < r["w_ma50"] else "정배열")),
            ("50주 이평 대비", "약 1년 평균", lambda r: cell_gap(r, "w_ma50")),
            ("주봉 RSI(14)", "최근 주봉 기준", lambda r: cell_rsi(r["w_rsi"])),
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
    archive, nav = archive_block(basis, editions, latest, hist)
    for k, v in {"{{CARDS}}": "".join(cards), "{{BARCHART}}": bar, "{{LINECHART}}": line, "{{DAILYTABLE}}": daily,
                 "{{HISTORY}}": history, "{{ARCHIVE}}": archive, "{{EDITION_NAV}}": nav, "{{GEN_TIME}}": gen, **tech}.items():
        body = body.replace(k, v)
    tpl = open(os.path.join(ROOT, "scripts", "template.html"), encoding="utf-8").read()
    fragment = tpl.replace("{{BODY}}", body)
    assert "{{" not in fragment, "치환되지 않은 자리표시자 있음"

    head_bits = ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')
    title_end = fragment.index("</style>") + len("</style>")
    full = head_bits + fragment[:title_end] + "\n</head>\n<body>\n" + fragment[title_end:] + "\n</body>\n</html>\n"

    # 회차 보관본: reports/ 하위에 있으므로 'reports/' 상대 경로를 같은 폴더 기준으로 바꿈
    open(os.path.join(ROOT, "reports", f"{basis.isoformat()}.html"), "w", encoding="utf-8").write(full.replace('href="reports/', 'href="').replace('"file": "reports/', '"file": "'))
    if basis == latest:
        os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
        # 아티팩트에는 reports/*.html 보관본을 함께 게시하므로 상대 경로 그대로 사용
        open(os.path.join(ROOT, "dist", "artifact.html"), "w", encoding="utf-8").write(fragment)
        open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8").write(full)
    for sym, *_ in COINS:
        s = stats[sym]; print(f'{sym:5} {fmt(s["base"]):>13} → {fmt(s["cur"]):>13}  주간 {pct(s["wk"])}  1일 {pct(s["d1"])}')
    print("완료:", gen)


def archive_block(basis, editions, latest, hist):
    """보관함·달력 섹션(HTML+JSON)과 표지의 회차 이동 막대를 만듦. 링크는 루트 기준 'reports/…'로 씀"""
    ev = json.load(open(os.path.join(ROOT, "data", "events.json"), encoding="utf-8"))["events"]
    eds = []
    for e in editions:
        eh = basis_hour(e); k = f"{e.isoformat()} {eh:02d}:00"; rets = {}
        for sym, m, *_ in COINS:
            r = hist.get(k, {}).get(m)
            rets[sym] = float(r["weekly_return_pct"]) if r else None   # 미보고는 None(0으로 채우지 않음)
        gt = os.path.join(ROOT, "reports", "src", f"{e.isoformat()}.gentime")
        pub = open(gt, encoding="utf-8").read().strip() if os.path.exists(gt) else ""
        start = e - dt.timedelta(days=7)
        eds.append({"date": e.isoformat(), "file": f"reports/{e.isoformat()}.html", "published": pub,
                    "period": f"{start.month}/{start.day} {eh:02d}:00 ~ {e.month}/{e.day} {eh:02d}:00", "ret": rets})
    data = {"current": basis.isoformat(), "latest": latest.isoformat(), "editions": eds, "events": ev,
            "reading": {"weekdays": [3, 5], "time": "07:00"}}  # JS getDay 기준: 일=0 … 수=3, 금=5
    rows = []
    for x in reversed(eds):
        cur = x["date"] == basis.isoformat()
        cells = "".join(
            f'<td class="r cc c-{sym}">' + ('<span class="na">미보고</span>' if v is None else f'<span class="{cls(v)}">{arrow(v)} {pct(v)}</span>') + '</td>'
            for sym, v in x["ret"].items())
        d = dt.date.fromisoformat(x["date"])
        label = f'{d.month}/{d.day}({WD[d.weekday()]}) 회차'
        link = f'<b>{label} · 지금 보는 중</b>' if cur else f'<a href="{x["file"]}">{label}</a>'
        rows.append(f'<tr{" class=cur-row" if cur else ""}><td>{link}{" <span class=new-chip>최신</span>" if x["date"] == latest.isoformat() else ""}<br><span class="small">{x["period"]} · 발행 {x["published"]}</span></td>{cells}</tr>')
    js = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    block = f'''<div class="arch" id="archive">
    <div class="cal" id="cal" aria-label="회차·일정 달력">
      <div class="cal-head"><button class="btn cal-nav" type="button" id="cal-prev" aria-label="이전 달">‹</button><h3 id="cal-title">달력</h3><button class="btn cal-nav" type="button" id="cal-next" aria-label="다음 달">›</button></div>
      <div class="cal-grid" id="cal-grid" role="grid"></div>
      <div class="cal-legend"><span><i class="lg-ed"></i>브리핑 회차 (가격 기준일)</span><span><i class="lg-read"></i>정기 열람일 수·금 07:00</span><span><i class="lg-ev"></i>주요 일정 (KST)</span><span><i class="lg-today"></i>오늘</span></div>
    </div>
    <div class="cal-side">
      <div class="cal-detail" id="cal-detail" aria-live="polite"><p class="small">달력에서 날짜를 누르면 그날의 회차와 일정이 여기에 표시됨</p></div>
    </div>
  </div>
  <h3>회차 목록</h3>
  <div class="tbl-wrap"><table class="num"><thead><tr><th>회차 (분석 기간 · 발행 시각)</th>{"".join(f'<th class="r cc c-{c[0]}">{c[0]} 주간</th>' for c in COINS)}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>
  <script type="application/json" id="archive-data">{js}</script>'''
    i = editions.index(basis)
    prev_e = editions[i - 1] if i > 0 else None
    next_e = editions[i + 1] if i + 1 < len(editions) else None
    def lk(e, txt): return f'<a class="ed-btn" href="reports/{e.isoformat()}.html">{txt}</a>' if e else f'<span class="ed-btn off">{txt}</span>'
    banner = '' if basis == latest else f'<p class="ed-banner">지금 보는 화면은 {basis.month}/{basis.day} 회차 보관본임 · <a href="reports/{latest.isoformat()}.html">최신 {latest.month}/{latest.day} 회차 보기 →</a></p>'
    nav = (f'<div class="ed-nav">{lk(prev_e, "‹ 이전 회차")}<a class="ed-btn" href="#sec-history">회차 {i + 1}/{len(editions)} · 보관함·달력</a>{lk(next_e, "다음 회차 ›")}</div>{banner}')
    return block, nav


def main(argv):
    eds = sorted(dt.date.fromisoformat(os.path.basename(p)[:10]) for p in glob.glob(os.path.join(ROOT, "reports", "src", "????-??-??.body.html")))
    if argv:  # 특정 회차만 지정해도 보관함·달력이 일치하도록 전 회차를 함께 다시 만듦
        assert dt.date.fromisoformat(argv[0]) in eds, "해당 날짜의 본문(reports/src/DATE.body.html)이 없음"
    latest = eds[-1]
    for e in eds:
        print(f"── {e} 회차" + (" (최신)" if e == latest else ""))
        build(e, eds, latest)
    stale = os.path.join(ROOT, "reports", "index.html")
    if os.path.exists(stale): os.remove(stale)   # 보관함은 보고서 안 ‘보관함·달력’ 섹션으로 대체


if __name__ == "__main__":
    main(sys.argv[1:])
