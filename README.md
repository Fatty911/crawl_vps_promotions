# 北京三网优化 VPS 优惠监控

这个目录用于通过 GitHub Actions 每天抓取候选 VPS 服务商页面，渲染 JavaScript 后提取库存、价格、线路关键词，并生成 GitHub Pages 静态页。

## 本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python -m vps_monitor.monitor --output
```

输出：

- `site/index.html`：按性价比分数排序的页面
- `site/data/results.json`：抓取结果 JSON

## 评分说明

分数会综合库存关键词、2C4G/30G/100M 规格关键词、CN2/AS9929/CMIN2/软银等线路关键词、识别到的月费。自动识别结果只做筛选和提醒，下单前仍需北京移动、联通、电信晚高峰实测。
