# 运维手册

## 离线与授权 live

离线验证运行 `python -m pytest -q` 和 `python -m compileall -q vps_monitor scripts`。fixture 只证明 parser；离线 `--output` 固定为 `mode=fixture`，结构门与产品门预期失败。

授权 live 前确认服务商条款与抓取速率。VPS 默认直连；不登录、不绕验证码，也不因达标压力启用未经授权的代理。live 后依次执行结构门、Pages 部署、正常 TLS 部署后核验和产品门。

## State 恢复与回滚

只从 main 历史 run、非当前 run、repo/branch/schema/config hash 均匹配的最新 state artifact 恢复。ZIP 路径、成员数量与大小均受限，跨主机重定向剥离 Authorization；失败原子回滚。

无合法 state 时从空历史开始。回滚应重新运行上一个已知良好 source SHA，并生成新批次；禁止复制旧 prices 到当前批次。

## 解释状态

- `blocked`: 验证码、403、风控或明确上游阻断。
- `rejected`: 页面可读但身份、域、币种、账期、库存或唯一性不满足。
- `error`: 网络、超时或解析执行失败。
- `out_of_stock`: 找到稳定套餐但明确售罄，不计产品门。
- `success`: 本轮明确可购、真实同卡、证据完整。

页面默认展示全部 14 状态。原金额和同币种月化值分开；不要跨币种比较。

## 证书、告警与 Release

公开验收必须正常校验证书并读取 `manifest.json`；不得用 `-k`。CNAME/DNS/SAN 异常时停止 Release 和完成声明。

失败按 audit fingerprint 更新同一 issue，恢复时自动关闭。上游阻断、证书或授权问题不能触发自动改码。每个合格日批次最多归档一个公开 Release；原始响应、Cookie、token 和浏览器 profile 不进入 Release。

## 交付

遵守 `AGENTS.md` 的独立 2/2 评审、严格 RED→GREEN 和完整 staged diff 重审。交付前执行测试、compileall、YAML/schema/workflow/安全/XSS/敏感扫描和 `git diff --check`。只允许非 force `HEAD:main`，远端前移后必须重新整合、测试和评审。
