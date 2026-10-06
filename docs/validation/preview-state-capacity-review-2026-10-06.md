# Preview 共享身份/账单状态容量审查 — 2026-10-06

本报告保留修复前的容量基线与源代码 SHA256。后续本地源码已完成逐记录改造：
五种身份/账单状态使用现有 `app_state` 的 `record:<kind>:<id>` 主键，账号记录
≤512 KiB，其他记录与 HTTP ingress ≤8 KiB；没有改变 schema 5、数据库或 journal。
旧数据经一次有界原子 copy/cutover 保留全部字段，旧容器清为空，完成 marker
防止重放。并发 CAS、未知停止、严格 JSON、规模、权限与成本证据分别验收。
本报告的基线数字没有改写成新系统容量承诺；新源码仍须通过原生迁移/重启验收
并正式发布，才能声称线上共享容量 blocker 已解除。

实际修复选择保留完整有界账号记录，未在此轮拆分 embedded repo/billing history：
原生本地规模测试已验证 197,041-byte 账号含 1,000 repos 和 100 条账单 history。
512 KiB 是单账号 UTF-8 包络，不能表述成所有可能字段形状的数量保证。会话、
OAuth、账单 event/pending 已逐记录拆分；有限分页最多加载 8 MiB 包络（16 个
大账号或 1,000 个小记录）。发行 session/state 最多顺带 CAS 清理 8 条过期记录。
严格 writer/cutover 验证重复 key、非有限数字和无效 Unicode；聚合校验只验证
结构、kind、identity、字节和数字基数。支持的语义闭包建立于空库初始化、严格
legacy cutover 和 typed 全记录 mutation，任意 SQL 导入/控制台旁路写入不受支持。

最初审查发现一个产品容量问题：preview 的 8 KiB 状态限制作用于全站共享
`app_state` JSON，而非每个账号/会话。它仍会限制正常用户增长；部分账单更新
还能在 native mutation 成功后触发 `PREVIEW_DATA_BOUND`，停止整个 preview journal。
当时双账号功能测试不能证明这个容量问题已解决。基线轮次只审查和本地复现，
未修改产品源码、未部署、未调用应用 HTTP、D1 或支付供应商。

## 修复前的存储与限制位置

`app_state.name='users'` 保存全站用户 map，`sessions` 保存全站会话 map，
`githubStates` 保存全站 OAuth state map。`billingEvents` 是全站 event map，
`billingPendingUpdates` 是全站 pending list。它们不是逐记录 D1 行。

- [`_write_user`](../../pullwise_server/cloudflare_github_identity_http.py#L100)
  读取整个 users map，并以完整新旧 JSON 做 CAS 更新。
- [`issue_session`](../../pullwise_server/cloudflare_session_adapter.py#L20)
  读取并重写整个 sessions map；发行时不移除过期会话。
- [`D1OAuthStates`](../../pullwise_server/cloudflare_oauth_state_adapter.py)
  同样重写 githubStates；未回调的过期 state 在发行时没有清理。
- [`D1AccountTransactions`](../../pullwise_server/cloudflare_account_adapter.py)
  从 users 的 `json_each` 提取单个账号，但事件与 pending 通路读取共享全量。
- [`_input_bound`](../../pullwise_server/cloudflare_preview_budget.py#L163)
  把每个普通 SQL 字符串参数限制为 8,192 UTF-8 bytes，因此共享 map 的完整新旧
  snapshot 都受此限制。
- 同模块 `_STATE_SQL` 为 `SELECT name,payload FROM app_state LIMIT 7`；初始化/
  升级验证及每次 mutation 后的 `refresh()` 还要求最多 6 个状态行，每行 payload
  不超过 8,192 bytes。`refresh()` 超限调用 journal `_reject`，形成持久全局停止。

这些限制是应用层 preview 校验规则，不是本轮验证出的 Cloudflare 账号配额。

## 有限离线量化

[脱敏记录](preview-state-capacity-review-2026-10-06.json)只使用 synthetic 对象，
按真实字段结构和 compact UTF-8 JSON 序列化。表中是具体形状的边界，不是账号
容量承诺；实际姓名、token、账单、repo metadata 的长度不同，边界会变化。
普通账号样本 token 使用 40-byte plaintext 经当前 AES-GCM envelope 的 96-character
储存长度，没有生成或使用真实 token。

| 共享状态/形状 | 最后接受的数量 / bytes | 首个超限数量 / bytes |
| --- | ---: | ---: |
| 普通登录账号，尚无账单/repo history | 20 / 7,921 | 21 / 8,317 |
| 代码允许的最大 profile 字段样本 | 4 / 6,881 | 5 / 8,601 |
| 一个账号的会话 | 44 / 8,097 | 45 / 8,281 |
| 未消费的登录 OAuth state | 40 / 8,041 | 41 / 8,242 |
| 单个账号安装回调缓存的 repos | 46 / 8,085 | 47 / 8,246 |

同一 repo 形状的 1,000 个记录占 161,679 bytes，但 `_repo_items` 允许最多
1,000 条，安装回调将其全部写入 users JSON。正常 repo GET/sync 已实时读取
GitHub grants，并不依赖这个持久列表。因此即使只有一个账号，代码允许的
合法安装/账单形状也不能保证装入当前 8 KiB 行。

不存在对所有当前合法记录通用的“最多 20 个账号”安全承诺。准确规则只能是
每个共享 payload 及 CAS snapshot 均 ≤8,192 bytes；把这一总字节条件包装成
用户数量会掩盖增长失败。

## 合法 mutation 后全局停止的复现

本地 CPython SQLite 运行当前 `stage_account_write` 相同的 JSON-set SQL。
这是 SQL 语义复现，没有声称使用 native D1、计费行数或客户数据：

1. users 全局 JSON 为 7,921 bytes，仍在 bound 内。
2. 下一用户对象加入一个小的 synthetic billing subscription 后，所有 SQL 参数
   最大为 932 bytes，现有 `_input_bound` 接受。
3. `json_set` 成功修改 1 行，使全局 users JSON 变成 8,480 bytes。
4. 对实际结果调用 canonical `_validated_product_data()`，得到
   `PREVIEW_DATA_BOUND`。真实 `refresh()` 对同样超限调用 journal `_reject`。

这不是 missing native meta 或未知 mutation。以“数据容量不够”为理由永久停止
所有用户，属于正常产品增长触发的 blocker；账单失败隔离不能修复共享存储结构。

## 最初提出的最小有界扩展方向

应把现有 8 KiB 防护应用到独立记录，并将读写量限定在当前 owner/record：

1. 逐记录存储 user、session、OAuth state、billing event/pending。最少 DDL 的方案
   可用现有 app_state 的 `user:<id>` 等 key，但必须同步替换所有全局 JSON/CAS/
   `json_each` readers，以及 `LIMIT 7` payload scan。需要按 customer/subscription/
   owner/expiry 的索引查询时，一个规范化 record 表加明确索引更合适，走显式版本
   迁移。只修改 wrapper 字节数不能实现扩展。
2. user 保留有界当前身份/账单字段。将 embedded billingSubscriptions、最多
   100 条 billingSubscriptionEvents 移到按 owner/event 的独立记录；安装回调仅
   保留有界授权 metadata，实时 repo 列表仍按现有 10 installations/1,000 repos
   的 provider pagination/response bound 获取。
3. 在发行 session/OAuth state 时用 expiry 索引有限清理已过期记录，避免匿名授权
   未回调或旧登录累计堵塞全站。清理不写 DO/D1 定时轮询，不删除支付 replay/
   receipt/audit 历史。合理 per-owner/session bounds 不应变成全站账号上限。
4. 保持 user revision、ownership、entitlement dirty/refresh、receipt pending→applied
   与 guard 的同一 D1 原子批次；迁移有明确 cutover marker，保留用户字段与真实
   凭据隔离。沿用原 DO namespace/journal 和历史计数，不自动恢复未知/incomplete stops。
5. `refresh()` 保存有界的数字基数/必要记录数组最大值，不拉取所有账号 payload。
   逐主键查询降低全量 JSON parse 和热点 CAS，成本与当前请求涉及的记录有关。

把全局 map 临时改成 64/256 KiB 只推迟同样的失败，还保留全站热点行、全量
反序列化和扫描。它最多是明确标注容量的短期补丁，不能作为可扩展交付完成。

变更应先用有限本地验收覆盖旧数据迁移、多个账号/会话规模、当前 8 KiB 边界、
单账号 100 条账单 history、1,000 repo 返回、并发 CAS/权限快照、失败隔离、
重启和完整 native meta，再协调新的 preview 发布。本审查没有擅自触发迁移。
