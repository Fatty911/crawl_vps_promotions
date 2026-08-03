# 数据契约（schema v4）

Pages 只把 `schema_version: 4` 且 `mode: live` 的批次当作当前套餐。旧 schema 显示为“历史待迁移”，不会成为新鲜优惠。

## Envelope

`site/data/batch.json` 包含：

- `batch_id`: `crawl_vps_promotions:<run_id>:<run_attempt>`
- `run_id`、`run_attempt`、`source_sha`、`config_sha256`
- `started_at`、`finished_at`、`mode`、`baseline_batch_id`
- `expected_tasks: 14`
- `statuses`、`prices`、守恒 `summary`
- `evidence_sha256`、`audit_status`

`source_sha` 必须为 40 位小写十六进制，配置和证据摘要必须为 64 位 SHA-256。五类 outcome 的总和必须精确等于 14。

## 脱敏运行证据

`site/data/live-evidence.json` 是面向诊断的最小证据文件，顶层固定为 `schema_version`、`mode`、`tasks`、`summary`。每个 `tasks` 元素固定包含 `task_id`、`provider`、`http_status`、`final_url`、`method`、`outcome`、`block_reason`、`attempts`、`latency_ms`。

`final_url` 只允许 `http(s)://hostname` origin；原因码只允许有限 ASCII 字符集。不会发布凭据、端口、路径、query、fragment、正文、headers、cookies 或代理节点名。`summary` 守恒任务数、服务商数、五类 outcome 和四类 method，且任务顺序必须与 `providers.yaml` 的 14 项一致。`batch.json.evidence_sha256` 必须等于该文件的规范化 SHA-256。

## Status 与 offer

每条状态必须有 `task_id`、`outcome`、`attempts`、起止时间、`source_url`、`final_url`、`rejection_reason`、`evidence_hash` 和 `parser_version`。

只有 `success` 能对应一条价格记录。价格记录必须为本轮 `mode: live`，且包含稳定 `offer_id`、`availability: in_stock`、正金额、币种、`monthly|quarterly|yearly` 账期、同币种月化值和官方 HTTPS `product_url`。非成功状态不允许携带 offer 或商品 URL。

`out_of_stock` 保留为状态，但不能出现在当前 prices，也不计入八条产品门。多 offer、跨域、币种/账期冲突和无法确定库存分别进入拒绝漏斗。

## 历史与 manifest

历史按 `event_id` 和 `observed_at` 追加、去重并保留 180 天；不会把旧价回填当前轮。v4 从第一个有效批次重建此前不可靠的 VPS 历史。

`manifest.json` 绑定 schema、batch、run、attempt、source/config SHA、mode、audit 状态和公开文件摘要。部署后必须通过正常 TLS 重新获取并核对每个文件。
