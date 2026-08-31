# -*- coding: utf-8 -*-
"""
把預測結果產生成手機看的網頁，並拆成免費／付費兩份。

  index.html    免費版。純模型的預測 + 分差曲線 + 戰績。
                日誌裡「所有日期」的資料都內嵌在檔案裡，可以切換日期，
                而且離線也能翻。

  premium.json  付費版。市場融合後的預測，加上完整分差分布、攻守評分、
                休息天數、傷兵、預測移動軌跡、完整校準表。同樣涵蓋所有日期。

為什麼曲線改成前端畫
  舊版每場比賽都在 Python 端產生一段 SVG，一天三場沒問題，但要放進
  「所有日期」就會膨脹到幾百 KB。改成前端用同一支函式畫，資料只要存
  分差與標準差兩個數字，整份檔案小一個數量級。

設計取向
  這不是下注工具，是給球迷看的賽事解讀，所以畫面上不會出現讓分線、賠率、
  大小分線這類莊家給的數字 —— 判準是「這個數字是誰的」。融合在伺服器端
  完成，送到手機的一律是模型自己的機率與預期分數。

用法
  python app_builder.py <預測日誌.json> <輸出資料夾> [--locked]
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

# 跟主程式同一套「賽事日」定義：ESPN 把比賽歸在美東當天，
# 用 UTC 減 10 小時換算，避開台灣跨日與夏令時間的問題。
SPORTS_DAY_OFFSET_HOURS = 10


def sports_today():
    return (datetime.now(timezone.utc)
            - timedelta(hours=SPORTS_DAY_OFFSET_HOURS)).strftime("%Y-%m-%d")

PALETTE = {
    "ink": "#0E151C", "surface": "#17222D", "surface2": "#1E2C39",
    "line": "#2A3B49", "home": "#E9A13B", "away": "#45BFD6",
    "chalk": "#E9EFF3", "muted": "#7C919F",
}


def _implied_std(margin, p_home, fallback=13.5, lo=8.0, hi=20.0):
    """從勝率反推當時用的分差標準差；反推不穩就用預設值。"""
    from statistics import NormalDist
    try:
        z = NormalDist().inv_cdf(min(0.999, max(0.001, p_home)))
        if abs(z) > 0.05 and abs(margin) > 0.2:
            return min(max(abs(margin / z), lo), hi)
    except Exception:
        pass
    return fallback


def _model_view(e):
    """純模型的預測（免費版用）。沒有 three_way 就退回最終輸出。"""
    tw = e.get("three_way") or {}
    if tw.get("model_margin") is not None and tw.get("model_total") is not None:
        margin, total = tw["model_margin"], tw["model_total"]
        p_home = (tw.get("model_p_home") or 50.0) / 100.0
    else:
        margin = e["home_proj"] - e["away_proj"]
        total = e["home_proj"] + e["away_proj"]
        p_home = (e["winner_prob"] / 100.0 if e["winner"] == e["home_name"]
                  else 1.0 - e["winner_prob"] / 100.0)
    return {"margin": margin, "total": total, "p_home": p_home,
            "std": _implied_std(margin, p_home)}


def _market_view(e):
    """市場對照（付費版用）。沒有盤口資料就回 None，畫面自己會處理。"""
    tw = e.get("three_way") or {}
    if tw.get("model_margin") is None:
        return None
    gap = None
    if tw.get("market_margin") is not None:
        gap = round(abs(tw["model_margin"] - tw["market_margin"]), 1)
    return {
        # 三方對照：模型自己算的 / 市場共識的 / 加權合併後的
        # 「市場共識預期分差、預期總分」就是盤口的統計原意，
        # 這裡回歸它的本義呈現，不帶賠率、書商名稱或任何投注引導。
        "mm": tw.get("model_margin"),   "mt": tw.get("model_total"),   "mp": tw.get("model_p_home"),
        "km": tw.get("market_margin"),  "kt": tw.get("market_total"),  "kp": tw.get("market_p_home"),
        "bm": tw.get("blend_margin"),   "bt": tw.get("blend_total"),   "bp": tw.get("blend_p_home"),
        "w": tw.get("weight"),                 # 這場實際採用的市場權重
        "gap": gap,                            # 模型與市場的分歧大小
        "alert": 1 if e.get("market_alert") else 0,
        "sources": e.get("market_books"),      # 綜合了幾個來源
        # 「開盤／收盤」是投注用語（opening / closing line），畫面上一律用
        # 「首次收錄以來」—— 詞彙表的同一條原則：數字可以呈現，但要用它的
        # 統計原意，不要用莊家的說法。
        "move": e.get("line_move_margin"),     # 分差共識自首次收錄以來的變動
        "movet": e.get("line_move_total"),      # 總分共識自首次收錄以來的變動
        # 市場認為總分會高於共識值的機率。50% 代表市場覺得兩邊機會均等，
        # 偏離越多代表市場對總分的方向越有意見。
        "kov": e.get("over_prob"),
        # 模型預期總分與市場共識的差距（正 = 模型看得比較高）。
        # 這個數字每場方向幾乎一致，是模型的固定傾向，不是單場判讀 ——
        # 不標出來的話 kov 會被誤讀成逐場訊號。
        "toff": e.get("total_offset"),
        "toffs": e.get("total_offset_stat"),
        # 狀態未定（Day-To-Day）的輪替球員，以及「全部缺陣」時預期分差會移到哪。
        # 不計入預測，但它常常就是模型與市場共識分歧的來源，要讓人看得到。
        "watch": _watch_list(e),
        "wswing": e.get("watch_swing"),
        # 【v1.25】總分的擺動。predictor 那邊只有在斜率達顯著時才會寫這個欄位，
        # 所以缺值＝「測不出來」，不是「沒影響」—— 缺值時不顯示。
        "wtswing": e.get("watch_total_swing"),
        # 【v1.38】確定缺陣、但已久到球隊評分本身就反映了的球員。
        # 不重複扣分是對的，完全不顯示就不對 —— 畫面上看到「0 人缺陣」
        # 但傷兵名單有人，會以為漏算。
        "lt": _longterm_list(e),
        # 【v1.38】總分的實測校準修正。App 顯示的純模型預期總分已經含了
        # 這個修正，先前完全沒講，數字看起來像憑空來的。
        "corr": (None if e.get("total_correction_in") in (None, 0) else {
            "v": e.get("total_correction_in"),
            "a": e.get("total_before_corr"), "b": e.get("total_after_corr")}),
        # 【v1.38】合併區間：報告裡標「抽水最低」的那組，這裡只呈現機率。
        "merged": {"o10": e.get("over10_prob"), "u10": e.get("under10_prob"),
                   "o15": e.get("over15_prob")},
        # 【v1.38】各種判斷方式的命中機率。predictor 存的每一列都帶
        # kind/team/line，App 拿它自己組中性描述 —— 報告那邊的標籤
        # （獨贏／讓／受讓／大分／小分）是投注術語，不能照搬。
        # 公平賠率那一欄日誌裡有，這裡刻意不送。
        "opts": [{"p": o.get("prob"), "k": o.get("kind"),
                  "t": o.get("team"), "l": o.get("line")}
                 for o in (e.get("options") or [])
                 if o.get("kind")],
        "optb": 1 if e.get("options_blended") else 0,
    }


def _longterm_list(e):
    out = []
    for side in ("home", "away"):
        for item in (e.get("longterm_" + side) or []):
            out.append({"n": item[0], "t": e.get(side + "_name"),
                        "g": item[1] if len(item) > 1 else None})
    return out


# ⚠️ 這是 predictor `_STATUS_ZH` 的鏡像。predictor 那邊改了，這裡要一起改
#    —— 手抄副本在這個專案已經出過五次事。
_STATUS_ZH_APP = {
    "doubtful": "很可能不打",
    "questionable": "不確定",
    "game-time decision": "臨場決定",
    "game time decision": "臨場決定",
    "day-to-day": "待觀察",
}


def _watch_list(e):
    """把主客兩隊的待觀察名單合成一份，附上所屬球隊。"""
    out = []
    for side in ("home", "away"):
        team = e.get(side + "_name")
        for item in (e.get("watch_" + side) or []):
            try:
                nm, val = item[0], item[1]
            except Exception:
                continue
            # 【v1.22】第三欄是狀態（predictor v4.38 起）。舊紀錄沒有，
            # 用索引取值並容忍長度不足，不要用 tuple 解包。
            st = item[2] if len(item) > 2 else None
            row = {"n": nm, "t": team, "v": round(float(val), 1)}
            if st:
                row["s"] = _STATUS_ZH_APP.get(str(st).strip().lower(), str(st))
            out.append(row)
    out.sort(key=lambda x: -x["v"])
    return out


BLEND_WEIGHT_ASSUMED = 0.65      # 反解舊紀錄用；與 predictor 的 MARKET_WEIGHT 對應


def _pure_model_total(e, tw, line):
    """取這一場的純模型預期總分。

    ⚠️ 不能用日誌頂層的 `model_total` 或 `home_proj + away_proj`。
    predictor 在合併市場共識時會就地覆寫那兩個欄位，之後才寫入
    `model_total` —— 所以頂層存的是**合併後**的值，裡面有六成五是共識值本身。
    拿它去算「模型與市場共識的差距」，等於把差距壓成三分之一，
    永遠測不到真正的分歧（指紋是差距的標準差小到不合理）。

    乾淨的原值在 `three_way.model_total`；舊紀錄沒有的話從合併值反解。
    """
    mt = tw.get("model_total")
    if mt is not None:
        return mt
    top = e.get("model_total")
    if top is None and e.get("home_proj") is not None:
        top = e["home_proj"] + e["away_proj"]
    if top is None:
        return None
    if e.get("market_blended") and line is not None:
        w = BLEND_WEIGHT_ASSUMED
        return (top - w * line) / (1.0 - w)
    return top


def _market_bias(log):
    """市場共識本身準不準：把市場的總分與分差共識拿去跟實際結果對帳。

    這一段的用意是把「模型算錯」和「市場給錯價」分開。模型低估總分，
    不代表市場也低估總分 —— 前者只是模型要修的問題。兩者混在一起看，
    會把自己的誤差誤讀成機會。算法與 predictor 報告裡那段完全一致。
    """
    import math as _m
    rows = []
    for entries in (log or {}).values():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            at, am = e.get("actual_total"), e.get("actual_margin")
            if at is None:
                continue
            line = e.get("ou_line")
            tw = e.get("three_way") or {}
            mt = _pure_model_total(e, tw, line)
            if line is None or mt is None:
                continue
            r = {"mkt_t": at - line, "mdl_t": at - mt}
            if am is not None and tw.get("market_margin") is not None:
                r["mkt_m"] = am - tw["market_margin"]
                if tw.get("model_margin") is not None:
                    r["mdl_m"] = am - tw["model_margin"]
            rows.append(r)
    if len(rows) < 15:
        return None

    def stat(key):
        vals = [r[key] for r in rows if key in r]
        if len(vals) < 10:
            return None
        n = len(vals)
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
        sd = _m.sqrt(var)
        t = mean / (sd / _m.sqrt(n)) if sd > 0 else 0.0
        return {"n": n, "bias": round(mean, 2), "t": round(t, 2)}

    out = {"n": len(rows),
           "mdl_total": stat("mdl_t"), "mkt_total": stat("mkt_t"),
           "mdl_margin": stat("mdl_m"), "mkt_margin": stat("mkt_m")}
    if out["mdl_total"] and out["mkt_total"]:
        out["gap"] = round(out["mdl_total"]["bias"] - out["mkt_total"]["bias"], 2)
        out["mkt_sig"] = abs(out["mkt_total"]["t"]) > 2.0
    return out


def _watch_audit(log):
    """狀態未定（Day-To-Day）球員的事後對帳：實際出賽率。

    ⚠️ 這是 predictor 的 `print_watch_audit()` 的鏡像，判定條件要一致
    （出賽分鐘 > 0.5 才算有上場）。改一邊記得改另一邊 ——
    這個專案已經因為手抄副本分岔出過三次事。
    App 只呈現出賽率這一段；「若事先知道誰不打」那組對照屬於模型研發，
    對使用者沒有意義，留在本機報告。
    """
    n_players = n_played = 0
    prod_all = prod_sat = 0.0
    games = 0
    for entries in (log or {}).values():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict) or "actual_margin" not in e:
                continue
            wp = e.get("watch_played")
            if not wp:
                continue
            games += 1
            for side in ("home", "away"):
                for item in (e.get("watch_" + side) or []):
                    try:
                        nm, val = item[0], float(item[1])
                    except Exception:
                        continue
                    mins = wp.get(nm)
                    if mins is None:
                        continue
                    n_players += 1
                    prod_all += val
                    if mins > 0.5:
                        n_played += 1
                    else:
                        prod_sat += val
    if not n_players:
        return None
    return {"games": games, "n": n_players, "played": n_played,
            "rate": round(100.0 * n_played / n_players, 1),
            "prod_all": round(prod_all, 1), "prod_sat": round(prod_sat, 1),
            "prod_rate": round(100.0 * prod_sat / prod_all, 1) if prod_all else None}


def _threeway_audit(log):
    """模型 / 市場共識 / 融合，三方的歷史成績對帳。

    這是付費版最有說服力的一段：不是宣稱模型多強，而是把三者
    攤在同一張表上，讓數字自己講話。已結算的場次才算。
    """
    src = {k: {"n": 0, "win": 0, "mae": [], "tae": []}
           for k in ("model", "market", "blend")}
    for entries in (log or {}).values():
        if not isinstance(entries, list):
            continue
        for e in entries:
            _tw_tally(e, src)
    return _tw_finish(src)


def _tw_tally(e, src):
    tw = e.get("three_way") or {}
    for k, acc in src.items():
        hit = tw.get(k + "_win_hit")
        if hit is None:
            continue
        acc["n"] += 1
        acc["win"] += 1 if hit else 0
        me = tw.get(k + "_margin_err")
        te = tw.get(k + "_total_err")
        if me is not None:
            acc["mae"].append(abs(me))
        if te is not None:
            acc["tae"].append(abs(te))


def _noise_band(n):
    """n 場的 95% 雜訊帶。真實機率剛好五成時，觀測值會落在的範圍。

    ⚠️ 與 predictor 的同名函式一致。過盤率的真實偏離只有幾個百分點，
    而 30 場的雜訊帶就有 ±18 個百分點 —— 不把帶子一起顯示的話，
    使用者一定會把小樣本的起伏讀成趨勢。
    """
    if n <= 0:
        return None
    se = 0.5 / math.sqrt(n) * 100
    return round(50 - 1.96 * se, 1), round(50 + 1.96 * se, 1)


def _cover_audit(log):
    """過盤統計。App 只呈現聯盟層級與模型命中，單隊那層不放 ——
    那一層每一格的雜訊帶都在 ±18 個百分點以上，放上去只會被當成選邊依據。
    """
    rows = []
    for date in sorted(log or {}):
        for e in (log[date] if isinstance(log[date], list) else []):
            if isinstance(e, dict) and e.get("home_cover") is not None:
                rows.append(e)
    if len(rows) < 10:
        return None
    n = len(rows)
    out = {"n": n, "band": _noise_band(n),
           "home": sum(1 for e in rows if e["home_cover"])}
    ou = [e for e in rows if e.get("actual_total") is not None
          and e.get("ou_line") is not None]
    if ou:
        out["over"] = sum(1 for e in ou if e["actual_total"] > e["ou_line"])
        out["over_n"] = len(ou)
    for key, short in (("hit_spread", "sp"), ("hit_ou", "ou")):
        sub_rows = [e for e in rows if e.get(key) is not None]
        if sub_rows:
            out[short] = sum(1 for e in sub_rows if e[key])
            out[short + "_n"] = len(sub_rows)
    recent = rows[-10:]
    if len(recent) == 10:
        rs = [e for e in recent if e.get("hit_spread") is not None]
        out["r10"] = {"home": sum(1 for e in recent if e["home_cover"]),
                      "sp": sum(1 for e in rs if e["hit_spread"]),
                      "sp_n": len(rs), "band": _noise_band(10)}
    return out


def _opposite_pick(log):
    """模型與市場共識**看好不同球隊**的場次，各自命中幾場。

    ⚠️ 這是 predictor 的 `_print_opposite_pick()` 的鏡像，判定要一致
    （兩邊預期分差正負號相反）。

    為什麼不能靠分歧大小那張表回答：那張是按絕對值分層的。
    「模型 +8 / 共識 +12」（同向、共識更強烈）跟
    「模型 -4 / 共識 +0.5」（看好不同隊）會落在同一格裡，
    但前者只是強度差異，後者是勝負判斷相反 —— 兩件事混在一起就數不出來。
    """
    n = mw = kw = 0
    gaps = []
    for entries in (log or {}).values():
        if not isinstance(entries, list):
            continue
        for e in entries:
            tw = e.get("three_way") or {}
            mm, km = tw.get("model_margin"), tw.get("market_margin")
            if mm is None or km is None or tw.get("model_win_hit") is None:
                continue
            if (mm > 0) == (km > 0) or abs(mm) <= 0.01 or abs(km) <= 0.01:
                continue
            n += 1
            gaps.append(abs(mm - km))
            mw += 1 if tw.get("model_win_hit") else 0
            kw += 1 if tw.get("market_win_hit") else 0
    if not n:
        return None
    return {"n": n, "model": mw, "market": kw,
            "gap": round(sum(gaps) / len(gaps), 1)}


def _threeway_strata(log):
    """按「模型與市場的分歧大小」分層，看誰在什麼情況下比較準。

    這比一個總平均有價值得多 —— 它直接回答「什麼時候該相信模型、
    什麼時候該讓步給市場」，而那正是融合權重要解決的問題。
    """
    bands = [(0.0, 2.0, "差 2 分內"), (2.0, 4.0, "差 2–4 分"),
             (4.0, 7.0, "差 4–7 分"), (7.0, 999.0, "差 7 分以上")]
    out = []
    for lo, hi, label in bands:
        acc = {k: {"n": 0, "win": 0, "mae": []} for k in ("model", "market", "blend")}
        for entries in (log or {}).values():
            if not isinstance(entries, list):
                continue
            for e in entries:
                tw = e.get("three_way") or {}
                mm, km = tw.get("model_margin"), tw.get("market_margin")
                if mm is None or km is None or tw.get("blend_win_hit") is None:
                    continue
                if not (lo <= abs(mm - km) < hi):
                    continue
                for k, a in acc.items():
                    hit = tw.get(k + "_win_hit")
                    if hit is None:
                        continue
                    a["n"] += 1
                    a["win"] += 1 if hit else 0
                    me = tw.get(k + "_margin_err")
                    if me is not None:
                        a["mae"].append(abs(me))
        n = acc["blend"]["n"]
        if n < 4:          # 樣本太少就不列，免得看起來像結論
            continue
        row = {"band": label, "n": n}
        for k, a in acc.items():
            if a["n"]:
                row[k] = {
                    "win": round(100.0 * a["win"] / a["n"], 1),
                    "mae": round(sum(a["mae"]) / len(a["mae"]), 1) if a["mae"] else None,
                }
        out.append(row)
    return out or None


def _tw_finish(src):
    out = {}
    for k, acc in src.items():
        if acc["n"] < 5:
            continue
        out[k] = {
            "n": acc["n"],
            "win": round(100.0 * acc["win"] / acc["n"], 1),
            "mae": round(sum(acc["mae"]) / len(acc["mae"]), 1) if acc["mae"] else None,
            "tae": round(sum(acc["tae"]) / len(acc["tae"]), 1) if acc["tae"] else None,
        }
    return out or None


def _blend_view(e):
    """市場融合後的預測（付費版用）。這是主程式最終發布的那組數字。"""
    margin = e["home_proj"] - e["away_proj"]
    total = e.get("model_total") or (e["home_proj"] + e["away_proj"])
    p_home = (e["winner_prob"] / 100.0 if e["winner"] == e["home_name"]
              else 1.0 - e["winner_prob"] / 100.0)
    return {"margin": margin, "total": total, "p_home": p_home,
            "home_proj": e["home_proj"], "away_proj": e["away_proj"],
            "std": _implied_std(margin, p_home)}


def _implied_total_std(total, line, over_prob, fallback=20.4, lo=14.0, hi=28.0):
    """
    從「大分機率」反推當時用的總分標準差。

    over_prob = 1 - Φ((line - total)/σ)  →  σ = (total - line) / Φ⁻¹(over_prob)

    ⚠️ line 是市場盤口，只在這裡當中間變數用來還原 σ，
    絕不會出現在輸出裡 —— 送到手機的一律是模型自己的預期總分與機率。
    """
    from statistics import NormalDist
    try:
        z = NormalDist().inv_cdf(min(0.999, max(0.001, over_prob / 100.0)))
        gap = total - line
        if abs(z) > 0.03 and abs(gap) > 0.2:
            return min(max(abs(gap / z), lo), hi)
    except Exception:
        pass
    return fallback


def _margin_thresholds(margin, std, thresholds=(5, 10, 15, 20)):
    """
    勝負幅度：模型看好的那一隊，贏超過 N 分的機率。
    比單一「預期分差 9.6 分」有用得多 —— 它回答的是「這場會不會變成大勝」。
    """
    from statistics import NormalDist
    nd = NormalDist(mu=abs(margin), sigma=std)
    return [{"t": t, "p": round((1.0 - nd.cdf(t)) * 100, 1)} for t in thresholds]


def _total_distribution(total, std, width=10):
    """
    總分落在哪一段。以模型的預期總分為中心，往兩邊各切兩段，
    最外兩段是開放區間。全部用模型自己的數字，不參照任何外部基準線。
    """
    from statistics import NormalDist
    nd = NormalDist(mu=total, sigma=std)
    center = round(total / width) * width
    edges = [center - width * 2, center - width, center, center + width, center + width * 2]
    out = [{"label": "%d 以下" % edges[0],
            "p": round(nd.cdf(edges[0]) * 100, 1)}]
    for i in range(len(edges) - 1):
        out.append({"label": "%d–%d" % (edges[i], edges[i + 1]),
                    "p": round((nd.cdf(edges[i + 1]) - nd.cdf(edges[i])) * 100, 1)})
    out.append({"label": "%d 以上" % edges[-1],
                "p": round((1.0 - nd.cdf(edges[-1])) * 100, 1)})
    return out



def _team_score_distribution(proj, margin_std, total_std, width=10):
    """
    單隊得分的落點分布。

    推導：主隊得分 H = (總分 + 分差)/2，客隊 A = (總分 - 分差)/2。
    把總分與分差視為近似獨立（兩者的相關在籃球裡很弱），
        Var(H) = (σ總分² + σ分差²) / 4
    以 WNBA 的 σ總分≈20.5、σ分差≈13.1 代入，單隊 σ≈12.2 分，
    跟實際球隊單場得分的離散度相符。
    """
    from statistics import NormalDist
    sd = math.sqrt((total_std ** 2 + margin_std ** 2) / 4.0)
    nd = NormalDist(mu=proj, sigma=sd)
    # 以 5 分為刻度取整，讓中間那一段正好包住預期得分
    center = round(proj / 5.0) * 5
    half = width / 2.0
    edges = [center - width - half, center - half, center + half, center + width + half]
    edges = [int(x) for x in edges]
    out = [{"label": "%d 以下" % edges[0], "p": round(nd.cdf(edges[0]) * 100, 1)}]
    for i in range(len(edges) - 1):
        out.append({"label": "%d–%d" % (edges[i], edges[i + 1]),
                    "p": round((nd.cdf(edges[i + 1]) - nd.cdf(edges[i])) * 100, 1)})
    out.append({"label": "%d 以上" % edges[-1],
                "p": round((1.0 - nd.cdf(edges[-1])) * 100, 1)})
    return {"sd": round(sd, 1), "rows": out}


def _norm_cdf(x):
    """標準常態累積分布。與 predictor 的 normal_cdf 同一個定義。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _headline_call(e, b):
    """模型對這場「最有把握的一句話」。

    需求來自：多數人不會逐項看統計，只想要一個明確的結論。
    給一個結論是合理的 —— 但那個結論必須是**對比賽的預測**，
    而且必須帶著它的機率，否則就變成沒有根據的斷言。

    做法是把幾個彼此獨立的陳述（誰獲勝、分差相對市場共識、總分高低）
    各算一次機率，挑最有把握的那個講。挑不出 60% 以上的，
    就誠實說這場沒有把握的說法 —— 那本身也是有用的資訊。
    """
    margin, std = b.get("margin"), b.get("std")
    if margin is None or not std:
        return None
    hn, an = e.get("home_name"), e.get("away_name")
    cands = []
    p_home = 1 - _norm_cdf((0.0 - margin) / std)
    if p_home >= 0.5:
        cands.append((p_home, f"{hn} 會贏", "勝負"))
    else:
        cands.append((1 - p_home, f"{an} 會贏", "勝負"))
    km = ((e.get("three_way") or {}).get("market_margin"))
    if km is not None:
        p = 1 - _norm_cdf((km - margin) / std)
        if p >= 0.5:
            cands.append((p, f"{hn} 的分差會超過市場共識的 {km:+.1f} 分", "分差"))
        else:
            cands.append((1 - p, f"{an} 的表現會優於市場共識的 {-km:+.1f} 分", "分差"))
    ov, line = e.get("over_prob"), e.get("ou_line")
    if ov is not None and line is not None:
        if ov >= 50:
            cands.append((ov / 100.0, f"總分會高於市場共識的 {line:g} 分", "總分"))
        else:
            cands.append((1 - ov / 100.0, f"總分會低於市場共識的 {line:g} 分", "總分"))
    if not cands:
        return None
    cands.sort(reverse=True)
    p, text, kind = cands[0]
    return {"p": round(100 * p, 1), "text": text, "kind": kind,
            "strong": p >= 0.60,
            "others": [{"p": round(100 * q, 1), "text": t}
                       for q, t, _k in cands[1:3]]}


def _cross_table(e):
    """把 predictor 算好的四格聯合機率整理成列聯表。

    這是「哪一隊獲勝」×「總分高於或低於市場共識」的交叉機率，
    四格必然加總 100%。用途是看兩個維度有沒有連動 ——
    例如強隊獲勝時是否傾向高比分。
    """
    cb = e.get("combo")
    line = e.get("ou_line")
    if not isinstance(cb, dict) or line is None:
        return None
    hn, an = e.get("home_name"), e.get("away_name")
    cells = [
        {"w": hn, "t": "高", "p": cb.get("home_over")},
        {"w": hn, "t": "低", "p": cb.get("home_under")},
        {"w": an, "t": "高", "p": cb.get("away_over")},
        {"w": an, "t": "低", "p": cb.get("away_under")},
    ]
    cells = [c for c in cells if c["p"] is not None]
    if len(cells) < 4:
        return None
    for c in cells:
        c["p"] = round(100.0 * c["p"], 1)
    return {"line": line, "cells": cells}


def _outcome_quadrants(margin, std):
    """
    把比賽結果拆成四種情境，機率加總必為 100%。
    比單一「勝率 76.9%」多一層：告訴你贏得會不會辛苦。
    """
    from statistics import NormalDist
    nd = NormalDist(mu=margin, sigma=std)
    # 籃球沒有平手（延長賽會分出勝負），所以用 0 當分界，四格加總剛好 100%
    return [
        {"label": "主隊大勝 10 分以上", "p": round((1 - nd.cdf(10)) * 100, 1)},
        {"label": "主隊小勝 10 分內", "p": round((nd.cdf(10) - nd.cdf(0)) * 100, 1)},
        {"label": "客隊小勝 10 分內", "p": round((nd.cdf(0) - nd.cdf(-10)) * 100, 1)},
        {"label": "客隊大勝 10 分以上", "p": round(nd.cdf(-10) * 100, 1)},
    ]



def _calibration(log, tiers=None):
    rows = [e for day in log.values() for e in day if "hit_winner" in e]
    tiers = tiers or [(50, 60, "五成半上下"), (60, 70, "六成"),
                      (70, 80, "七成"), (80, 101, "八成以上")]
    out = []
    for lo, hi, label in tiers:
        sub = [e for e in rows if lo <= e["winner_prob"] < hi]
        if len(sub) < 3:
            continue
        hit = sum(1 for e in sub if e["hit_winner"])
        out.append({"label": label, "n": len(sub),
                    "said": round(sum(e["winner_prob"] for e in sub) / len(sub), 1),
                    "was": round(100.0 * hit / len(sub), 1)})
    return out, len(rows)


def _by_team(log):
    """
    模型預測某一隊的比賽時準不準。

    這會揭露一件總命中率藏不住的事：模型對某些球隊就是抓不準
    （通常是輪換不穩、或當家球星在傷停邊緣的那幾隊）。
    公開這個對我們不利，但它正是使用者想知道的。
    """
    acc = {}
    for day in log.values():
        for e in day:
            if "hit_winner" not in e:
                continue
            for name in (e["home_name"], e["away_name"]):
                d = acc.setdefault(name, [0, 0])
                d[0] += 1
                d[1] += 1 if e["hit_winner"] else 0
    out = [{"team": k, "n": v[0], "hit": v[1],
            "rate": round(100.0 * v[1] / v[0], 1)}
           for k, v in acc.items() if v[0] >= 4]
    out.sort(key=lambda r: -r["rate"])
    return out


def _total_accuracy(log):
    """
    總分預測的誤差。用模型自己的預期總分當基準，不碰任何外部基準線。
    """
    errs = []
    for day in log.values():
        for e in day:
            if "actual_total" not in e:
                continue
            mt = e.get("model_total")
            if mt is None:
                tw = e.get("three_way") or {}
                mt = tw.get("blend_total") or (e["home_proj"] + e["away_proj"])
            errs.append(abs(e["actual_total"] - mt))
    if len(errs) < 6:
        return None
    return {"n": len(errs), "mae": round(sum(errs) / len(errs), 1),
            "within10": round(100.0 * sum(1 for x in errs if x <= 10) / len(errs), 1)}


def _brier(log):
    """
    Brier score：機率預測的標準評分，越低越好。
    永遠喊 50% 會得到 0.25，所以低於 0.25 才算有資訊。
    這比命中率更嚴格 —— 它會懲罰「講得很有把握卻猜錯」。
    """
    rows = [e for day in log.values() for e in day if "hit_winner" in e]
    if len(rows) < 10:
        return None
    tot = 0.0
    for e in rows:
        p = e["winner_prob"] / 100.0
        tot += (p - (1.0 if e["hit_winner"] else 0.0)) ** 2
    return {"n": len(rows), "score": round(tot / len(rows), 4)}


def _strata(log):
    """
    戰績分層：模型在哪種場合可信、哪種不可信。

    只報一個總命中率是沒有資訊的 —— 使用者想知道的是
    「你看好大熱門的時候準不準」「五五波的場次能不能信」。
    把這個攤開來反而增加可信度，因為它不像在吹。
    """
    rows = [e for day in log.values() for e in day if "hit_winner" in e]
    if len(rows) < 6:
        return []

    def pack(label, sub):
        if len(sub) < 3:
            return None
        hit = sum(1 for e in sub if e["hit_winner"])
        return {"label": label, "n": len(sub),
                "rate": round(100.0 * hit / len(sub), 1)}

    out = [
        pack("看好主隊", [e for e in rows if e["winner"] == e["home_name"]]),
        pack("看好客隊", [e for e in rows if e["winner"] != e["home_name"]]),
        pack("大熱門 65%以上", [e for e in rows if e["winner_prob"] >= 65]),
        pack("勢均力敵 58%以下", [e for e in rows if e["winner_prob"] < 58]),
    ]
    return [x for x in out if x]


def _rolling(log, window=20, step=5):
    """滾動命中率走勢。起伏本身是誠實的訊號，藏起來反而可疑。"""
    rows = []
    for d in sorted(log):
        for e in log[d]:
            if "hit_winner" in e:
                rows.append((d, bool(e["hit_winner"])))
    n = len(rows)
    if n < window + step:
        return []
    out = []
    for i in range(0, n - window + 1, step):
        sub = rows[i:i + window]
        out.append({"at": sub[-1][0][5:].replace("-", "/"),
                    "rate": round(100.0 * sum(1 for _, ok in sub if ok) / window, 1)})
    return out[-8:]



def _restrict_day(games, is_today):
    """把一天的比賽降級成免費版能看的內容。"""
    # 示範場次：當天信心最高的那一場。用固定規則挑，所有人看到的一樣，
    # 重新整理也不會換 —— 隨機挑會讓人以為是 bug。
    feat = None
    if is_today and games:
        feat = max(range(len(games)),
                   key=lambda i: (abs(games[i]["php"] - 50.0), -i))
    out = []
    for i, g in enumerate(games):
        if i == feat:
            g["feat"] = 1
            out.append(g)
            continue
        keep = {
            "h": g["h"], "a": g["a"],
            "pick": g["h"] if g["php"] >= 50 else g["a"],
            "cf": _conf_level(g["php"]),
            "st": g["st"], "ok": g["ok"], "sc": g["sc"],
            "t": g.get("t"), "v": g.get("v"), "ct": g.get("ct"),
            "nt": g.get("nt"), "mb": g.get("mb"),
        }
        if not is_today:
            # 過去的日期連看好誰都不留，只留結果當證據
            keep.pop("pick", None)
            keep.pop("cf", None)
            keep["past"] = 1
        out.append(keep)
    return out


def _conf_level(prob):
    """把勝率壓成信心等級。免費版不給精確數字，只給方向與把握程度。"""
    p = max(prob, 100.0 - prob)
    if p >= 65:
        return 3      # 高
    if p >= 57:
        return 2      # 中
    return 1          # 接近五五


def build_free(log, restrict=False, today=None):
    """所有日期的免費版資料。鍵名刻意縮短，因為要整份塞進 HTML。

    restrict=True 時進入付費牆模式：
      - 每場只留「看好誰 + 信心等級」，拿掉比分、勝率、分差、離散度
      - 當天挑一場完整開放當示範（feat=1）
      - 非當天的日期一律只留結果，不留當初的預測細節
    已結算的比分與命中與否一律保留 —— 那是準確度的證據，也是最好的說服材料。
    """
    days = {}
    newest = max(log) if log else None
    today = today or newest
    for date, entries in log.items():
        games = []
        for e in entries:
            v = _model_view(e)
            g = {
                "h": e["home_name"], "a": e["away_name"],
                "hp": round((v["total"] + v["margin"]) / 2.0, 1),
                "ap": round((v["total"] - v["margin"]) / 2.0, 1),
                "php": round(v["p_home"] * 100, 1),
                "mg": round(v["margin"], 2),
                "sd": round(v["std"], 2),
                "st": 1 if "hit_winner" in e else 0,
                # ok = 實際發布的預測（已合併市場共識）對不對。
                # okm = 純模型自己的判斷對不對。兩者可能相反：合併後 =
                # 0.35×模型 + 0.65×市場，跨過零時看好的隊伍就會換人。
                # 只顯示 ok 會讓人以為模型看錯了，其實是合併把模型的判斷蓋掉。
                "ok": 1 if e.get("hit_winner") else 0,
                "okm": (None if e.get("hit_winner_model") is None
                        else (1 if e["hit_winner_model"] else 0)),
                "sc": ("%s-%s" % (e["actual_away"], e["actual_home"])
                       if "actual_home" in e else ""),
                "mb": 1 if e.get("market_blended") else 0,
                "t": e.get("start_utc"),          # 開賽時間（UTC）
                "v": e.get("venue"), "ct": e.get("city"),
                "nt": 1 if e.get("neutral") else 0,
            }
            # 對戰歷史與近況只掛在最新一天：那是使用者真正會看的，
            # 而且每場都帶一份的話，整季規模會讓 HTML 膨脹好幾倍。
            if date == newest:
                if e.get("h2h"):
                    g["h2h"] = [{"d": r["date"], "h": r["home"], "a": r["away"],
                                 "hp": r["home_pts"], "ap": r["away_pts"]}
                                for r in e["h2h"]]
                for side, key in (("hf", "home_form"), ("af", "away_form")):
                    if e.get(key):
                        g[side] = [{"d": r["date"], "o": r["opp"], "H": 1 if r["home"] else 0,
                                    "p": r["pts"], "op": r["opp_pts"],
                                    "w": 1 if r["win"] else 0} for r in e[key]]
            games.append(g)
        if restrict:
            games = _restrict_day(games, is_today=(date == today))
        if games:
            days[date] = games
    calib, n = _calibration(log)
    return {"days": days, "order": sorted(days, reverse=True),
            # 前端要靠這個才分得出「市場對照被鎖住」還是「就在旁邊那一頁」。
            # 沒有它的話標籤只能寫死，公開模式也會謊稱內容是付費的。
            "locked": 1 if restrict else 0,
            "calibration": calib, "settled": n,
            "strata": _strata(log), "rolling": _rolling(log),
            "brier": _brier(log),
            "by_team": _by_team(log),
            "total_acc": _total_accuracy(log),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "gen_ts": int(datetime.now().timestamp()),
            # 【v1.37】資料本身最後一次改變的時間，跟建置時間分開。
            # 每天有好幾班會重建網站（含只做回填、什麼都沒改的那幾班），
            # 只印建置時間的話，畫面上每次都像更新過，實際上資料沒動。
            "data_ts": _latest_data_ts(log)}


def _latest_data_ts(log):
    """日誌裡最新的 updated_ts。predictor v4.70 起，內容沒變就沿用舊時戳，
    所以這個值代表「資料最後一次真的改變」，不是「最後一次被寫過」。
    舊紀錄沒有這個欄位就回 None，畫面退回只顯示建置時間。"""
    best = None
    for entries in (log or {}).values():
        if not isinstance(entries, list):
            continue
        for e in entries:
            ts = e.get("updated_ts") if isinstance(e, dict) else None
            if isinstance(ts, (int, float)) and (best is None or ts > best):
                best = int(ts)
    return best


def _records_lite(league_table):
    """免費版只要勝敗與場均得失分，一隊一筆就好。"""
    out = {}
    for t in (league_table or {}).get("teams", []):
        if t.get("w") is None:
            continue
        out[t["team"]] = {"w": t["w"], "l": t["l"], "pf": t["pf"], "pa": t["pa"]}
        if t.get("streak"):
            out[t["team"]]["st"] = t["streak"]
        # 【v1.22】數學上已無緣季後賽。純顯示，不影響任何數字。
        if t.get("eliminated"):
            out[t["team"]]["elim"] = True
    return out


def build_premium(log, league_table=None):
    """所有日期的付費版資料。"""
    # 用全聯盟表的節奏推算「這場預計打多少回合」——兩隊節奏的交互作用
    pace_by_team = {}
    lg_pace = None
    if league_table:
        lg_pace = league_table.get("avg_pace")
        for t in league_table.get("teams", []):
            if t.get("pace") is not None:
                pace_by_team[t["team"]] = t["pace"]

    days = {}
    for date, entries in log.items():
        games = []
        for e in entries:
            m, b = _model_view(e), _blend_view(e)
            home_fav = b["margin"] > 0
            exp_poss = None
            hp_, ap_ = pace_by_team.get(e["home_name"]), pace_by_team.get(e["away_name"])
            if hp_ is not None and ap_ is not None and lg_pace:
                exp_poss = round(hp_ + ap_ - lg_pace, 1)
            tstd = _implied_total_std(b["total"], e.get("ou_line") or b["total"],
                                      e.get("over_prob") or 50.0)
            games.append({
                "home": e["home_name"], "away": e["away_name"],
                "blend": {
                    "home_proj": round(b["home_proj"], 1),
                    "away_proj": round(b["away_proj"], 1),
                    "margin": round(b["margin"], 1), "total": round(b["total"], 1),
                    "pick": e["home_name"] if home_fav else e["away_name"],
                    "prob": round((b["p_home"] if home_fav else 1 - b["p_home"]) * 100, 1),
                },
                "shift": round(b["margin"] - m["margin"], 1),
                # 市場對照。刻意不送讓分盤／大小分線本身 —— 顯示可下注的
                # 盤口數字可能被歸類成博弈內容，影響 Play 的內容分級。
                # 這裡只給「模型怎麼被修正、修了多少、分歧多大」。
                "market": _market_view(e),
                "buckets": e.get("bucket_breakdown") or [],
                # 分差六格是「預測方贏這個分差」的聯合機率，加總只等於獲勝機率。
                # 輸球機率一定要一起顯示 —— 少了它，畫面會讓人以為六格就是全部，
                # 看不到「這一注有多少機率直接歸零」。
                "lose": e.get("lose_prob"),
                # 最可能的那一格與其機率。六格都列出來了，但「結論是哪一格」
                # 值得單獨講一次 —— 使用者不該自己去比對六條長條。
                "top": e.get("bucket"),
                "topp": e.get("bucket_prob"),
                "thresholds": _margin_thresholds(b["margin"], b["std"]),
                "totals": _total_distribution(b["total"], tstd),
                "home_scores": _team_score_distribution(b["home_proj"], b["std"], tstd),
                "away_scores": _team_score_distribution(b["away_proj"], b["std"], tstd),
                "quadrants": _outcome_quadrants(b["margin"], b["std"]),
                # 一句話結論。放在最前面，因為多數人只會看這一行。
                "call": _headline_call(e, b),
                # 勝負 × 總分高低 的交叉機率（四格）。predictor 用雙變量常態
                # 算的聯合機率，不是兩個機率相乘 —— 相乘等於假設兩件事獨立。
                "cross": _cross_table(e),
                "margin_sd": round(b["std"], 1),
                "total_sd": round(tstd, 1),
                "exp_poss": exp_poss,
                # 攻守對位：模型的預期得分就是這樣拼出來的，把過程攤開來
                "matchup": {
                    "home_off": e.get("home_off"), "away_def": e.get("away_def"),
                    "away_off": e.get("away_off"), "home_def": e.get("home_def"),
                },
                "ratings": {"home_off": e.get("home_off"), "home_def": e.get("home_def"),
                            "away_off": e.get("away_off"), "away_def": e.get("away_def")},
                "rest": {"home": e.get("home_rest_label"), "away": e.get("away_rest_label")},
                "injuries": {"home": e.get("home_injuries") or [],
                             "away": e.get("away_injuries") or []},
                # 舊紀錄沒有這個欄位，預設 True —— 那些是傷兵端點還正常的時期。
                "injuries_ok": e.get("injuries_ok", True),
                "absence": {"home_n": e.get("absent_home"), "away_n": e.get("absent_away"),
                            "home_lost": e.get("lost_home"), "away_lost": e.get("lost_away")},
                "history": e.get("history") or [],
            })
        if games:
            days[date] = games
    calib, n = _calibration(log, tiers=[(50, 55, "五成"), (55, 60, "五成半"),
                                        (60, 65, "六成"), (65, 70, "六成半"),
                                        (70, 80, "七成"), (80, 101, "八成以上")])
    return {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "days": days, "calibration": calib, "settled": n,
            "threeway": _threeway_audit(log),
            "tw_strata": _threeway_strata(log),
            "opposite": _opposite_pick(log),
            "mktbias": _market_bias(log),
            "watch": _watch_audit(log),
            "cover": _cover_audit(log)}


# ============ 版本 ============
# 改動請一律往上加一版，並在下面補一行說明。
# 版本號會印在建置摘要，也會寫進產生的網頁（檢視原始碼搜 BUILDER_VERSION），
# 所以線上跑的是哪一版可以直接確認，不必猜。
#
#   1.21 「過盤紀錄」改名「超出共識的頻率」。「過盤」是 cover the spread
#        的中文投注用語，是詞彙對照表唯一漏掉的一項，而且出現在分頁標題，
#        商店截圖會拍到。表格內容本身的用詞原本就是乾淨的。
#   1.16 模型頁新增「過盤紀錄」分頁（同步 predictor v4.23）。每一列都帶
#        95% 雜訊帶；單隊那層刻意不放，那層的雜訊帶都在 ±18 個百分點以上。
#   1.18 預測摘要最前面加「這場最有把握的判斷」一句話結論。
#        從勝負／分差／總分三個陳述裡挑機率最高的，機率一起顯示。
#        不到六成就改講「這場沒有把握的判斷」並說明原因 ——
#        給結論可以，但結論必須帶著它的不確定性。
#   1.17 機率分布頁新增「勝負 × 總分」交叉機率（同步 predictor v4.29）。
#   1.16 模型頁新增「過盤紀錄」分頁
#   1.15 「市場對照為付費內容」原本無條件顯示（條件只有 mb，而 mb 的意思是
#        「這場有融合市場資料」，跟付費無關）。公開模式下等於謊稱內容被鎖。
#        改由新的 locked 旗標決定，公開模式顯示「另有市場對照」。
#        同時撤掉 1.14 —— 那版修的是不存在的問題（build_free 內部本來就有
#        `today = today or newest`）。仍保留明確傳 today，理由見呼叫端註解。
#   1.14 build_free 補傳 today —— 少傳時「當天免費示範一場」永遠不觸發，
#        付費牆模式下會變成每一場都上鎖。公開模式測不出來。
#   1.13 修掉 1.12 多打的一個大括號 —— 該版整支前端 JS 解析失敗、整頁不動。
#        Python 語法檢查看不到字串裡的 JS，之後改前端一律用 node --check 驗。
#   1.12 分歧分層下面加「看好不同球隊時」（模型與共識指向相反的場次單獨統計）
#   1.11 命中率標明是合併後的口徑；合併前後看好不同隊時在該場註明
#   1.10 狀態未定球員的歷史出賽率
#   1.9  狀態未定名單與「若全部缺陣」的分差區間
#   1.8  模型與共識的總分系統性落差；修掉市場對帳讀到合併後總分的問題
#   1.7  市場對照加上「市場共識本身準不準」（與本機報告的市場偏誤對帳同步）
#   1.6  補上所有抓到但沒顯示的資料：防守四要素、回測偏誤與平方平均誤差、
#        市場隱含的分差離散度
#   1.5  分格提到贏球幅度之前、標出最可能的那一格、加上「最可能」結論列
#   1.4  分差機率改為聯合機率（配合 predictor v4.10），加上「未獲勝」機率
#   1.3  日期改成月份＋日期下拉、市場對照分頁、三方對帳與分歧分層
#   1.2  付費牆（信心等級、每日一場示範、歷史只留結果）
#   1.1  購買流程、解鎖診斷、service worker 改 stale-while-revalidate
#   1.0  免費版／付費版分離，付費內容改由 Worker 驗證後發送
VERSION = "1.40"

# 更新紀錄。顯示在「關於」分頁最下面，讓使用者知道版本號與改了什麼。
#
# 寫法規則：
#   · 用使用者看得懂的話，不要寫函式名或欄位名
#   · 一律遵守既有的詞彙表（市場共識、綜合來源家數…），不出現博弈語彙
#   · 只列使用者感覺得到的改動；純內部重構不必列
#   · 最新的排最上面
CHANGELOG = [
    # ⚠️ 這裡的日期是**改版日**，不是資料日期。2026-08-30 這批原本
    #    全被寫成 "2026-08-23"（那是當時報告裡的比賽日期），畫面上看起來
    #    像同一天發了六個版本。
    #    另外 1.32~1.37 是同一段連續作業，對使用者來說拆成六條只是雜訊，
    #    合併成一條。
    ("2026-08-30", "1.40", "4.74", [
        "預測摘要整頁改用同一套機率：先前最上面的結論是網頁自己重算的，"
        "跟下方的命中機率不同源，同一件事會出現兩個數字（最多差 2.8 個百分點）。"
        "現在一律以命中機率為準，用詞也統一",
    ]),
    ("2026-08-30", "1.39", "4.73", [
        "「各種判斷的命中機率」移到預測摘要、「合併區間」移到機率分布 ——"
        "市場對照那頁該講的是模型與市場的差別，其他內容各歸各位",
    ]),
    ("2026-08-30", "1.38", "4.72", [
        "市場對照新增「長期缺陣」：確定缺陣但已久到球隊評分本身就反映了的"
        "球員，先前完全不顯示，看起來像漏算。現在會列出來並說明為什麼不重複扣分",
        "市場對照新增「合併區間」：領先 1~10 分／超過 10 分／超過 15 分的機率。"
        "單一分差格的機率天生就低，合併之後才看得出傾向",
        "市場對照新增「各種判斷的命中機率」，並標明那是合併市場共識後的結果",
        "模型預期總分旁新增「其中實測校準修正」：那個數字一直含有依實測"
        "校準出來的修正值，先前沒有說明",
    ]),
    ("2026-08-30", "1.37", "4.70", [
        "「更新於」先前顯示的是網頁產生的時間，而網站每天會重新產生好幾次，"
        "即使預測內容一個數字都沒變。現在改成顯示資料本身最後變動的時間；"
        "若網頁比資料新，會另外註明期間資料沒有變動",
        "「綜合來源家數」「與共識分歧較大」「分差／總分共識自首次收錄以來的"
        "變動」這幾項先前因資料未存檔而始終不顯示，現已修復",
        "共識變動改為至少要有 4 個綜合來源家數才計算：先前少數來源時，"
        "單一來源報錯就會算出十幾二十分的誇張變動，並誤觸「變動劇烈」提示。"
        "來源不足時該項不顯示，而不是顯示一個不可靠的數字",
        "「共識變動劇烈」提示現在也會看總分共識的變動；先前只看分差共識",
        "「模型與市場共識的落差近期擴大中」先前是拿後半段（約 40 場、"
        "橫跨三週）跟全樣本比，一個早已過去的高峰會讓它一直亮著。"
        "改成看最近 20 場，並要求差距超出誤差範圍才顯示",
        "單隊缺陣產能損失超出校準範圍時會標示，提醒該場的缺陣修正是外推值",
    ]),
    ("2026-08-23", "1.30", "4.55", [
        "新增這個「更新紀錄」區塊，同時標示網頁與預測引擎的版本",
        "修正市場共識資料的一個重大問題：先前在比賽開打後才更新的場次，"
        "會誤把「場中即時變動共識」當成賽前共識收進來。已回頭清除受影響的 22 場，"
        "並加上防止再次發生的檢查",
    ]),
    ("2026-08-22", "1.29", "4.53", [
        "修正窄螢幕上比分被切掉、以及表格把整頁推寬的問題",
        "「分享這場」改成產生圖卡，圖上會一併標示分差的可能範圍",
    ]),
    ("2026-08-21", "1.26", "4.51", [
        "傷勢部位的中英對照補齊（含複數寫法），不再出現英文原文",
        "調整行距與換行，長隊名不再和數值擠在一起",
    ]),
    ("2026-08-20", "1.25", "4.48", [
        "新增：若傷缺球員全部缺陣，預期總分會移動多少"
        "（統計上顯著時才顯示，測不出來就不列）",
        "更新時間改用你所在時區顯示",
    ]),
    ("2026-08-19", "1.24", "4.47", [
        "修正季後賽資格判定：已打完賽季但追不上的球隊，先前標記會消失",
        "新增市場內容 「超出共識的頻率」",
    ]),
    ("2026-08-18", "1.22", "4.41", [
        "球員傷缺狀態與部位改為中文顯示",
        "數學上已無緣季後賽的球隊會加註標示",
        "修正季中交易後的名單歸屬：被交易的球員原本仍掛在原球隊，"
        "導致陣容深度與缺陣影響都算錯",
    ]),
    ("2026-08-17", "1.20", "4.38", [
        "傷缺資料當時未取得時，明確標示該場預測未計入球員缺陣",
        "待觀察名單改為只列真正未定的球員，並依「越可能不打」排序",
    ]),
]

# Play Console 上建立的內購商品 ID。
# 要跟 Worker 的 wrangler.toml 裡 PRODUCT_IDS 完全一致，
# 不然買得成功但驗證查不到，內容還是解不開。
PRODUCT_ID = "premium_lifetime"

# 購買流程測試面板可以試買的商品。
# 這個面板平常不存在於畫面上：要在「關於」分頁的版本說明那行連點五下才會出現，
# 或在桌面瀏覽器的網址後面加 #buytest。一般使用者不會誤觸。
# 正式版要不要留著都可以 —— 它只是多幾個按鈕，不影響任何既有流程。
TEST_SKUS = ["daily_pass", "premium_lifetime"]

CSS = """
:root{
  --ink:%(ink)s;--surface:%(surface)s;--surface2:%(surface2)s;--line:%(line)s;
  --home:%(home)s;--away:%(away)s;--chalk:%(chalk)s;--muted:%(muted)s;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --cjk:"PingFang TC","Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%%}
body{background:var(--ink);color:var(--chalk);font-family:var(--cjk);
  font-size:15px;line-height:1.5;padding:0 0 48px;max-width:520px;margin:0 auto;
  -webkit-font-smoothing:antialiased}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.02em}

header{padding:22px 18px 12px}
.eyebrow{font-size:11px;letter-spacing:.18em;color:var(--muted);text-transform:uppercase}
h1{font-size:25px;font-weight:800;letter-spacing:-.02em;margin-top:5px}
h1 .d{font-family:var(--mono);color:var(--home)}
.sub{color:var(--muted);font-size:13px;margin-top:4px}

/* 日期選擇：月份 + 日期兩個下拉，兩側是前後天的箭頭。
   原本是橫向捲動的日期籤，賽季累積下來要滑很久，
   而且每一格的命中紀錄常常滑過頭就看不到了。 */
.dbar{display:flex;gap:7px;align-items:center;padding:12px 18px 14px;
  border-bottom:1px solid var(--line)}
.dbar select{min-width:0;padding:9px 8px;border-radius:8px;background:var(--surface);
  border:1px solid var(--line);color:inherit;font-size:13px;font-weight:600;
  font-family:var(--mono);cursor:pointer;max-width:100%%}
.dbar select#dsel{flex:1}
.dbar select#msel{flex:none}
.dbar .nav{flex:none;width:32px;height:34px;padding:0;border-radius:8px;
  background:var(--surface);border:1px solid var(--line);color:var(--muted);
  cursor:pointer;font-size:15px;line-height:1;font-family:var(--mono)}
.dbar .nav:disabled{opacity:.3;cursor:default}

.receipt{margin:14px 18px 0;padding:11px 13px;background:var(--surface);
  border:1px solid var(--line);border-radius:9px;font-size:12.5px;color:var(--muted)}
.receipt b{color:var(--chalk);font-weight:600}
.receipt .num{color:var(--home)}
.receipt.warn{border-color:var(--home)}

section{margin-top:24px}
.shead{display:flex;align-items:baseline;gap:9px;padding:0 18px 9px;
  border-bottom:1px solid var(--line)}
.shead h2{font-size:13px;font-weight:700;letter-spacing:.04em}
.shead span{font-size:11.5px;color:var(--muted)}

.card{margin:12px 18px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;overflow:hidden}
.teams{padding:15px 15px 4px}
/* 【v1.29】賽事列的元素很多：圖示、星號、隊名、無緣季後賽、連敗、戰績、比分。
   隊名沒設 min-width:0 時，flex 子項不會縮到內容寬度以下，
   於是整列被撐爆，最右邊的比分被推出畫面（2026-08-18 實際發生）。
   允許換行，並讓比分固定不縮。 */
.row{display:flex;align-items:center;flex-wrap:wrap;gap:4px 8px;padding:5px 0}
.dot{width:3px;height:19px;border-radius:2px;flex:none}
.row.a .dot{background:var(--away)}.row.h .dot{background:var(--home)}
.tname{flex:1 1 auto;min-width:0;font-size:15.5px;font-weight:600;
  letter-spacing:-.01em;overflow-wrap:anywhere}
.tscore{font-size:19px;font-weight:700;flex:none;margin-left:auto;
  padding-left:2px}
.row.a .tscore{color:var(--away)}.row.h .tscore{color:var(--home)}

.bar{display:flex;height:5px;margin:11px 15px 0;border-radius:3px;overflow:hidden;
  background:var(--surface2)}
.bar i{display:block;height:100%%}
.bar .ba{background:var(--away)}.bar .bh{background:var(--home)}
.barlab{display:flex;justify-content:space-between;padding:6px 15px 0;
  font-size:11.5px;color:var(--muted)}

.curve{display:block;width:100%%;height:74px;margin-top:6px}
.axis{display:flex;justify-content:space-between;padding:0 15px 12px;
  font-size:10.5px;color:var(--muted)}

.meta{display:flex;border-top:1px solid var(--line);background:var(--surface2)}
.meta div{flex:1;padding:10px 12px;border-right:1px solid var(--line)}
.meta div:last-child{border-right:0}
.mk{font-size:10.5px;color:var(--muted);letter-spacing:.03em}
.mv{font-size:14.5px;font-weight:700;margin-top:2px}

.tag{display:inline-block;margin:0 15px 13px;padding:3px 9px;border-radius:5px;
  font-size:11.5px;font-weight:600}
.tag.close{background:rgba(124,145,159,.16);color:var(--muted)}
.tag.strong{background:rgba(233,161,59,.14);color:var(--home)}

table{width:100%%;border-collapse:collapse;font-size:13px}
/* 【v1.20】左右內距原本 18px，四欄就吃掉 144px。加上「主隊超出市場共識分差」
   這種長標題，整個表格比手機視窗寬 —— 表格撐寬文件之後整頁都能左右捲，
   看起來像所有段落都被切掉（其實是被拖著偏移）。縮內距 + 首欄允許斷行。 */
th,td{padding:9px 8px;text-align:right;border-bottom:1px solid var(--line)}
/* 【v1.29】原本用「 ←」標示超出雜訊帶，那兩個字元會把整欄撐寬 ——
   表格一超過視窗寬，整份文件就能左右捲，看起來像所有段落都被切掉。
   改用底色標示，不占任何寬度。 */
/* 用 td.outb 提高特異性 —— 單獨的 .outb 會被後面的 .mut 覆蓋掉顏色。 */
td.outb{color:var(--home);background:rgba(233,161,59,.14);border-radius:3px}
.opts{margin:6px 0 2px}
.optrow{padding:3px 0;font-size:13px;line-height:1.5}
/* 更新紀錄 */
.chg{margin:10px 0 0;padding:9px 0 0;border-top:1px solid rgba(42,59,73,.55)}
.chg .chgv{font-weight:700;font-size:13px;color:var(--chalk)}
.chg .chgd{margin-left:8px;font-weight:400;font-size:11.5px;color:var(--muted)}
.chg ul{margin:5px 0 0;padding-left:19px}
.chg li{font-size:12.5px;color:var(--muted);line-height:1.75}
th:first-child,td:first-child{text-align:left;overflow-wrap:anywhere}
th{font-size:11px;color:var(--muted);font-weight:500;letter-spacing:.03em}
td.said{color:var(--muted)}td.was{color:var(--home);font-weight:700}

.deep{border-top:1px solid var(--line);background:var(--surface2);padding:13px 15px}
.dtitle{font-size:11px;letter-spacing:.1em;color:var(--muted);margin:16px 0 6px}
.dtitle:first-child{margin-top:0}
/* 【v1.26】中文字比拉丁字母高，5px 上下內距在手機上看起來黏在一起。
   放寬到 7px，並把行高定住，讓折行的列與單行的列節奏一致。 */
/* 【v1.26】允許換行：標籤很長時（攻守對位那兩列是「客隊名 進攻 vs 主隊名
   防守」），數值會整塊掉到下一行並靠右，標籤因此拿得到整行寬度，
   不會被擠成三行、最後一行只剩一個字。短的列不受影響，照樣同一行。 */
.krow{display:flex;flex-wrap:wrap;justify-content:space-between;
  gap:2px 12px;padding:7px 0;
  font-size:13px;line-height:1.65;border-bottom:1px solid rgba(42,59,73,.55)}
.krow span{flex:1 1 auto}
.krow:last-child{border-bottom:0}
/* 【v1.20】標籤原本是 flex:none —— 永遠不縮。攻守對位那兩列的標籤是
   「客隊名 進攻 vs 主隊名 防守」，兩個長隊名就 45 字元，於是右邊的 <b>
   被壓到剩幾像素，數值一個字一個字直向折出畫面。
   改成可縮（拿掉 flex:none）+ min-width:0（否則 flex 子項不會縮到內容寬以下）
   + 長字可斷行；數值那側改成不縮不折，保持完整一行。 */
.krow span{color:var(--muted);min-width:0;overflow-wrap:anywhere}
/* 【v1.26】v1.20 把整個 <b> 設成 nowrap，是為了防止數值被一個字一個字
   拆開直向排列。但有些數值含隊名（例如「Golden State Valkyries 63.5%%」），
   整串不准換行就會把那一列撐到貼齊邊緣、跟標籤擠在一起。
   改成：<b> 本身可以換行，但裡面的數字（.num）不拆 —— 兩個需求都滿足。 */
.krow b{font-weight:700;text-align:right;flex:0 0 auto;margin-left:auto;
  max-width:100%%;overflow-wrap:anywhere}
.krow b .num,.krow b.num{white-space:nowrap}
.bk{display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12.5px}
.bk .bl,.bk .bl2{color:var(--muted)}
/* 【v1.20】兩個都是硬寬度，內容塞不下時會直接畫到框外。加裁切。 */
.bk .bl{width:66px;flex:none;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.bk .bl2{width:92px;flex:none;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.bk .bb{flex:1;height:6px;background:rgba(42,59,73,.7);border-radius:3px;overflow:hidden}
.bk .bb i{display:block;height:100%%;background:var(--home)}
/* 【v1.26】沒有進度條那幾列，數值會卡在中間、右邊留一大塊空白，
   跟上下的 .krow 對不齊。margin-left:auto 把它推到最右邊。 */
.bk .bv{width:46px;text-align:right;margin-left:auto}
.inj{font-size:12.5px;color:var(--muted);line-height:1.75}
.inj b{color:var(--chalk);font-weight:600}

.lock{margin:12px 18px;padding:18px 16px;background:var(--surface);
  border:1px dashed var(--line);border-radius:12px}
.lock h3{font-size:15px;margin-bottom:6px}
.lock p{font-size:12.5px;color:var(--muted);line-height:1.7}
.lock ul{list-style:none;margin:11px 0 0;font-size:12.5px;color:var(--muted)}
.lock li{padding:3px 0 3px 16px;position:relative}
.lock li:before{content:"·";position:absolute;left:5px;color:var(--home)}
.callbox{margin:14px 15px 4px;padding:14px 16px;border-radius:12px;
  background:var(--surface2);border:1px solid var(--line)}
.callbox.hot{border-color:var(--home)}
.calltag{font-size:11px;letter-spacing:.08em;color:var(--muted);margin-bottom:6px}
.calltext{font-size:17px;line-height:1.45;font-weight:600;color:var(--chalk)}
.callp{margin-top:8px;font-size:12px;color:var(--muted)}
.callbox.hot .callp .num{color:var(--home);font-size:15px}
.badge.lockb{color:var(--home);border-color:var(--home);opacity:.85}
.cmp{width:100%%;border-collapse:collapse;margin:10px 0 4px;font-size:13px}
.cmp th,.cmp td{padding:7px 5px;border-bottom:1px solid var(--line);text-align:right;
  font-family:var(--mono)}
.cmp th:first-child,.cmp td:first-child{text-align:left;color:var(--muted);
  font-family:var(--cjk);font-weight:400}
.cmp thead th{color:var(--muted);font-size:11px;font-weight:600;font-family:var(--cjk)}
.cmp tbody tr:last-child td{border-bottom:0}
.cmp .best{color:var(--home)}
.dsub{margin:18px 0 2px;font-size:13px;font-weight:700;color:var(--chalk)}
.mut{color:var(--muted);font-weight:400;font-size:12px}
.pick1{margin:10px 0 6px;padding:9px 11px;border-radius:8px;
  background:rgba(233,161,59,.12);border:1px solid var(--home);
  font-size:13px;font-weight:700}
.bk.hot .bl2{color:var(--home);font-weight:700}
.bk.hot .bb i{background:var(--home)}
.lockrow{margin-top:10px;padding:9px 10px;border:1px dashed var(--line);
  border-radius:8px;font-size:12px;color:var(--muted);text-align:center}
.buy{display:block;width:100%%;margin-top:15px;padding:12px 10px;border:0;
  border-radius:9px;background:var(--home);color:#0E151C;
  font-size:14px;font-weight:700;letter-spacing:.02em;cursor:pointer}
.buy:disabled{opacity:.55}
.diag{display:none;margin-top:12px;padding:8px 9px;border-radius:7px;
  background:rgba(255,255,255,.05);font-size:11px;line-height:1.7;
  color:var(--muted);word-break:break-all}
.buymsg{margin-top:9px;min-height:16px;font-size:12px;
  color:var(--muted);text-align:center;line-height:1.6}
.trow{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.trow .buy{margin-top:8px;padding:10px 6px;font-size:13px}
.tlog{display:block;white-space:pre-wrap;font-family:var(--mono);
  min-height:80px;color:var(--chalk)}
#tsku{width:100%%;margin-top:10px;padding:9px 10px;border-radius:8px;
  border:1px solid var(--line);background:var(--surface2);
  color:var(--chalk);font-size:13px}

.lg{width:22px;height:22px;object-fit:contain;flex:none;border-radius:3px}
.ginfo{padding:10px 15px 0;font-size:11.5px;color:var(--muted);
  display:flex;flex-wrap:wrap;align-items:center}
.rec{font-size:11px;color:var(--muted);flex:none;margin-right:2px}
.tname.tap{cursor:pointer;border-bottom:1px dotted rgba(124,145,159,.5)}
.thead{display:flex;align-items:center;gap:10px;margin:14px 18px 0;padding:10px 12px;
  background:var(--surface);border:1px solid var(--line);border-radius:9px;
  font-size:12.5px;color:var(--muted)}
.thead b{color:var(--home)}
.back{background:none;border:1px solid var(--line);color:var(--chalk);
  border-radius:7px;padding:5px 9px;font-size:12px;cursor:pointer;font-family:inherit}
.gdate{margin:16px 18px -4px;font-size:11.5px;color:var(--muted);letter-spacing:.05em}

.form{display:flex;align-items:center;gap:5px;flex-wrap:wrap;padding:2px 0 4px}
.fm{display:inline-block;width:22px;text-align:center;font-size:11px;font-weight:700;
  padding:2px 0;border-radius:4px}
.fm.w{background:rgba(233,161,59,.18);color:var(--home)}
.fm.l{background:rgba(124,145,159,.14);color:var(--muted)}
.fmn{font-size:11px;color:var(--muted);margin-left:4px}
.spark{display:block;width:calc(100%% - 36px);height:52px;margin:12px 18px 0}

/* 主分頁：固定在頂端，五個並排 */
.tabs{display:flex;position:sticky;top:0;z-index:20;background:var(--ink);
  border-bottom:1px solid var(--line)}
.tab{flex:1;padding:11px 4px 9px;text-align:center;font-size:12.5px;font-weight:600;
  color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;
  transition:color .12s,border-color .12s;-webkit-tap-highlight-color:transparent}
.tab.on{color:var(--home);border-bottom-color:var(--home)}
.tab small{display:block;font-size:9.5px;font-weight:400;margin-top:1px;opacity:.7}

/* 子分頁：橫向捲動的膠囊 */
.subs{display:flex;gap:6px;overflow-x:auto;padding:12px 18px 2px;
  scrollbar-width:none;-webkit-overflow-scrolling:touch}
.subs::-webkit-scrollbar{display:none}
.sub{flex:none;padding:6px 12px;border-radius:14px;font-size:12px;cursor:pointer;
  background:var(--surface);border:1px solid var(--line);color:var(--muted)}
.sub.on{background:rgba(233,161,59,.14);border-color:var(--home);color:var(--home)}

/* 資料過期警示：付費使用者看到舊資料而不自知，是會退款的等級 */
.stale{margin:12px 18px 0;padding:10px 12px;border-radius:9px;font-size:12.5px;
  background:rgba(233,161,59,.12);border:1px solid var(--home);color:var(--chalk)}
.fresh{margin:12px 18px 0;font-size:11.5px;color:var(--muted);text-align:right}

.star{cursor:pointer;font-size:13px;color:var(--line);flex:none;padding:0 4px;
  -webkit-tap-highlight-color:transparent}
.star.on{color:var(--home)}
/* 【v1.26】徽章在賽事列裡是 flex 子項，隊名一長就被壓扁。
   flex:none 保住寬度；align-self 讓它跟長隊名的第一行對齊，
   不會因為隊名折兩行而浮在中間。 */
.badge{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:4px;
  font-size:10px;font-weight:600;background:rgba(124,145,159,.16);color:var(--muted);
  flex:none;align-self:center;white-space:nowrap}
.badge.hi{background:rgba(233,161,59,.16);color:var(--home)}
.actions{display:flex;gap:8px;padding:0 15px 13px}
.act{flex:1;padding:8px;border-radius:8px;text-align:center;font-size:12px;
  background:var(--surface2);border:1px solid var(--line);color:var(--muted);
  cursor:pointer;-webkit-tap-highlight-color:transparent}
.act:active{background:rgba(233,161,59,.1)}
.sum{margin:12px 18px;padding:13px 14px;background:var(--surface);
  border:1px solid var(--line);border-radius:11px}
.sum .sv{display:flex;justify-content:space-between;padding:5px 0;font-size:13px}
.sum .sv span{color:var(--muted)}
.sum .sv b{color:var(--home);font-family:var(--mono)}
.upd{position:fixed;left:50%%;transform:translateX(-50%%);bottom:18px;z-index:60;
  background:var(--home);color:#12181f;font-weight:700;font-size:12.5px;
  padding:9px 16px;border-radius:20px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.4)}

/* 下拉選單：選項多的時候比橫向捲動的膠囊好用 —— 一眼看到全部，不用左右滑 */
.dd{margin:12px 18px 0;position:relative}
.dd:after{content:"▾";position:absolute;right:13px;top:50%%;transform:translateY(-50%%);
  color:var(--home);pointer-events:none;font-size:12px}
.dd select{width:100%%;appearance:none;-webkit-appearance:none;
  background:var(--surface);color:var(--chalk);border:1px solid var(--line);
  border-radius:9px;padding:11px 34px 11px 13px;font-size:13.5px;
  font-family:inherit;cursor:pointer}
.dd select:focus{outline:none;border-color:var(--home)}
.dd label{display:block;font-size:10.5px;letter-spacing:.08em;color:var(--muted);
  margin-bottom:5px;padding-left:2px}

.hint{margin:12px 18px 0;font-size:12px;color:var(--muted);line-height:1.7}
.empty{margin:26px 18px;padding:22px 16px;text-align:center;font-size:13px;
  color:var(--muted);border:1px dashed var(--line);border-radius:11px}
.about{margin:16px 18px;font-size:13.5px;line-height:1.85;color:#C6D3DB}
.about h3{font-size:14px;color:var(--home);margin:20px 0 6px}
.about h3:first-child{margin-top:0}
.about p{margin-bottom:10px}

footer{margin:30px 18px 0;padding-top:16px;border-top:1px solid var(--line);
  font-size:11.5px;color:var(--muted);line-height:1.65}
footer b{color:#9db0bd;font-weight:600}
"""

JS = r"""
var LOCKED = __LOCKED__;
var DATA   = __DATA__;
var TODAY  = "__TODAY__";
var BUILD  = "__BUILD__";
var BUILDER_VERSION = "__BVER__";   /* 產生這頁的 app_builder 版本 */
var CHANGELOG = __CHANGELOG__;      /* 更新紀錄，由 Python 端的 CHANGELOG 注入 */
var API    = "__API__";
var PREM   = null;

/* ── 介面狀態 ──
   TAB   目前主分頁
   SUB   每個主分頁各自記住自己的子分頁，切回來時不會跳掉
   cur   賽事分頁選的日期；curTeam 有值時改成看某一隊
   gi    解析分頁選的是第幾場 */
var TAB = "games";
var SUB = {deep: "sum", teams: "rating", model: "calib"};
var ROTTEAM = null;   /* 陣容分頁目前選的球隊 */
var cur = (function(){
  /* 預設顯示「正在進行或即將開打」的那一天。
     TODAY 是產生網頁當下的賽事日（賽事日在台灣時間 18:00 換日），
     以它為錨點，不要靠掃描日期猜 —— 舊日期只要有一場沒回填到結果，
     用猜的就會被拉回過去卡住。
     今天還有沒打完的 → 看今天；今天全打完了 → 跳到預抓的下一個賽事日。 */
  var o = DATA.order, t = DATA.days[TODAY];
  if (t && t.length) {
    for (var i = 0; i < t.length; i++) if (!t[i].st) return TODAY;
  }
  var next = null;
  for (var j = 0; j < o.length; j++) {
    if (o[j] > TODAY && (next === null || o[j] < next)) next = o[j];
  }
  if (next) return next;
  return (t && t.length) ? TODAY : o[0];
})();
var curTeam = null;
var gi = 0;

function esc(s){
  return String(s==null?"":s).replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];
  });
}
function sign(v){ return (v>0?"+":"") + v; }
function sign1(v){ return v==null ? "\u2014" : (v>0?"+":"") + v.toFixed(1); }
function num(v,d){ return v==null ? "\u2014" : v.toFixed(d==null?1:d); }
function $(id){ return document.getElementById(id); }

/* localStorage 在某些隱私模式下會丟例外，全部包起來，壞掉就當作沒有。 */
function lsGet(k,d){
  try{ var v=localStorage.getItem(k); return v==null?d:JSON.parse(v); }
  catch(e){ return d; }
}
function lsSet(k,v){ try{ localStorage.setItem(k,JSON.stringify(v)); }catch(e){} }

var FAV = lsGet("fav_teams", []);
function isFav(n){ return FAV.indexOf(n)>=0; }
function toggleFav(n){
  var i=FAV.indexOf(n);
  if(i>=0) FAV.splice(i,1); else FAV.push(n);
  lsSet("fav_teams",FAV);
}

/* 資料新鮮度。Actions 壞掉時網站會停在舊檔，
   使用者看到「7/25 三場」會以為是今天的 —— 這裡明講。 */
/* 【v1.24】更新時間改用使用者當地時區顯示。
   `generated` 是在 GitHub Actions 上用 datetime.now() 產生的，
   runner 的時區是 UTC —— 直接顯示那個字串等於給台灣使用者看 UTC 時間，
   會少 8 小時。2026-08-12 就發生過：比賽台灣 10:00 開打、
   排程在開打前半小時（UTC 01:30）正確觸發，畫面卻寫「01:31」，
   看起來像半夜跑的。
   gen_ts 本來就是 epoch，直接拿它格式化即可，不必改資料結構。 */
function genLocal(){
  if(!DATA.gen_ts) return esc(DATA.generated||"");
  var d=new Date(DATA.gen_ts*1000);
  var p=function(n){ return (n<10?"0":"")+n; };
  return (d.getMonth()+1)+"/"+d.getDate()+" "+p(d.getHours())+":"+p(d.getMinutes());
}

/* 【v1.37】把「資料更新」和「頁面產生」分開。
   先前只有建置時間，而網站每天會重建好幾次（含只做結果回填、
   預測一個數字都沒動的那幾班），畫面上每次都寫「更新於 07:36」，
   看起來像有新東西，實際上資料還是 01:34 那份。 */
function tsLocal(ts){
  var d=new Date(ts*1000);
  var p=function(n){ return (n<10?"0":"")+n; };
  return (d.getMonth()+1)+"/"+d.getDate()+" "+p(d.getHours())+":"+p(d.getMinutes());
}
/* 【v1.38】把 predictor 存的結構化資料組成中性描述。
   報告端的標籤是投注術語（獨贏／讓／受讓／大分／小分／勝分差），
   照搬過來會違反詞彙表，所以兩邊各自組字。 */
function optText(o){
  var t=o.t||"", l=o.l;
  if(o.k==="win")       return t+" 獲勝";
  if(o.k==="lead_by")   return t+" 領先超過 "+num(l)+" 分";
  if(o.k==="within")    return t+" 獲勝，或落後在 "+num(l)+" 分以內";
  if(o.k==="over")      return "總分高於 "+num(l)+" 分";
  if(o.k==="under")     return "總分低於 "+num(l)+" 分";
  if(o.k==="win_over")  return t+" 獲勝且總分高於 "+num(l)+" 分";
  if(o.k==="win_under") return t+" 獲勝且總分低於 "+num(l)+" 分";
  if(o.k==="bucket")    return "勝分差落在 "+esc(String(l));
  return "";
}
function freshness(){
  var dts=DATA.data_ts||DATA.gen_ts;
  if(!dts) return "";
  var hrs=(Date.now()/1000 - dts)/3600;
  if(hrs>26){
    return '<div class="stale">⚠️ 這份資料更新於 <b>'+tsLocal(dts)+
      '</b>，已經超過 '+Math.floor(hrs/24)+' 天沒有更新。'+
      '可能是資料來源暫時中斷，請稍後再看。</div>';
  }
  var s='<div class="fresh">資料更新於 '+tsLocal(dts);
  /* 頁面比資料新超過 5 分鐘，才值得分開講 —— 兩者相近時多印一行只是雜訊 */
  if(DATA.data_ts && DATA.gen_ts && DATA.gen_ts - DATA.data_ts > 300){
    s+='　（本頁產生於 '+tsLocal(DATA.gen_ts)+'，期間資料沒有變動）';
  }
  return s+'</div>';
}

/* 模型對這場有多確定：用預期分差相對於不確定範圍的比例 */
/* 開賽時間：一個賽事APP不顯示幾點開打是說不過去的。
   ESPN 給的是 UTC，這裡轉成使用者裝置的當地時間。 */
function tipoff(iso){
  if(!iso) return "";
  try{
    var d=new Date(iso);
    if(isNaN(d.getTime())) return "";
    var hh=("0"+d.getHours()).slice(-2), mm=("0"+d.getMinutes()).slice(-2);
    var today=new Date();
    var same=d.toDateString()===today.toDateString();
    return (same?"":(d.getMonth()+1)+"/"+d.getDate()+" ")+hh+":"+mm;
  }catch(e){ return ""; }
}

/* 隊徽與代表色。抓不到就退回原本的色塊，畫面不會壞。 */
function teamMark(name){
  var m=(DATA.meta||{})[name];
  if(m&&m.logo) return '<img class="lg" src="'+esc(m.logo)+'" alt="" loading="lazy">';
  return '<i class="dot"></i>';
}
function teamColor(name, fallback){
  var m=(DATA.meta||{})[name];
  return (m&&m.color)?m.color:fallback;
}
function elimTag(name){
  var r=(DATA.records||{})[name];
  if(!r||!r.elim) return "";
  return '<span class="badge" title="數學上已無緣季後賽">無緣季後賽</span>';
}

function streakTag(name){
  var r=(DATA.records||{})[name];
  var st=r&&r.st;
  if(!st||!st.n||st.n<2) return "";
  return '<span class="badge'+(st.w?" hi":"")+'">'+st.n+(st.w?"連勝":"連敗")+'</span>';
}

function confBadge(g){
  var z=Math.abs(g.mg)/(g.sd||13.5);
  if(z>=0.75) return '<span class="badge hi">高信心</span>';
  if(z>=0.35) return '<span class="badge">中等</span>';
  return '<span class="badge">貼身</span>';
}

/* ── 分享圖卡 ──────────────────────────────────────────
   把一場預測畫成一張 1080x1080 的圖，可以存到相簿或直接傳出去。

   設計上最重要的一件事：**不確定性要畫進圖裡**。
   圖片離開 App 之後就沒有雜訊帶、沒有免責聲明、沒有模型分頁的校準表 ——
   一張只寫「63.5%」的圖傳到群組裡，看起來就是在報明牌。
   所以曲線、標準差、以及「實際結果常常差很多」那句話都必須在圖上。
*/
function cssVar(n){
  return getComputedStyle(document.documentElement).getPropertyValue(n).trim()
    || "#888";
}

function shareCard(g){
  var S=1080, c=document.createElement("canvas");
  c.width=S; c.height=S;
  var x=c.getContext("2d");
  var INK=cssVar("--ink"), SUR=cssVar("--surface"), LINE=cssVar("--line"),
      CHALK=cssVar("--chalk"), MUTED=cssVar("--muted"),
      HOME=cssVar("--home"), AWAY=cssVar("--away");
  var CJK='"PingFang TC","Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif';

  x.fillStyle=INK; x.fillRect(0,0,S,S);

  /* 標頭 */
  x.fillStyle=MUTED; x.font="600 26px "+CJK;
  x.fillText("賽事預測 · 機率與誤差範圍", 72, 96);

  /* 隊名與預期比分 */
  var homeFav=g.mg>0, pick=homeFav?g.h:g.a;
  x.font="700 44px "+CJK;
  x.fillStyle=AWAY; x.fillText(g.a, 72, 200);
  x.fillStyle=MUTED; x.font="500 30px "+CJK;
  x.fillText("＠", 72, 258);
  x.fillStyle=HOME; x.font="700 44px "+CJK;
  x.fillText(g.h, 130, 258);

  /* 預期比分 */
  x.fillStyle=MUTED; x.font="500 26px "+CJK;
  x.fillText("預期比分", 72, 340);
  x.fillStyle=CHALK; x.font="700 58px "+CJK;
  x.fillText(g.ap.toFixed(0)+" － "+g.hp.toFixed(0), 72, 406);

  /* 勝率。用 homeFav 決定取哪一邊，不要用 Math.max —— 那是取「比較大的
     那個數」，萬一 mg 與 php 方向不一致（融合後兩者由不同路徑算出，
     貼身的場次有可能），就會出現「A 隊」配上「B 隊的機率」。 */
  var prob=homeFav ? g.php : 100-g.php;
  x.fillStyle=MUTED; x.font="500 26px "+CJK;
  x.fillText("模型認為勝出機率較高的是", 72, 476);
  x.fillStyle=homeFav?HOME:AWAY; x.font="700 40px "+CJK;
  x.fillText(pick+"　"+prob.toFixed(1)+"%", 72, 534);

  /* 分差分布曲線 —— 這一段是重點，不能省 */
  var CX=72, CY=600, CW=S-144, CH=250, sd=g.sd||13.5, mg=g.mg;
  var LO=-45, HI=45;
  x.fillStyle=SUR; x.fillRect(CX,CY,CW,CH);
  var peak=1/(sd*Math.sqrt(2*Math.PI)), pts=[];
  for(var i=0;i<=180;i++){
    var vx=LO+(HI-LO)*i/180;
    var vy=Math.exp(-Math.pow(vx-mg,2)/(2*sd*sd))/(sd*Math.sqrt(2*Math.PI));
    pts.push([CX+(vx-LO)/(HI-LO)*CW, CY+CH-30-(vy/peak)*(CH-70)]);
  }
  var zx=CX+(0-LO)/(HI-LO)*CW;
  /* 模型看好的那一側填色，另一側留白 —— 面積比就是勝率 */
  x.beginPath(); x.moveTo(homeFav?zx:pts[0][0], CY+CH-30);
  pts.forEach(function(p){ if(homeFav? p[0]>=zx : p[0]<=zx) x.lineTo(p[0],p[1]); });
  x.lineTo(homeFav?pts[pts.length-1][0]:zx, CY+CH-30); x.closePath();
  x.fillStyle=homeFav?HOME:AWAY; x.globalAlpha=0.18; x.fill(); x.globalAlpha=1;
  x.beginPath(); pts.forEach(function(p,i){ i?x.lineTo(p[0],p[1]):x.moveTo(p[0],p[1]); });
  x.strokeStyle=homeFav?HOME:AWAY; x.lineWidth=4; x.stroke();
  /* 平手線 */
  x.beginPath(); x.setLineDash([6,8]);
  x.moveTo(zx,CY+18); x.lineTo(zx,CY+CH-30);
  x.strokeStyle=LINE; x.lineWidth=2; x.stroke(); x.setLineDash([]);
  x.fillStyle=MUTED; x.font="500 22px "+CJK;
  x.fillText("客隊贏 45", CX+8, CY+CH-6);
  x.fillText("平手", zx-26, CY+CH-6);
  x.fillText("主隊贏 45", CX+CW-116, CY+CH-6);

  /* 不確定性說明 —— 這是這張圖存在的意義。
     數字要從 sd 算出來，不能寫死：標準差每場不同，
     寫死的「差 10 分很常見」在 sd=8 和 sd=16 時意思差很多。
     用 ±1 標準差的區間，那是常態分布下約 68% 會落入的範圍。 */
  var lo=Math.round(mg-sd), hi=Math.round(mg+sd);
  var fmt=function(v){
    if(Math.abs(v)<0.5) return "平手";
    return (v>0?g.h:g.a)+" 贏 "+Math.abs(v).toFixed(0)+" 分";
  };
  x.fillStyle=MUTED; x.font="500 25px "+CJK;
  x.fillText("曲線越寬代表越難預測。約三分之二的機率會落在", 72, 916);
  x.fillStyle=CHALK; x.font="600 25px "+CJK;
  x.fillText(fmt(lo)+" 到 "+fmt(hi)+" 之間，", 72, 956);
  x.fillStyle=MUTED; x.font="500 25px "+CJK;
  x.fillText("剩下三分之一會落在這個範圍之外。", 72, 996);

  /* 頁尾 */
  x.fillStyle=LINE; x.fillRect(72, 1022, S-144, 2);
  x.fillStyle=MUTED; x.font="500 21px "+CJK;
  x.fillText("由統計模型與歷史數據產生，僅供資訊參考及娛樂用途", 72, 1058);
  return c;
}

function shareGame(g){
  var url=location.href.split("#")[0];
  var pick=g.mg>0?g.h:g.a;
  var prob=(g.mg>0 ? g.php : 100-g.php).toFixed(1);
  /* 文字版也要帶不確定性，不能只有結論 */
  var txt=g.a+" ＠ "+g.h+"\n"+
    "模型認為勝出機率較高的是 "+pick+"（"+prob+"%）\n"+
    "預期比分 "+g.ap.toFixed(0)+" － "+g.hp.toFixed(0)+"\n"+
    "約三分之二的機率會落在分差 "+Math.round(g.mg-(g.sd||13.5))+" 到 "+
    Math.round(g.mg+(g.sd||13.5))+" 之間（主隊為正），剩下三分之一在範圍之外。";

  var canvas;
  try{ canvas=shareCard(g); }catch(e){ canvas=null; }
  if(!canvas || !canvas.toBlob){ return shareText(txt,url); }

  canvas.toBlob(function(blob){
    if(!blob) return shareText(txt,url);
    var name=(g.a+"-"+g.h).replace(/[^\w\u4e00-\u9fa5-]+/g,"_")+".png";
    var file=new File([blob], name, {type:"image/png"});
    /* canShare 要先問過 —— 不是每個瀏覽器都支援帶檔案分享，
       直接呼叫會丟例外而不是回 false。 */
    if(navigator.canShare && navigator.canShare({files:[file]})){
      navigator.share({files:[file], text:txt}).catch(function(){});
      return;
    }
    var a=document.createElement("a");
    a.href=URL.createObjectURL(blob); a.download=name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(a.href); }, 4000);
  }, "image/png");
}

function shareText(txt,url){
  if(navigator.share){
    navigator.share({title:"賽事預測",text:txt,url:url}).catch(function(){});
  }else if(navigator.clipboard){
    navigator.clipboard.writeText(txt+"\n"+url).then(function(){
      alert("已複製，可以貼給朋友了");
    }).catch(function(){});
  }
}

/* ── 圖形 ── */

/* 分差分布曲線。x 軸 -35~35 分，主隊領先為正，只填模型看好的那一側。 */
function curveSVG(margin, sd, homeFav){
  var W=300,H=74,LO=-35,HI=35,N=72;
  var peak=1/(sd*Math.sqrt(2*Math.PI)), pts=[];
  for(var i=0;i<=N;i++){
    var x=LO+(HI-LO)*i/N;
    var y=Math.exp(-Math.pow(x-margin,2)/(2*sd*sd))/(sd*Math.sqrt(2*Math.PI));
    pts.push([(x-LO)/(HI-LO)*W, H-6-(y/peak)*(H-16)]);
  }
  var line="M"+pts.map(function(p){return p[0].toFixed(1)+","+p[1].toFixed(1);}).join(" L");
  var zx=(0-LO)/(HI-LO)*W;
  var side=pts.filter(function(p){ return homeFav ? p[0]>=zx : p[0]<=zx; });
  var fill="";
  if(side.length){
    fill="M"+side[0][0].toFixed(1)+","+(H-6)+" L"+
      side.map(function(p){return p[0].toFixed(1)+","+p[1].toFixed(1);}).join(" L")+
      " L"+side[side.length-1][0].toFixed(1)+","+(H-6)+" Z";
  }
  var c=homeFav?"var(--home)":"var(--away)";
  return '<svg class="curve" viewBox="0 0 300 74" preserveAspectRatio="none" aria-hidden="true">'+
    '<path d="'+fill+'" fill="'+c+'" opacity=".16"/>'+
    '<path d="'+line+'" fill="none" stroke="'+c+'" stroke-width="1.6"/>'+
    '<line x1="'+zx.toFixed(1)+'" y1="4" x2="'+zx.toFixed(1)+'" y2="68" '+
    'stroke="var(--line)" stroke-width="1" stroke-dasharray="2 3"/></svg>';
}

function sparkline(pts){
  if(!pts || pts.length<2) return "";
  var W=300,H=52,lo=35,hi=95;
  var xs=pts.map(function(p,i){ return i/(pts.length-1)*W; });
  var ys=pts.map(function(p){ return H-6-((Math.min(hi,Math.max(lo,p.rate))-lo)/(hi-lo))*(H-16); });
  var line="M"+xs.map(function(x,i){ return x.toFixed(1)+","+ys[i].toFixed(1); }).join(" L");
  var fifty=H-6-((50-lo)/(hi-lo))*(H-16);
  var dots=xs.map(function(x,i){ return '<circle cx="'+x.toFixed(1)+'" cy="'+ys[i].toFixed(1)+
    '" r="2.6" fill="var(--home)"/>'; }).join("");
  return '<svg class="spark" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
    '<line x1="0" y1="'+fifty.toFixed(1)+'" x2="'+W+'" y2="'+fifty.toFixed(1)+
    '" stroke="var(--line)" stroke-width="1" stroke-dasharray="3 3"/>'+
    '<path d="'+line+'" fill="none" stroke="var(--home)" stroke-width="1.8"/>'+dots+'</svg>';
}

/* full = 這一組數字的滿格值。機率分布的最大值通常只有三成多，
   用 100 當滿格會讓所有長條都短到看不出差異，所以分組指定。 */
function bar(label, pct, full, cls){
  var w=Math.max(0, Math.min(100, pct/(full||38)*100));
  return '<div class="bk'+(cls||"")+'"><span class="bl2">'+esc(label)+'</span>'+
    '<span class="bb"><i style="width:'+w.toFixed(1)+'%"></i></span>'+
    '<span class="bv num">'+pct.toFixed(1)+'%</span></div>';
}
function kv(k,v){ return '<div class="krow"><span>'+esc(k)+'</span><b>'+v+'</b></div>'; }
/* 下拉選單。opts 是 [[值, 顯示文字], ...]，選好之後由 bind() 接 change 事件。 */
function dropdown(id, label, opts, cur){
  return '<div class="dd">'+(label?'<label>'+esc(label)+'</label>':"")+
    '<select id="'+id+'">'+opts.map(function(o){
      return '<option value="'+esc(o[0])+'"'+(String(o[0])===String(cur)?' selected':'')+
        '>'+esc(o[1])+'</option>';
    }).join("")+'</select></div>';
}

function head(t,s){ return '<div class="shead"><h2>'+esc(t)+'</h2><span>'+esc(s||"")+'</span></div>'; }
function formStrip(rows){
  if(!rows||!rows.length) return "";
  return rows.map(function(r){
    return '<span class="fm '+(r.w?"w":"l")+'">'+(r.w?"勝":"敗")+'</span>';
  }).join("")+'<span class="fmn num">'+
    rows.map(function(r){ return r.p+"-"+r.op; }).join("　")+'</span>';
}

/* ── 賽事分頁 ── */

/* 受限卡片：付費牆模式下，非示範場次只給方向與信心，不給數字。
   已結算的比分照樣顯示 —— 那是準確度的證據，藏起來反而少了說服力。 */
function lockedCard(g){
  var R=DATA.records||{};
  function rec(n){ var r=R[n]; return r?'<span class="rec num">'+r.w+'勝'+r.l+'敗</span>':""; }
  var h='<article class="card"><div class="row">'+
    '<div class="team">'+esc(g.a)+rec(g.a)+'</div>'+
    '<div class="at">＠</div>'+
    '<div class="team">'+esc(g.h)+rec(g.h)+'</div></div>';

  if(g.pick){
    var lv=["","接近五五","中等信心","高信心"][g.cf||0];
    h+='<div class="tags"><span class="tag strong">模型看好 '+esc(g.pick)+'</span>'+
       '<span class="badge">'+lv+'</span></div>';
  }
  if(g.st){
    h+='<div class="kv"><div class="mk">實際比分</div>'+
       '<div class="mv num">'+esc(g.sc)+'　'+(g.ok?"命中":"沒中")+'</div></div>';
  }
  h+='<div class="lockrow">'+
     (g.past ? '完整預測紀錄為付費內容'
             : '預期比分、勝率、分差分布為付費內容')+'</div>';
  return h+'</article>';
}

function gameCard(g, showExtra){
  if(g.php==null) return lockedCard(g);
  var homeFav=g.mg>0, hp=Math.max(3,Math.min(97,g.php));
  var pick=homeFav?g.h:g.a;
  var R=DATA.records||{};
  function rec(n){ var r=R[n]; return r?'<span class="rec num">'+r.w+'勝'+r.l+'敗</span>':""; }
  var tag=(Math.abs(g.mg)<4
    ? '<span class="tag close">勢均力敵</span>'
    : '<span class="tag strong">模型看好 '+esc(pick)+'</span>')+confBadge(g)+
    (g.feat?'<span class="badge lockb">今日免費完整場次</span>':'')+
    '<span class="badge">純模型預測</span>'+
    (g.mb?(DATA.locked
            ? '<span class="badge lockb">市場對照為付費內容</span>'
            : '<span class="badge">另有市場對照</span>')
        :'');
  var settled="";
  if(g.st){
    settled='<div><div class="mk">實際比分</div><div class="mv num">'+
      esc(g.sc)+'　'+(g.ok?"命中":"沒中")+'</div></div>';
  }
  var extra="";
  if(showExtra && !(g.hf||g.af||g.h2h) && g.st===0){
    extra='<div class="deep"><div class="hint">近況與交手紀錄要等程式跑過一次'+
      '之後才會出現（只會掛在最新一天的比賽上）。</div></div>';
  }
  if(showExtra && (g.hf||g.af||g.h2h)){
    extra='<div class="deep">';
    if(g.hf) extra+='<div class="dtitle">'+esc(g.h)+' 近 5 場</div><div class="form">'+formStrip(g.hf)+'</div>';
    if(g.af) extra+='<div class="dtitle">'+esc(g.a)+' 近 5 場</div><div class="form">'+formStrip(g.af)+'</div>';
    if(g.h2h&&g.h2h.length){
      extra+='<div class="dtitle">本季交手</div><div class="inj">'+
        g.h2h.map(function(r){ return esc(r.d)+'　'+esc(r.a)+' '+r.ap+' － '+r.hp+' '+esc(r.h); }).join("<br>")+
        '</div>';
    }
    extra+='</div>';
  }
  var info=[];
  var tt=tipoff(g.t);
  if(tt) info.push('<span class="num">'+tt+'</span>');
  if(g.v) info.push(esc(g.v)+(g.ct?"・"+esc(g.ct):""));
  if(g.nt) info.push('<span class="badge">中立場</span>');
  var infobar=info.length?'<div class="ginfo">'+info.join("　")+'</div>':"";
  return '<article class="card">'+infobar+'<div class="teams">'+
    '<div class="row a">'+teamMark(g.a)+'<span class="star'+(isFav(g.a)?" on":"")+
    '" data-fav="'+esc(g.a)+'">★</span>'+
    '<span class="tname tap" data-team="'+esc(g.a)+'">'+esc(g.a)+'</span>'+elimTag(g.a)+streakTag(g.a)+rec(g.a)+
    '<span class="tscore num">'+g.ap.toFixed(0)+'</span></div>'+
    '<div class="row h">'+teamMark(g.h)+'<span class="star'+(isFav(g.h)?" on":"")+
    '" data-fav="'+esc(g.h)+'">★</span>'+
    '<span class="tname tap" data-team="'+esc(g.h)+'">'+esc(g.h)+'</span>'+elimTag(g.h)+streakTag(g.h)+rec(g.h)+
    '<span class="tscore num">'+g.hp.toFixed(0)+'</span></div></div>'+
    '<div class="bar"><i class="ba" style="width:'+(100-hp).toFixed(1)+'%;'+
    'background:'+teamColor(g.a,"var(--away)")+'"></i>'+
    '<i class="bh" style="width:'+hp.toFixed(1)+'%;'+
    'background:'+teamColor(g.h,"var(--home)")+'"></i></div>'+
    '<div class="barlab"><span class="num">'+(100-hp).toFixed(0)+'%</span><span>勝率</span>'+
    '<span class="num">'+hp.toFixed(0)+'%</span></div>'+
    curveSVG(g.mg,g.sd,homeFav)+
    '<div class="axis"><span>客隊贏 35</span><span>平手</span><span>主隊贏 35</span></div>'+
    tag+
    '<div class="meta">'+
    '<div><div class="mk">預期分差</div><div class="mv num">'+Math.abs(g.mg).toFixed(1)+' 分</div></div>'+
    '<div><div class="mk">預期總分</div><div class="mv num">'+(g.hp+g.ap).toFixed(0)+'</div></div>'+
    settled+'</div>'+
    '<div class="actions"><div class="act" data-share="'+esc(g.a)+"|"+esc(g.h)+'">分享這場</div>'+
    '<div class="act" data-team="'+esc(g.h)+'">看 '+esc(g.h)+' 全部預測</div></div>'+
    extra+'</article>';
}

function teamGames(name){
  var out=[];
  DATA.order.forEach(function(d){
    DATA.days[d].forEach(function(g){ if(g.h===name||g.a===name) out.push({d:d,g:g}); });
  });
  return out;
}

function viewGames(){
  var h="";
  if(curTeam){
    var rows=teamGames(curTeam);
    var done=rows.filter(function(r){ return r.g.st; });
    var hit=done.filter(function(r){ return r.g.ok; }).length;
    h+='<div class="thead"><button class="back" id="backbtn">← 回到日期</button>'+
      '<span>'+esc(curTeam)+'　本季 <b class="num">'+rows.length+'</b> 場預測'+
      (done.length?'，對過答案 <b class="num">'+hit+'/'+done.length+'</b>':'')+'</span></div>';
    h+=rows.map(function(r){
      return '<div class="gdate num">'+r.d.slice(5).replace("-","/")+'</div>'+gameCard(r.g,false);
    }).join("");
    return h;
  }

  h+=freshness();
  /* DATA.order 是降冪（最新在前），所以清單天然就是新的在上面；
     相對地箭頭的索引方向是反的 —— 往前一天等於索引 +1。 */
  var MON=[], seenM={};
  DATA.order.forEach(function(d){
    var m=d.slice(0,7);
    if(!seenM[m]){ seenM[m]=1; MON.push(m); }
  });
  var curM=cur.slice(0,7), curI=DATA.order.indexOf(cur);
  var inM=DATA.order.filter(function(d){ return d.slice(0,7)===curM; });

  function dayLabel(d){
    var gs=DATA.days[d]||[], done=gs.filter(function(g){ return g.st; });
    var hit=done.filter(function(g){ return g.ok; }).length;
    return d.slice(5).replace("-","/")+"　"+gs.length+"場　"+
           (done.length ? "命中 "+hit+"/"+done.length : "未開打");
  }

  h+='<div class="dbar">'+
    '<select id="msel" aria-label="選擇月份">'+MON.map(function(m){
      var mp=m.split("-");
      return '<option value="'+m+'"'+(m===curM?" selected":"")+'>'+
             mp[0]+"/"+mp[1]+'</option>';
    }).join("")+'</select>'+
    '<button class="nav" id="dprev" aria-label="前一天"'+
      (curI>=DATA.order.length-1?" disabled":"")+'>‹</button>'+
    '<select id="dsel" aria-label="選擇日期">'+inM.map(function(d){
      return '<option value="'+d+'"'+(d===cur?" selected":"")+'>'+
             esc(dayLabel(d))+'</option>';
    }).join("")+'</select>'+
    '<button class="nav" id="dnext" aria-label="後一天"'+
      (curI<=0?" disabled":"")+'>›</button>'+
    '</div>';

  if(cur < TODAY && !DATA.days[TODAY]){
    h+='<div class="receipt warn">今天<b>沒有比賽</b>。下次有賽事時這裡會自動更新。</div>';
  }
  if(DATA.calibration && DATA.calibration.length){
    var pool=DATA.calibration.filter(function(c){ return c.n>=4; });
    if(!pool.length) pool=DATA.calibration;
    var best=pool.reduce(function(a,b){ return b.said>a.said?b:a; });
    h+='<div class="receipt">模型說有<b>'+esc(best.label)+'</b>把握的那些比賽，實際打完命中 '+
      '<b class="num">'+best.was.toFixed(0)+'%</b>（'+best.n+' 場）。'+
      '宣稱幾成就真的是幾成，這一欄我們攤開來給你看。</div>';
  }
  /* 今日重點：一行講完今天最值得看的一場，省得使用者自己掃 */
  var gsAll=DATA.days[cur];
  if(gsAll.length){
    var best=gsAll.slice().sort(function(a,b){
      return Math.max(b.php,100-b.php)-Math.max(a.php,100-a.php); })[0];
    var bp=Math.max(best.php,100-best.php), bt=best.mg>0?best.h:best.a;
    var close=gsAll.filter(function(g){ return Math.abs(g.mg)<4; }).length;
    h+='<div class="receipt">今天 <b class="num">'+gsAll.length+'</b> 場。'+
      '模型最有把握的是 <b>'+esc(bt)+'</b>（<b class="num">'+bp.toFixed(0)+'%</b>）'+
      (close?'，另有 <b class="num">'+close+'</b> 場勢均力敵。':'。')+'</div>';
  }
  h+=head(cur>=TODAY?"今日賽事":"當日賽事","點★收藏球隊，收藏的會排在前面");
  /* 收藏的球隊排前面 —— 使用者通常只關心自己支持的隊 */
  var gs=DATA.days[cur].slice().sort(function(a,b){
    var fa=(isFav(a.h)||isFav(a.a))?1:0, fb=(isFav(b.h)||isFav(b.a))?1:0;
    return fb-fa;
  });
  h+=gs.map(function(g){ return gameCard(g,true); }).join("");
  return h;
}

/* ── 解析分頁（付費） ── */

function lockPanel(what){
  return head("深度解析","進階內容")+
    '<div class="lock"><h3 id="lockhd">看得更深一層</h3>'+
    '<p>免費版是模型自己的判斷。深度解析再加上市場共識修正，以及每場的完整拆解。</p>'+
    '<ul><li>修正後的預期比分與勝率</li>'+
    '<li>四種可能結果的機率（大勝／小勝／小敗／大敗）</li>'+
    '<li>贏球幅度：贏 5／10／15／20 分以上的機率</li>'+
    '<li>分差落在各區間的完整機率</li>'+
    '<li>兩隊各自的得分落點分布</li>'+
    '<li>兩隊合計得分的落點分布</li>'+
    '<li>攻守對位拆解與預計回合數</li>'+
    '<li>兩隊主要輪替陣容與場均數據</li>'+
    '<li>缺陣影響：換算成缺掉多少場均得分</li>'+
    '<li>全聯盟評分、戰績主客分離、四要素</li>'+
    '<li>模型自我檢驗：走勢前推回測的完整成績</li></ul>'+buyBlock()+
    '<div class="diag" id="diagbox">'+esc(diagText())+'</div></div>';
}

function deepGame(){
  var list=(PREM&&PREM.days&&PREM.days[cur])||[];
  if(!list.length) return null;
  if(gi>=list.length) gi=0;
  return list[gi];
}

function viewDeep(){
  if(LOCKED||!PREM) return lockPanel();
  var pre="";
  if(PREM._cached && (Date.now()-PREM._cached)/3600000 > 26){
    pre='<div class="stale">⚠️ 目前顯示的是離線保存的內容，暫時連不上更新。</div>';
  }
  var list=(PREM.days&&PREM.days[cur])||[];
  if(!list.length) return pre+'<div class="empty">這一天沒有可解析的場次。</div>';

  var h=pre+dropdown("gamesel", "選擇比賽",
    list.map(function(g,i){ return [i, g.away+" ＠ "+g.home]; }), gi);

  var tabs=[["sum","預測摘要"],["dist","機率分布"],["mkt","市場對照"],["team","球隊狀況"]];
  h+='<div class="subs" id="deepsub">'+tabs.map(function(t){
    return '<div class="sub'+(SUB.deep===t[0]?" on":"")+'" data-s="'+t[0]+'">'+t[1]+'</div>';
  }).join("")+'</div>';

  var g=list[gi], b=g.blend, s=[];
  s.push('<article class="card"><div class="deep">');
  s.push('<div class="dtitle">'+esc(g.away)+' ＠ '+esc(g.home)+'</div>');

  if(SUB.deep==="sum"){
    /* 【v1.40】整頁只用一套機率。
       先前上面是「一句話結論＋其他判斷」，那三行的機率是網頁自己從
       合併分差＋反解離散度重算的；下面的命中機率則是預測引擎直接算的。
       同一頁兩套算法、同一件事兩個數字（實測相差最多 2.8 個百分點），
       而且上面的措辭還比較難懂。整頁改用命中機率這一套。 */
    var opts=(g.market&&g.market.opts)||[];
    if(opts.length){
      /* 頭條取機率最高那一項。不另外算 —— 它就是這張表的第一列。 */
      var c0=opts[0];
      /* 「這場好不好預測」要看獲勝那一項，不是看最高的那一項：
         明顯領先方的獲勝機率可以到 9 成，但那不代表判斷有價值。 */
      var wOpt=null;
      for(var wi=0;wi<opts.length;wi++){ if(opts[wi].k==="win"){
        if(!wOpt||opts[wi].p>wOpt.p) wOpt=opts[wi]; } }
      var closeGame=(wOpt && wOpt.p<60);
      s.push('<div class="callbox'+(closeGame?'':' hot')+'">'+
             '<div class="calltag">'+(closeGame?'這場沒有把握的判斷':'這場命中機率最高的判斷')+
             '</div>'+
             '<div class="calltext">'+esc(optText(c0))+'</div>'+
             '<div class="callp"><span class="num">'+num(c0.p)+'%</span>'+
             '　'+(g.market.optb?'合併市場共識後的命中機率':'純模型的命中機率')+
             '</div></div>');
      if(closeGame){
        s.push('<div class="note">兩隊獲勝機率都不到六成，這場的結果本來就難以預測。'+
               '這不是模型算不出來，是這場比賽真的比較接近擲硬幣。</div>');
      }
      s.push('<div class="dtitle">各種判斷的命中機率'+
             '<span class="mut">　'+(g.market.optb?"合併市場共識後":"純模型")+
             '</span></div>');
      s.push('<div class="opts">'+opts.map(function(o){
               return '<div class="optrow"><span class="num">'+num(o.p)+
                      '%</span>　'+esc(optText(o))+'</div>'; }).join("")+'</div>');
      s.push('<div class="note">同一場比賽的不同判讀方式，按命中機率排序。'+
             '機率高不代表比較值得注意 —— 越接近必然發生的事，'+
             '本來就越沒有資訊量。</div>');
    }else if(g.call){
      /* 舊紀錄沒有命中機率欄位，退回原本的一句話結論 */
      var c=g.call;
      s.push('<div class="callbox'+(c.strong?' hot':'')+'">'+
             '<div class="calltag">'+(c.strong?'這場最有把握的判斷':'這場沒有把握的判斷')+
             '</div>'+
             '<div class="calltext">'+esc(c.text)+'</div>'+
             '<div class="callp"><span class="num">'+num(c.p)+'%</span>'+
             (c.strong?'　把握程度':'　最強的說法也只有這樣')+'</div>'+
             '</div>');
    }
    s.push(kv("修正後預期比分",'<span class="num">'+num(b.away_proj)+' － '+num(b.home_proj)+'</span>'));
    s.push(kv("修正後勝率",esc(b.pick)+' <span class="num">'+num(b.prob)+'%</span>'));
    if(g.shift!=null&&Math.abs(g.shift)>=0.3)
      s.push(kv("與純模型的差距",'<span class="num">'+sign1(g.shift)+' 分</span>'));
    s.push(kv("預期總分",'<span class="num">'+num(b.total)+'</span>'));
    if(g.exp_poss!=null) s.push(kv("預計回合數",'<span class="num">'+num(g.exp_poss)+' 回合</span>'));
    var mu=g.matchup;
    if(mu&&mu.home_off!=null&&mu.away_def!=null){
      s.push('<div class="dtitle">攻守對位</div>');
      s.push(kv(g.home+" 進攻 vs "+g.away+" 防守",
        '<span class="num">'+sign1(mu.home_off)+' / '+sign1(mu.away_def)+
        ' → '+sign1(mu.home_off+mu.away_def)+'</span>'));
      s.push(kv(g.away+" 進攻 vs "+g.home+" 防守",
        '<span class="num">'+sign1(mu.away_off)+' / '+sign1(mu.home_def)+
        ' → '+sign1(mu.away_off+mu.home_def)+'</span>'));
    }
  }

  if(SUB.deep==="dist"){
    if(g.quadrants&&g.quadrants.length){
      s.push('<div class="dtitle">四種可能的結果</div>');
      s.push(g.quadrants.map(function(q){ return bar(q.label,q.p,55); }).join(""));
    }
    if(g.cross){
      s.push('<div class="dtitle">勝負 × 總分　交叉機率</div>');
      s.push('<div class="note">兩個維度一起看：哪一隊獲勝，以及總分會落在'+
             '市場共識預期總分（'+num(g.cross.line)+' 分）的上方還是下方。'+
             '四格加總 100%。這不是把兩個機率相乘 —— 相乘等於假設兩件事互不相干，'+
             '這裡用的是實測相關係數算出來的聯合機率。</div>');
      s.push(g.cross.cells.map(function(c){
        return bar(esc(c.w)+'　總分'+esc(c.t), c.p, 40);
      }).join(""));
    }
    if(g.buckets&&g.buckets.length){
      s.push('<div class="dtitle">'+esc(b.pick)+' 贏這個分差的機率</div>');
      if(g.top&&g.topp!=null){
        s.push('<div class="pick1">最可能：'+esc(b.pick)+' 贏 '+esc(g.top)+
               '　<span class="num">'+num(g.topp)+'%</span></div>');
      }
      s.push('<div class="note">每一格是「'+esc(b.pick)+'獲勝、而且分差正好落在這一格」'+
             '兩個條件同時成立的機率。所以六格加總等於獲勝機率而不是 100%，'+
             '差額是'+esc(b.pick)+'未獲勝的情形。</div>');
      s.push(g.buckets.map(function(r){
        var on=(g.top&&r[0]===g.top)?" hot":"";
        return bar(r[0],r[1],38,on);
      }).join(""));
      if(g.market && g.market.merged &&
         (g.market.merged.o10!=null||g.market.merged.u10!=null||
          g.market.merged.o15!=null)){
        /* 【v1.39】合併區間。單一格的機率天生就低（勢均力敵時上限約
           16~17%），把相鄰的格合起來才看得出傾向。放在六格正下方，
           因為它就是那六格的另一種切法。 */
        s.push('<div class="dtitle">合併區間</div>');
        var mgd=g.market.merged, mgr=[];
        if(mgd.u10!=null) mgr.push(bar("領先 1~10 分", mgd.u10, 55));
        if(mgd.o10!=null) mgr.push(bar("領先超過 10 分", mgd.o10, 55));
        if(mgd.o15!=null) mgr.push(bar("領先超過 15 分", mgd.o15, 55));
        s.push(mgr.join(""));
        s.push('<div class="note">「超過 10 分」與「超過 15 分」是包含關係，'+
               '不能相加 —— 後者是前者的一部分。</div>');
      }
      if(g.lose!=null){
        s.push(bar(esc(b.pick)+" 未獲勝", g.lose, 55));
      }
    }
    if(g.thresholds&&g.thresholds.length){
      s.push('<div class="dtitle">'+esc(b.pick)+' 贏球幅度（累計）</div>');
      s.push('<div class="note">上面是「正好落在某一格」，這裡是「達到某個幅度以上」，'+
             '把該幅度之後的所有格子加起來。</div>');
      s.push(g.thresholds.map(function(t){ return bar("贏 "+t.t+" 分以上",t.p,100); }).join(""));
    }
    if(g.home_scores&&g.home_scores.rows){
      s.push('<div class="dtitle">'+esc(g.home)+' 得分落點</div>');
      s.push(g.home_scores.rows.map(function(t){ return bar(t.label,t.p,38); }).join(""));
    }
    if(g.away_scores&&g.away_scores.rows){
      s.push('<div class="dtitle">'+esc(g.away)+' 得分落點</div>');
      s.push(g.away_scores.rows.map(function(t){ return bar(t.label,t.p,38); }).join(""));
    }
    if(g.totals&&g.totals.length){
      s.push('<div class="dtitle">兩隊合計得分落點</div>');
      s.push(g.totals.map(function(t){ return bar(t.label,t.p,38); }).join(""));
    }
    if(g.margin_sd||g.total_sd){
      s.push('<div class="dtitle">這場有多難算</div>');
      s.push(kv("分差不確定範圍",'<span class="num">±'+num(g.margin_sd)+' 分</span>'));
      s.push(kv("總分不確定範圍",'<span class="num">±'+num(g.total_sd)+' 分</span>'));
      if(g.home_scores) s.push(kv("單隊得分不確定範圍",
        '<span class="num">±'+num(g.home_scores.sd)+' 分</span>'));
    }
  }

  if(SUB.deep==="mkt"){
    var mk=g.market;
    if(!mk){
      s.push('<div class="empty">這場沒有市場資料，顯示的就是純模型預測。</div>');
    }else{
      s.push('<div class="note">模型先獨立算出一組預測，再跟市場共識加權合併。'+
             '「市場共識」是所有參考來源綜合後對這場比賽的預期值，'+
             '兩邊差距越大，代表市場可能掌握了模型還沒反映的消息'+
             '（傷兵、輪休、臨場調整），這時候市場權重會自動調高。</div>');

      function row(label, a, b, c, suf){
        function f(v){ return v==null ? "—" : num(v)+(suf||""); }
        return '<tr><td>'+label+'</td><td>'+f(a)+'</td><td>'+f(b)+
               '</td><td class="best">'+f(c)+'</td></tr>';
      }
      s.push('<table class="cmp"><thead><tr><th></th><th>純模型</th>'+
             '<th>市場共識</th><th>融合後</th></tr></thead><tbody>'+
             row("預期分差", mk.mm, mk.km, mk.bm)+
             row("預期總分", mk.mt, mk.kt, mk.bt)+
             row("主隊勝率", mk.mp, mk.kp, mk.bp, "%")+
             '</tbody></table>');
      s.push('<div class="note">最右欄「融合後」就是本 App 採用的最終預測。</div>');

      s.push(kv("分差修正幅度",'<span class="num">'+(g.shift>0?"+":"")+num(g.shift)+' 分</span>'));
      if(mk.w!=null) s.push(kv("這場採用的市場權重",'<span class="num">'+Math.round(mk.w*100)+'%</span>'));
      if(mk.gap!=null) s.push(kv("模型與市場分歧",'<span class="num">'+num(mk.gap)+' 分</span>'));
      if(g.st && g.okm!=null && g.okm!==g.ok){
        s.push('<div class="note">這場合併前後看好的隊伍不同：純模型'+
               (g.okm?'看對了':'看錯了')+'，本 App 採用的合併後預測'+
               (g.ok?'看對了':'看錯了')+'。合併後 = 35% 模型 + 65% 市場共識，'+
               '兩邊分歧又剛好跨過勝負分界時就會這樣。上方「準確度」頁的命中率'+
               '一律以合併後為準，因為那才是你實際看到的預測。</div>');
      }
      /* 【v1.38】長期傷停。確定缺陣但已久到球隊評分本身就反映了的球員 ——
         不重複扣分是對的，但完全不顯示會讓人以為漏算。 */
      if(mk.lt && mk.lt.length){
        s.push(kv("長期缺陣（已反映在球隊評分中）",
          '<span class="num">'+mk.lt.length+' 人</span>'));
        s.push('<div class="note">'+mk.lt.map(function(x){
                 return esc(x.n)+'（'+esc(x.t)+
                        (x.g!=null?'・已缺 '+x.g+' 場':'')+'）'; }).join("、")+
               '。這些人缺席已久，球隊近期評分本來就是在他們不在的情況下算出來的，'+
               '所以不會再另外扣一次 —— 不是漏掉。</div>');
      }
      /* 【v1.38】上面那個「模型預期總分」已經含實測校準修正，先前沒講，
         看起來像憑空來的數字。 */
      if(mk.corr && mk.corr.v!=null){
        s.push(kv("其中實測校準修正",
          '<span class="num">'+(mk.corr.v>0?"+":"")+num(mk.corr.v)+' 分</span>'+
          (mk.corr.a!=null&&mk.corr.b!=null
            ? '　<span class="mut">'+num(mk.corr.a)+' → '+num(mk.corr.b)+'</span>' : "")));
        s.push('<div class="note">模型的預期總分長期偏低多少，是拿過去已結束的'+
               '比賽實測出來的，再回頭加到每一場上。樣本越小修得越保守，'+
               '偏誤呈現一陣一陣的時候也會自動縮手。</div>');
      }
      if(mk.watch && mk.watch.length){
        s.push(kv("狀態未定的輪替球員",'<span class="num">'+mk.watch.length+' 人</span>'));
        s.push('<div class="note">'+mk.watch.map(function(w){
                 return esc(w.n)+'（'+esc(w.t)+(w.s?'・'+esc(w.s):'')+
                        '・'+num(w.v)+' 分產能）'; }).join("、")+
               '。這些人賽前才會確定是否出賽，模型一律當作會上場。'+
               '「很可能不打」比「不確定」更接近確定缺陣，排序由上而下。</div>');
        if(mk.wtswing!=null && Math.abs(mk.wtswing)>=0.5){
          s.push(kv("若全部缺陣　市場共識預期總分會移到",
            '<span class="num">'+sign1(mk.wtswing)+' 分</span>'));
        }
        if(mk.wswing!=null && Math.abs(mk.wswing)>=0.5 && mk.mm!=null){
          var lo=Math.min(mk.mm, mk.mm+mk.wswing), hi=Math.max(mk.mm, mk.mm+mk.wswing);
          s.push(kv("若全部缺陣，預期分差移到",
            '<span class="num">'+sign1(mk.mm+mk.wswing)+' 分</span>'));
          if(mk.km!=null){
            var inside=(mk.km>=lo && mk.km<=hi);
            var near=(Math.abs(mk.km-mk.mm)<Math.abs(mk.km-mk.mm-mk.wswing))?"全部出賽":"全部缺陣";
            s.push('<div class="note">市場共識 '+sign1(mk.km)+' 分，'+
              (inside
                ? '落在 '+sign1(lo)+' ~ '+sign1(hi)+' 這個區間內，偏「'+near+'」那一端。'+
                  '代表市場已經對出賽情況有判斷，賽前確認就能對照。'
                : '在 '+sign1(lo)+' ~ '+sign1(hi)+' 這個區間之外。'+
                  '代表出賽與否解釋不完兩邊的差距，還有別的原因。')+
              '</div>');
          }
        }
        var wa=PREM.watch;
        if(wa && wa.n){
          s.push('<div class="note">歷史對照：狀態未定的球員實際出賽 '+
                 wa.played+'/'+wa.n+' 人次（'+num(wa.rate)+'%，'+wa.games+' 場）'+
                 (wa.n<30 ? '。人次還少，先當參考。'
                          : '。可以當作這類球員的出賽基礎率。')+'</div>');
        }
      }
      if(b.std!=null){
        s.push(kv("市場隱含的分差離散度",'<span class="num">±'+num(b.std)+' 分</span>'+
          (g.margin_sd!=null?'　<span class="mut">模型 ±'+num(g.margin_sd)+'</span>':"")));
      }
      if(mk.kov!=null){
        s.push(kv("模型認為總分高於共識的機率",'<span class="num">'+num(mk.kov)+'%</span>'));
        if(mk.toff!=null){
          s.push(kv("融合後預期總分與共識差距",
            '<span class="num">'+(mk.toff>0?"+":"")+num(mk.toff)+' 分</span>'));
        }
        if(mk.toffs && mk.toffs.n){
          var o=mk.toffs;
          s.push('<div class="note">上面這個百分比幾乎全部來自一個固定傾向：'+
                 '模型的預期總分長期比市場共識'+(o.mean>0?"低":"高")+' '+
                 num(Math.abs(o.mean))+' 分（'+o.n+' 場對照，t='+num(o.t)+'）'+
                 (o.widening?"，且近期擴大中":"")+'。'+
                 '每一場的方向幾乎一樣，所以它反映的是模型的整體傾向，'+
                 '不是這一場獨立的判斷。</div>');
        }
      }
      if(mk.move!=null && mk.move!==0){
        s.push(kv("分差共識自首次收錄以來的變動",'<span class="num">'+(mk.move>0?"+":"")+num(mk.move)+' 分</span>'));
      }
      if(mk.movet!=null && mk.movet!==0){
        s.push(kv("總分共識自首次收錄以來的變動",'<span class="num">'+(mk.movet>0?"+":"")+num(mk.movet)+' 分</span>'));
      }
      if(mk.sources) s.push(kv("綜合來源家數",'<span class="num">'+mk.sources+'</span>'));
      if(mk.alert){
        s.push('<div class="stale">⚠️ 模型與市場分歧偏大。這類場次歷史上準確度較低，'+
               '通常表示有模型還沒反映到的消息。</div>');
      }

      /* 歷史對帳：不宣稱模型多強，把三者攤在同一張表上讓數字自己講話 */
      var tw=PREM.threeway;
      if(tw && tw.blend){
        var order=[["model","純模型"],["market","市場共識"],["blend","融合後"]];
        var best=null;
        order.forEach(function(o){
          var r=tw[o[0]]; if(r && (best===null || r.win>tw[best].win)) best=o[0];
        });
        var body=order.map(function(o){
          var r=tw[o[0]];
          if(!r) return "";
          var hi=(o[0]===best)?' class="best"':"";
          return '<tr><td>'+o[1]+'</td><td'+hi+'>'+num(r.win)+'%</td><td>'+
                 (r.mae==null?"—":num(r.mae))+'</td><td>'+
                 (r.tae==null?"—":num(r.tae))+'</td></tr>';
        }).join("");
        s.push('<div class="dsub">歷史對帳（已結算 '+tw.blend.n+' 場）</div>');
        s.push('<table class="cmp"><thead><tr><th></th><th>勝負命中</th>'+
               '<th>分差誤差</th><th>總分誤差</th></tr></thead><tbody>'+
               body+'</tbody></table>');
        s.push('<div class="note">誤差是平均絕對值，越小越好。'+
               '融合的目的不是贏過市場，是在模型有把握時保留自己的判斷、'+
               '在市場明顯知道更多時讓步。</div>');
      }

      /* 市場自己準不準 —— 把「模型算錯」和「市場給錯價」分開 */
      var mb=PREM.mktbias;
      if(mb && mb.mdl_total && mb.mkt_total){
        s.push('<div class="dsub">市場共識本身準不準（'+mb.n+' 場）</div>');
        function brow(label, d){
          if(!d) return "";
          return '<tr><td>'+label+'</td><td>'+(d.bias>0?"+":"")+num(d.bias)+
                 '</td><td>'+(Math.abs(d.t)>2?"明顯":"不明顯")+'</td></tr>';
        }
        s.push('<table class="cmp"><thead><tr><th></th><th>偏差</th>'+
               '<th>是否明顯</th></tr></thead><tbody>'+
               '<tr><td colspan="3" class="best">總分（正值＝實際比預期高）</td></tr>'+
               brow("模型預期", mb.mdl_total)+
               brow("市場共識", mb.mkt_total)+
               (mb.mdl_margin||mb.mkt_margin
                 ? '<tr><td colspan="3" class="best">分差（正值＝主隊表現優於預期）</td></tr>'+
                   brow("模型預期", mb.mdl_margin)+brow("市場共識", mb.mkt_margin)
                 : "")+
               '</tbody></table>');
        if(mb.mkt_sig){
          s.push('<div class="note">市場共識本身也偏了，而且偏得明顯。這有兩種可能：'+
                 '市場的定價確實落後，或者這段期間剛好如此、而市場已經在調整'+
                 '（共識值持續往同方向移動就是在調整）。這份報告分不出是哪一種，'+
                 '而持續性的偏差通常會被市場自己修掉。</div>');
        }else{
          s.push('<div class="note">市場共識本身沒有明顯偏差。所以模型的偏差是模型自己的問題，'+
                 '不代表市場給錯了值 —— 這一點很重要，兩者混在一起看'+
                 '會把自己的誤差誤讀成別人的破綻。</div>');
        }
        if(mb.gap!=null){
          s.push(kv("模型相對市場的落後幅度",
            '<span class="num">'+(mb.gap>0?"+":"")+num(mb.gap)+' 分</span>'));
        }
      }

      /* 分歧分層：什麼時候該相信模型、什麼時候該讓給市場 */
      var st=PREM.tw_strata;
      if(st && st.length){
        s.push('<div class="dsub">依分歧程度分層</div>');
        s.push('<table class="cmp"><thead><tr><th></th><th>場數</th>'+
               '<th>純模型</th><th>市場共識</th><th>融合後</th></tr></thead><tbody>'+
               st.map(function(r){
                 function c(k){
                   return r[k] ? num(r[k].win)+'%' : "—";
                 }
                 return '<tr><td>'+esc(r.band)+'</td><td>'+r.n+'</td><td>'+
                        c("model")+'</td><td>'+c("market")+
                        '</td><td class="best">'+c("blend")+'</td></tr>';
               }).join("")+'</tbody></table>');
        s.push('<div class="note">數字是勝負命中率。分歧小的時候模型與市場說的差不多，'+
               '分歧大的時候通常是市場掌握了消息 —— 這張表就是權重會隨分歧調高的依據。'+
               '場數少的分層參考價值有限。</div>');
      }

      /* 上表按分歧「大小」分層，數不出「看好不同隊」這件事 —— 另外列 */
      var op=PREM.opposite;
      if(op && op.n){
        s.push('<div class="dsub">看好不同球隊時</div>');
        s.push(kv("這類場次",'<span class="num">'+op.n+' 場</span>'));
        s.push(kv("純模型命中",'<span class="num">'+op.model+'/'+op.n+'</span>'));
        s.push(kv("市場共識命中",'<span class="num">'+op.market+'/'+op.n+'</span>'));
        s.push('<div class="note">上面那張表是按分歧「大小」分層的，'+
               '所以「模型 +8、共識 +12」（同向、共識更強烈）跟'+
               '「模型 -4、共識 +0.5」（看好不同隊）會被歸在同一格。'+
               '這一格單獨把後者挑出來 —— 那才是勝負判斷真的相反的場次。'+
               '平均分歧 '+num(op.gap)+' 分，通常不大，因為兩邊都接近平手。'+
               (op.n<15 ? '目前 '+op.n+' 場，差一場就翻盤，還不能當依據。'
                        : '')+'</div>');
      }
    }
  }

  if(SUB.deep==="team"){
    var r=g.ratings||{};
    var hasTeamInfo = (r.home_off!=null) || (g.rest&&(g.rest.home||g.rest.away)) ||
      (g.injuries&&((g.injuries.home||[]).length||(g.injuries.away||[]).length)) ||
      (g.absence&&(g.absence.home_lost!=null)) ||
      (PREM.league&&PREM.league.rotations);
    if(!hasTeamInfo){
      s.push('<div class="hint">這一場是舊版程式產生的，還沒有球隊狀況資料。'+
        '下次有比賽、程式跑過一次之後就會出現攻守評分、休息狀態、傷兵名單與輪替陣容。</div>');
    }
    if(r.home_off!=null) s.push(kv(g.home,
      '<span class="num">進攻 '+sign1(r.home_off)+'　防守 '+sign1(r.home_def)+'</span>'));
    if(r.away_off!=null) s.push(kv(g.away,
      '<span class="num">進攻 '+sign1(r.away_off)+'　防守 '+sign1(r.away_def)+'</span>'));
    if(g.rest&&(g.rest.home||g.rest.away))
      s.push(kv("休息狀態",esc(g.home)+' '+esc(g.rest.home||"－")+'　'+
        esc(g.away)+' '+esc(g.rest.away||"－")));
    var ab=g.absence||{};
    if(ab.home_lost!=null||ab.away_lost!=null){
      s.push('<div class="dtitle">缺陣影響</div>');
      if(ab.home_lost!=null) s.push(kv(g.home,
        '<span class="num">'+(ab.home_n||0)+' 人／'+num(ab.home_lost)+' 分產能</span>'));
      if(ab.away_lost!=null) s.push(kv(g.away,
        '<span class="num">'+(ab.away_n||0)+' 人／'+num(ab.away_lost)+' 分產能</span>'));
    }
    if(g.injuries_ok===false){
      s.push('<div class="hint">這一場的球員傷缺資料當時沒有取得，'+
        '下方預測<b>未計入任何球員缺陣</b>。缺陣是模型的主要輸入之一，'+
        '這批預測的誤差會比平常大。名單留白不代表全員健康。</div>');
    }
    var ih=(g.injuries&&g.injuries.home)||[], ia=(g.injuries&&g.injuries.away)||[];
    if(ih.length||ia.length){
      var t='<div class="dtitle">傷兵名單</div><div class="inj">';
      if(ih.length) t+='<b>'+esc(g.home)+'</b>：'+ih.map(esc).join("、")+'<br>';
      if(ia.length) t+='<b>'+esc(g.away)+'</b>：'+ia.map(esc).join("、");
      s.push(t+'</div>');
    }
    var rot=PREM.league&&PREM.league.rotations;
    if(rot){
      [g.home,g.away].forEach(function(nm){
        var l=rot[nm];
        if(l&&l.length) s.push('<div class="dtitle">'+esc(nm)+' 主要輪替</div><div class="inj">'+
          l.map(function(p){
            var ex=[];
            if(p.reb!=null) ex.push(num(p.reb)+" 籃板");
            if(p.ast!=null) ex.push(num(p.ast)+" 助攻");
            return esc(p.name)+'　<span class="num">'+num(p.min)+' 分鐘 / '+
              num(p.pts)+' 分'+(ex.length?" / "+ex.join(" / "):"")+'</span>';
          }).join("<br>")+'</div>');
      });
    }
    if(g.history&&g.history.length){
      s.push('<div class="dtitle">當天預測怎麼移動</div><div class="inj">'+
        g.history.map(function(x){
          return esc(x.t)+'　'+esc(x.winner)+' '+x.prob+'%（分差 '+sign(x.margin)+'）';
        }).join("<br>")+'</div>');
    }
  }

  s.push('</div></article>');
  return h+s.join("");
}

/* ── 球隊分頁 ── */

function viewTeams(){
  var L=PREM&&PREM.league;
  var R=DATA.records||{};
  if(!L||!L.teams||!L.teams.length){
    var names=Object.keys(R);
    if(!names.length) return '<div class="empty">還沒有球隊資料。<br><br>'+
      '全聯盟評分、戰績與四要素要等程式跑過一次之後才會產生。'+
      '如果你是在電腦上直接開啟檔案，瀏覽器會擋掉這部分的讀取 —— '+
      '要用網址開才看得到。</div>';
    names.sort(function(a,b){ return (R[b].w-R[b].l)-(R[a].w-R[a].l); });
    return head("戰績","免費版")+
      '<table><tr><th>球隊</th><th>戰績</th><th>場均得/失</th></tr>'+
      names.map(function(n){
        var r=R[n];
        return '<tr><td>'+esc(n)+'</td><td class="num was">'+r.w+'-'+r.l+
          '</td><td class="num">'+num(r.pf)+'/'+num(r.pa)+'</td></tr>';
      }).join("")+'</table>'+
      '<div class="hint">實力評分、四要素與節奏排行在深度解析裡。</div>';
  }

  var opts=[["rating","實力評分"]];
  if(L.teams.some(function(t){ return t.w!=null; })) opts.push(["record","戰績主客"]);
  if(L.teams.some(function(t){ return t.seed!=null||t.conf; })) opts.push(["conf","分區排名"]);
  if(L.teams.some(function(t){ return t.ff; })) opts.push(["ff","四要素"]);
  if(L.teams.some(function(t){ return t.pace!=null; })) opts.push(["pace","節奏"]);
  if(L.rotations && Object.keys(L.rotations).length) opts.push(["rot","陣容"]);
  if(!opts.some(function(o){ return o[0]===SUB.teams; })) SUB.teams=opts[0][0];

  var h='<div class="subs" id="teamsub">'+opts.map(function(o){
    return '<div class="sub'+(SUB.teams===o[0]?" on":"")+'" data-s="'+o[0]+'">'+o[1]+'</div>';
  }).join("")+'</div>';

  if(SUB.teams==="rating"){
    h+=head("實力評分","對手強度調整後，0＝聯盟平均")+
      '<table><tr><th>球隊</th><th>綜合</th><th>進攻</th><th>防守</th></tr>'+
      L.teams.map(function(t){
        return '<tr><td>'+esc(t.team)+'</td><td class="num was">'+sign1(t.overall)+
          '</td><td class="num">'+sign1(t.off)+'</td><td class="num">'+sign1(t["def"])+'</td></tr>';
      }).join("")+'</table>'+
      '<div class="hint">進攻是每場比聯盟平均多得幾分，防守是少讓幾分。'+
      '綜合＝兩者相加。</div>';
  }
  if(SUB.teams==="record"){
    h+=head("戰績與主客表現","主客判若兩隊的球隊值得留意")+
      '<table><tr><th>球隊</th><th>戰績</th><th>得/失</th><th>主場</th><th>客場</th></tr>'+
      L.teams.filter(function(t){ return t.w!=null; }).map(function(t){
        function wl(x){ return x?x.w+"-"+x.l:"\u2014"; }
        return '<tr><td>'+esc(t.team)+'</td><td class="num was">'+t.w+'-'+t.l+
          '</td><td class="num">'+num(t.pf)+'/'+num(t.pa)+
          '</td><td class="num">'+wl(t.home)+'</td><td class="num">'+wl(t.away)+'</td></tr>';
      }).join("")+'</table>';
  }
  if(SUB.teams==="conf"){
    /* 分區排名：ESPN standings 端點才有的官方分組與勝差，自己算 W-L 算不出來 */
    var byConf={};
    L.teams.forEach(function(t){
      if(t.seed==null&&!t.conf) return;
      var c=t.conf||"聯盟";
      (byConf[c]=byConf[c]||[]).push(t);
    });
    Object.keys(byConf).sort().forEach(function(c){
      var rows=byConf[c].slice().sort(function(a,b){
        return (a.seed==null?99:a.seed)-(b.seed==null?99:b.seed);
      });
      h+=head(c,"依官方排名")+
        '<table><tr><th>#</th><th>球隊</th><th>戰績</th><th>勝差</th><th>近10</th><th>連續</th></tr>'+
        rows.map(function(t){
          return '<tr><td class="num was">'+(t.seed!=null?t.seed:"\u2014")+'</td>'+
            '<td>'+esc(t.team)+'</td>'+
            '<td class="num">'+(t.w!=null?t.w+"-"+t.l:"\u2014")+'</td>'+
            '<td class="num">'+(t.gb!=null?(t.gb===0?"\u2014":num(t.gb)):"\u2014")+'</td>'+
            '<td class="num said">'+esc(t.l10||"\u2014")+'</td>'+
            '<td class="num">'+esc(t.strk||"\u2014")+'</td></tr>';
        }).join("")+'</table>';
    });
    h+='<div class="hint">排名與勝差來自聯盟官方分組。'+
      '勝差是距離該分區龍頭還差幾場，「近10」是最近十場的勝敗。</div>';
  }
  if(SUB.teams==="ff"){
    h+=head("四要素","描述一支球隊的標準框架")+
      '<table><tr><th>球隊</th><th>命中率</th><th>失誤率</th><th>進攻籃板</th><th>罰球率</th></tr>'+
      L.teams.filter(function(t){ return t.ff; }).map(function(t){
        var f=t.ff;
        return '<tr><td>'+esc(t.team)+'</td><td class="num was">'+num(f.efg)+
          '</td><td class="num">'+num(f.tov)+'</td><td class="num">'+num(f.oreb)+
          '</td><td class="num">'+num(f.ftr)+'</td></tr>';
      }).join("")+'</table>'+
      head("防守四要素","同一套指標，看的是「讓對手做到什麼」")+
      '<table><tr><th>球隊</th><th>對手命中率</th><th>逼失誤率</th>'+
      '<th>對手進攻籃板</th><th>對手罰球率</th></tr>'+
      L.teams.filter(function(t){ return t.ff&&t.ff.d_efg!=null; }).map(function(t){
        var f=t.ff;
        return '<tr><td>'+esc(t.team)+'</td><td class="num was">'+num(f.d_efg)+
          '</td><td class="num">'+num(f.d_tov)+'</td><td class="num">'+num(f.d_oreb)+
          '</td><td class="num">'+num(f.d_ftr)+'</td></tr>';
      }).join("")+'</table>'+
      '<div class="hint">這四欄除了「逼失誤率」越高越好，其餘都是越低越好 —— '+
      '它們描述的是對手在你面前做到了什麼。一支球隊的綜合評分如果來自防守，'+
      '會在這張表上看得出來。</div>'+
      '<div class="hint">命中率是把三分算 1.5 顆的有效命中率；失誤率是每百回合失誤數；'+
      '進攻籃板率是自家進攻籃板佔可搶籃板的比例。'+
      '<b>兩隊綜合評分一樣，靠的可能是完全不同的東西</b> —— 這張表看得出打法。</div>';
  }
  if(SUB.teams==="rot"){
    var rot=L.rotations, names=Object.keys(rot);
    /* 依實力評分的順序排，跟上面幾張表一致 */
    var order=L.teams.map(function(t){ return t.team; })
      .filter(function(n){ return rot[n]; });
    names.forEach(function(n){ if(order.indexOf(n)<0) order.push(n); });
    if(order.indexOf(ROTTEAM)<0) ROTTEAM=order[0];
    h+=dropdown("rotsel","選擇球隊",order.map(function(n){ return [n,n]; }),ROTTEAM);
    h+=head("主要輪替陣容","場均上場時間與得分")+
      [ROTTEAM].map(function(n){
        var l=rot[n]||[];
        return '<article class="card"><div class="deep">'+
          '<div class="dtitle">'+esc(n)+'</div>'+
          l.map(function(pl){
            var ex=[];
            if(pl.reb!=null) ex.push(num(pl.reb)+" 籃板");
            if(pl.ast!=null) ex.push(num(pl.ast)+" 助攻");
            if(pl.pm!=null) ex.push((pl.pm>0?"+":"")+num(pl.pm));
            return '<div class="krow"><span>'+esc(pl.name)+'</span>'+
              '<b class="num">'+num(pl.min)+' 分鐘　'+num(pl.pts)+' 分'+
              (ex.length?'　'+ex.join("　"):"")+'</b></div>';
          }).join("")+'</div></article>';
      }).join("")+
      '<div class="hint">依場均上場時間排序。缺陣影響就是用這份名單換算的 —— '+
      '缺一個場均 18 分的球員，跟缺一個場均 3 分的，不會被算成一樣。</div>';
  }
  if(SUB.teams==="pace"){
    h+=head("節奏與效率","每場回合數、每百回合得失分")+
      '<table><tr><th>球隊</th><th>節奏</th><th>進攻效率</th><th>防守效率</th></tr>'+
      L.teams.filter(function(t){ return t.pace!=null; }).map(function(t){
        return '<tr><td>'+esc(t.team)+'</td><td class="num was">'+num(t.pace)+
          '</td><td class="num">'+num(t.ortg)+'</td><td class="num">'+num(t.drtg)+'</td></tr>';
      }).join("")+'</table>';
    if(L.avg_pace) h+='<div class="hint">聯盟平均每場 <b class="num">'+L.avg_pace+
      '</b> 個回合、每百回合 <b class="num">'+L.avg_rtg+'</b> 分。節奏數字大代表打得快。</div>';
  }
  return h;
}

/* ── 模型分頁 ── */

function viewModel(){
  var rep=PREM&&PREM.league&&PREM.league.model_report;
  var opts=[];
  if(DATA.calibration&&DATA.calibration.length) opts.push(["calib","信心校準"]);
  if(DATA.strata&&DATA.strata.length) opts.push(["strata","哪種場合準"]);
  if(DATA.rolling&&DATA.rolling.length>1) opts.push(["roll","近期走勢"]);
  if(DATA.by_team&&DATA.by_team.length) opts.push(["team","各隊準度"]);
  if(PREM&&PREM.cover) opts.push(["cover","超出共識的頻率"]);
  if(PREM&&PREM.league&&PREM.league.calibration) opts.push(["calibv","模型參數"]);
  if(rep) opts.push(["back","走勢前推回測"],["drift","偏誤漂移"]);
  if(PREM&&PREM.results&&PREM.results.length) opts.push(["hist","全部歷史"]);
  if(!opts.length) return '<div class="empty">等比賽打完之後，這裡會出現模型的成績。</div>';
  if(!rep && opts.length){
    /* 回測與全部歷史屬於深度內容，沒有就講清楚為什麼 */
  }
  if(!opts.some(function(o){ return o[0]===SUB.model; })) SUB.model=opts[0][0];

  var h=dropdown("modelsub", "看哪一項", opts, SUB.model);

  if(SUB.model==="calib"){
    /* 賽季總覽：一眼看完模型至今的成績 */
    var ov='<div class="sum">';
    ov+='<div class="sv"><span>已結算場次</span><b>'+DATA.settled+'</b></div>';
    if(DATA.strata){
      var all=DATA.strata.filter(function(r){ return r.label.indexOf("看好")===0; });
      var tn=all.reduce(function(a,r){ return a+r.n; },0);
      var th=all.reduce(function(a,r){ return a+r.n*r.rate/100; },0);
      if(tn) ov+='<div class="sv"><span>勝負命中率</span><b>'+
        (100*th/tn).toFixed(1)+'%</b></div>';
    }
    if(DATA.brier) ov+='<div class="sv"><span>Brier score</span><b>'+
      DATA.brier.score.toFixed(3)+'</b></div>';
    if(DATA.rolling&&DATA.rolling.length)
      ov+='<div class="sv"><span>近 20 場</span><b>'+
        DATA.rolling[DATA.rolling.length-1].rate.toFixed(0)+'%</b></div>';
    ov+='</div>';
    ov+='<div class="note">這些數字算的是<b>你實際看到的預測</b>（已合併市場共識），'+
      '不是模型單獨的表現。少數場次兩者看好的隊伍會不同，'+
      '那時個別比賽的「市場對照」頁會註明。</div>';
    if(DATA.brier) ov+='<div class="hint">Brier score 是機率預測的標準評分，越低越好。'+
      '永遠喊 50% 會得到 0.250 —— 低於這個數字才算真的有資訊。'+
      '它比命中率嚴格，因為會懲罰「講得很有把握卻猜錯」。</div>';
    h+=head("賽季總覽","至今的成績")+ov;
    h+=head("信心指數準不準","已結算 "+DATA.settled+" 場")+
      '<table><tr><th>模型說</th><th>場次</th><th>宣稱</th><th>實際</th></tr>'+
      (PREM&&PREM.calibration&&PREM.calibration.length?PREM.calibration:DATA.calibration)
        .map(function(c){
          return '<tr><td>'+esc(c.label)+'</td><td class="num">'+c.n+
            '</td><td class="num said">'+c.said.toFixed(0)+'%</td>'+
            '<td class="num was">'+c.was.toFixed(0)+'%</td></tr>';
        }).join("")+'</table>'+
      '<div class="hint">「宣稱」是模型當時說的勝率，「實際」是那批比賽真的命中幾成。'+
      '兩欄越接近，代表機率越可信。</div>';
  }
  if(SUB.model==="strata"){
    h+=head("哪種場合比較準","攤開來看")+
      '<table><tr><th>場合</th><th>場次</th><th>命中率</th></tr>'+
      DATA.strata.map(function(r){
        return '<tr><td>'+esc(r.label)+'</td><td class="num">'+r.n+
          '</td><td class="num was">'+r.rate.toFixed(0)+'%</td></tr>';
      }).join("")+'</table>';
  }
  if(SUB.model==="roll"){
    var last=DATA.rolling[DATA.rolling.length-1];
    h+=head("近期走勢","每點是往前 20 場的命中率")+sparkline(DATA.rolling)+
      '<div class="axis"><span>'+esc(DATA.rolling[0].at)+'</span><span>虛線 = 50%</span>'+
      '<span>'+esc(last.at)+'　'+last.rate.toFixed(0)+'%</span></div>';
  }
  if(SUB.model==="cover"&&PREM&&PREM.cover){
    var cv=PREM.cover;
    function coverRow(label, hit, tot, band){
      if(!tot) return "";
      var pct=100*hit/tot, out=(pct<band[0]||pct>band[1]);
      return '<tr><td>'+esc(label)+'</td><td class="num">'+hit+'/'+tot+
        '</td><td class="num was">'+pct.toFixed(1)+'%</td>'+
        '<td class="num mut'+(out?" outb":"")+'">'+
        band[0].toFixed(0)+'~'+band[1].toFixed(0)+'%</td></tr>';
    }
    h+=head("超出共識的頻率","含雜訊帶，避免把起伏讀成趨勢")+
      '<div class="note">「雜訊帶」是真實機率剛好五成時，'+
      '這個場次數下觀測值的 95% 範圍。落在帶子裡就是分不出訊號 —— '+
      '不是「接近五成」，是「什麼都沒說」。</div>'+
      '<table><tr><th>項目</th><th>場次</th><th>比率</th><th>雜訊帶</th></tr>'+
      coverRow("主隊超出市場共識分差", cv.home, cv.n, cv.band)+
      coverRow("實際總分高於共識", cv.over, cv.over_n, cv.band)+
      coverRow("模型挑的那一邊（分差）", cv.sp, cv.sp_n, cv.band)+
      coverRow("模型挑的那一邊（總分）", cv.ou, cv.ou_n, cv.band)+
      '</table>'+
      '<div class="note">前兩列是市場自身的健康度檢查，期待值本來就是五成，'+
      '市場會持續校正它們。真正可能有價值的是後兩列 —— '+
      '模型挑的那一邊有沒有比五成好。</div>';
    if(cv.r10){
      h+=head("近 10 場","列出來是為了讓你看見它有多不穩")+
        '<div class="kv"><div class="mk">主隊超出共識分差</div>'+
        '<div class="mv num">'+cv.r10.home+'/10</div></div>'+
        (cv.r10.sp_n?'<div class="kv"><div class="mk">模型挑的那一邊</div>'+
          '<div class="mv num">'+cv.r10.sp+'/'+cv.r10.sp_n+'</div></div>':'')+
        '<div class="note">10 場的雜訊帶是 '+cv.r10.band[0].toFixed(0)+'~'+
        cv.r10.band[1].toFixed(0)+'% —— 連 8/10 都落在裡面。'+
        '要看出「真的偏離 10 個百分點」需要約 100 場，偏離 5 個百分點要約 400 場。'+
        '這兩個數字每天都會跳，不要當成趨勢。</div>';
    }
  }
  if(SUB.model==="team"&&DATA.by_team){
    h+=head("模型預測各隊的準度","至少 4 場才列入")+
      '<table><tr><th>球隊</th><th>場次</th><th>命中</th><th>命中率</th></tr>'+
      DATA.by_team.map(function(r){
        return '<tr><td>'+esc(r.team)+'</td><td class="num">'+r.n+
          '</td><td class="num">'+r.hit+'</td><td class="num was">'+
          r.rate.toFixed(0)+'%</td></tr>';
      }).join("")+'</table>'+
      '<div class="hint">總命中率會藏住一件事：模型對某些球隊就是抓不準，'+
      '通常是輪換不穩、或當家球星長期在傷停邊緣的那幾隊。'+
      '公開這張表對我們不利，但它正是你想知道的。</div>';
    if(DATA.total_acc){
      h+=head("總分預測的準度",DATA.total_acc.n+" 場")+
        '<table>'+
        '<tr><td>平均誤差</td><td class="num was">'+num(DATA.total_acc.mae)+' 分</td></tr>'+
        '<tr><td>誤差在 10 分內</td><td class="num">'+num(DATA.total_acc.within10)+'%</td></tr>'+
        '</table>'+
        '<div class="hint">用模型自己的預期總分當基準算出來的。'+
        '籃球單場總分本來就很跳，這個數字不會太好看 —— 但它是誠實的。</div>';
    }
  }
  if(SUB.model==="calibv"&&PREM&&PREM.league&&PREM.league.calibration){
    var c=PREM.league.calibration;
    h+=head("模型參數","每天從實際比賽反推，不是寫死的")+'<table>';
    if(c.n_games!=null) h+='<tr><td>校準用的比賽場數</td><td class="num was">'+c.n_games+'</td></tr>';
    if(c.league_avg_pts!=null) h+='<tr><td>聯盟單隊場均得分</td><td class="num">'+num(c.league_avg_pts)+'</td></tr>';
    if(c.home_court_adv!=null) h+='<tr><td>主場優勢</td><td class="num">'+num(c.home_court_adv,2)+' 分</td></tr>';
    if(c.b2b_penalty!=null) h+='<tr><td>背靠背疲勞</td><td class="num">'+num(c.b2b_penalty,2)+' 分</td></tr>';
    if(c.margin_std!=null) h+='<tr><td>分差標準差</td><td class="num">±'+num(c.margin_std)+' 分</td></tr>';
    if(c.total_std!=null) h+='<tr><td>總分標準差</td><td class="num">±'+num(c.total_std)+' 分</td></tr>';
    if(c.absence_slope!=null) h+='<tr><td>缺陣修正斜率</td><td class="num">'+num(c.absence_slope,3)+'</td></tr>';
    h+='</table><div class="hint">主場優勢與背靠背疲勞都是從本季實際比賽反推的，'+
      '不同賽季會不一樣。標準差決定機率分布有多寬 —— '+
      '分差標準差 13 分左右，這就是為什麼看好贏 10 分的比賽還是常常翻船。</div>';
  }
  if(SUB.model==="back"&&rep){
    h+=head("走勢前推回測",rep.n+" 場")+
      '<div class="receipt">每一場都只用「該場開打前」的資料重新算一次，不讓模型看到未來。'+
      '這個數字通常比宣傳數字難看，但它才是真的。</div>'+
      '<table>'+
      '<tr><td>勝負命中率</td><td class="num was">'+num(rep.win_rate)+'%</td></tr>'+
      '<tr><td>近 30 場</td><td class="num">'+num(rep.recent30_win)+'%</td></tr>'+
      '<tr><td>分差平均誤差</td><td class="num">'+num(rep.margin_mae)+' 分</td></tr>'+
      '<tr><td>分差離散度</td><td class="num">±'+num(rep.margin_sd)+' 分</td></tr>'+
      '<tr><td>總分平均誤差</td><td class="num">'+num(rep.total_mae)+' 分</td></tr>'+
      '<tr><td>總分離散度</td><td class="num">±'+num(rep.total_sd)+' 分</td></tr>'+
      (rep.margin_rmse!=null
        ? '<tr><td>分差誤差（平方平均）</td><td class="num">'+num(rep.margin_rmse)+' 分</td></tr>'
        : "")+
      (rep.margin_bias!=null
        ? '<tr><td>分差偏誤</td><td class="num">'+sign1(rep.margin_bias)+' 分</td></tr>'
        : "")+
      (rep.total_bias!=null
        ? '<tr><td>總分偏誤</td><td class="num">'+sign1(rep.total_bias)+' 分</td></tr>'
        : "")+
      '</table>'+
      '<div class="hint">「平均誤差」是差距的絕對值平均；「平方平均」會放大偶爾的大失誤，'+
      '兩個一起看才知道錯得穩不穩定。偏誤是有方向的：分差為正代表模型低估主隊，'+
      '總分為正代表低估得分。偏誤會被自動修正，但只修一部分 —— '+
      '因為它會隨時間漂移（見下一頁）。</div>';
    if(rep.rolling&&rep.rolling.length>1)
      h+=head("回測走勢","每點是往前 30 場")+sparkline(rep.rolling);
  }
  if(SUB.model==="drift"&&rep&&rep.segments){
    h+=head("偏誤會漂移","整體平均會藏住階段性的偏差")+
      '<table><tr><th>期間</th><th>場次</th><th>分差偏誤</th></tr>'+
      rep.segments.map(function(sg){
        return '<tr><td>'+esc(sg.from)+'~'+esc(sg.to)+'</td><td class="num">'+sg.n+
          '</td><td class="num was">'+sign1(sg.bias)+' 分</td></tr>';
      }).join("")+'</table>'+
      '<div class="hint">正值代表那段期間模型低估主隊。四段數字不一樣，'+
      '就表示偏誤是一陣一陣的，不是固定的 —— 這是我們自己找出來、也公開承認的弱點。</div>';
  }
  if(SUB.model==="hist"&&(!PREM||!PREM.results||!PREM.results.length)){
    h+='<div class="empty">還沒有可顯示的歷史紀錄。</div>';
  }
  if(SUB.model==="hist"&&PREM&&PREM.results&&PREM.results.length){
    h+=head("全部歷史",PREM.results.length+" 場")+
      PREM.results.map(function(r){
        return '<div class="res"><span class="m num">'+esc(r.date)+'</span>'+
          '<span class="t">'+esc(r.pick)+'</span><span class="s num">'+esc(r.score)+'</span>'+
          '<span class="f '+(r.ok?"y":"n")+'">'+(r.ok?"✓":"✕")+'</span></div>';
      }).join("");
  }
  return h;
}

/* ── 關於分頁 ── */

function viewAbout(){
  return head("關於這個模型","怎麼來的、怎麼讀")+
    '<div class="about">'+
    '<h3>數字是怎麼算出來的</h3>'+
    '<p>模型用整季每一場比賽重建全聯盟的球隊評分，而且會扣掉對手強弱 —— '+
    '連打弱隊打出來的漂亮數據不會被當成實力。再加上主場優勢、休息天數、'+
    '背靠背疲勞，算出每場的預期比分與機率分布。</p>'+
    '<h3>勝率不是保證</h3>'+
    '<p>「勝率 76%」的意思是：這種形勢的比賽，長期跑下來大約四次贏三次。'+
    '單場還是可能輸。籃球單場的分差標準差大約 13 分，所以就算模型看好某隊贏 10 分，'+
    '實際打出 20 分或反而輸球都很常見 —— 那條分布曲線畫的就是這件事。</p>'+
    '<h3>曲線越寬代表越難算</h3>'+
    '<p>每場比賽的曲線寬度不一樣。窄的是模型比較有把握，寬的代表這場本身就難預測。'+
    '看曲線比看單一數字誠實。</p>'+
    '<h3>我們公開自己的成績</h3>'+
    '<p>「模型」分頁裡有走勢前推回測：每一場都只用該場開打前的資料重算一次，'+
    '不讓模型偷看未來。那個數字通常比宣傳數字難看，但它才是真的。'+
    '連「偏誤會漂移」這種對我們不利的發現也一起公開。</p>'+
    '<h3>資料來源</h3>'+
    '<p>賽程、比分與球隊數據來自公開的體育資料來源。'+
    '每場比賽開打前半小時會更新一次，把最新的出賽狀況與市場共識帶進預測；'+
    '當天全部比賽結束後再更新一次戰果，並先算出隔日的賽事。'+
    '不蒐集任何使用者個人資料。</p>'+
    '<h3>免責</h3>'+
    '<p>所有內容由統計模型依歷史數據產生，僅供資訊參考及娛樂用途，'+
    '不構成任何形式的建議。</p>'+
    '<h3>更新紀錄</h3>'+
    '<div class="note" id="bvertap">這個 App 由兩支程式組成：<b>預測引擎</b>負責算出機率，'+
    '<b>網頁</b>負責呈現。兩者各自更新，所以版本號有兩個。<br>'+
    '目前：網頁 <b>'+esc(BUILDER_VERSION)+'</b>'+
    (DATA.engine ? '　預測引擎 <b>'+esc(DATA.engine)+'</b>' : '')+
    '</div>'+
    (CHANGELOG.length ? CHANGELOG.map(function(c){
      return '<div class="chg"><div class="chgv">'+esc(c.d)+
        '<span class="chgd">網頁 '+esc(c.web)+'　引擎 '+esc(c.eng)+'</span>'+
        '</div><ul>'+
        c.items.map(function(i){ return '<li>'+esc(i)+'</li>'; }).join("")+
        '</ul></div>';
    }).join("") : "")+
    '</div>'+
    testPanel();
}

/* ── 分派與事件 ── */

var TABS=[["games","賽事"],["deep","解析"],["teams","球隊"],["model","模型"],["about","關於"]];

function renderTabs(){
  $("tabs").innerHTML=TABS.map(function(t){
    return '<div class="tab'+(TAB===t[0]?" on":"")+'" data-t="'+t[0]+'">'+t[1]+'</div>';
  }).join("");
  $("tabs").querySelectorAll(".tab").forEach(function(el){
    el.addEventListener("click",function(){
      TAB=el.getAttribute("data-t");
      lsSet("last_tab",TAB);
      if(TAB!=="games") curTeam=null;
      render(); window.scrollTo({top:0,behavior:"smooth"});
    });
  });
}

function renderHeader(){
  if(TAB==="games"&&curTeam){
    var rows=teamGames(curTeam);
    $("hd").innerHTML='<span class="d num">'+rows.length+'</span> 場預測紀錄';
    $("sub").textContent=curTeam+" 本季";
    return;
  }
  if(TAB==="games"||TAB==="deep"){
    var p=cur.split("-"), n=DATA.days[cur].length;
    $("hd").innerHTML='<span class="d num">'+parseInt(p[1],10)+'/'+parseInt(p[2],10)+
      '</span> 共 <span class="d num">'+n+'</span> 場';
    $("sub").textContent=(cur>=TODAY)
      ? "用整季資料重建的球隊評分，算出每場的機率分布"
      : "以下是 "+cur+" 的預測";
    return;
  }
  var titles={teams:["球隊資料","全聯盟的實力、戰績與打法"],
              model:["模型表現","我們自己的成績，攤開來看"],
              about:["關於","這些數字是怎麼來的"]};
  var t=titles[TAB]||["",""];
  $("hd").textContent=t[0];
  $("sub").textContent=t[1];
}

function bind(){
  var host=$("view");
  host.querySelectorAll("[data-team]").forEach(function(el){
    el.addEventListener("click",function(){
      curTeam=el.getAttribute("data-team"); TAB="games";
      render(); window.scrollTo({top:0,behavior:"smooth"});
    });
  });
  host.querySelectorAll("#lockhd").forEach(function(el){
    el.addEventListener("click",function(){
      DIAGTAP++;
      if(DIAGTAP>=3){
        var d=$("diagbox");
        if(d){ d.style.display="block"; d.textContent=diagText(); }
      }
    });
  });
  host.querySelectorAll("#buybtn").forEach(function(el){
    el.addEventListener("click", buy);
  });
  /* 版本說明連點五下叫出購買流程測試面板 */
  host.querySelectorAll("#bvertap").forEach(function(el){
    el.addEventListener("click",function(){
      if(TESTBUY) return;
      TESTTAP++;
      if(TESTTAP>=5){ TESTBUY=true; render(); }
    });
  });
  host.querySelectorAll("#tdetails").forEach(function(el){
    el.addEventListener("click", tDetails); });
  host.querySelectorAll("#tbuy").forEach(function(el){
    el.addEventListener("click", tBuy); });
  host.querySelectorAll("#tlist").forEach(function(el){
    el.addEventListener("click", tList); });
  host.querySelectorAll("#tconsume").forEach(function(el){
    el.addEventListener("click", tConsume); });
  function goDate(d){
    if(!d || !DATA.days[d]) return;
    cur=d; curTeam=null; gi=0;
    render(); window.scrollTo({top:0,behavior:"smooth"});
  }
  host.querySelectorAll("#dsel").forEach(function(el){
    el.addEventListener("change",function(){ goDate(el.value); });
  });
  host.querySelectorAll("#msel").forEach(function(el){
    el.addEventListener("change",function(){
      /* 換月份就跳到那個月最新的一天，不要停在一個空日期 */
      var first=DATA.order.filter(function(d){ return d.slice(0,7)===el.value; })[0];
      goDate(first);
    });
  });
  host.querySelectorAll("#dprev").forEach(function(el){
    el.addEventListener("click",function(){
      goDate(DATA.order[DATA.order.indexOf(cur)+1]);   /* 降冪，+1 是更早 */
    });
  });
  host.querySelectorAll("#dnext").forEach(function(el){
    el.addEventListener("click",function(){
      goDate(DATA.order[DATA.order.indexOf(cur)-1]);
    });
  });
  host.querySelectorAll(".star").forEach(function(el){
    el.addEventListener("click",function(ev){
      if(ev && ev.stopPropagation) ev.stopPropagation();
      toggleFav(el.getAttribute("data-fav")); render();
    });
  });
  host.querySelectorAll("[data-share]").forEach(function(el){
    el.addEventListener("click",function(){
      var pair=el.getAttribute("data-share").split("|");
      var g=(DATA.days[cur]||[]).filter(function(x){
        return x.a===pair[0] && x.h===pair[1]; })[0];
      if(g) shareGame(g);
    });
  });
  var back=$("backbtn");
  if(back) back.addEventListener("click",function(){ curTeam=null; render(); });
  ["deepsub","teamsub"].forEach(function(id){
    var box=$(id);
    if(!box) return;
    var which=id==="deepsub"?"deep":"teams";
    box.querySelectorAll(".sub").forEach(function(el){
      el.addEventListener("click",function(){
        SUB[which]=el.getAttribute("data-s"); render();
      });
    });
  });
  /* 下拉選單：change 事件。
     ⚠️ 這裡一定要用函式把元素包起來。曾經寫成三個 addEventListener 共用一個
     `var sel`，結果三個閉包都指向最後一次賦值 —— 在沒有 rotsel 的分頁上
     sel 是 null，觸發時直接拋錯，選單看起來完全沒反應。 */
  function onChange(id, fn){
    var node=$(id);
    if(node && node.addEventListener){
      node.addEventListener("change", function(){ fn(node.value); });
    }
  }
  onChange("gamesel", function(v){ gi=parseInt(v,10)||0; render(); });
  onChange("modelsub", function(v){ SUB.model=v; render(); });
  onChange("rotsel", function(v){ ROTTEAM=v; render(); });
}

function render(){
  renderTabs(); renderHeader();
  var v={games:viewGames,deep:viewDeep,teams:viewTeams,
         model:viewModel,about:viewAbout}[TAB];
  $("view").innerHTML=v?v():"";
  bind();
}

/* ── 取得付費內容 ── */

/* ── 解鎖診斷 ──
   手機上的 TWA 沒有開發者工具，解鎖失敗時只能盲猜。
   把每一步的結果記下來，在上鎖畫面的標題連點三下就會顯示出來。
   一般使用者不會誤觸，所以這段可以一直留著不用拿掉。 */
var DIAG = {api:"?", dg:"?", buys:"?", tok:"?", srv:"?"};
var DIAGTAP = 0;

function diagText(){
  return "API "+DIAG.api+"｜商店 "+DIAG.dg+"｜購買 "+DIAG.buys+
         "｜憑證 "+DIAG.tok+"｜驗證 "+DIAG.srv;
}

function getPurchaseToken(){
  /* Digital Goods API：TWA 裡向 Google Play 問「這個人買過嗎」。
     一般瀏覽器沒有這個 API，直接視為未購買。 */
  DIAG.api = API ? "有" : "無";
  if(!("getDigitalGoodsService" in window)){
    DIAG.dg = "不支援";
    return Promise.resolve(null);
  }
  return window.getDigitalGoodsService("https://play.google.com/billing")
    .then(function(svc){
      if(!svc||!svc.listPurchases){ DIAG.dg="無服務"; return null; }
      DIAG.dg = "正常";
      return svc.listPurchases().then(function(l){
        l = l || [];
        DIAG.buys = l.length;
        /* 認明商品再取憑證。原本直接拿 l[0]，帳號裡若還有別的
           （或已退款的）購買紀錄就會拿錯那一筆，驗證當然過不了。 */
        var hit = null, i;
        for(i=0;i<l.length;i++){
          if(l[i] && l[i].itemId === SKU_ID){ hit = l[i]; break; }
        }
        if(!hit && l.length) hit = l[0];
        var t = (hit && hit.purchaseToken) || null;
        DIAG.tok = t ? "有" : "無";
        return t;
      });
    }).catch(function(e){
      DIAG.dg = "錯誤 " + (e && (e.name || e.message) || "?");
      return null;
    });
}

/* 付費內容離線快取。
   沒有這個的話，Worker 暫時掛掉或使用者在捷運上沒訊號時，
   付費使用者會看到上鎖畫面 —— 他付了錢卻被鎖在外面。
   所以最後一次成功取得的內容要留著，抓不到就先用舊的。 */
function cachePrem(d){
  try{ localStorage.setItem("prem_cache", JSON.stringify({t:Date.now(),d:d})); }catch(e){}
}
function loadCachedPrem(){
  var c=lsGet("prem_cache",null);
  if(c&&c.d){ PREM=c.d; PREM._cached=c.t; return true; }
  return false;
}

/* ── 購買 ──
   價格一律向 Google Play 要（getDetails），不寫死在程式裡：
   同一個商品在不同國家是不同幣別與金額，寫死一定會錯。
   一般瀏覽器沒有 Digital Goods API，SKU 會是 null，按鈕就不會出現，
   不會有「按了沒反應」的狀況。 */
var SKU_ID = "__SKU__";
var SKU    = null;
var BUYING = false;

/* ── 購買流程測試面板 ──
   TWA 沒有網址列也開不了開發者工具，所以測購買流程只能在 App 裡面測。
   進入方式：「關於」分頁的版本說明那行連點五下。桌面瀏覽器可以用 #buytest。
   平常完全不出現，一般使用者不會誤觸。 */
var TESTSKUS = __TESTSKUS__;
var TESTBUY  = (location.hash||"").indexOf("buytest") >= 0;
var TESTTAP  = 0;

function playSvc(){
  if(LOCKED) return Promise.resolve(null);
  /* 測試面板要能在還沒設 WORKER_URL（API 為空）時就用，
     否則得先切成 API 模式，那會把所有人鎖在外面。 */
  if(!API && !TESTBUY) return Promise.resolve(null);
  if(!("getDigitalGoodsService" in window)) return Promise.resolve(null);
  return window.getDigitalGoodsService("https://play.google.com/billing")
    .catch(function(){ return null; });
}

function loadSku(){
  playSvc().then(function(svc){
    if(!svc || !svc.getDetails) return null;
    return svc.getDetails([SKU_ID]).then(function(items){
      if(items && items.length){ SKU = items[0]; render(); }
    });
  }).catch(function(){});
}

function skuPrice(){
  if(!SKU || !SKU.price) return "";
  try{
    return new Intl.NumberFormat(navigator.language,
      {style:"currency", currency:SKU.price.currency}).format(Number(SKU.price.value));
  }catch(e){ return SKU.price.value+" "+SKU.price.currency; }
}

function buyBlock(){
  if(!SKU) return "";
  return '<button class="buy" id="buybtn">解鎖深度解析　'+esc(skuPrice())+'</button>'+
         '<div class="buymsg" id="buymsg"></div>';
}

function buy(){
  if(BUYING || !SKU) return;
  BUYING = true;
  var btn = $("buybtn"), box = $("buymsg");
  function say(t){ if(box) box.textContent = t; }
  if(btn) btn.disabled = true;

  var req;
  try{
    req = new PaymentRequest(
      [{supportedMethods:"https://play.google.com/billing",
        data:{sku:SKU.itemId}}],
      {total:{label:SKU.title||"解鎖深度解析",
              amount:{currency:SKU.price.currency, value:SKU.price.value}}});
  }catch(e){
    say("這台裝置無法使用 Google Play 付款。");
    BUYING = false; if(btn) btn.disabled = false; return;
  }

  /* 購買完成後憑證不一定馬上查得到，重試幾次再放棄 */
  function unlock(n){
    return loadPremium().then(function(){
      if(PREM) return true;
      if(n <= 0) return false;
      return new Promise(function(r){ setTimeout(r, 1500); })
        .then(function(){ return unlock(n-1); });
    });
  }

  req.show().then(function(resp){
    return resp.complete("success").then(function(){
      say("購買成功，正在解鎖…");
      return unlock(3);
    }).then(function(ok){
      if(!ok) say("購買已完成，但內容還沒同步。稍後重開 App 就會出現。");
    });
  }).catch(function(e){
    /* 使用者自己關掉付款畫面不算錯誤，不要嚇他 */
    say(e && e.name === "AbortError" ? "" : "沒有完成付款。");
  }).then(function(){
    BUYING = false; if(btn) btn.disabled = false;
  });
}

/* ── 測試面板本體 ── */

function testPanel(){
  if(!TESTBUY) return "";
  var opts = TESTSKUS.map(function(s){
    return '<option value="'+esc(s)+'">'+esc(s)+'</option>'; }).join("");
  return '<div class="about">'+
    '<h3>購買流程測試</h3>'+
    '<div class="note">這個區塊只有連點版本說明五下才會出現。'+
    '測完重開 App 就會消失，不必改程式。</div>'+
    '<select id="tsku">'+opts+'</select>'+
    '<div class="trow">'+
    '<button class="buy" id="tdetails">查商品</button>'+
    '<button class="buy" id="tbuy">購買</button>'+
    '<button class="buy" id="tlist">查已購買</button>'+
    '<button class="buy" id="tconsume">消耗掉</button>'+
    '</div>'+
    '<div class="diag tlog" id="tlog">按上面的按鈕開始。建議順序：'+
    '查商品 → 購買 → 查已購買 → 消耗掉 → 再購買一次。</div>'+
    '</div>';
}

function tsay(t){ var b=$("tlog"); if(b) b.textContent = t; }
function tadd(t){ var b=$("tlog"); if(b) b.textContent += "\n" + t; }
function tid(){ var s=$("tsku"); return s ? s.value : TESTSKUS[0]; }
function terr(e){
  return "✗ " + (e && ((e.name||"") + (e.message ? ": "+e.message : "")) || "不明錯誤");
}

function tNeedSvc(){
  return playSvc().then(function(svc){
    if(!svc){
      tadd("✗ 拿不到 Digital Goods 服務。");
      tadd("　常見原因：這不是從 Google Play 安裝的版本（側載的 APK 不算），");
      tadd("　或這個殼沒有打開 Play Billing。");
      return null;
    }
    return svc;
  });
}

function tDetails(){
  var id = tid();
  tsay("查商品 " + id + " …");
  tNeedSvc().then(function(svc){
    if(!svc) return;
    if(!svc.getDetails){ tadd("✗ 服務沒有 getDetails"); return; }
    return svc.getDetails([id]).then(function(items){
      if(!items || !items.length){
        tadd("✗ 查不到這個商品。");
        tadd("　檢查：商品 ID 拼字、商品是否為「有效」、這個版本是否已在測試軌道上線。");
        return;
      }
      var d = items[0];
      tadd("✓ " + (d.title||"(無標題)"));
      tadd("　價格 " + d.price.value + " " + d.price.currency);
      tadd("　type " + (d.type || "(未提供)"));
    });
  }).catch(function(e){ tadd(terr(e)); });
}

function tBuy(){
  var id = tid();
  tsay("購買 " + id + " …");
  tNeedSvc().then(function(svc){
    if(!svc) return;
    return svc.getDetails([id]).then(function(items){
      if(!items || !items.length){ tadd("✗ 查不到商品，先按「查商品」確認"); return; }
      var d = items[0], req;
      try{
        req = new PaymentRequest(
          [{supportedMethods:"https://play.google.com/billing", data:{sku:id}}],
          {total:{label:d.title||id,
                  amount:{currency:d.price.currency, value:d.price.value}}});
      }catch(e){ tadd("✗ 這台裝置建立不了 PaymentRequest"); tadd("　"+terr(e)); return; }
      return req.show().then(function(resp){
        var tok = resp.details && resp.details.token;
        return resp.complete("success").then(function(){
          tadd("✓ 付款流程完成");
          tadd("　憑證 " + (tok ? String(tok).slice(0,28)+"…" : "（沒拿到 token）"));
          if(!tok) tadd("　沒拿到 token 是問題：Worker 沒有東西可以驗證。");
        });
      });
    });
  }).catch(function(e){
    tadd(e && e.name === "AbortError" ? "（自己關掉付款畫面，不算錯誤）" : terr(e));
  });
}

function tList(){
  tsay("查已購買 …");
  tNeedSvc().then(function(svc){
    if(!svc) return;
    if(!svc.listPurchases){ tadd("✗ 服務沒有 listPurchases"); return; }
    return svc.listPurchases().then(function(l){
      l = l || [];
      tadd("共 " + l.length + " 筆");
      l.forEach(function(p){
        tadd("・" + p.itemId + "　" + String(p.purchaseToken||"").slice(0,20) + "…");
      });
      if(!l.length){
        tadd("（消耗性商品被 consume 之後就會從這裡消失，那是正常的）");
      }
    });
  }).catch(function(e){ tadd(terr(e)); });
}

function tConsume(){
  var id = tid();
  tsay("消耗 " + id + " …");
  tNeedSvc().then(function(svc){
    if(!svc) return;
    if(!svc.consume){
      tadd("✗ 這個版本的 Digital Goods API 沒有 consume。");
      tadd("　沒有 consume，消耗性商品買過一次就再也買不了第二次。");
      return;
    }
    return svc.listPurchases().then(function(l){
      l = l || [];
      var hit = null, i;
      for(i=0;i<l.length;i++){ if(l[i] && l[i].itemId === id){ hit = l[i]; break; } }
      if(!hit){ tadd("✗ 沒有這個商品的購買紀錄可以消耗"); return; }
      return svc.consume(hit.purchaseToken).then(function(){
        tadd("✓ 已消耗。現在再按一次「購買」應該要能買第二次 ——");
        tadd("　買不了就代表一日券的核心是壞的。");
      });
    });
  }).catch(function(e){ tadd(terr(e)); });
}

function loadPremium(){
  if(LOCKED) return Promise.resolve();

  if(!API){
    return fetch("premium.json?v="+BUILD)
      .then(function(r){ return r.ok?r.json():null; })
      .then(function(d){ if(d){ PREM=d; cachePrem(d); render(); } })
      .catch(function(){ if(loadCachedPrem()) render(); });
    return;
  }

  /* 先把快取端上來，使用者不必等驗證就看得到東西 */
  if(loadCachedPrem()) render();

  return getPurchaseToken().then(function(token){
    if(!token){ DIAG.srv="沒送出"; return null; }
    return fetch(API,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({token:token,v:BUILD})})
      .then(function(r){ DIAG.srv=r.status; return r.ok?r.json():null; });
  }).then(function(d){
    if(d){ PREM=d; cachePrem(d); render(); }
    else if(document.querySelector(".diag")) render();
  }).catch(function(e){ DIAG.srv="連不上"; });
}

render();
loadPremium();
loadSku();

/* 記住上次看的分頁，回來時不用重找 */
try{
  var lastTab=lsGet("last_tab",null);
  if(lastTab && ["games","deep","teams","model","about"].indexOf(lastTab)>=0){
    TAB=lastTab; render();
  }
}catch(e){}

function updBanner(){
  /* 已經有一個就不要疊第二個 */
  if(document.querySelector(".upd")) return;
  var b=document.createElement("div");
  b.className="upd";
  b.textContent="有新資料，點一下更新";
  b.addEventListener("click",function(){ location.reload(); });
  document.body.appendChild(b);
}

if("serviceWorker" in navigator && location.protocol.indexOf("http")===0){
  /* sw.js 用 stale-while-revalidate：畫面先用快取秒開，背景抓到新的
     才發這個訊息。舊版把提示綁在 service worker 檔案更新上，但那個檔
     幾乎不變，所以每天換的內容根本觸發不到，提示等於不存在。 */
  navigator.serviceWorker.addEventListener("message",function(ev){
    if(ev.data && ev.data.type==="content-updated") updBanner();
  });
  window.addEventListener("load",function(){
    navigator.serviceWorker.register("sw.js").then(function(reg){
      /* 有新版本時給一個可以點的提示，而不是讓使用者一直看到舊畫面 */
      reg.addEventListener("updatefound",function(){
        var nw=reg.installing;
        if(!nw) return;
        nw.addEventListener("statechange",function(){
          if(nw.state==="installed" && navigator.serviceWorker.controller) updBanner();
        });
      });
    }).catch(function(){});
  });
}
"""


def render_html(free, league="WNBA", locked=False, today=None, api_url=None):
    today = today or sports_today()
    js = (JS.replace("__LOCKED__", "true" if locked else "false")
            .replace("__DATA__", json.dumps(free, ensure_ascii=False,
                                            separators=(",", ":")))
            .replace("__TODAY__", today)
            .replace("__BUILD__", datetime.now().strftime("%Y%m%d%H%M"))
            .replace("__API__", api_url or "")
            .replace("__SKU__", PRODUCT_ID)
            .replace("__TESTSKUS__", json.dumps(TEST_SKUS))
            .replace("__BVER__", VERSION)
            .replace("__CHANGELOG__", json.dumps(
                [{"d": d, "web": web, "eng": eng, "items": items}
                 for d, web, eng, items in CHANGELOG], ensure_ascii=False)))
    return """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="%(ink)s">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="description" content="用整季資料重建球隊評分，算出每場比賽的勝率與分差分布。僅供資訊參考及娛樂用途。">
<link rel="manifest" href="manifest.json">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="icon-192.png">
<title>%(league)s 賽事預測</title>
<style>%(css)s</style>
</head>
<body>
<header>
  <div class="eyebrow">%(league)s · 賽事預測</div>
  <h1 id="hd"></h1>
  <div class="sub" id="sub"></div>
</header>
<nav class="tabs" id="tabs"></nav>
<div id="view"></div>
<footer>
  <b>怎麼讀這些數字</b><br>
  勝率是模型認為某隊獲勝的機率，不是保證。曲線畫的是分差可能落在哪，
  越寬代表這場越難預測 —— 籃球單場的分差標準差大約 13 分，
  所以就算模型看好某隊贏 10 分，實際打出 20 分或反而輸球都很常見。<br><br>
  所有內容由統計模型與歷史數據產生，僅供資訊參考及娛樂用途。
</footer>
<script>%(js)s</script>
</body>
</html>""" % {"ink": PALETTE["ink"], "league": league,
               "css": CSS % PALETTE, "js": js}


def build(log_path, out_dir, league="WNBA", locked=False,
          premium_dir=None, api_url=None):
    """league 是顯示在頁首的聯盟名稱字串（"WNBA"／"NBA"），不是資料。"""
    with open(log_path, encoding="utf-8") as f:
        log = json.load(f)
    # 全聯盟表是日誌旁邊的獨立檔案。舊版曾把它塞在日誌的 "_league" 鍵，
    # 兩種來源都讀得進來，換版本不會漏資料。
    #
    # ⚠️ 變數名一定要跟參數 league 區隔開。曾經兩者同名，結果整張表被
    # 當成聯盟名稱塞進頁首模板 %(league)s，網頁最上面就印出一整串 dict。
    league_table = log.get("_league")
    side = os.path.join(os.path.dirname(os.path.abspath(log_path)), "wnba_league.json")
    if os.path.exists(side):
        try:
            with open(side, encoding="utf-8") as f:
                league_table = json.load(f)
        except Exception:
            pass
    # 日誌只該有 {日期: [比賽,...]}；跳過底線開頭的鍵與非清單的值
    log = {d: v for d, v in log.items()
           if isinstance(v, list) and v and not d.startswith("_")}
    if not log:
        return None
    os.makedirs(out_dir, exist_ok=True)

    # 公開模式（宣傳期）不設限，一切照舊全開；
    # 切回 Worker 模式（有 api_url）才啟用付費牆。
    # today 不傳的話 build_free 會退回「日誌裡最新的日期」。多數時候一樣，
    # 但 13:07 那班預抓下一個賽事日之後，最新日期會是明天 ——
    # 免費示範那一場就會落在還沒開打的明天，而不是今天。所以明確傳。
    free = build_free(log, restrict=bool(api_url), today=sports_today())
    rec = _records_lite(league_table)
    if rec:
        free["records"] = rec
    if league_table:
        # 【v1.31】預測引擎版本。「關於」分頁是公開內容，所以放免費 payload。
        # ⚠️ 這一段要放在 `if league_table:` 底下，不能放進 `if rec:` ——
        #    戰績表在賽季初或資料不全時可能是空的，那時就讀不到版本了。
        if league_table.get("engine_version"):
            free["engine"] = league_table["engine_version"]
        if league_table.get("meta"):
            free["meta"] = league_table["meta"]          # 隊徽與代表色
        st = {}
        for t in league_table.get("teams", []):
            if t.get("streak"):
                st[t["team"]] = t["streak"]
        if st:
            free["streaks"] = st
    prem = build_premium(log, league_table)
    if league_table:
        prem["league"] = league_table
    html = render_html(free, league, locked,
                       today=sports_today(),
                       api_url=api_url)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    # premium_dir 有給就寫到那裡（不會被發布到公開網站），
    # 由後端驗證購買憑證之後才提供給App。
    pdir = premium_dir or out_dir
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "premium.json"), "w", encoding="utf-8") as f:
        json.dump(prem, f, ensure_ascii=False, separators=(",", ":"))
    return {"builder": VERSION, "days": len(free["days"]), "latest": free["order"][0],
            "games": sum(len(v) for v in free["days"].values()),
            "settled": free["settled"], "locked": locked,
            "premium_dir": pdir, "api": bool(api_url),
            "html_kb": round(len(html.encode("utf-8")) / 1024, 1)}


if __name__ == "__main__":
    #   python app_builder.py <日誌> <輸出資料夾>
    #        [--locked]                  付費內容顯示上鎖畫面
    #        [--premium 資料夾]          付費檔寫到別處，不發布到公開網站
    #        [--api https://…/premium]   App 改向這個端點索取付費內容
    argv = sys.argv[1:]
    locked = "--locked" in argv

    def opt(name):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                return argv[i + 1]
        return None

    pdir, api = opt("--premium"), opt("--api")
    args = [a for a in argv
            if not a.startswith("--") and a != pdir and a != api]
    src = args[0] if args else "wnba_prediction_log.json"
    dst = args[1] if len(args) > 1 else "."
    print(build(src, dst, locked=locked, premium_dir=pdir, api_url=api))