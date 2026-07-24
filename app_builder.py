# -*- coding: utf-8 -*-
"""
把預測結果產生成一個「手機看的網頁」。

設計取向：
  這不是下注工具，是給球迷看的賽事解讀。所以畫面裡不會出現賠率、莊家、
  期望值這些字眼 —— 那些留在自用的 scan / ledger 裡。

  視覺語彙取自 box score：所有數字用等寬字、對齊成表，
  主隊用木地板的琥珀色、客隊用冷調的青色，顏色本身就是資訊。
  招牌元素是每場比賽的「分差分佈曲線」—— 模型本來就算得出來，
  但市面上的預測APP只給一個數字，不給你看它有多不確定。

  整份 HTML 自成一體，不連外部網路，放進 OneDrive 用手機開就能看。
"""

import json
import math
import os
from datetime import datetime

PALETTE = {
    "ink": "#0E151C",
    "surface": "#17222D",
    "surface2": "#1E2C39",
    "line": "#2A3B49",
    "home": "#E9A13B",
    "away": "#45BFD6",
    "chalk": "#E9EFF3",
    "muted": "#7C919F",
}


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _implied_margin_std(entry):
    """從勝率反推當時用的分差標準差；反推不穩就用預設 13.5。"""
    from statistics import NormalDist
    margin = entry["home_proj"] - entry["away_proj"]
    p_home = (entry["winner_prob"] / 100.0
              if entry["winner"] == entry["home_name"]
              else 1.0 - entry["winner_prob"] / 100.0)
    try:
        z = NormalDist().inv_cdf(min(0.999, max(0.001, p_home)))
        if abs(z) > 0.05 and abs(margin) > 0.2:
            return min(max(abs(margin / z), 8.0), 20.0)
    except Exception:
        pass
    return 13.5


def _curve_svg(margin, std, home_favored):
    """
    分差分佈曲線。x 軸 -35~35 分，主隊領先為正。
    填色只填「模型看好的那一側」，讓人一眼看到重心偏哪邊、以及有多分散。
    """
    w, h = 300.0, 74.0
    lo, hi = -35.0, 35.0
    n = 72
    peak = 1.0 / (std * math.sqrt(2 * math.pi))
    pts = []
    for i in range(n + 1):
        x = lo + (hi - lo) * i / n
        y = math.exp(-((x - margin) ** 2) / (2 * std * std)) / (std * math.sqrt(2 * math.pi))
        px = (x - lo) / (hi - lo) * w
        py = h - 6 - (y / peak) * (h - 16)
        pts.append((px, py))
    line = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    zero_x = (0 - lo) / (hi - lo) * w

    # 只填勝方那一側
    if home_favored:
        side = [(px, py) for px, py in pts if px >= zero_x]
        edge = zero_x
    else:
        side = [(px, py) for px, py in pts if px <= zero_x]
        edge = zero_x
    if side:
        fill = ("M" + f"{side[0][0]:.1f},{h-6} L"
                + " L".join(f"{px:.1f},{py:.1f}" for px, py in side)
                + f" L{side[-1][0]:.1f},{h-6} Z")
    else:
        fill = ""
    color = "var(--home)" if home_favored else "var(--away)"
    return f"""<svg class="curve" viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none" aria-hidden="true">
  <path d="{fill}" fill="{color}" opacity=".16"/>
  <path d="{line}" fill="none" stroke="{color}" stroke-width="1.6"/>
  <line x1="{zero_x:.1f}" y1="4" x2="{zero_x:.1f}" y2="{h-6}" stroke="var(--line)" stroke-width="1" stroke-dasharray="2 3"/>
</svg>"""


def _calibration_rows(log):
    """從已結算的預測算校準表：模型說幾成，實際就是幾成嗎。"""
    rows = [e for day in log.values() for e in day if "hit_winner" in e]
    tiers = [(50, 60, "五成半上下"), (60, 70, "六成"), (70, 80, "七成"), (80, 101, "八成以上")]
    out = []
    for lo, hi, label in tiers:
        sub = [e for e in rows if lo <= e["winner_prob"] < hi]
        if len(sub) < 3:
            continue
        hit = sum(1 for e in sub if e["hit_winner"])
        out.append({
            "label": label,
            "n": len(sub),
            "said": sum(e["winner_prob"] for e in sub) / len(sub),
            "was": 100.0 * hit / len(sub),
        })
    return out, len(rows)


def _recent_results(log, limit=6):
    days = sorted([d for d in log if any("hit_winner" in e for e in log[d])], reverse=True)
    out = []
    for d in days:
        for e in log[d]:
            if "hit_winner" not in e:
                continue
            out.append({
                "date": d[5:].replace("-", "/"),
                "away": e["away_name"], "home": e["home_name"],
                "pick": e["winner"], "prob": e["winner_prob"],
                "score": f"{e['actual_away']}-{e['actual_home']}",
                "ok": bool(e["hit_winner"]),
            })
            if len(out) >= limit:
                return out
    return out


def build_app_data(log, target_date=None):
    dated = [d for d in log if log[d]]
    if not dated:
        return None
    day = target_date or max(dated)
    games = []
    for e in log.get(day, []):
        margin = e["home_proj"] - e["away_proj"]
        std = _implied_margin_std(e)
        home_favored = e["winner"] == e["home_name"]
        p = e["winner_prob"]
        games.append({
            "home": e["home_name"], "away": e["away_name"],
            "home_proj": e["home_proj"], "away_proj": e["away_proj"],
            "pick": e["winner"], "prob": p,
            "home_prob": p if home_favored else 100 - p,
            "margin": abs(margin),
            "total": e["home_proj"] + e["away_proj"],
            "close": abs(margin) < 4,
            "curve": _curve_svg(margin, std, home_favored),
            "spread_10": e.get("over10_prob"),
            "settled": "hit_winner" in e,
            "ok": e.get("hit_winner"),
            "score": (f"{e['actual_away']}-{e['actual_home']}"
                      if "actual_home" in e else None),
        })
    calib, n_settled = _calibration_rows(log)
    return {"date": day, "games": games, "calibration": calib,
            "settled": n_settled, "recent": _recent_results(log)}


CSS = """
:root{
  --ink:%(ink)s; --surface:%(surface)s; --surface2:%(surface2)s;
  --line:%(line)s; --home:%(home)s; --away:%(away)s;
  --chalk:%(chalk)s; --muted:%(muted)s;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --cjk:"PingFang TC","Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%%}
body{
  background:var(--ink); color:var(--chalk);
  font-family:var(--cjk); font-size:15px; line-height:1.5;
  padding:0 0 48px; max-width:520px; margin:0 auto;
  -webkit-font-smoothing:antialiased;
}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.02em}

header{padding:22px 18px 14px;border-bottom:1px solid var(--line)}
.eyebrow{font-size:11px;letter-spacing:.18em;color:var(--muted);text-transform:uppercase}
h1{font-size:25px;font-weight:800;letter-spacing:-.02em;margin-top:5px}
h1 .d{font-family:var(--mono);color:var(--home)}
.sub{color:var(--muted);font-size:13px;margin-top:4px}

.receipt{
  margin:14px 18px 0;padding:11px 13px;
  background:var(--surface);border:1px solid var(--line);border-radius:9px;
  font-size:12.5px;color:var(--muted);
}
.receipt b{color:var(--chalk);font-weight:600}
.receipt .num{color:var(--home)}

section{margin-top:26px}
.shead{
  display:flex;align-items:baseline;gap:9px;
  padding:0 18px 9px;border-bottom:1px solid var(--line);
}
.shead h2{font-size:13px;font-weight:700;letter-spacing:.04em}
.shead span{font-size:11.5px;color:var(--muted)}

.card{
  margin:12px 18px;background:var(--surface);
  border:1px solid var(--line);border-radius:12px;overflow:hidden;
}
.teams{padding:15px 15px 4px}
.row{display:flex;align-items:center;gap:10px;padding:5px 0}
.dot{width:3px;height:19px;border-radius:2px;flex:none}
.row.a .dot{background:var(--away)} .row.h .dot{background:var(--home)}
.tname{flex:1;font-size:15.5px;font-weight:600;letter-spacing:-.01em}
.tscore{font-size:19px;font-weight:700}
.row.a .tscore{color:var(--away)} .row.h .tscore{color:var(--home)}

.bar{display:flex;height:5px;margin:11px 15px 0;border-radius:3px;overflow:hidden;background:var(--surface2)}
.bar i{display:block;height:100%%}
.bar .ba{background:var(--away)} .bar .bh{background:var(--home)}
.barlab{display:flex;justify-content:space-between;padding:6px 15px 0;font-size:11.5px;color:var(--muted)}

.curve{display:block;width:100%%;height:74px;margin-top:6px}
.axis{display:flex;justify-content:space-between;padding:0 15px 12px;font-size:10.5px;color:var(--muted)}

.meta{
  display:flex;gap:0;border-top:1px solid var(--line);background:var(--surface2);
}
.meta div{flex:1;padding:10px 12px;border-right:1px solid var(--line)}
.meta div:last-child{border-right:0}
.mk{font-size:10.5px;color:var(--muted);letter-spacing:.03em}
.mv{font-size:14.5px;font-weight:700;margin-top:2px}

.tag{
  display:inline-block;margin:0 15px 13px;padding:3px 9px;border-radius:5px;
  font-size:11.5px;font-weight:600;
}
.tag.close{background:rgba(124,145,159,.16);color:var(--muted)}
.tag.strong{background:rgba(233,161,59,.14);color:var(--home)}

table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 18px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{font-size:11px;color:var(--muted);font-weight:500;letter-spacing:.03em}
td.said{color:var(--muted)} td.was{color:var(--home);font-weight:700}

.res{display:flex;align-items:center;gap:11px;padding:9px 18px;border-bottom:1px solid var(--line);font-size:13px}
.res .m{width:34px;font-size:11px;color:var(--muted)}
.res .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.res .s{color:var(--muted);font-size:12px}
.res .f{width:17px;text-align:center;font-weight:700}
.res .f.y{color:var(--home)} .res .f.n{color:var(--muted)}

footer{margin:30px 18px 0;padding-top:16px;border-top:1px solid var(--line);
  font-size:11.5px;color:var(--muted);line-height:1.65}
footer b{color:#9db0bd;font-weight:600}
"""


def render_html(data, league_label="WNBA"):
    p = data
    d = datetime.strptime(p["date"], "%Y-%m-%d")
    cards = []
    for g in p["games"]:
        hp = max(3.0, min(97.0, g["home_prob"]))
        tag = ('<span class="tag close">勢均力敵</span>' if g["close"]
               else f'<span class="tag strong">模型看好 {g["pick"]}</span>')
        settled = ""
        if g["settled"]:
            mark = "命中" if g["ok"] else "沒中"
            settled = (f'<div><div class="mk">實際比分</div>'
                       f'<div class="mv num">{g["score"]}　{mark}</div></div>')
        cards.append(f"""
<article class="card">
  <div class="teams">
    <div class="row a"><i class="dot"></i><span class="tname">{g['away']}</span>
      <span class="tscore num">{g['away_proj']:.0f}</span></div>
    <div class="row h"><i class="dot"></i><span class="tname">{g['home']}</span>
      <span class="tscore num">{g['home_proj']:.0f}</span></div>
  </div>
  <div class="bar"><i class="ba" style="width:{100-hp:.1f}%"></i><i class="bh" style="width:{hp:.1f}%"></i></div>
  <div class="barlab"><span class="num">{100-hp:.0f}%</span><span>勝率</span><span class="num">{hp:.0f}%</span></div>
  {g['curve']}
  <div class="axis"><span>客隊贏 35</span><span>平手</span><span>主隊贏 35</span></div>
  {tag}
  <div class="meta">
    <div><div class="mk">預期分差</div><div class="mv num">{g['margin']:.1f} 分</div></div>
    <div><div class="mk">預期總分</div><div class="mv num">{g['total']:.0f}</div></div>
    {settled}
  </div>
</article>""")

    calib = ""
    if p["calibration"]:
        rows = "".join(
            f'<tr><td>{c["label"]}</td><td class="num">{c["n"]}</td>'
            f'<td class="num said">{c["said"]:.0f}%</td>'
            f'<td class="num was">{c["was"]:.0f}%</td></tr>'
            for c in p["calibration"])
        calib = f"""
<section>
  <div class="shead"><h2>信心指數準不準</h2><span>已結算 {p['settled']} 場</span></div>
  <table>
    <tr><th>模型說</th><th>場次</th><th>宣稱</th><th>實際</th></tr>
    {rows}
  </table>
</section>"""

    recent = ""
    if p["recent"]:
        items = "".join(
            f'<div class="res"><span class="m num">{r["date"]}</span>'
            f'<span class="t">{r["pick"]}</span>'
            f'<span class="s num">{r["score"]}</span>'
            f'<span class="f {"y" if r["ok"] else "n"}">{"✓" if r["ok"] else "✕"}</span></div>'
            for r in p["recent"])
        recent = f"""
<section>
  <div class="shead"><h2>最近的預測</h2><span>對過答案的</span></div>
  {items}
</section>"""

    n = len(p["games"])
    receipt = ""
    if p["calibration"]:
        # 挑「樣本夠、而且信心最高」的那一層來當招牌 —— 有把握的預測準不準，
        # 比五五波的預測準不準有意義得多。
        pool = [c for c in p["calibration"] if c["n"] >= 4] or p["calibration"]
        best = max(pool, key=lambda c: c["said"])
        receipt = (f'<div class="receipt">模型說有<b>{best["label"]}</b>把握的那些比賽，'
                   f'實際打完命中 <b class="num">{best["was"]:.0f}%</b>'
                   f'（{best["n"]} 場）。宣稱幾成就真的是幾成，'
                   f'這一欄我們攤開來給你看。</div>')

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="{PALETTE['ink']}">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="description" content="用整季資料重建球隊評分，算出每場比賽的勝率與分差分佈。僅供資訊參考及娛樂用途。">
<link rel="manifest" href="manifest.json">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="icon-192.png">
<title>{league_label} 賽事預測</title>
<style>{CSS % PALETTE}</style>
</head>
<body>
<header>
  <div class="eyebrow">{league_label} · 賽事預測</div>
  <h1><span class="d num">{d.month}/{d.day}</span> 共 <span class="d num">{n}</span> 場</h1>
  <div class="sub">用整季資料重建的球隊評分，算出每場的機率分佈</div>
</header>
{receipt}
<section>
  <div class="shead"><h2>今日賽事</h2><span>曲線越寬代表越難算</span></div>
  {''.join(cards)}
</section>
{calib}
{recent}
<footer>
  <b>怎麼讀這些數字</b><br>
  勝率是模型認為某隊獲勝的機率，不是保證。曲線畫的是分差可能落在哪，
  越寬代表這場越難預測 —— 籃球單場的分差標準差大約 13 分，
  所以就算模型看好某隊贏 10 分，實際打出 20 分或反而輸球都很常見。<br><br>
  所有內容由統計模型與歷史數據產生，僅供資訊參考及娛樂用途。
</footer>
<script>
// 註冊離線快取。用 file:// 直接開時瀏覽器不允許，靜靜略過即可。
if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {{
  window.addEventListener("load", function () {{
    navigator.serviceWorker.register("sw.js").catch(function () {{}});
  }});
}}
</script>
</body>
</html>"""


def build_app_page(log_path, out_path, league_label="WNBA", target_date=None):
    with open(log_path, encoding="utf-8") as f:
        log = json.load(f)
    data = build_app_data(log, target_date)
    if not data:
        return None
    html = render_html(data, league_label)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return {"path": out_path, "date": data["date"],
            "games": len(data["games"]), "settled": data["settled"]}


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "wnba_prediction_log.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "wnba_app.html"
    print(build_app_page(src, dst))
