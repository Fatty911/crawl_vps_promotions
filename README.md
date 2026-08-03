# VPS 同卡套餐监控

本仓每轮固定监控 `providers.yaml` 中 14 个稳定任务，覆盖 10 家服务商。配置包含 `config_revision`、显式退休列表、任务优先级、官方域、稳定套餐 token、预期币种/账期和生命周期；任务集合不能静默变化。

## 真实性边界

宽泛购物车或列表页只用于发现。一个任务只有在同一具体套餐卡、订单项或无歧义 JSON-LD Product/Offer 中同时取得套餐 token、金额、币种、账期、库存和官方 order/detail URL，形成稳定 `offer_id` 时才是 `success`。

解析支持 `@graph`、嵌套 Product、多 offer 和 DOM 卡片。多候选、币种/账期冲突、跨域重定向、验证码、403 和无法绑定库存的页面均拒绝或阻断，不选择“最像”的金额。`out_of_stock` 是真实观察，但没有可发布 offer，也不计入产品门。

原始金额和币种始终保留；`monthly_amount` 只是同币种内按月/季/年除法，不进行汇率换算或跨币种暗中排序。线路声明、页面解析证据与实测线路分开；本仓没有北京探针，因此 `measured_routes` 为 `null`。

## 调度与状态

任务按显式 priority 执行，Requests 全局并发 4、每 host 1，并有可取消 deadline、重试预算与 provider 级熔断。浏览器 fallback 全局并发 1，不登录、不绕验证码。VPS 默认直连；只有供应商级阻断和代理授权另行确认后，才可引入动态代理。

跨 run state 只从 prior main、非当前 run、schema/config/branch 均匹配的有界 artifact 恢复。ZIP 路径、成员数和大小受限，失败会回滚。历史按稳定事件 ID 追加并保留 180 天，旧价永不回填本轮。

## v4 输出与两层门

```text
site/data/status.json          精确 14 条本轮状态
site/data/prices.json          仅本轮明确 in_stock success
site/data/price_history.json   180 天追加事件
site/data/summary.json         结果守恒摘要
site/data/live-evidence.json   有界脱敏的运行证据
site/data/batch.json           schema v4 完整 envelope
site/manifest.json             batch/SHA/config 与文件摘要
site/audit.json                结构门、产品门、fingerprint
```

部署前结构门要求 v4、精确 14 个唯一任务、状态守恒、SHA/config/file hash 一致，且非成功记录无 offer。结构有效但 live 被阻断时可发布真实状态。

`live-evidence.json` 只保留任务、服务商、HTTP 状态、脱敏 origin、固定枚举、有限原因码、尝试次数和延迟；不写入 query、凭据、路径、响应正文、请求头或代理节点。envelope 的 `evidence_sha256` 对应这份文件，结构门和产品门都会重新校验它。

部署后通过正常 TLS 拉取公开 manifest 并逐文件核验。产品门要求至少 8 个不同 `task_id` 同时为本轮 live、真实同卡、明确 `in_stock`、具备稳定 `offer_id` 和官方 URL。8 个 success 中只要一个售罄、重复或证据不完整，就不能按 8 条计数。失败时 workflow 红并按 fingerprint 告警，不生成合格 Release。

## 本地验证

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium
python -m pytest -q
python -m compileall -q vps_monitor scripts
python -m vps_monitor.monitor --output
```

离线输出固定为 `mode=fixture`，结构门和产品门都必须失败。授权 live 运行使用：

```bash
python -m vps_monitor.monitor --live --output
python -m vps_monitor.monitor --structure-gate
python -m vps_monitor.monitor --quality-gate
```

详细字段见 `docs/schema.md`，恢复、证书、告警、回滚和 Release 操作见 `docs/operations.md`。
