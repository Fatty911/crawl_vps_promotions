# 评审流程规范（review process）

> 本文件固化 2026-08-10 的评审有效性复盘结论：静态 diff 评审能发现
> diff 内逻辑错误（参数有效性、配置发现、路径白名单），但无法发现
> 三类动态问题（跨文件环境一致性、运行时行为、外部世界页面结构）。
> 以下要求把这三类问题纳入评审输入，防止同类 bug 再次漏过评审。

## 1. 评审输入包（Review Evidence Bundle）

每次两家评审前，实施方必须准备评审输入包，包含：

| 组件 | 要求 | 缺失后果 |
|---|---|---|
| staged diff | `git diff --cached --binary` | 门禁拒绝 |
| candidate hash | `candidate_state.candidate_hash(Path, "staged")` | 门禁拒绝 |
| 变更说明 | 根因/修复/验证一句话链 | 评审无法判断意图 |
| **运行时证据** | 见下节 | 动态类问题评审盲区 |
| **跨文件核对清单** | 见下节 | 环境一致性盲区 |
| **外部世界证据** | 见下节 | 页面结构盲区 |

## 2. 运行时证据（dynamic evidence）

凡涉及以下任一场景，必须把实测运行输出嵌入评审 prompt：

- **LLM/Agent 工具调用链**：opencode/Codex 的 `--format json` 事件流关键行
  （`step_finish reason`、`event types`、`tokens.output`、`full stdout` 截断）。
  例：glm-5.2 `reason=length, output=16000, 无 text part` 证明 thinking 烧预算。
- **网络抓取**：`curl` 实测的页面片段（含真实 HTML 结构，如 BuyVM
  `<div class="plan fourplan">`、`<a data-plan=... class=orderbutton>` 无 href）。
- **数据流**：evidence/status 实际值（如 `final_url` 是域名根而非产品页）。
- **失败诊断**：workflow 日志中 `[vps-repair]`/`[vps-monitor]` 关键行。

没有运行时证据的评审，评审模型只能静态猜——这正是
2026-08-08~10 多轮 bug（verify 直连、span 分隔、href-less 按钮、
thinking 预算）漏过的根因。

## 3. 跨文件一致性核对清单（cross-file consistency checklist）

评审 prompt 必须要求审查者逐项核对（尤其当 diff 只涉及单文件时）：

- [ ] workflow 间通道一致：`vps-monitor.yml` vs `vps-repair.yml` 的
      mihomo/playwright/secret 配置是否对齐？
- [ ] 抓取链一致：`vps_monitor/verify.py` 与 `vps_monitor/monitor.py` 的
      代理/渲染/解析通道是否一致？
- [ ] 数据源一致：`classify_vps_failure.py` 用的 URL/token 是否来自
      providers.yaml（配置源）而非 evidence 运行时值（可能是域名根/挑战页）？
- [ ] 新文件是否登记 `codex_delivery_gate.py` AUTHORIZED_PATHS？
- [ ] 新运行产物是否加入 `.gitignore`？

## 4. 外部世界证据（external-world evidence）

凡修复涉及**页面结构/API 交互/模型行为**，评审前必须附：

- 真实页面 HTML 片段（curl 抓取，标注来源 URL 与日期）
- 或 API 实测响应（curl 输出）
- 或模型/工具实测行为（opencode 事件流、max_tokens 截断等）

禁止仅凭推断声称"页面结构是 X"——评审模型必须能看到一手证据。

## 5. 实施方自检（pre-review self-check）

提交前实施方必须自查：

1. 变更是否涉及 2/3/4 节场景？涉及则证据已附？
2. 变更文件是否在当次 staged diff 之外还有关联文件（workflow/verify/
   classify）需要纳入一致性核对？
3. 是否本地实测过（单测/curl/事件流）且输出可追溯？

## 6. 例外

- 纯注释/文档/测试断言改动且不涉外部世界：证据可简化（注明"无外部世界"）。
- 运行时证据获取成本过高（如需生产环境）：注明并给出可复现路径。

---

*复盘依据：2026-08-08~10 crawl_vps_promotions 24 轮修复迭代。*
