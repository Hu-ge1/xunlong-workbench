"""Seven-step review engine — follows Simon's 短线复盘七步框架."""
import os, statistics, datetime
from collections import defaultdict
from typing import Any, Dict, List, Optional

# 复盘报告默认写入本机 Obsidian 库。用 OBSIDIAN_VAULT_PATH 覆盖;
# 未配置时回退到 %USERPROFILE%\Documents\Obsidian Vault,不含任何个人用户名。
VAULT = os.path.expandvars(
    os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    or r"%USERPROFILE%\Documents\Obsidian Vault"
)
LOG_DIR = os.path.join(VAULT, "07-市场日志")

def _avg(v): return statistics.mean(v) if v else None
def _pct(n,d): return round(n/max(1,d)*100,1)

# ═══ 01 ═══
def step1_market_mood(market, candidates, pushed):
    indices = market.get("indices",[])
    score = market.get("score",0)
    breadth = market.get("breadth",{})
    limit_up = [c for c in candidates if float(c.get("snapshot",{}).get("change_pct") or c.get("change_pct",0) or 0) >= 9.5]
    limit_down = [c for c in candidates if float(c.get("snapshot",{}).get("change_pct") or c.get("change_pct",0) or 0) <= -9.5]
    pushed_low = [p for p in pushed if float(p.get("snapshot",{}).get("change_pct") or p.get("change_pct",0) or 0) < 5]
    bbr = _pct(len(pushed_low), len(pushed)) if pushed else 0
    up_c = breadth.get("advance",0); dn_c = breadth.get("decline",0)
    if score >= 2 and len(limit_up) >= 5 and bbr < 30: mood, icon = "进攻", "🟢"
    elif score >= -3 and len(limit_up) >= 2: mood, icon = "中性", "🟡"
    else: mood, icon = "防守", "🔴"
    v = {"进攻":"容错率高，可积极","中性":"精选个股，控制仓位","防守":"降低预期，优先保本"}.get(mood,"")
    return {"mood":mood,"icon":icon,"score":score,"limit_up":len(limit_up),"limit_down":len(limit_down),
            "bbr":bbr,"breadth":f"{up_c}涨/{dn_c}跌","verdict":v,
            "indices":[{"n":i.get("name","?"),"p":i.get("price",0),"chg":i.get("change_pct",0)} for i in indices]}

# ═══ 02 ═══
def step2_board_ladder(candidates):
    cons = sorted([c for c in candidates if (c.get("consecutive_boards") or 0) >= 1],
                  key=lambda x: -(x.get("consecutive_boards") or 0))
    if not cons: return {"max_h":0,"ladder":"无连板数据","fb":"无","stocks":[]}
    mx = cons[0].get("consecutive_boards",0)
    heights = sorted(set(c.get("consecutive_boards",0) for c in cons), reverse=True)
    missing = [h for h in range(mx,0,-1) if h not in heights]
    lad = "完整" if not missing else f"断层(缺{missing})"
    top = cons[0]; tc = float(top.get("snapshot",{}).get("change_pct") or top.get("change_pct",0) or 0)
    fb = "正向" if tc>0 else "负向⚠️" if tc<-5 else "中性"
    return {"max_h":mx,"ladder":lad,"fb":fb,
            "stocks":[{"n":c.get("name","?"),"code":c.get("code","?"),"h":c.get("consecutive_boards",0)} for c in cons[:8]]}

# ═══ 03 ═══
def step3_board_structure(board_data):
    boards = sorted(board_data, key=lambda b: b.get("avg_change_pct",0) or 0, reverse=True)
    r = []
    for b in boards[:5]:
        name = b.get("name","?"); stocks = b.get("stocks",[]); chg = b.get("avg_change_pct",0); cnt = b.get("count",0)
        if chg > 5 and cnt > 5: stage = "高潮"
        elif chg > 2 and cnt > 3: stage = "发酵"
        elif chg > 0: stage = "首次启动"
        else: stage = "轮动"
        r.append({"name":name,"change":chg,"count":cnt,"stage":stage,
                   "leader":stocks[0] if stocks else "?",
                   "mid":stocks[1] if len(stocks)>1 else "-",
                   "structure":"完整" if cnt>=3 and chg>0 else "龙头独舞" if cnt<=2 else "待观察"})
    return {"boards":r}

# ═══ 04 ═══
def step4_fund_flow(candidates):
    srt = sorted(candidates, key=lambda c: float(c.get("snapshot",{}).get("amount_wan") or c.get("amount",0) or 0), reverse=True)
    top = srt[:8]
    off, df, rc = [], [], []
    for c in top:
        n = c.get("name","?"); chg = float(c.get("snapshot",{}).get("change_pct") or c.get("change_pct",0) or 0)
        if chg>3: off.append(n)
        elif chg<-2: df.append(n)
        else: rc.append(n)
    pref = "进攻" if len(off)>=3 else "防御" if len(df)>=3 else "回流" if rc else "分散"
    return {"pref":pref,"offense":off[:3],"defense":df[:3],
            "top":[{"n":c.get("name","?"),"code":c.get("code","?"),
                     "amt":float(c.get("snapshot",{}).get("amount_wan") or c.get("amount",0) or 0),
                     "chg":float(c.get("snapshot",{}).get("change_pct") or c.get("change_pct",0) or 0)} for c in top[:6]]}

# ═══ 05 ═══
def step5_loss_effect(candidates, pushed):
    bl = [c for c in candidates if (float(c.get("snapshot",{}).get("amplitude_pct") or 0)>8
           and float(c.get("snapshot",{}).get("change_pct") or c.get("change_pct",0) or 0)<-3)]
    ld = [c for c in candidates if float(c.get("snapshot",{}).get("change_pct") or c.get("change_pct",0) or 0)<=-9.5]
    hf = [c for c in candidates if (c.get("consecutive_boards") or 0)>=2
          and float(c.get("snapshot",{}).get("change_pct") or c.get("change_pct",0) or 0)<-5]
    total = len(bl)+len(ld)+len(hf)
    if total>=8: level, limit = "严重", "仓位≤10%，回避高位接力"
    elif total>=3: level, limit = "扩散", "仓位≤30%，减少接力"
    else: level, limit = "可控", "正常仓位，注意止盈"
    return {"level":level,"limit":limit,"big_loss":len(bl),"limit_down":len(ld),
            "high_fall":len(hf),"total":total,"samples":[c.get("name","?") for c in (bl+ld+hf)[:5]]}

# ═══ 06 ═══
def step6_watchlist(pushed):
    w = []
    for p in pushed[:5]:
        name = p.get("name","?"); code = p.get("code","?"); score = p.get("score",0)
        trig = (p.get("triggers") or {}).get("selected","?")
        w.append({"name":name,"code":code,"score":score,
                   "reason":f"擒龙推送(触发={trig}，评分={score:.0f})",
                   "observe":"竞价高于昨收+量比>1.5+不跌破开盘价",
                   "invalid":"竞价低开>2%或开盘5分钟跌破昨收"})
    return {"stocks":w}

# ═══ 07 ═══
def step7_next_day(mood, loss, watchlist):
    plans = []
    first = watchlist[0]["name"] if watchlist else "观察池"
    if mood == "防守":
        plans.append({"if":"竞价低开且跌停家数>5","then":"空仓等待，不开新仓"})
        plans.append({"if":"龙头高开高走且板块跟涨","then":f"轻仓试探{first}，仓位<=10%"})
    elif mood == "中性":
        plans.append({"if":"龙头强势+板块放量","then":f"关注{first}，仓位<=30%"})
        plans.append({"if":"高位股开始补跌","then":"降低仓位，优先回避高位"})
    else:
        plans.append({"if":"龙头继续超预期","then":"重点观察同方向补涨"})
        plans.append({"if":"板块继续放量","then":"关注第一次分歧后的承接机会"})
    bans = ["不追竞价高开>5%的票（已抢跑）","不做临时起意的追涨","不加仓亏损票"]
    if loss["level"] in ("扩散","严重"): bans.append("不参与连板接力（亏钱效应扩散）")
    return {"plans":plans,"bans":bans}

# ═══ PUSH ═══
def pushed_review(pushed, backtest):
    if not pushed: return {"stocks":[],"stats":{"total":0,"note":"无推送"}}
    stocks, pnls = [], []
    for ps in pushed:
        code = ps.get("code","")
        matches = [r for r in backtest if r.get("code")==code]
        lt = matches[-1] if matches else {}
        pnl = lt.get("pnl_pct")
        if pnl is not None: pnls.append(pnl)
        trig = (ps.get("triggers") or {}).get("selected","?")
        board = (ps.get("snapshot") or {}).get("industry") or ps.get("industry","?")
        stocks.append({"name":ps.get("name","?"),"code":code,"score":ps.get("score",0),
                        "trigger":trig,"board":board,"pnl":pnl,"best7":lt.get("best_7d_pct")})
    return {"stocks":stocks,"stats":{"total":len(stocks),"avg_pnl":_avg(pnls) if pnls else None,
            "positive":sum(1 for p in pnls if p>0) if pnls else 0,
            "pos_rate":_pct(sum(1 for p in pnls if p>0),len(pnls)) if pnls else 0}}

# ═══ REPORT ═══
def generate_full_report(market, candidates, pushed, backtest, board_data, trade_date=None, emotion_phase="低迷"):
    d = trade_date or datetime.date.today().isoformat()
    s1 = step1_market_mood(market, candidates, pushed)
    s2 = step2_board_ladder(candidates)
    s3 = step3_board_structure(board_data)
    s4 = step4_fund_flow(candidates)
    s5 = step5_loss_effect(candidates, pushed)
    s6 = step6_watchlist(pushed)
    s7 = step7_next_day(s1["mood"], s5, s6["stocks"])
    pr = pushed_review(pushed, backtest)

    L = []
    L.append(f"# 🐉 寻龙复盘日报 — {d}")
    L.append("")
    L.append(f"> {s1['icon']} 市场定性：**{s1['mood']}** | {s1['verdict']}")
    L.append(f"> 策略版本：dragon-v1.2-2026.07.23-sevenstep")
    L.append("")
    L.append("---")
    L.append("")

    # Push
    L.append("## 📌 今日推送复盘")
    L.append("")
    if not pr["stocks"]:
        L.append("今日无推送。")
    else:
        L.append("| 股票 | 评分 | 触发 | 板块 | 今日收益 | 7日最佳 |")
        L.append("|------|:--:|------|------|:--:|:--:|")
        for s in pr["stocks"]:
            ps = f"{s['pnl']:+.2f}%" if s['pnl'] is not None else "-"
            bs = f"{s['best7']:+.2f}%" if s['best7'] is not None else "-"
            L.append(f"| **{s['name']}**({s['code']}) | {s['score']:.0f} | {s['trigger']} | {s['board']} | {ps} | {bs} |")
        st = pr["stats"]
        if st["avg_pnl"] is not None:
            L.append("")
            L.append(f"> 均收益 **{st['avg_pnl']:+.2f}%** | 正收益 {st['positive']}/{st['total']} ({st['pos_rate']:.0f}%)")
    L.append("")

    # 01
    L.append("## 01 市场情绪")
    L.append("")
    L.append(f"**{s1['icon']} {s1['mood']}** — {s1['verdict']}")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|------|:--|")
    L.append(f"| 涨停家数 | {s1['limit_up']} |")
    L.append(f"| 跌停家数 | {s1['limit_down']} |")
    L.append(f"| 炸板率 | {s1['bbr']}% |")
    L.append(f"| 涨跌比 | {s1['breadth']} |")
    L.append(f"| 市场信号 | {s1['score']:+d} |")
    L.append("")
    L.append("| 指数 | 点位 | 涨跌 |")
    L.append("|------|:--:|:--:|")
    for idx in s1["indices"]:
        L.append(f"| {idx['n']} | {idx['p']:.1f} | {idx['chg']:+.2f}% |")
    L.append("")

    # 02
    L.append("## 02 连板梯队")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|------|:--|")
    L.append(f"| 最高板 | {s2['max_h']}板 |")
    L.append(f"| 梯队 | {s2['ladder']} |")
    L.append(f"| 龙头反馈 | {s2['fb']} |")
    if s2["stocks"]:
        names = ", ".join(f"{s['n']}({s['h']}板)" for s in s2["stocks"][:5])
        L.append(f"| 连板股 | {names} |")
    L.append("")

    # 03
    L.append("## 03 板块结构")
    L.append("")
    if s3["boards"]:
        L.append("| 板块 | 涨幅 | 阶段 | 结构 | 龙头 | 中军 |")
        L.append("|------|:--:|------|------|------|------|")
        for b in s3["boards"]:
            L.append(f"| {b['name']} | {b['change']:+.1f}% | {b['stage']} | {b['structure']} | {b['leader']} | {b['mid']} |")
    L.append("")

    # 04
    L.append("## 04 资金去向")
    L.append("")
    L.append(f"**资金偏好：{s4['pref']}**")
    L.append("")
    if s4["top"]:
        L.append("| 股票 | 成交额(万) | 涨跌 |")
        L.append("|------|:--:|:--:|")
        for t in s4["top"]:
            L.append(f"| {t['n']} | {t['amt']:.0f} | {t['chg']:+.1f}% |")
    if s4["offense"]: L.append("")
    if s4["offense"]: L.append(f"进攻方向：{', '.join(s4['offense'])}")
    if s4["defense"]: L.append(f"防御方向：{', '.join(s4['defense'])}")
    L.append("")

    # 05
    L.append("## 05 亏钱效应")
    L.append("")
    icon5 = {"可控":"🟢","扩散":"🟡","严重":"🔴"}.get(s5["level"],"⚪")
    L.append(f"**{icon5} {s5['level']}** — {s5['limit']}")
    L.append("")
    L.append("| 类型 | 数量 |")
    L.append("|------|:--:|")
    L.append(f"| 大面(高开回落) | {s5['big_loss']} |")
    L.append(f"| 跌停 | {s5['limit_down']} |")
    L.append(f"| 高位补跌 | {s5['high_fall']} |")
    if s5["samples"]: L.append(f"| 样本 | {', '.join(s5['samples'][:4])} |")
    L.append("")

    # 06
    L.append("## 06 观察池")
    L.append("")
    if s6["stocks"]:
        for i, w in enumerate(s6["stocks"], 1):
            L.append(f"**{i}. {w['name']}**({w['code']}) 评分{w['score']:.0f}")
            L.append(f"  - 入池理由：{w['reason']}")
            L.append(f"  - 观察条件：{w['observe']}")
            L.append(f"  - 失效条件：{w['invalid']}")
            L.append("")
    else:
        L.append("无推送，观察池为空。")
    L.append("")

    # 07
    L.append("## 07 次日预案")
    L.append("")
    for p in s7["plans"]:
        L.append(f"- **如果** {p['if']}，**那么** {p['then']}")
    L.append("")
    L.append("### 🚫 今日禁做项")
    for b in s7["bans"]:
        L.append(f"- {b}")
    L.append("")

    L.append("---")
    L.append(f"*{datetime.datetime.now():%Y-%m-%d %H:%M} | 七步框架 · 短线复盘*")
    L.append("")
    L.append("> ⚠️ 仅供研究，不构成投资建议。")
    return "\n".join(L)

def write_report(md, trade_date=None):
    d = trade_date or datetime.date.today().isoformat()
    os.makedirs(LOG_DIR, exist_ok=True)
    p = os.path.join(LOG_DIR, f"{d}.md")
    with open(p, "w", encoding="utf-8") as f: f.write(md)
    return p

def run_review(market, board_data, candidates, pushed, backtest, trade_date=None, emotion_phase="低迷"):
    md = generate_full_report(market, candidates, pushed, backtest, board_data, trade_date, emotion_phase)
    path = write_report(md, trade_date)
    # Return the complete report to API/MCP callers.  Previously only a
    # 400-character preview and a local Obsidian path were returned, so Hermes
    # rebuilt an unrelated legacy summary instead of delivering this report.
    return {"path": path, "length": len(md), "preview": md[:400] + "...", "markdown": md}

def analyze_backtest(records):
    pnls = [r.get("pnl_pct",0) or 0 for r in records]
    return {"total":len(records),"avg_pnl":_avg(pnls) or 0}

def generate_markdown(stats, d=None):
    return f"# 复盘\n\n统计完成。均收益 {stats.get('avg_pnl',0):.2f}%。"
