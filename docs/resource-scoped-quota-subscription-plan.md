# Pullwise 资源维度订阅与免费额度改造计划

日期：2026-05-25 | 实现状态：已实现 (2026-05-30)

> 本方案中描述的资源维度额度系统已实现。`quota.py` 模块提供
> `consume_scan_quota()` 函数，`quota_buckets` 和 `quota_ledger` 表
> 提供 workspace/repository 级别的配额追踪。billing 和 scan 创建链路
> 已改为原子扣减、scope-aware 的模型。

## 目标

把 Pullwise 当前“按登录账户赠送免费扫描额度”的模型，改造成“按 workspace / GitHub App installation / repository 共享额度”的模型，解决同一个真实用户创建多个 GitHub 账号、分别登录并扫描同一个 repo 来重复获取免费额度的问题。

最终状态：

- GitHub 登录账户只作为身份与成员权限来源，不再作为免费额度主体。
- 订阅、免费额度、用量统计归属于 workspace。
- repo 使用 GitHub 稳定 ID 识别；同一个 `github_repo_id` 的免费额度在所有可访问账号之间共享。
- 同一个 workspace 内的成员共享订阅与 workspace 级额度。
- 服务端 scan 创建链路原子扣减额度，并且不信任前端传入的 workspace/repo 归属。
- 前端展示“workspace / repository 共享额度”，不再暗示“account quota”。

## 当前现状

### pullwise-server

当前后端已经具备这次改造需要的 GitHub App 与 repo 授权基础，但额度仍然是用户维度。

- `pullwise_server/app.py`
  - `billing_usage_for_user()` 从 `user["billingUsage"]` 读取月度用量。
  - `consume_review_quota(user)` 直接扣当前登录 user 的额度。
  - `POST /scans` 在创建 scan 前调用 `consume_review_quota(user)`。
  - scan 记录里保存了 `installationId`、`installationAccount`、`repositorySelection`、`cloneUrl` 和当时的 `billingUsage`。
  - `GET /billing/plan` 返回 `account: billing_account_payload(user)`，也是 user/account 视角。
- `pullwise_server/billing.py`
  - `review_limit(plan)` 目前只区分 `free` 和 `pro`，默认 free 5 次、pro 60 次。
  - Creem checkout metadata 使用 `userId` 绑定订阅。
- `pullwise_server/github_auth.py`
  - `repo_to_pullwise()` 和 `repo_payload_to_pullwise()` 已经把 GitHub repo 的 `id` 映射到前端 repo item 的 `id`。
  - 目前没有显式保存 `node_id`、owner id、fork parent/source 等反滥用字段。
- `pullwise_server/db.py`
  - 当前主要把逻辑状态存为 SQLite `app_state` JSON payload。
  - 已有 `api_rate_limits` 表，但没有 workspace、repo、quota bucket、quota ledger 等独立表。

### pullwise-web

前端已经有 billing、repo 选择、scan 创建、错误跳转的页面基础，但 UI 仍是 account 用量口径。

- `src/api/pullwise.js`
  - 已有 `billing.getPlan()`、`billing.createCheckoutSession()`、`billing.changeSubscriptionInterval()`、`billing.cancelSubscription()`；不再暴露 Creem customer portal。
  - scan 创建仍只提交 `{ repo, branch, commit, requestId }`。
- `src/lib/pullwise-data.js`
  - `normalizeRepo()` 保留 repo item 原始字段，但没有 workspace/quota 语义。
  - `useScanRun()` 和 `useScanBatchRun()` 调用 `pullwiseApi.scans.create()`。
- `src/screens/billing.jsx`
  - 使用 `plan.account.usage` 展示 `Free usage` / `Pro usage`。
  - plan card 文案是 `reviews / month`，没有说明额度是 workspace/repo 共享。
- `src/screens/flow.jsx`
  - `ReposScreen` 能展示 GitHub installations。
  - `ScanningScreen` 的错误处理靠字符串包含 `monthly review limit` 跳转到 Billing。
- `src/shell.jsx`
  - Sidebar 已有 Workspace 区域，但当前只是静态 Pullwise 标识。
- `src/screens/issues.jsx`
  - `SettingsScreen` 展示 GitHub repository authorization 与 installations，可扩展 workspace 归属说明。

## 目标数据模型

推荐把数据拆成以下主体。第一阶段仍可继续使用 SQLite，但 quota 相关数据应使用独立表，避免 JSON 状态下并发扣减不可靠。

### User

代表 Pullwise 登录用户。

关键字段：

- `id`
- `github_user_id`
- `github_login`
- `email`

职责：

- 身份认证。
- 权限验证。
- workspace membership。

不再承担：

- 免费额度主体。
- 订阅主体。

### Workspace

计费与额度主体。通常对应一个 GitHub App installation 或一个 GitHub org/user owner。

关键字段：

- `id`
- `name`
- `github_owner_id`
- `github_owner_login`
- `github_owner_type`: `User` / `Organization`
- `github_app_installation_id`
- `plan`: `free` / `pro`
- `billing_provider`
- `billing_customer_id`
- `billing_subscription_id`
- `billing_subscription_item_id`
- `billing_status`
- `billing_interval`

约束：

- `github_app_installation_id` 唯一。
- 如果 GitHub 安装不可用，允许 personal workspace，但不作为长期主路径。

### WorkspaceMember

用户与 workspace 的关系。

关键字段：

- `workspace_id`
- `user_id`
- `role`: `owner` / `admin` / `member`
- `source`: `github_installation` / `personal_workspace`

作用：

- 控制用户能否查看 workspace、repo、scan、billing。
- 不控制 repo 真实权限；scan 前仍要用 GitHub 权限校验。

### Repository

被扫描资源主体。

关键字段：

- `id`
- `github_repo_id`
- `github_node_id`
- `full_name`
- `owner_login`
- `owner_id`
- `default_branch`
- `private`
- `fork`
- `parent_github_repo_id`
- `source_github_repo_id`
- `html_url`
- `clone_url`
- `last_synced_at`

约束：

- `github_repo_id` 唯一。
- `full_name` 仅作展示与 fallback，不作为额度唯一键。

### WorkspaceRepository

workspace 与 repo 的授权关系。

关键字段：

- `workspace_id`
- `repository_id`
- `github_app_installation_id`
- `permissions`
- `repository_selection`
- `installation_account`
- `last_authorized_at`

作用：

- 一个 repo 被哪个 installation/workspace 授权。
- scan 时找到 repo 对应 workspace 与 installation token。

### QuotaBucket

额度桶。用于 workspace 级与 repo 级共享额度。

关键字段：

- `id`
- `scope_type`: `workspace` / `repository` / `repository_trial`
- `scope_id`
- `period`: 例如 `2026-05`
- `plan`
- `limit`
- `used`
- `reset_at`

约束：

- `(scope_type, scope_id, period, plan)` 唯一。

原子扣减：

```sql
UPDATE quota_buckets
SET used = used + 1
WHERE id = ?
  AND used < limit;
```

影响行数为 0 时，说明额度不足。

### QuotaLedger

额度审计日志。

关键字段：

- `id`
- `workspace_id`
- `repository_id`
- `github_repo_id`
- `scan_id`
- `requested_by_user_id`
- `request_id`
- `bucket_id`
- `delta`: 通常为 `1`
- `reason`: `scan_created`
- `created_at`

作用：

- 查账。
- 处理幂等与并发。
- 排查滥用。

### RepoFingerprint

第二阶段风控用，不阻塞第一阶段。

关键字段：

- `repository_id`
- `default_branch`
- `head_sha`
- `tree_sha`
- `lockfile_hash`
- `manifest_hash`
- `source_fingerprint`
- `computed_at`

作用：

- 识别 fork / clone / 高相似代码库重复拿免费额度。

## 额度规则

推荐产品规则：

```text
Free workspace:
- 每个 workspace 每月 N 次免费 scan。
- 单个 repo 每月最多 M 次免费 scan。
- 同一个 github_repo_id 的 repo 免费额度在所有用户账号之间共享。

Pro workspace:
- workspace 每月共享 Pro scan allowance。
- repo 级上限可以放宽或关闭，但仍保留滥用风控。
```

建议默认值：

- `PULLWISE_FREE_WORKSPACE_REVIEW_LIMIT=5`
- repo 级默认与用户/workspace 级保持一致：free 5、pro 60
- `PULLWISE_PRO_WORKSPACE_REVIEW_LIMIT=60`

保留当前 `PULLWISE_FREE_REVIEW_LIMIT` 和 `PULLWISE_PRO_REVIEW_LIMIT` 一段时间作为兼容别名，但新语义应迁移到 workspace/repo。

## 需要修改的地方与功能目的

### pullwise-server

| 文件 | 修改内容 | 功能目的 |
| --- | --- | --- |
| `pullwise_server/db.py` | 增加 workspace、workspace_members、repositories、workspace_repositories、quota_buckets、quota_ledger、repo_fingerprints 表与查询函数 | 让 repo/workspace/quota 成为可原子更新的数据，而不是 user JSON 字段 |
| `pullwise_server/github_auth.py` | 在 repo normalization 中保留 `node_id`、owner、fork、parent/source、permissions 等字段 | 用 GitHub 稳定资源 ID 识别同一个 repo，并为 fork/clone 风控准备数据 |
| `pullwise_server/app.py` | repository sync 时 upsert workspace/repository/member 关系 | 用户授权 GitHub App 后，把 installation 映射成计费 workspace |
| `pullwise_server/app.py` | scan 创建时从 `consume_review_quota(user)` 改成 `consume_scan_quota(workspace, repository, actor)` | 同 repo / 同 workspace 共享额度，阻断多 GitHub 账号重复白嫖 |
| `pullwise_server/app.py` | scan payload 增加 `workspaceId`、`repoId`、`githubRepoId`、`quota` 摘要 | 前端可以展示真实资源归属与共享额度状态 |
| `pullwise_server/app.py` | `GET /billing/plan` 返回 workspace 视角，同时保留 `account` alias | 前端逐步从 account 口径迁移到 workspace 口径 |
| `pullwise_server/app.py` | `/auth/session` 增加当前 workspace 和 workspace 列表 | 支持 Sidebar workspace、Billing、Repo 页面选择一致的计费主体 |
| `pullwise_server/billing.py` | checkout / webhook metadata 从 `userId` 扩展为 `workspaceId`，保留 `userId` 作为购买发起人 | 订阅真正绑定 workspace，不被个人 GitHub 账号替换绕过 |
| `pullwise_server/worker.py` | 保持 scan 执行逻辑，透传 scan 上的 workspace/repo metadata 到日志 | worker 不负责扣额度，但日志和故障定位要能按 workspace/repo 聚合 |
| `pullwise_server/checkout.py` | 使用 scan 中的 installation/repo metadata，保持 GitHub token 获取逻辑 | 确保 workspace 化后 clone 仍使用正确 installation token |
| `pullwise_server/scan_logging.py` | 日志事件增加 `workspaceId`、`repoId`、`githubRepoId`、`quotaBucketId` | 审计多账号、多 repo、并发 scan |
| `tests/*` | 增加 workspace quota、repo quota、并发扣减、迁移、billing webhook、API contract 测试 | 锁定新模型，防止回退到 user quota |
| `README.md` / `.env.example` | 更新订阅与额度语义、环境变量、迁移步骤 | 对部署与运营口径保持一致 |

建议新增模块：

- `pullwise_server/quota.py`
  - `current_period()`
  - `ensure_quota_bucket()`
  - `quota_entitlement_for_workspace()`
  - `quota_entitlement_for_repo()`
  - `consume_scan_quota()`
  - `quota_payload_for_workspace()`
  - `quota_payload_for_repository()`
- `pullwise_server/workspaces.py`
  - `workspace_for_installation()`
  - `upsert_workspace_from_installation()`
  - `upsert_workspace_repository()`
  - `workspace_membership_for_user()`

如果为了保持初期 diff 小，也可以先把这些函数放在 `app.py`，但长期建议拆模块，因为 `app.py` 已经承担过多职责。

### pullwise-web

| 文件 | 修改内容 | 功能目的 |
| --- | --- | --- |
| `src/api/pullwise.js` | 支持 billing/session/repositories/scans 的 workspace/repo quota 字段；错误对象保留 `code` | 前端不再靠错误字符串判断 quota 问题 |
| `src/lib/pullwise-data.js` | `normalizeRepo()` 保留 `repoId`、`githubRepoId`、`workspaceId`、`quota`；scan normalize 保留 quota 摘要 | Repo 列表、scan 页面可以展示共享额度 |
| `src/lib/pullwise-data.js` | `scanCreatePayload()` 优先提交 `repoId`，保留 `repo` fallback | 避免只用 full name 发起 scan；服务端仍做最终校验 |
| `src/screens/flow.jsx` | Repo 列表展示 workspace/repo 剩余额度与 quota exceeded 状态 | 用户在 scan 前知道免费额度按仓库/工作区共享 |
| `src/screens/flow.jsx` | `scanErrorAction()` 改为基于错误 `code`，兼容旧文案 | quota exceeded 时稳定跳 Billing，不依赖英文字符串 |
| `src/screens/billing.jsx` | `account` 改为 `workspace` 口径展示，文案改为 Workspace usage / Shared repository quota | 消除“换账号就有新额度”的产品暗示 |
| `src/screens/billing.jsx` | plan card 说明 “quota is shared by workspace/repository” | 订阅页面明确免费额度规则 |
| `src/screens/issues.jsx` | SettingsScreen 展示当前 workspace、GitHub installation、成员/授权关系 | 帮用户理解订阅归属，不把 GitHub 登录账号当付费主体 |
| `src/components/github-installations.jsx` | 安装列表展示绑定 workspace 信息 | 多 installation 时减少混淆 |
| `src/shell.jsx` | Sidebar Workspace 区域显示真实 workspace，后续支持切换 | 将现有静态 Workspace 区域接到服务端数据 |
| `src/screens/dashboard.jsx` | KPI 可增加 workspace usage / authorized repos by workspace | 首页体现 workspace 化 |
| `src/screens/*.test.jsx` | 更新 billing、repo、scan error、settings 测试 | 防止前端回到 account quota 文案 |
| `README.md` / `docs/plans/*` | 更新产品订阅口径 | 保持开发文档一致 |

## API 合同建议

### `GET /auth/session`

新增：

```json
{
  "workspaces": [
    {
      "id": "ws_123",
      "name": "acme",
      "githubOwnerLogin": "acme",
      "githubOwnerType": "Organization",
      "githubAppInstallationId": "999",
      "role": "admin"
    }
  ],
  "currentWorkspace": {
    "id": "ws_123",
    "name": "acme"
  }
}
```

目的：

- 前端统一知道当前计费/额度主体。
- Settings/Billing/Sidebar 不再自己猜 workspace。

### `GET /repositories`

新增或规范化：

```json
{
  "items": [
    {
      "id": "repo_internal_1",
      "repoId": "repo_internal_1",
      "githubRepoId": "123456789",
      "githubNodeId": "R_kgDO...",
      "workspaceId": "ws_123",
      "fullName": "acme/api",
      "installationId": "999",
      "quota": {
        "period": "2026-05",
        "used": 2,
        "limit": 3,
        "remaining": 1,
        "scope": "repository"
      }
    }
  ]
}
```

目的：

- 前端显示“这个 repo 还剩几次免费 scan”。
- scan 创建优先提交 `repoId`。

### `POST /scans`

推荐请求：

```json
{
  "repoId": "repo_internal_1",
  "repo": "acme/api",
  "branch": "main",
  "commit": "pending",
  "requestId": "scan_req_..."
}
```

服务端规则：

- `repoId` 优先，`repo` 只作兼容 fallback。
- 服务端根据 session user 的 workspace membership 与 GitHub repo 权限查找 workspace/repo。
- 如果 repo 缺失 `github_repo_id`，要求重新 sync GitHub repositories 后再 scan。
- 幂等检查先于扣额度。
- 新 scan 创建时原子扣减 workspace bucket 和 repo bucket。

成功响应新增：

```json
{
  "id": "sc_123",
  "workspaceId": "ws_123",
  "repoId": "repo_internal_1",
  "githubRepoId": "123456789",
  "billingUsage": {
    "scope": "workspace",
    "period": "2026-05",
    "used": 4,
    "limit": 10,
    "remaining": 6
  },
  "repoUsage": {
    "scope": "repository",
    "period": "2026-05",
    "used": 2,
    "limit": 3,
    "remaining": 1
  }
}
```

错误响应建议：

```json
{
  "message": "This repository has used its free scan quota for the current workspace.",
  "code": "QUOTA_EXCEEDED_REPOSITORY",
  "workspaceId": "ws_123",
  "repoId": "repo_internal_1"
}
```

错误码：

- `QUOTA_EXCEEDED_WORKSPACE`
- `QUOTA_EXCEEDED_REPOSITORY`
- `REPOSITORY_SYNC_REQUIRED`
- `REPOSITORY_NOT_AUTHORIZED`
- `WORKSPACE_MEMBERSHIP_REQUIRED`

### `GET /billing/plan`

推荐响应：

```json
{
  "provider": "creem",
  "enabled": true,
  "plans": [],
  "workspace": {
    "id": "ws_123",
    "name": "acme",
    "status": "active",
    "plan": "pro",
    "interval": "month",
    "reviewLimit": 100,
    "usage": {
      "period": "2026-05",
      "used": 42,
      "limit": 100,
      "remaining": 58,
      "scope": "workspace"
    }
  },
  "account": {
    "deprecated": true
  }
}
```

迁移期保留 `account`，但前端优先读取 `workspace`。

## 实施计划

### 阶段 1：后端资源模型与 repo 稳定 ID

目标：

- 建立 workspace/repo/installation 的后端资源模型。
- scan 不再只能根据 full name 定位 repo。

任务：

1. 在 `db.py` 增加 normalized tables 和初始化 SQL。
2. 增加 workspace/repository upsert 查询函数。
3. 在 `github_auth.py` 保留 GitHub repo `id`、`node_id`、owner、fork、parent/source 信息。
4. 在 GitHub App 授权和 `/repositories/sync` 流程中创建或更新 workspace、repository、workspace_repository。
5. 给 `/repositories` payload 添加 `workspaceId`、`repoId`、`githubRepoId`。

验收：

- 同一个 GitHub repo 通过不同 GitHub 用户同步时，落到同一个 `repositories.github_repo_id`。
- 同一个 GitHub App installation 只创建一个 workspace。
- `/repositories` 响应中每个 repo 都带稳定 `githubRepoId`。

### 阶段 2：Quota service 与 scan 原子扣减

目标：

- 免费额度从 user 维度迁移到 workspace/repo 维度。
- 并发 scan 不会突破额度。

任务：

1. 新增 `quota.py` 或等价服务层。
2. 实现 workspace bucket 和 repository bucket 的创建/读取/扣减。
3. scan 创建时先做幂等检查，再做权限校验，再做 quota 原子扣减。
4. scan 记录增加 `workspaceId`、`repoId`、`githubRepoId`、`quotaBucketIds`、`billingUsage`、`repoUsage`。
5. quota 拒绝时返回结构化错误码。
6. 旧 `consume_review_quota(user)` 标记为兼容或删除。

验收：

- User A 扫 `github_repo_id=123` 用完 repo 免费额度后，User B 即使有该 repo 高权限也无法再次获得免费额度。
- 两个并发请求在剩余 1 次 quota 时最多只有 1 个成功。
- 相同 `requestId` 重试不重复扣额度。

### 阶段 3：Billing 迁移到 workspace

目标：

- 订阅主体从 user account 改为 workspace。

任务：

1. `billing.py` checkout metadata 增加 `workspaceId`，保留 `userId` 作为 actor。
2. Creem webhook 优先按 `workspaceId` 应用订阅状态。
3. `billing_user_for_update()` 迁移为 `billing_workspace_for_update()`，保留 customer/subscription fallback。
4. `GET /billing/plan` 返回 `workspace` 口径。
5. change interval/cancel 使用 workspace billing customer/subscription；不暴露 Creem customer portal，避免用户绕过 Pullwise 的订阅变更限制。

验收：

- 同一 workspace 的不同用户看到相同订阅状态。
- 换 GitHub 登录账号不会获得新的 Pro/free workspace allowance。
- Webhook 重放仍幂等。

### 阶段 4：前端 workspace / quota 语义迁移

目标：

- UI 明确展示 workspace/repo 共享额度。
- scan error 不靠英文文案判断。

任务：

1. `pullwiseApi` 和 `http` 层保留错误 `code`。
2. `normalizeRepo()`、`normalizeScan()` 增加 workspace/repo/quota 字段。
3. `ReposScreen` 展示 repo 剩余额度和 workspace 来源；quota 用完时仍允许点击尝试，但预提示升级，最终以后端为准。
4. `ScanningScreen` 根据错误 `code` 跳 Billing / Repos。
5. `BillingScreen` 改为 Workspace usage，plan card 文案改为共享额度。
6. `SettingsScreen` 展示当前 workspace 与 GitHub App installation 绑定。
7. `Sidebar` Workspace 区域显示真实 workspace name。

验收：

- Billing 页面不再显示容易误导的 account quota。
- Repo 页面能看出额度与 repo/workspace 绑定。
- quota exceeded 错误稳定跳转 Billing。

### 阶段 5：初始数据整理

目标：

- 已有用户、订阅、scan 历史不丢失。

迁移策略：

1. 为每个已有 user 创建 personal workspace。
2. 如果用户只有一个 GitHub App installation，把 user 的 billing 状态迁移到该 installation workspace。
3. 如果用户有多个 installations：
   - 保守策略：先迁移到 personal workspace。
   - UI 提示用户选择要绑定订阅的 workspace。
   - 选择后迁移 billing customer/subscription 到目标 workspace。
4. `user.billingUsage` 迁移为 workspace quota bucket。
5. 旧 scan 根据 `installationId`、`repo` 和 `repositoryItems.id` 回填 `workspaceId`、`repoId`、`githubRepoId`；无法回填的历史 scan 保持只读。
6. 迁移期保留 `account` response alias，前端优先读 `workspace`。

验收：

- 老用户登录后能看到原订阅。
- 老 scan 历史仍可查看。
- 无法确定 workspace 的历史数据不会误扣新 quota。

### 阶段 6：Fork / clone 风控

目标：

- 处理用户 fork 或复制同一代码到新 repo 后重新拿免费额度的问题。

第一版规则：

- GitHub API 返回 `fork=true` 且 `source_github_repo_id` 已用过免费额度时，不重新赠送完整 repo 免费额度。
- 同一 workspace 下多个 repo 的 manifest/lockfile/source fingerprint 高相似时，限制为 1 次 trial scan 或进入 review。

任务：

1. 保存 fork parent/source repo ID。
2. scan checkout 后生成 lockfile/manifest/source fingerprint。
3. 增加 risk decision：
   - `allow`
   - `allow_limited_trial`
   - `deny_trial_reuse`
   - `manual_review`
4. 日志记录 risk reason。

验收：

- fork 同一个 source repo 不会获得完整新免费额度。
- 普通不同 repo 不受误伤。

## 测试覆盖面

### 后端单元/集成测试

新增或更新：

- `tests/test_quota_contracts.py`
  - workspace quota bucket 创建与周期重置。
  - repo quota bucket 创建与周期重置。
  - quota used 非法值 sanitize。
  - 原子扣减成功/失败。
- `tests/test_scan_quota_routes.py`
  - 同一个 `github_repo_id`，不同 user，共享 repo quota。
  - 同一个 workspace，不同 user，共享 workspace quota。
  - repo quota 用完返回 `QUOTA_EXCEEDED_REPOSITORY`。
  - workspace quota 用完返回 `QUOTA_EXCEEDED_WORKSPACE`。
  - 相同 `requestId` 不重复扣。
  - 剩余 1 次额度下并发双请求最多 1 个成功。
- `tests/test_workspace_billing_routes.py`
  - `GET /billing/plan` 返回 workspace usage。
  - checkout metadata 包含 `workspaceId`。
  - webhook 按 workspace 更新订阅。
  - 旧 user billing fallback 仍兼容。
- `tests/test_github_auth_contracts.py`
  - repo payload 保留 `id`、`node_id`、owner、fork parent/source。
  - malformed repo metadata 不污染 stable IDs。
- `tests/test_workspace_isolation.py`
  - 非 workspace member 不能查看/扫描 workspace repo。
  - 前端传错 workspaceId 时服务端仍按真实授权关系判定。
- `tests/test_migration.py`
  - user billingUsage 迁移到 workspace bucket。
  - 单 installation 自动迁移订阅。
  - 多 installation 进入 pending binding。

保留并更新：

- `tests/test_billing_routes.py`
- `tests/test_billing_contracts.py`
- `tests/test_billing_webhooks.py`
- `tests/test_security_contracts.py`
- `tests/test_db_contracts.py`

后端验证命令：

```powershell
python -m unittest discover -s tests
```

### 前端测试

新增或更新：

- `src/screens/billing.test.jsx`
  - 展示 Workspace usage。
  - plan 文案说明 quota shared by workspace/repository。
  - malformed workspace usage 不显示 NaN。
- `src/screens/flow.test.jsx`
  - repo row 展示 quota remaining。
  - `QUOTA_EXCEEDED_WORKSPACE` 和 `QUOTA_EXCEEDED_REPOSITORY` 都跳 Billing。
  - 兼容旧 `monthly review limit` 文案。
  - scan 创建优先传 `repoId`。
- `src/lib/pullwise-data.test.js`
  - normalizeRepo 保留 `repoId`、`githubRepoId`、`workspaceId`、`quota`。
  - normalizeScan 保留 workspace/repo quota fields。
- `src/screens/settings.test.jsx`
  - Settings 展示 workspace 与 installation 绑定。
- `src/App.test.jsx`
  - session workspace 初始化后 Sidebar/Billing 使用同一 workspace。

前端验证命令：

```powershell
npm run check
```

必要时拆开：

```powershell
npm run lint
npm run test
npm run build
```

## 关键验收场景

1. **多账号同 repo 免费额度共享**
   - User A 有 `acme/api` admin 权限并扫描 3 次。
   - User B 另一个 GitHub 账号也有 `acme/api` admin 权限。
   - User B 再扫同一个 `github_repo_id` 时不能重新获得 repo 免费额度。

2. **同 workspace 多成员共享订阅**
   - Org `acme` 安装 Pullwise。
   - User A 升级 Pro。
   - User B 是该 org/repo 有权限成员。
   - User B 登录后看到 workspace Pro 状态，scan 使用同一 workspace allowance。

3. **换账号不重置 workspace quota**
   - 同一 GitHub App installation 下，不同 GitHub OAuth user 登录。
   - `/billing/plan` 返回相同 workspace usage。

4. **并发不能超扣**
   - workspace 剩余 1 次。
   - 两个请求同时创建 scan。
   - 只有一个 scan 创建成功，另一个返回 quota exceeded。

5. **幂等不重复扣**
   - 同一个 user、repo、requestId 重试。
   - 返回同一个 scan 或等价已有 scan，不新增 quota ledger。

6. **前端文案不误导**
   - Billing 页面不出现 “Current account plan” 这种暗示账号维度的核心文案。
   - 显示 “Workspace usage” / “quota shared by repository and workspace”。

7. **旧数据安全迁移**
   - 已有用户登录后仍能看历史 scans/issues。
   - 无法映射 workspace 的旧 scan 不参与新 quota 决策。

## 建议实施顺序

1. 后端先加数据模型和 quota tests，但不切 scan。
2. GitHub sync 写入 workspace/repository 表。
3. scan 创建链路切换到 `consume_scan_quota()`。
4. billing checkout/webhook 迁到 workspace。
5. 前端读取 workspace billing 和 repo quota。
6. 迁移脚本与兼容 alias 清理。
7. fork/clone 风控增强。

这个顺序的原因：

- scan quota 是真实防滥用核心，必须先在服务端闭环。
- 前端展示只能作为解释，不能作为 enforcement。
- billing 迁移要等 workspace 主体稳定后再做，避免订阅挂错主体。

## 不建议采用的方案

### 不建议：按 GitHub user 限制

原因：

- 用户可以创建多个 GitHub 账号。
- 同一个 repo 只要多个账号都有权限，就能重复拿额度。

### 不建议：只按 email / IP / device fingerprint 限制

原因：

- 误伤高。
- 容易绕过。
- 不能准确表达产品价值主体。

这些可以作为风控辅助，但不能作为主额度模型。

### 不建议：只按 repo full name 限制

原因：

- repo rename 后 full name 会变。
- transfer owner 后 full name 会变。
- GitHub numeric repo ID / node ID 更稳定。

## 主要风险与处理

| 风险 | 影响 | 处理 |
| --- | --- | --- |
| 旧订阅不知道该绑定哪个 workspace | 多 installation 用户可能迁移不准确 | 单 installation 自动迁移，多 installation 进入 pending binding |
| GitHub repo metadata 缺少 stable ID | 不能可靠共享 quota | 要求 repositories sync，缺 ID 时禁止 scan 并返回 `REPOSITORY_SYNC_REQUIRED` |
| 并发 scan 超额 | 免费额度被突破 | quota 独立表 + 原子 UPDATE + ledger |
| 前端传错 workspace/repo | 越权或错扣额度 | 服务端只信 session + GitHub App 授权 + DB 关系 |
| fork/clone 误伤 | 合法 fork 被限制 | 第一阶段只处理 GitHub fork network；内容指纹先用于 risk flag，不直接硬拒 |
| API 兼容破坏 | 老前端或历史数据出错 | `account` alias 和 `repo` fallback 保留一个迁移周期 |

## 最终交付定义

这次改造完成时，应满足：

- `user.billingUsage` 不再是新 scan 的额度来源。
- 新 scan 必须带 `workspaceId`、`repoId`、`githubRepoId`。
- 同一个 GitHub repo 的免费额度跨 GitHub 登录账号共享。
- workspace 订阅跨 workspace 成员共享。
- Billing 页面使用 workspace 口径。
- Repo/Scan 页面能解释 quota exceeded 的真实原因。
- 后端测试覆盖 quota、billing、migration、authorization、concurrency。
- 前端测试覆盖 workspace billing、repo quota、scan error routing。

