# 厂商可靠性 / 超售 / 性价比评分依据

> 说明：可靠性分与超售等级为**社区口碑 + 用户经验的参考分，非实测数据**。
> 来源为 LowEndTalk / LowEndBox / NodeSeek 等社区长期共识与用户（Fatty911）实际使用经验。
> 前端展示时标注"社区参考分"；评分会随社区口碑变化在后续轮次人工校正。

## 评分字段

- `reliability`: 0–10，越高越可靠（口碑、工单、稳定性、跑路风险）。
- `oversell`: none | low | medium | high，超售程度。
- `reliability_note`: 一句话依据。
- `specs`: 套餐规格（cpu 核 / ram_gb / storage_gb / bandwidth_gbps），供性价比计算。

## 性价比公式（value_score, 1.0–10.0）

```
specs_index = min(cpu,4)/4 × 0.3 + min(ram_gb,16)/16 × 0.4
            + min(storage_gb,200)/200 × 0.2 + min(bandwidth_gbps,10)/10 × 0.1
raw = specs_index / monthly_amount_USD × 100
value_score = clamp(1 + 9 × (log10(raw + 0.1) + 1) / 2, 1, 10)，保留 1 位小数
```

- 月化价取**同币种月化**（`monthly_amount`，季/年付按除法折算，不做汇率换算）。
- 非 success 状态（无价格）不产生 value_score（前端显示 —）。
- 该分数是**规格/价格的相对性价比**，不含线路质量权重；线路由
  `provider_claimed_routes` 与 `parsed_route_evidence` 单独展示。

## 现有厂商评分（2026-08-07 首版）

| 厂商 | reliability | oversell | 依据 |
|---|---|---|---|
| BuyVM (Frantech) | 9 | none | 社区口碑极好，长期不超售 |
| DMIT | 9 | none | 高端 CN2 GIA/CMIN2，口碑极好 |
| BandwagonHost | 8 | none | 老牌 CN2 GIA，口碑稳定 |
| RackNerd | 8 | low | 北美性价比标杆，口碑好 |
| CloudIPLC | 8 | low | 泉州/洛杉矶 CN2 老牌，线路稳定 |
| SpartanHost | 8 | low | 洛杉矶 CN2 GIA 口碑好 |
| HostDare | 7 | medium | CN2 GIA 老牌，偶有限速反馈 |
| V.PS | 7 | low | 欧洲商家，香港/东京 CN2 GIA 中上 |
| LayerStack | 7 | low | 云平台正规，香港直连，价格偏高 |
| ZoroCloud | 6 | medium | 小众国内商家，线路宣传多于实测 |
| CloudCone | 6 | medium | 按小时计费特色，黑五促销多 |
| Contabo | 6 | high | 德国大厂便宜大碗，超售争议多 |
| HostHatch | 6 | medium | 性价比高，促销少 |
| ion | 5 | medium | 廉价 CN2，性能波动 |
| Jtti | 5 | medium | 营销大，客服响应慢 |
| LisaHost | 5 | medium | 9929 性价比高，口碑中等 |
| GreenCloud | 5 | high | 黑五促销多，超售争议两极 |
| PacificRack | 2 | high | 用户点名避坑：口碑差、超售严重、客服难联系 |

## 测评站情报流（deals.json）

- 来源：LowEndBox `/feed/`、LowEndTalk `/discussions/feed.rss`（均为公开 RSS，实测 200）。
- 过滤：强关键词（vps/hosting/promo/coupon/…）命中即收；弱关键词需双命中；
  排除专享服务器/主机托管/域名/安全新闻/提问帖（`EXCLUDE_KEYWORDS`、
  `REQUEST_PATTERNS`、`OFFER_PATTERNS` 见 `scripts/fetch_deals.py`）。
- **AFF 合规**：只发布文章原文 URL，不处理/不发布 affiliate 参数；
  情报流不进入产品门，不构成购买建议。
