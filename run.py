# ================================================================
#   晚 報　Colab 完整版
#   貼進 Google Colab，按 ▶，等約 2 分鐘
#   會自己去證交所＋期交所抓資料，算好判讀，印出報告
# ================================================================

天數 = 40          # 要往回抓幾個交易日（40 天約兩個月，夠算波段高點）

# ----------------------------------------------------------------
import requests, time, io, csv, re
from datetime import datetime, timedelta, timezone

台北 = timezone(timedelta(hours=8))
今天 = datetime.now(台北).strftime("%Y%m%d")
UA = {"Referer": "https://www.taifex.com.tw/cht/3/futContractsDate",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
原始樣本 = {}          # 出錯時拿來給 Claude 看


def 轉數字(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("元", "")
    if s in ("", "-", "--", "N/A"):
        return None
    負 = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if 負 else v


def 抓(url, params=None, post=False):
    for _ in range(3):
        try:
            if post:
                r = requests.post(url, data=params, headers=UA, timeout=25)
            else:
                r = requests.get(url, params=params, headers=UA, timeout=25)
            if r.status_code == 200:
                return r
        except Exception:
            pass
        time.sleep(2)
    return None


# ================================================================
print("=" * 60)
print("  晚報　資料抓取中")
print("=" * 60)

# ---------- 1. 指數與成交值（順便取得交易日清單） ----------
print("\n[1/4] 加權指數 ", end="", flush=True)

指數表 = {}
年, 月 = int(今天[:4]), int(今天[4:6])
for _ in range(4):                       # 往回抓四個月
    r = 抓("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK",
          {"date": f"{年}{月:02d}01", "response": "json"})
    if r:
        try:
            j = r.json()
            原始樣本.setdefault("FMTQIK", j)
            if j.get("stat") == "OK":
                for row in j.get("data", []):
                    try:
                        y, m, d = str(row[0]).strip().split("/")
                        指數表[f"{int(y)+1911}{m}{d}"] = {
                            "成交值": 轉數字(row[2]),
                            "收盤": 轉數字(row[4]),
                            "漲跌": 轉數字(row[5])}
                    except Exception:
                        pass
        except Exception:
            pass
    print(".", end="", flush=True)
    time.sleep(1.5)
    月 -= 1
    if 月 < 1:
        年, 月 = 年 - 1, 12

交易日 = sorted(指數表)[-天數:]
print(f" 取得 {len(交易日)} 個交易日")

if not 交易日:
    print("\n證交所指數抓取失敗。把下面這段貼給 Claude：")
    print(str(原始樣本.get("FMTQIK"))[:1500])
    import sys; sys.exit(1)


# ---------- 2. 融資餘額 ----------
print(f"[2/4] 融資餘額 ", end="", flush=True)

融資表 = {}
for i, d in enumerate(交易日, 1):
    r = 抓("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
          {"date": d, "selectType": "MS", "response": "json"})
    if r:
        try:
            j = r.json()
            原始樣本.setdefault("MI_MARGN", j)
            列 = []
            for t in j.get("tables", []):
                列 += t.get("data", [])
            列 += j.get("data", [])
            for row in 列:
                if not row:
                    continue
                名 = str(row[0])
                if "融資" in 名 and "金額" in 名:
                    # 欄位：項目 買進 賣出 現金償還 前日餘額 今日餘額
                    # 標題寫「融資金額(仟元)」→ 仟元換億要除 1e5，不是 1e8
                    v = 轉數字(row[5]) if len(row) > 5 else 轉數字(row[-1])
                    if v:
                        除數 = 1e5 if "仟元" in 名 else 1e8
                        融資表[d] = round(v / 除數, 2)
                    break
        except Exception:
            pass
    if i % 10 == 0:
        print(".", end="", flush=True)
    time.sleep(1.0)
print(f" 取得 {len(融資表)} 天")


# ---------- 3. 三大法人 ----------
print(f"[3/4] 三大法人 ", end="", flush=True)

法人表 = {}
for i, d in enumerate(交易日, 1):
    r = 抓("https://www.twse.com.tw/rwd/zh/fund/BFI82U",
          {"dayDate": d, "type": "day", "response": "json"})
    if r:
        try:
            j = r.json()
            原始樣本.setdefault("BFI82U", j)
            if j.get("stat") == "OK":
                rec = {}
                for row in j.get("data", []):
                    if not row:
                        continue
                    名, v = str(row[0]), 轉數字(row[-1])
                    if v is None:
                        continue
                    v = round(v / 1e8, 2)
                    if "投信" in 名:
                        rec["投信"] = v
                    elif "外資" in 名 and "自營" not in 名:
                        rec["外資"] = v
                if rec:
                    法人表[d] = rec
        except Exception:
            pass
    if i % 10 == 0:
        print(".", end="", flush=True)
    time.sleep(1.0)
print(f" 取得 {len(法人表)} 天")


# ---------- 4. 外資台指期淨空單 ----------
print("[4/4] 外資台指期 ", end="", flush=True)

期貨表 = {}

def 解析期貨(html):
    """
    期交所表格用 rowspan：商品名稱只寫在該商品的第一列，
    自營商／投信／外資三列共用。所以「外資」那列不含「臺股期貨」字樣。
    解法：邊掃邊記住目前是哪個商品。

    未平倉區塊欄位（表格最後 6 個數字）：
      多方口數 多方金額 空方口數 空方金額 淨額口數 淨額金額
    取 多方口數 − 空方口數 自己算，避免欄位位移時默默給錯數。
    """
    h = html.replace("\n", "").replace("\r", "")
    目前商品 = ""
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.I | re.S):
        tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)
        欄 = [re.sub(r"<[^>]+>", "", x).replace("&nbsp;", " ").strip() for x in tds]
        if not 欄:
            continue

        # 這列有沒有帶出新的商品名稱
        for c in 欄[:3]:
            if "期貨" in c and len(c) <= 12:
                目前商品 = c
                break

        if not any("外資" in c for c in 欄[:4]):
            continue
        if "臺股期貨" not in 目前商品 and "台股期貨" not in 目前商品:
            continue

        數 = [轉數字(x) for x in 欄]
        數 = [x for x in 數 if x is not None]
        if len(數) >= 6:
            多, 空 = 數[-6], 數[-4]
            return int(多 - 空), "未平倉多空相減"
        if len(數) >= 2:
            return int(數[-2]), "淨額欄"
    return None, None


解析方式 = None
for i, d in enumerate(交易日, 1):
    dd = f"{d[:4]}/{d[4:6]}/{d[6:8]}"
    r = 抓("https://www.taifex.com.tw/cht/3/futContractsDate",
          {"queryType": "1", "goDay": "", "doQuery": "1",
           "dateaddcnt": "", "queryDate": dd, "commodityId": "TXF"},
          post=True)
    if r:
        try:
            txt = r.text
            i外 = txt.find("外資")
            原始樣本.setdefault("TAIFEX",
                txt[max(0, i外 - 2500): i外 + 1500] if i外 > 0 else txt[:2000])
            v, how = 解析期貨(txt)
            if v is not None:
                期貨表[d] = v
                解析方式 = how
        except Exception:
            pass
    if i % 10 == 0:
        print(".", end="", flush=True)
    time.sleep(1.2)
print(f" 取得 {len(期貨表)} 天" + (f"（{解析方式}）" if 解析方式 else ""))


# ================================================================
#   組報告
# ================================================================
最新 = max(融資表) if 融資表 else 交易日[-1]
前一 = None
for d in reversed(交易日):
    if d < 最新 and d in 融資表:
        前一 = d
        break

收 = 指數表.get(最新, {})
融 = 融資表.get(最新)
融前 = 融資表.get(前一) if 前一 else None
法 = 法人表.get(最新, {})
期 = 期貨表.get(最新)
期前 = 期貨表.get(前一) if 前一 else None

融增減 = round(融 - 融前, 2) if (融 and 融前) else None
融漲跌率 = round(融增減 / 融前 * 100, 2) if (融增減 and 融前) else None
指漲跌率 = None
if 收.get("收盤") and 收.get("漲跌") is not None:
    昨 = 收["收盤"] - 收["漲跌"]
    指漲跌率 = round(收["漲跌"] / 昨 * 100, 2) if 昨 else None

高點 = max(融資表.values()) if 融資表 else None
去化 = round((融 - 高點) / 高點 * 100, 2) if (融 and 高點) else None

比值 = None
逆勢加碼 = False
if 融漲跌率 is not None and 指漲跌率 is not None and 指漲跌率 < 0:
    if 融漲跌率 < 0:
        比值 = round(融漲跌率 / 指漲跌率, 2)   # 兩者同向下跌才有意義
    else:
        逆勢加碼 = True                        # 指數跌、融資增 → 最差情況

# 投信連買
連買, 累計 = 0, 0.0
for d in reversed(交易日):
    v = 法人表.get(d, {}).get("投信")
    if v is None:
        continue
    if v > 0:
        連買 += 1
        累計 += v
    else:
        break


def 顯示(v, 單位="", 小數=2):
    return "—" if v is None else f"{v:,.{小數}f}{單位}"


def 正負(v, 單位="", 小數=2):
    return "—" if v is None else f"{'+' if v > 0 else ''}{v:,.{小數}f}{單位}"


print("\n\n" + "=" * 60)
print(f"  晚 報　{最新[:4]}-{最新[4:6]}-{最新[6:8]}")
print("=" * 60)

print(f"\n  加權指數      {顯示(收.get('收盤'))}   "
      f"{正負(收.get('漲跌'))}（{正負(指漲跌率, '%')}）")
print(f"  成交值        {顯示((收.get('成交值') or 0)/1e8, ' 億')}")
print(f"  融資餘額      {顯示(融, ' 億')}   {正負(融增減, ' 億')}（{正負(融漲跌率, '%')}）")
print(f"  投信買賣超    {正負(法.get('投信'), ' 億')}   連 {連買} 買，累計 {顯示(累計, ' 億')}")
print(f"  外資買賣超    {正負(法.get('外資'), ' 億')}")
print(f"  外資台指期    {顯示(期, ' 口', 0)}   "
      f"{正負(期 - 期前, ' 口', 0) if (期 is not None and 期前 is not None) else '—'}")

print("\n" + "-" * 60)
print("  判　讀")
print("-" * 60)

if 逆勢加碼:
    print(f"\n  🔴 指數下跌 {指漲跌率}%，融資反增 {正負(融增減,' 億')}（{正負(融漲跌率,'%')}）")
    print("     逆勢加碼 — 比任何比值都糟，浮額不減反增")
elif 比值 is not None:
    判 = ("❌ 硬撐，浮額未清" if 比值 < 0.5 else
          "⚪ 中性" if 比值 < 1.0 else
          "🟡 開始認賠" if 比值 < 1.5 else "✅ 主動出清")
    print(f"\n  去槓桿比值（融資減幅÷指數跌幅）  {比值}  {判}")
else:
    print("\n  去槓桿比值　當日非下跌，不適用")

反彈回補 = None
if 指漲跌率 is not None and 指漲跌率 >= 2.0 and 融增減 is not None:
    反彈回補 = 融增減 <= 0
    print("\n  反彈回補檢查（最高權重）")
    print("    " + ("✅ 指數反彈但融資未增，換手成功" if 反彈回補 else
                    f"⚠️ 指數反彈 {指漲跌率}% 但融資 {正負(融增減,' 億')}，新槓桿進場"))

if 去化 is not None:
    print(f"\n  融資自波段高點  {顯示(高點,' 億')} → {顯示(融,' 億')}  （{顯示(去化,'%')}）")

if 連買 == 0:
    print("\n  🔴 投信連買中斷 — 內資承接力示警")
else:
    print(f"\n  投信連 {連買} 買，未中斷")

if 期 is not None and 期前 is not None:
    差 = 期 - 期前
    if abs(差) >= 5000:
        print(f"\n  外資期貨　單日 {正負(差,' 口',0)} — 新增方向性部位"
              + ("（不單獨作為看空理由）" if 差 < 0 else "，需現貨同步轉買才成立"))
    else:
        print(f"\n  外資期貨　單日 {正負(差,' 口',0)}，未達 ±5,000 門檻，無訊息")

print("\n" + "-" * 60)
print("  落底檢核")
print("-" * 60)
必要 = [("反彈日融資未回補", 反彈回補 is True,
        "今日非反彈日" if 反彈回補 is None else ""),
       ("融資自高點去化 ≥ 20%", (去化 or 0) <= -20, f"目前 {顯示(去化,'%')}")]
for 名, ok, 註 in 必要:
    print(f"    [{'✓' if ok else ' '}] {名}　{註}")
達標 = sum(1 for _, ok, _ in 必要 if ok)
print(f"\n  必要條件 {達標}/2 → " + ("籌碼面初步落底" if 達標 == 2 else "尚未落底"))

if 期貨表:
    print("\n" + "=" * 60)
    print("  外資台指期淨部位　趨勢")
    print("=" * 60)
    值 = list(期貨表.values())
    最空 = min(值); 最多 = max(值)
    最空日 = [k for k, v in 期貨表.items() if v == 最空][0]
    print(f"  區間最大淨空單   {abs(最空):,} 口（{最空日[:4]}/{最空日[4:6]}/{最空日[6:8]}）")
    print(f"  區間最小         {abs(最多):,} 口")
    if 期 is not None:
        print(f"  目前             {abs(期):,} 口"
              f"　距最大空單 {期 - 最空:+,} 口")
        if 期 <= 最空:
            print("  → 空單處於區間最高，未見回補")
        elif 期 - 最空 >= 5000:
            print(f"  → 已自最大空單回補 {期 - 最空:,} 口，留意是否延續")
    print(f"\n  {'日期':<12}{'淨部位(口)':>14}{'增減':>10}")
    近期 = [d for d in 交易日 if d in 期貨表][-10:]
    for i, d in enumerate(近期):
        前 = 近期[i-1] if i > 0 else None
        ch = 期貨表[d] - 期貨表[前] if 前 else None
        print(f"  {d[:4]}/{d[4:6]}/{d[6:8]}{期貨表[d]:>14,}"
              f"{(正負(ch,'',0)):>10}")

print("\n" + "=" * 60)
print("  近 10 日融資")
print("=" * 60)
近 = [d for d in 交易日 if d in 融資表][-10:]
print(f"  {'日期':<12}{'融資(億)':>12}{'增減':>10}{'指數':>12}")
for i, d in enumerate(近):
    前 = 近[i-1] if i > 0 else None
    ch = round(融資表[d] - 融資表[前], 2) if 前 else None
    print(f"  {d[:4]}/{d[4:6]}/{d[6:8]}{融資表[d]:>12,.2f}"
          f"{(正負(ch)):>10}{指數表.get(d,{}).get('收盤',0):>12,.0f}")

缺 = [n for n, t in [("融資", 融資表), ("法人", 法人表), ("期貨", 期貨表)] if not t]
if 缺:
    print(f"\n⚠️ 這些沒抓到：{'、'.join(缺)}")
    print("把下面這段貼給 Claude：\n")
    for k in ("MI_MARGN", "BFI82U", "TAIFEX"):
        if k in 原始樣本:
            print(f"--- {k} ---\n{str(原始樣本[k])[:1200]}\n")

print("\n※ 僅供研究追蹤，不構成投資建議")


# ================================================================
#   存檔：history.json + LATEST.md（GitHub Actions 版新增）
# ================================================================
import json, os, io as _io, contextlib

os.makedirs("data", exist_ok=True)
os.makedirs("reports", exist_ok=True)

紀錄 = []
for d in 交易日:
    r = {"date": d}
    if d in 指數表:
        r["close"] = 指數表[d].get("收盤")
        r["change"] = 指數表[d].get("漲跌")
        r["turnover"] = 指數表[d].get("成交值")
    if d in 融資表:
        r["margin"] = 融資表[d]
    if d in 法人表:
        r["trust"] = 法人表[d].get("投信")
        r["foreign"] = 法人表[d].get("外資")
    if d in 期貨表:
        r["txf_net"] = 期貨表[d]
    紀錄.append(r)

with open("data/history.json", "w", encoding="utf-8") as f:
    json.dump(紀錄, f, ensure_ascii=False, indent=2)

# 把上面印出來的報告再跑一次，收進字串存成 Markdown
def 產出報告文字():
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        print(f"# 晚報 {最新[:4]}-{最新[4:6]}-{最新[6:8]}")
        print()
        print("| 指標 | 數值 | 增減 |")
        print("|---|---|---|")
        print(f"| 加權指數 | {顯示(收.get('收盤'))} | {正負(收.get('漲跌'))}（{正負(指漲跌率,'%')}） |")
        print(f"| 成交值 | {顯示((收.get('成交值') or 0)/1e8, ' 億')} | — |")
        print(f"| 融資餘額 | {顯示(融, ' 億')} | {正負(融增減, ' 億')}（{正負(融漲跌率,'%')}） |")
        print(f"| 投信買賣超 | {正負(法.get('投信'), ' 億')} | 連 {連買} 買 |")
        print(f"| 外資買賣超 | {正負(法.get('外資'), ' 億')} | — |")
        print(f"| 外資台指期 | {顯示(期, ' 口', 0)} | "
              f"{正負(期-期前,' 口',0) if (期 is not None and 期前 is not None) else '—'} |")
        print()
        print("## 判讀")
        print()
        if 逆勢加碼:
            print(f"🔴 **逆勢加碼** — 指數跌 {指漲跌率}%，融資反增 {正負(融增減,' 億')}")
        elif 比值 is not None:
            判 = ("❌ 硬撐" if 比值 < 0.5 else "⚪ 中性" if 比值 < 1.0
                  else "🟡 開始認賠" if 比值 < 1.5 else "✅ 主動出清")
            print(f"去槓桿比值 `{比值}` {判}")
        else:
            print("當日非下跌，去槓桿比值不適用")
        print()
        if 去化 is not None:
            print(f"融資自波段高點 {顯示(高點,' 億')} → {顯示(融,' 億')}（{顯示(去化,'%')}）")
        print()
        print("🔴 投信連買中斷" if 連買 == 0 else f"投信連 {連買} 買，累計 {顯示(累計,' 億')}")
        print()
        print("## 落底檢核")
        print()
        for 名, ok, 註 in 必要:
            print(f"- [{'x' if ok else ' '}] {名} — {註}")
        print()
        print(f"**必要條件 {達標}/2 → " + ("籌碼面初步落底**" if 達標 == 2 else "尚未落底**"))
        print()
        print("## 近 10 日融資")
        print()
        print("| 日期 | 融資(億) | 增減 | 指數 |")
        print("|---|---|---|---|")
        for i, d in enumerate(近):
            前 = 近[i-1] if i > 0 else None
            ch = round(融資表[d]-融資表[前], 2) if 前 else None
            print(f"| {d[:4]}/{d[4:6]}/{d[6:8]} | {融資表[d]:,.2f} | {正負(ch)} | "
                  f"{指數表.get(d,{}).get('收盤',0):,.0f} |")
        print()
        print("*自動產出，僅供研究追蹤，不構成投資建議*")
    return buf.getvalue()

報告 = 產出報告文字()
open("LATEST.md", "w", encoding="utf-8").write(報告)
open(f"reports/{最新}.md", "w", encoding="utf-8").write(報告)
print("\n已存檔：LATEST.md、data/history.json")
