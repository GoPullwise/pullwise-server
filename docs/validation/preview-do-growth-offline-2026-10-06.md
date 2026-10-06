# Preview journal 离线容量评审 — 2026-10-06

当前证据不支持把 journal 容量列为此次发布的阻塞项。它是持续高流量下需要
提前评审的真实容量约束：单个 SQLite Durable Object 上限为 **10 GB**，当前
追加历史没有删除或分段策略。每月 200 万次 API、平均每次 12 组操作的假设下，
从空对象推算约 **3.3–21.6 个月**接近上限，跨度来自结算比例和保守留量；
这不是已测线上余量，也不是保证的最短可用时间。

本次仅阅读源码、既有原生成本证据和官方文档，在内存中的主机 SQLite 执行
四个有限的合成页容量样本。没有应用请求、D1 请求、provider 调用或新的
Cloudflare native fixture，没有修改源码、原 journal、旧证据、计数、不明结果、
财务历史，也没有清理、重置、切换或创建 namespace。

## 官方依据

[Cloudflare SQLite DO GA 公告](https://developers.cloudflare.com/changelog/post/2025-04-07-sqlite-in-durable-objects-ga/)
明确每对象 10 GB；[概念页正文](https://developers.cloudflare.com/durable-objects/concepts/what-are-durable-objects/)
同样写 up to 10 GB，该页旧的 1 GB/pre-GA 脚注没有用于计算。这里按
`10 × 10^9` 字节保守规划；对象当前实际存储字节未读取。

[当前价格说明](https://developers.cloudflare.com/durable-objects/platform/pricing/)
为 Paid 账户每月包含 5,000 万行写和 5 GB-month，超额分别为 $1/百万行和
$0.20/GB-month。10 GB 的单对象容量限制和账户聚合存储费用分别计算。
若仅此对象且包含量未被其他项目占用，接近 10 GB 时单纯存储超额约 $1/月，
因此先遇到的是容量约束；
更主要的成本敏感项仍是频繁会计写入，不能据此保证整体月费。

## 实际增长结构

`BudgetJournal._create_product_evidence_tables()` 的追加表存储整数列，
**并非每组存储一份 JSON**。`preview_operation_evidence.id` 与
`preview_read_settlements.evidence_id` 都是 `INTEGER PRIMARY KEY`，没有额外
声明的二级索引。保留的旧 JSON 证据位于原 `validation_budget` 行，新操作不
继续向旧数组追加；新计数与基数状态反复更新该行。

每组追加一条 evidence；只有所有原生结果的 `meta.total_attempts` 都明确为
整数 1 时，才追加 settlement。null、不明结果或重试不释放该预留。此前原生
D1 fixture 的 attempts 为 null；DO 成本微基准则显式使用完整单次尝试的
synthetic 元数据。因此本评审分别计算每月 **2,400 万条 evidence** 与
**0–2,400 万条 settlement**，没有把最大结算比例当作已测线上比例。

原生成本微基准的稳定 12 组事务计费 68 行写，其中 24 行是追加证据与结算，
44 行是计数、状态及短期 bucket 更新。成本方案的 80 行写/API，即每月
1.6 亿行计费写入，不等于每月新增 1.6 亿条持久历史。

## 离线样本与敏感性

从当前源码 AST 原样提取三个追加表的 DDL，在主机 SQLite 3.42.0、4 KiB 页
中插入 24,000 条 evidence，并按场景插入同量 settlement。每场景相当于
2,000 个合成 API，各 12 组。跳过累计计数触发器，扣除空表初始页；测得的是
这些追加表的新页占用，包含主键/B-tree 页，不是 Cloudflare native 存储或计费。
主键/请求编号分别按第一个月和第十二个月量级选取，以体现整数宽度变化。

| 离线场景 | evidence 字节/行 | settlement 字节/行 | 每月增长，十进制 GB | 从空对象到 10 GB |
| --- | ---: | ---: | ---: | ---: |
| 第一个月量级，无结算 | 19.3 | 0 | 0.463 | 21.6 月 |
| 第一个月量级，全部结算 | 19.3 | 11.1 | 0.729 | 13.7 月 |
| 第十二个月编号量级，全部结算 | 21.3 | 12.1 | 0.803 | 12.5 月 |
| 第十二个月编号量级，读数/预留差为百万 | 23.4 | 14.2 | 0.901 | 11.1 月 |

若把相同整数事实转换为 compact JSON，含主键的 evidence 为 91–99 字节，
settlement 为 36–42 字节；它们仅说明序列化敏感性，不是当前 SQL 列存储尺寸。
全部结算时按 JSON 长度简单外推约 3.05–3.38 GB/月，尚未加页和索引留量。

为覆盖实际后台页开销、列值变化、碎片和其他未测差异，另给每个
evidence/settlement 对 64、96、128 字节的规划留量：

| 规划字节/操作组 | 每月增长 | 从空对象到 10 GB |
| --- | ---: | ---: |
| 64 | 1.536 GB | 6.5 月 |
| 96 | 2.304 GB | 4.3 月 |
| 128 | 3.072 GB | 3.3 月 |

这些留量是敏感性假设，不是已证明的字节上界。现有其他表、旧记录、失败行和
后台存储也占容量；实际剩余时间应为 `(10 GB − 当前字节) / 每月增长`。
12 组是流量平均值假设，代码允许的单请求最大组数更高；平均组数上升会线性
缩短时间。范围不能用于声称任何流量下都有至少三个月容量。

## 实施方向

持续达到上述高流量前安排容量评审。未来一次有明确范围的状态查看可读取
`sql.databaseSize`，用原生字节和新增证据数校正估算；官方
[本地测试示例](https://developers.cloudflare.com/durable-objects/examples/testing-with-durable-objects/)
展示了这个只读属性。本轮没有读取它，也没有新增轮询或按累计金额停机。

长期可研究经过验证的历史分段或归档，以及减少重复累计状态更新。容量方案
必须保持协调、会计原子性、连续计数、原始旧证据和不明原生结果的权威性；
财务和账单历史保持原义。跨对象分段还需要证明安全交接与恢复，不能直接
更换对象名来绕过原状态。归档副本未经完整校验也不能作为删掉原记录的依据。
本评审没有实施这些变更，当前记录全部保留。

完整合成尺寸与假设见[脱敏 JSON](preview-do-growth-offline-2026-10-06.json)。
既有[原生成本微基准](preview-do-cost-native-2026-10-06.md)用于解释计费行写，
与此次主机 SQLite 字节估算分别保留。
