# EVBC 挖矿雷达

ENI 主网 EVBC 持币收益算力分布看板。每天北京时间 08:00 由 GitHub Actions 自动重算并发布。

数据来源：
- `eni-radr.com/api/product/overview`（当日持币收益池、官方建议区间、发行上限）
- `scan.eni.top`（全量持币地址，约 4.6 万个）
- `scan.eni.top/api?module=stats&action=tokensupply`（当前全网总量）
- `graph.eni.top`（订单簿 DEX 最新成交价，页面打开时每 30 秒实时刷新）

## 本地手动生成

```bash
python radar_build.py            # 全量重扫（约 2–4 分钟）
python radar_build.py --cached   # 复用 evbc_holders.json，只重算
```

## 说明

- 页面为单文件 HTML，含手机端专用布局。
- 若当日持币池取不到，脚本会直接退出，本次不发布，线上保留上一版。
- `radar_history.json` 保存每日快照，用于计算「较昨日」增量，由 workflow 自动写回。
