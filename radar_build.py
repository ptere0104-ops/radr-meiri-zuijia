# -*- coding: utf-8 -*-
"""
EVBC 挖矿雷达 · 数据构建脚本
------------------------------------------------------------
作用：
  1) 读取 ENI RADR 官方接口，拿到当日持币收益池、官方建议区间、EVBC 实时价格
  2) 扫描 ENI 主网 EVBC 全量持币地址（Blockscout 接口分页）
  3) 按项目方用户指南公式计算：持币算力 / 全网算力总和 / 每枚回报率 / 效率峰值
  4) 生成一个自带数据的单文件网页（内嵌 SVG 图表 + 交互计算器）
     网页打开即用，无需联网、不依赖 CDN

公式来源（用户指南 / 白皮书 4.5 节）：
  M_i = 1 + #{ j ∈ E : H_j < H_i }
  A_i = (W_d / 2) × M_i / Σ_{j∈E} M_j

运行：python radar_build.py
输出：EVBC_挖矿雷达.html
"""
import json, math, os, shutil, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime
from decimal import Decimal, getcontext

getcontext().prec = 40

# ---------------- 常量配置 ----------------
EVBC_ADDR = "0x70348b77227619d4908a0bf43e96a3caa438045f"          # ENI 主网 EVBC 合约
SCAN_API  = "https://scan.eni.top/api?module=token&action=getTokenHolders&contractaddress={}&page={}&offset=1000"
SITE_API  = "https://eni-radr.com/api/product/{}"
SUPPLY_API = ("https://scan.eni.top/api?module=stats&action=tokensupply"
              "&contractaddress=0x70348b77227619d4908a0bf43e96a3caa438045f")
EXCLUDE   = {                                                        # 不计入个人排名的合约地址
    "0xc7182f3e2e030cf83d627be9fa3bfdb6fd04ce04",   # EVBC/USDT 交易对（流动性池）
    "0x956730b8eedd581cfcbf79399307c4c5690e10c8",   # airdropMarket 合约
}
MIN_HOLD  = Decimal("100")        # 最低参与持仓
D         = Decimal(10) ** 18
HERE      = os.path.dirname(os.path.abspath(__file__))
CACHE     = os.path.join(HERE, "evbc_holders.json")
OUT       = os.path.join(HERE, "EVBC_挖矿雷达.html")


# ---------------- 通用请求 ----------------
def http_json(url, retry=4, timeout=40):
    """带重试的 GET，统一返回 dict；失败返回 None
    说明：部分站点会重置 urllib 的 TLS 连接，这里追加系统 curl 作为兜底"""
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print("  urllib 失败(第%d次): %s" % (i + 1, e), file=sys.stderr)
            # 兜底：调用系统 curl
            try:
                exe = shutil.which("curl.exe") or shutil.which("curl")
                if exe:
                    p = subprocess.run([exe, "-s", "-m", "30", "-L", url],
                                       capture_output=True, text=True)
                    if p.returncode == 0 and p.stdout.strip():
                        return json.loads(p.stdout)
            except Exception as e2:
                print("  curl 兜底失败: %s" % e2, file=sys.stderr)
            time.sleep(2)
    return None


# ---------------- 1. 官方参数 ----------------
def fetch_official():
    """取当日收益池 / 官方建议区间 / 价格"""
    info = {"pool": None, "official": None, "price": None, "change": None,
            "business_date": None, "total_addresses": None, "cap": None}

    d = http_json(SITE_API.format("overview"))
    if d:
        prod = d.get("production") or {}
        info["pool"] = Decimal(prod["holdingBudget"]) if prod.get("holdingBudget") else None
        rng = prod.get("bestHoldingRange")
        if rng:
            info["official"] = (Decimal(rng[0]), Decimal(rng[1]))
        info["business_date"] = prod.get("businessDate")
        info["total_addresses"] = d.get("totalAddresses")
        if d.get("cap"):
            info["cap"] = d["cap"]

    p = http_json(SITE_API.format("price"))
    if p and p.get("quote"):
        info["price"] = p["quote"].get("price")
        info["change"] = p["quote"].get("change")
    return info


def fetch_supply():
    """当前全网总发行量（链上 totalSupply）"""
    d = http_json(SUPPLY_API)
    if d and d.get("status") == "1" and d.get("result"):
        try:
            return Decimal(d["result"]) / D
        except Exception:
            return None
    return None


# ---------------- 2. 全量持币地址 ----------------
def fetch_holders(use_cache_only=False):
    """分页拉取全部持币地址；失败时回退到本地缓存"""
    if use_cache_only and os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f), False   # 主动使用缓存，不算抓取失败
    rows, page = [], 1
    while page <= 130:
        d = http_json(SCAN_API.format(EVBC_ADDR, page))
        if not d or d.get("status") != "1":
            break
        batch = d.get("result") or []
        if not batch:
            break
        rows.extend(batch)
        print("  持币地址第 %d 页，累计 %d" % (page, len(rows)), file=sys.stderr)
        if len(batch) < 1000:
            break
        page += 1

    if len(rows) < 1000:
        if os.path.exists(CACHE):
            print("  在线数据不足，改用本地缓存", file=sys.stderr)
            with open(CACHE, encoding="utf-8") as f:
                return json.load(f), True
        raise SystemExit("持币数据获取失败，且无本地缓存")
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return rows, False


# ---------------- 3. 按公式计算 ----------------
def build_model(rows):
    """把链上余额整理为合资格地址的升序数组，并算全网算力总和"""
    q = []
    for r in rows:
        if r["address"].lower() in EXCLUDE:
            continue
        bal = Decimal(r["value"]) / D
        if bal >= MIN_HOLD:
            q.append(bal)
    q.sort()
    n = len(q)

    # ΣM = Σ (1 + 严格小于自己的地址数)；按同值分组累加
    sum_m, idx = 0, 0
    while idx < n:
        v = q[idx]
        j = idx
        while j < n and q[j] == v:
            j += 1
        sum_m += (j - idx) * (1 + idx)
        idx = j
    return q, n, sum_m


def M_at(q, X):
    """任意持仓量对应的算力（二分查找严格小于的数量）"""
    import bisect
    return 1 + bisect.bisect_left(q, X)


def income_of(M, sum_m, pool):
    return pool * M / sum_m


def find_peak(q, pool, sum_m, upper=20000):
    """效率峰值：遍历真实档位，找 M/X 最大的位置
    返回 (临界档位, 算力, 日收益, 每枚日回报率)"""
    import bisect
    best = None
    idx, n = 0, len(q)
    while idx < n:
        v = q[idx]
        j = idx
        while j < n and q[j] == v:
            j += 1
        if v <= upper:
            X = v + Decimal("0.001")          # 刚好跨过该档位的持仓量
            m = 1 + idx
            inc = income_of(m, sum_m, pool)
            roi = float(inc / X)
            if best is None or roi > best[0]:
                best = (roi, v, m, inc, X)
        idx = j
    return best


def find_biggest_cluster(q):
    """最拥挤的单一持仓档位（决定低成本跨档点）"""
    best = (0, None)
    idx, n = 0, len(q)
    while idx < n:
        v = q[idx]
        j = idx
        while j < n and q[j] == v:
            j += 1
        if j - idx > best[0]:
            best = (j - idx, v)
        idx = j
    return best


def tier_table(q, pool, sum_m, tiers):
    """关键档位的算力 / 日收益 / 每枚回报 / 边际回本天数"""
    rows, prev = [], None
    for X in tiers:
        m = M_at(q, Decimal(str(X)))
        inc = float(income_of(m, sum_m, pool))
        roi = inc / X * 100 if X else 0
        pay = None
        if prev and inc > prev[1]:
            pay = (X - prev[0]) / (inc - prev[1])
        rows.append({"x": X, "m": m, "inc": inc, "roi": roi, "pay": pay})
        prev = (X, inc)
    return rows


def hist_buckets(q):
    """持仓分布分桶"""
    edges = [(100, 150), (150, 190), (190, 200), (200, 210), (210, 250),
             (250, 300), (300, 500), (500, 1000), (1000, 10 ** 15)]
    labels = ["100–150", "150–190", "190–200", "200–210", "210–250",
              "250–300", "300–500", "500–1k", ">1k"]
    shorts = ["100-150", "150-190", "190-200", "200-210", "210-250",
              "250-300", "300-500", "500-1k", ">1k"]
    out = []
    for (lo, hi), lb, sh in zip(edges, labels, shorts):
        c = sum(1 for x in q if Decimal(lo) <= x < Decimal(hi))
        out.append({"label": lb, "short": sh, "count": c})
    return out


# ---------------- 4. SVG 图表 ----------------
def svg_combo(labels, bars, line, hi_idx):
    """柱（日收益）+ 折线（每枚回报率）双轴图"""
    W, Hh = 560, 340
    pl, pr, pt, pb = 54, 60, 40, 66
    pw, ph = W - pl - pr, Hh - pt - pb
    bmax = max(bars) * 1.12 if max(bars) > 0 else 1
    lmax = max(line) * 1.15 if max(line) > 0 else 1
    slot = pw / len(labels)

    def y_bar(v):
        return pt + ph - v / bmax * ph

    def y_line(v):
        return pt + ph - v / lmax * ph

    p = ['<svg viewBox="0 0 %d %d" role="img" aria-label="持币量收益阶梯图" '
         'style="width:100%%;height:auto;display:block">' % (W, Hh)]

    # 网格 + 左右刻度
    for k in range(5):
        y = pt + ph * k / 4
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#243149" stroke-width="1"/>'
                 % (pl, y, pl + pw, y))
        p.append('<text x="%d" y="%.1f" text-anchor="end" font-size="10" fill="#7F8EA6">%.2f</text>'
                 % (pl - 8, y + 4, (4 - k) / 4 * bmax))
        p.append('<text x="%d" y="%.1f" text-anchor="start" font-size="10" fill="#7F8EA6">%.2f%%</text>'
                 % (pl + pw + 8, y + 4, (4 - k) / 4 * lmax))

    # 柱子
    bw = slot * 0.56
    for i, (lb, v) in enumerate(zip(labels, bars)):
        x = pl + i * slot + (slot - bw) / 2
        hgt = (pt + ph) - y_bar(v)
        color = "#5AB0F0" if i == hi_idx else "#27506F"
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
                 % (x, y_bar(v), bw, max(hgt, 0.6), color))

    # 折线
    pts = " ".join("%.1f,%.1f" % (pl + i * slot + slot / 2, y_line(v)) for i, v in enumerate(line))
    p.append('<polyline points="%s" fill="none" stroke="#F0A05A" stroke-width="2"/>' % pts)
    for i, v in enumerate(line):
        p.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="#F0A05A"/>'
                 % (pl + i * slot + slot / 2, y_line(v)))

    # X 轴标签（旋转，避免相邻档位文字重叠）
    for i, lb in enumerate(labels):
        cx = pl + i * slot + slot / 2
        p.append('<text x="%.1f" y="%d" text-anchor="end" font-size="9.5" fill="#8C9AB0" '
                 'transform="rotate(-34 %.1f %d)">%s</text>'
                 % (cx, pt + ph + 16, cx, pt + ph + 16, lb))
    p.append('<text x="%d" y="%d" text-anchor="middle" font-size="10" fill="#7F8EA6">持币量（EVBC）</text>'
             % (pl + pw / 2 - 10, Hh - 4))

    # 图例
    p.append('<rect x="%d" y="10" width="10" height="8" fill="#5AB0F0" rx="1"/>' % pl)
    p.append('<text x="%d" y="18" font-size="10" fill="#9AA8BD">日收益 EVBC</text>' % (pl + 14))
    p.append('<line x1="%d" y1="14" x2="%d" y2="14" stroke="#F0A05A" stroke-width="2"/>' % (pl + 100, pl + 116))
    p.append('<text x="%d" y="18" font-size="10" fill="#9AA8BD">每枚回报率</text>' % (pl + 120))
    p.append('</svg>')
    return "".join(p)


def svg_hist(bu, hi_idx, total):
    """持仓分布柱状图"""
    W, Hh = 560, 330
    pl, pr, pt, pb = 54, 20, 40, 62
    pw, ph = W - pl - pr, Hh - pt - pb
    vmax = max(b["count"] for b in bu) * 1.1
    slot = pw / len(bu)
    p = ['<svg viewBox="0 0 %d %d" role="img" aria-label="持币量分布图" '
         'style="width:100%%;height:auto;display:block">' % (W, Hh)]

    for k in range(4):
        y = pt + ph * k / 3
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#243149" stroke-width="1"/>'
                 % (pl, y, pl + pw, y))
        p.append('<text x="%d" y="%.1f" text-anchor="end" font-size="10" fill="#7F8EA6">%s</text>'
                 % (pl - 8, y + 4, "{:,}".format(int((3 - k) / 3 * vmax))))

    bw = slot * 0.6
    for i, b in enumerate(bu):
        x = pl + i * slot + (slot - bw) / 2
        h = b["count"] / vmax * ph
        color = "#5AB0F0" if i == hi_idx else "#27506F"
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
                 % (x, pt + ph - h, bw, max(h, 0.6), color))
        if b["count"] > 0:
            p.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9" fill="#C7D3E3">%s</text>'
                     % (x + bw / 2, pt + ph - h - 6, "{:,}".format(b["count"])))

    for i, b in enumerate(bu):
        p.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="9" fill="#8C9AB0">%s</text>'
                 % (pl + i * slot + slot / 2, pt + ph + 16, b["label"]))
    p.append('<text x="%d" y="%d" text-anchor="middle" font-size="10" fill="#7F8EA6">持仓区间（EVBC）</text>'
             % (pl + pw / 2, Hh - 8))
    p.append('<text x="%d" y="24" font-size="10" fill="#9AA8BD">合计 %s 个合资格地址，深色为各区间地址数</text>'
             % (pl, "{:,}".format(total)))
    p.append('</svg>')
    return "".join(p)


# ---------------- 4b. 手机端专用图表（窄画布，字号相对更大） ----------------
def svg_combo_m(labels, bars, line, hi_idx):
    """手机端收益阶梯图：画布宽 390，档位精简到 8 个"""
    W, Hh = 390, 316
    pl, pr, pt, pb = 40, 42, 30, 60
    pw, ph = W - pl - pr, Hh - pt - pb
    bmax = max(bars) * 1.14 if max(bars) > 0 else 1
    lmax = max(line) * 1.16 if max(line) > 0 else 1
    slot = pw / len(labels)

    def yb(v):
        return pt + ph - v / bmax * ph

    def yl(v):
        return pt + ph - v / lmax * ph

    p = ['<svg viewBox="0 0 %d %d" role="img" aria-label="持币量收益阶梯图" '
         'style="width:100%%;height:auto;display:block">' % (W, Hh)]

    for k in range(5):
        y = pt + ph * k / 4
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#243149" stroke-width="1"/>'
                 % (pl, y, pl + pw, y))
        p.append('<text x="%d" y="%.1f" text-anchor="end" font-size="9.5" fill="#7F8EA6">%.2f</text>'
                 % (pl - 6, y + 4, (4 - k) / 4 * bmax))
        p.append('<text x="%d" y="%.1f" text-anchor="start" font-size="9.5" fill="#7F8EA6">%.2f%%</text>'
                 % (pl + pw + 5, y + 4, (4 - k) / 4 * lmax))

    bw = slot * 0.58
    for i, v in enumerate(bars):
        x = pl + i * slot + (slot - bw) / 2
        color = "#5AB0F0" if i == hi_idx else "#27506F"
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
                 % (x, yb(v), bw, max((pt + ph) - yb(v), 0.8), color))

    pts = " ".join("%.1f,%.1f" % (pl + i * slot + slot / 2, yl(v)) for i, v in enumerate(line))
    p.append('<polyline points="%s" fill="none" stroke="#F0A05A" stroke-width="2"/>' % pts)
    for i, v in enumerate(line):
        p.append('<circle cx="%.1f" cy="%.1f" r="2.8" fill="#F0A05A"/>'
                 % (pl + i * slot + slot / 2, yl(v)))

    for i, lb in enumerate(labels):
        cx = pl + i * slot + slot / 2
        p.append('<text x="%.1f" y="%d" text-anchor="end" font-size="10" fill="#8C9AB0" '
                 'transform="rotate(-40 %.1f %d)">%s</text>'
                 % (cx, pt + ph + 15, cx, pt + ph + 15, lb))

    p.append('<rect x="%d" y="8" width="9" height="8" fill="#5AB0F0" rx="1"/>' % pl)
    p.append('<text x="%d" y="15" font-size="10" fill="#9AA8BD">日收益 EVBC</text>' % (pl + 13))
    p.append('<line x1="%d" y1="12" x2="%d" y2="12" stroke="#F0A05A" stroke-width="2"/>' % (pl + 104, pl + 118))
    p.append('<text x="%d" y="15" font-size="10" fill="#9AA8BD">每枚回报率</text>' % (pl + 122))
    p.append('</svg>')
    return "".join(p)


def svg_hist_m(bu, hi_idx, total):
    """手机端分布图：标签用短版"""
    W, Hh = 390, 300
    pl, pr, pt, pb = 40, 12, 34, 58
    pw, ph = W - pl - pr, Hh - pt - pb
    vmax = max(b["count"] for b in bu) * 1.12
    slot = pw / len(bu)
    p = ['<svg viewBox="0 0 %d %d" role="img" aria-label="持币量分布图" '
         'style="width:100%%;height:auto;display:block">' % (W, Hh)]

    for k in range(4):
        y = pt + ph * k / 3
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#243149" stroke-width="1"/>'
                 % (pl, y, pl + pw, y))
        p.append('<text x="%d" y="%.1f" text-anchor="end" font-size="9.5" fill="#7F8EA6">%s</text>'
                 % (pl - 6, y + 4, "{:,}".format(int((3 - k) / 3 * vmax))))

    bw = slot * 0.62
    for i, b in enumerate(bu):
        x = pl + i * slot + (slot - bw) / 2
        h = b["count"] / vmax * ph
        color = "#5AB0F0" if i == hi_idx else "#27506F"
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
                 % (x, pt + ph - h, bw, max(h, 0.8), color))
        if b["count"] > 500:
            p.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9.5" fill="#C7D3E3">%s</text>'
                     % (x + bw / 2, pt + ph - h - 5, "{:,}".format(b["count"])))

    for i, b in enumerate(bu):
        cx = pl + i * slot + slot / 2
        p.append('<text x="%.1f" y="%d" text-anchor="end" font-size="9.5" fill="#8C9AB0" '
                 'transform="rotate(-42 %.1f %d)">%s</text>'
                 % (cx, pt + ph + 14, cx, pt + ph + 14, b.get("short", b["label"])))

    p.append('<text x="%d" y="20" font-size="10" fill="#9AA8BD">合计 %s 个合资格地址</text>'
             % (pl, "{:,}".format(total)))
    p.append('</svg>')
    return "".join(p)


# ---------------- 4c. 历史快照与日增量 ----------------
HIST = os.path.join(HERE, "radar_history.json")


def load_hist():
    try:
        with open(HIST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_hist(h):
    # 只保留最近 120 天
    keys = sorted(h.keys())[-120:]
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump({k: h[k] for k in keys}, f, ensure_ascii=False, indent=1)


def delta_ctx(bd, addr, qual):
    """返回 (地址增量, 合资格增量, 对比标签)，无历史则为 None"""
    h = load_hist()
    prevs = [k for k in h.keys() if k < bd]
    if not prevs:
        return None, None, "首日记录"
    pk = max(prevs)
    p = h[pk]
    d_addr = addr - int(p.get("addr", addr))
    d_qual = qual - int(p.get("qual", qual))
    try:
        gap = (datetime.strptime(bd, "%Y-%m-%d") - datetime.strptime(pk, "%Y-%m-%d")).days
        lbl = "较昨日" if gap == 1 else "较 %s" % pk[5:]
    except Exception:
        lbl = "较 %s" % pk[5:]
    return d_addr, d_qual, lbl


def fmt_delta(v):
    if v is None:
        return ""
    return ("+%d" % v) if v > 0 else ("%d" % v if v < 0 else "0")


def cls_delta(v):
    if v is None:
        return "none"
    return "plus" if v > 0 else ("minus" if v < 0 else "none")


# ---------------- 5. 组装网页 ----------------
TEMPLATE = open(os.path.join(HERE, "radar_template.html"), encoding="utf-8").read()


def main():
    print("[1/5] 读取官方接口参数 …", file=sys.stderr)
    off = fetch_official()
    if not off.get("pool"):
        raise SystemExit("未取得当日持币收益池，取消本次发布（保留上一版页面）")
    supply = fetch_supply()
    cap = Decimal("1000000000")          # 发行上限（官方 overview 的 cap）
    if off.get("cap"):
        cap = Decimal(off["cap"])

    print("[2/5] 扫描链上持币地址 …", file=sys.stderr)
    rows, stale = fetch_holders("--cached" in sys.argv)

    print("[3/5] 按公式计算算力与收益 …", file=sys.stderr)
    q, n, sum_m = build_model(rows)
    pool = off["pool"] or Decimal("17150.53")            # 接口失败时的兜底值
    peak = find_peak(q, pool, sum_m)
    cluster_cnt, cluster_val = find_biggest_cluster(q)
    bu = hist_buckets(q)

    peak_roi, peak_level, peak_m, peak_inc, peak_x = peak
    # 峰值档位取整（= 刚好越过最拥挤档位后的第一个整数值）
    P = int(math.ceil(float(peak_level)))
    # 主推区间：严格超过峰值档位，再给 2 枚安全边际（避免快照时并列）
    lo, hi = P + 1, P + 3
    # 低成本档：跨过最拥挤档位即可获得巨额算力跃升
    cheap_lo = Decimal(cluster_val) + Decimal("0.01")
    cheap_hi = Decimal(cluster_val) + Decimal("2")

    # 表格档位：连续档位之间才看得出"边际回本"
    tiers = [100, 150, float(cluster_val), float(cheap_lo), 200, P, 250, 300, 500, 1000]
    tbl = tier_table(q, pool, sum_m, tiers)

    # 图表用固定档位，保证可读性
    chart_x = [100, 150, float(cluster_val), float(cheap_lo), 200,
               P, P + 1, 250, 300, 500, 1000, 3000]
    bars, line_v = [], []
    for x in chart_x:
        m = M_at(q, Decimal(str(x)))
        inc = float(income_of(m, sum_m, pool))
        bars.append(round(inc, 4))
        line_v.append(round(inc / x * 100, 3))
    labels = ["100", "150", "%d" % int(float(cluster_val)), "%.2f" % float(cheap_lo),
              "200", str(P), str(P + 1), "250", "300", "500", "1k", "3k"]
    hi_idx = 5

    # 手机端图表：档位精简为 8 个，画布更窄
    chart_m_x = [100, float(cluster_val), float(cheap_lo), 200, P, 250, 300, 1000]
    bars_m, line_m = [], []
    for x in chart_m_x:
        m = M_at(q, Decimal(str(x)))
        inc = float(income_of(m, sum_m, pool))
        bars_m.append(round(inc, 4))
        line_m.append(round(inc / x * 100, 3))
    labels_m = ["100", "%d" % int(float(cluster_val)), "%.2f" % float(cheap_lo),
                "200", str(P), "250", "300", "1k"]
    hi_idx_m = 4

    hi_idx_hist = 2   # 190–200 桶

    # 计算器用的紧凑数据集（单位 0.0001 EVBC，升序整数数组）
    bal_units = [int(b * 10000) for b in q]

    pay_rows = []
    for r in tbl:
        pay_rows.append({
            "x": r["x"], "m": r["m"], "inc": round(r["inc"], 4),
            "roi": round(r["roi"], 3),
            "pay": ("%.0f" % r["pay"]) if r["pay"] else "—"
        })

    now = datetime.now()
    upd = now.strftime("%Y-%m-%d %H:%M")

    # 日增量：与上一个业务日比对
    bd = off["business_date"] or now.strftime("%Y-%m-%d")
    d_addr, d_qual, d_lbl = delta_ctx(bd, len(rows), n)
    h = load_hist()
    h[bd] = {
        "addr": len(rows), "qual": n, "summ": sum_m,
        "pool": float(pool), "peak": P, "lo": lo, "hi": hi,
        "supply": float(supply) if supply else None,
        "price": off["price"], "at": now.strftime("%H:%M"),
    }
    save_hist(h)

    # 以主推区间下界作为展示口径
    m_lo = M_at(q, Decimal(str(lo)))
    inc_lo = income_of(m_lo, sum_m, pool)

    ctx = {
        "__UPDATED__": upd,
        "__STALE__": "（本次抓取失败，沿用上次缓存）" if stale else "",
        "__POOL__": "{:,.2f}".format(float(pool)),
        "__PRICE__": off["price"] or "—",
        "__CHANGE__": (("+" if float(off["change"]) >= 0 else "") + str(off["change"]) + "%")
        if off["change"] else "—",
        "__OFFICIAL__": ("%d–%d" % (int(off["official"][0]), int(off["official"][1])))
        if off["official"] else "190–210",
        "__RANGE__": "%d–%d" % (lo, hi),
        "__RANGE_LO__": str(lo),
        "__RANGE_HI__": str(hi),
        "__ROI__": "%.3f" % float(inc_lo / Decimal(str(lo)) * 100),
        "__DINCOME__": "%.3f" % float(inc_lo),
        "__DINCOMENUM__": "%.8f" % float(inc_lo),
        "__USD__": "%.4f" % (float(inc_lo) * float(off["price"] or 0)),
        "__PEAK__": str(P),
        "__PEAKM__": "{:,}".format(m_lo),
        "__JUMPS__": "%.2f → %.2f 与 %d → %d" % (float(cluster_val), float(cheap_lo), P, P + 1),
        "__PEAKROI__": "%.3f" % (peak_roi * 100),
        "__N__": "{:,}".format(n),
        "__CAP__": "{:,.0f}".format(float(cap)),
        "__SUPPLY__": ("{:,.2f}".format(float(supply))) if supply else "—",
        "__PCT__": ("%.2f%%" % (float(supply / cap * 100))) if supply else "—",
        "__BARW__": ("%.4f" % max(float(supply / cap * 100), 0.6)) if supply else "0",
        "__REMAIN__": ("{:,.2f}".format(float(cap - supply))) if supply else "—",
        "__SUMM__": "{:,}".format(sum_m),
        "__CLUSTER__": "%.2f" % float(cluster_val),
        "__CLUSTERC__": "{:,}".format(cluster_cnt),
        "__CLUSTERPC__": "%.1f" % (cluster_cnt / n * 100),
        "__CHEAPLO__": "%.2f" % float(cheap_lo),
        "__CHEAPHI__": "%.0f" % float(cheap_hi),
        "__CHEAPINC__": "%.3f" % float(income_of(M_at(q, cheap_lo), sum_m, pool)),
        "__CHEAPROI__": "%.3f" % (float(income_of(M_at(q, cheap_lo), sum_m, pool)) / float(cheap_lo) * 100),
        "__SVG_COMBO__": svg_combo(labels, bars, line_v, hi_idx),
        "__SVG_HIST__": svg_hist(bu, hi_idx_hist, n),
        "__SVG_COMBO_M__": svg_combo_m(labels_m, bars_m, line_m, hi_idx_m),
        "__SVG_HIST_M__": svg_hist_m(bu, hi_idx_hist, n),
        "__TABLE__": json.dumps(pay_rows, ensure_ascii=False),
        "__BAL__": json.dumps(bal_units),
        "__POOLNUM__": "%.8f" % float(pool),
        "__SUMMNUM__": str(sum_m),
        "__PRICENUM__": str(off["price"] or 0),
        "__TOTALADDR__": "{:,}".format(len(rows)),
        "__DADDR__": fmt_delta(d_addr),
        "__DADDRCLS__": cls_delta(d_addr),
        "__DQUAL__": fmt_delta(d_qual),
        "__DQUALCLS__": cls_delta(d_qual),
        "__DELTALBL__": d_lbl,
        "__BUSINESS__": off["business_date"] or now.strftime("%Y-%m-%d"),
    }
    html = TEMPLATE
    for k, v in ctx.items():
        html = html.replace(k, v)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)

    print("[4/5] 网页已生成：%s" % OUT, file=sys.stderr)
    print("[5/5] 主推区间 %d–%d EVBC｜效率峰值 %d（%.3f%%/天）｜合资格地址 %s｜当日池 %s"
          % (lo, hi, P, peak_roi * 100, "{:,}".format(n), "{:,.2f}".format(float(pool))), file=sys.stderr)
    print("OK")


if __name__ == "__main__":
    main()


