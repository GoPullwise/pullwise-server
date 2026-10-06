# Preview DO 原生存储成本补充 — 2026-10-06

本轮以 3 次本地 HTTP 请求测量 4 个有限的代表性会计事务，结果通过。
稳定 3 组操作消耗 20 行 DO 写入，12 组操作加 3 次状态发布消耗 68–70 行。
它支持成本方案的 80 行写/API 规划参数用于这一形状；不能据此保证所有接口的
平均值、最大值或 200 美元月费。120 行写/API 的敏感性场景仍需保留。

## 来源与方法

运行[独立本地脚本](../../scripts/check-preview-do-cost-native.py)，复用既有的
Python Worker/workerd 本地运行器、SQLite Durable Object 和缓存 runtime。
fixture 没有 D1 binding，没有 provider gateway，也没有部署诊断接口。
HTTP 请求仅访问 loopback；请求上限为 3，无客户端重试。
没有重跑此前 1,161 次 admission 测试，也没有访问客户数据。

`ObservedSql` 保留每个原生 `ctx.storage.sql.exec()` cursor，等实际方法消费完后
读取并相加 `cursor.rowsRead` 与 `cursor.rowsWritten`，而非 Python mock、
SQLite `changes()` 或从 SQL 字符串猜测。原生 cursor 指标包含实际触发器和
索引维护的影响。逐阶段指标和测量时 canonical module SHA256 保存在
[脱敏 JSON](preview-do-cost-native-2026-10-06.json)。

事务依次调用现有 `PreviewRateLimiter.ingress()`、`actor()`、
`BudgetJournal.begin_product()`、`reserve_operation()`、`record()`、
`settle_product_reads()`、必要的 `save_product_state()` 与 `finish()`。
每组固定预留 20 行读、记录 2 行读；3 个 synthetic 写组分别预留 9 行写、
记录 3 行写。这些结果明确标记为 synthetic，只用于驱动 DO 会计路径，
**没有执行 D1 SQL，不能作为应用 D1 账单或客户记录证据**。
每个 synthetic 写组发布一次 verified 基数状态。

## 原生结果

| 代表性事务 | 操作组 | 状态发布 | 行读 | 行写 | SQL exec |
| --- | ---: | ---: | ---: | ---: | ---: |
| 首次 subject，读取形状 | 3 | 0 | 44 | 27 | 35 |
| 稳定 subject，读取形状 | 3 | 0 | 44 | 20 | 32 |
| 稳定 ingress，首次 actor 写 bucket | 12 | 3 | 139 | 70 | 101 |
| 同一存储重建构造器后的写形状 | 12 | 3 | 140 | 68 | 101 |

暖态 12 组事务分阶段的行写为：ingress 2、actor 1、begin 1、
operation accounting 63（12 × 5 加 3 次状态发布）、finish 1，共 68。
首次创建 actor 写 bucket 比暖态多 2 行，因此对应事务为 70 行。
暖态 3 组事务为 `3 × 5 + 5 = 20` 行写。
首次 ingress IP/credential subject 和 actor 读取 subject 创建增加 7 行，
得到 27 行。60/120 的 actor 限流使用同一 DO，不写 D1。

| 单独报告的构造/fixture 成本 | 行读 | 行写 | SQL exec |
| --- | ---: | ---: | ---: |
| 首次空 DO 构造器 | 15 | 23 | 20 |
| 同一 native SQLite 存储重建构造器 | 6 | 0 | 17 |
| fixture 状态 scaffold，排除在上述事务外 | 6 | 3 | 6 |

重建前后的 journal snapshot 严格相等；历史计数和 evidence 没有清空。
这里测试的是同一 native SQLite 存储上重建 canonical 构造器，未声称发生
workerd 进程重启。独立的[原生 journal 验收](preview-operation-journal-native-2026-10-06.json)
记录此前的持久化及恢复测试。本轮没有触碰线上 journal。

## 对成本方案的影响与边界

80 行写/API 对所测 12 组形状留有 10–12 行余量；对稳定 3 组读取形状留有
60 行余量。它仍是规划参数，微基准没有代表真实月流量的请求比例。
更多 operation groups、额外状态发布、过期 subject 清理、不同主体创建组合
会增加行写。例外失败事务与旧 journal 增长未在这 4 个事务内测量。

完整 `ProductMeteredD1` 的额外 check/snapshot 读取没有全部进入这次逐阶段求和；
因此 300 行读/API 仍是未通过完整接口 trace 验证的规划余量。
应用 D1 实际读写、基数扫描、Worker CPU、DO 时长、长 journal 存储与真实账单
也未在本轮测量。已有 D1 功能验收及会计保留额应分别解释，不能相加当作发票。

目前[成本方案](cloudflare-cost-plan-2026-10-06.md)的 2 百万 API/月增长形状，
按 80 行写估计总费用 $124.10；若实测月平均达到 120 行写，则变成 $204.10。
这项敏感性限制仍然成立，不能因本地 68–70 的样本删除。合理限流和成本优化
继续服务正常用户，没有日/月硬停机限额。

## 复现

从仓库根目录运行，下列命令仅建立一个新的本地 fixture，运行目录必须尚不存在：

```sh
.venv/bin/python scripts/check-preview-do-cost-native.py \
  --run-dir /workspace/qa-private/do-cost-native-new \
  --output /workspace/qa-private/do-cost-native-new.json --port 8907
```

运行器结束时停止本地 runtime。测量完成后的 provenance 注释仅离线读写文件，
没有额外 HTTP 请求。JSON 保留确切原生指标，不包含账号、cookie、token、IP、
SQL 参数或 provider 响应；fixture 中的主体是公开保留地址与 synthetic 名称。
