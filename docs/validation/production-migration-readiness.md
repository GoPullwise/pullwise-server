# 同一代码从 Preview 迁移到正式环境

2026-10-06 工程修复。当前任务只做 Preview 远程验收；正式 D1 仍默认暂停。
后续多人账本、多仓库和 Organization 新版本已实现，其独立验收见
`workspaces-native-local-2026-10-06.json` 与最新发布记录。

## 已修复的迁移阻塞

Server 原入口只允许 local/preview，即使正式模式显式开启 D1 也会返回
`VALIDATION_CONTROL_REQUIRED`。正式模式现在进入相同的认证、owner 隔离、
API-key 范围和原子商业配额应用链，使用自己的 D1，不依赖 Preview 的临时
验证 DO，也不会在业务请求中自动建表。Preview 仍使用原固定验证器和预算。

旧静态检查也无条件拒绝正式 D1 开关 `1`。新检查允许正确的正式配置，
同时拒绝混入 Preview 数据库、域名、callback、测试支付端点或验证 DO。
普通部署和 GitHub main 自动 Builds 的正式配置仍为 `0`，不产生业务 D1
访问。正式需要启用时有显式、可验证的部署选项，不必修改代码或默认配置。

Web 正式配置已补 ASSETS binding 与 HTML 中间件路由。公开页面的 SEO、
私有页面 noindex 以及 www 跳转会实际执行；浏览器 API 仍使用 `/api`，
服务绑定指向自己的 Server。Preview 配置、现有浏览器资源和无索引策略不变。

Jev 的 36 条实际英中结果及独立门槛已通过；Preview 的两个开关现在写入
版本化配置为 `1`，普通部署会保留已验收的建议功能。正式两个开关保持 `0`；
TypeSafe 凭据使用独立 Worker Secret，套餐与每日/月度模型限额继续执行。

## 正式环境的显式启用路径

使用同一份代码和五个 canonical migrations，正式数据库、GitHub App、
callback、Creem live 产品与 Secret 按正式配置绑定。迁移由独立操作应用到
正式 D1；部署脚本不自动迁移或复制 Preview 数据。实际启用前应确认正式
schema 与必要 Secret 已就绪，业务入口不会用 Preview 初始化替代它们。

0005 仅追加成员、邀请、审计、仓库关联及项目列，回填旧单仓库关联；
已有正式 v4 schema 可按顺序应用 0005，空正式库应用 0001–0005。
既有 owner/project/expense ID 和金额历史不重写。正式仍由独立迁移操作
准备 schema，不会自动调用 Preview 的受控升级器。本轮没有执行正式迁移。

以下检查完全在本地执行，只审查正式启用候选，不访问 Cloudflare：

```sh
python scripts/check-ledger-s01.py --environment production --activate-production
./scripts/deploy-cloudflare.sh --environment production --activate-production
```

第二条默认仅打印 dry-run 计划。未来实际正式启用时，部署命令是：

```sh
./scripts/deploy-cloudflare.sh --environment production \
  --activate-production --execute --local-checks-passed
```

它先验证隔离配置，再用 `--var PULLWISE_D1_ACCESS_ENABLED:1` 显式启用；
checked-in 默认 `0` 不变。该选项拒绝 preview，正常部署不带它就保持正式
暂停。以上正式远程启用命令没有在本轮执行；本轮远程发布与回归仅选 preview。

## 验证范围

新增正式入口回归覆盖 Cookie/Bearer、失效与撤销 key、跨 owner 隔离、
Cookie Origin、Shared key 限制、原子配额与重放、错误脱敏、缺 schema 和
未知模式。配置/发布回归覆盖正确启用、误用 Preview 资源拒绝、普通正式
部署保持暂停以及显式启用仍使用固定 Python builder。

最终完整 Server 测试通过 497 项；Web 通过 343 项及构建。首次 Server
全套运行的唯一失败是沙箱未允许一个本地 loopback socket，授权网络环境
下复跑全部通过；没有把该失败算成产品错误。真实本地 Python Worker/D1
正式模式通过 22/22 请求，覆盖 251 行 CSV、认证、owner/Origin、修订冲突、
幂等及月度限额；落盘 SQL 证实拒绝与重放没有消耗写额度。证据在
production-native-local-2026-10-06.json，最终 Preview 发布证据在
local-acceptance.md。该本地正式模式验证不是远程正式验收，也没有激活
正式数据库。
