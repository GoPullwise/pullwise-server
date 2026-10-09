# 当前版本：多人账本、独立项目与可选 GitHub 关联

2026-10-08 补充：开发／产品链接与周期支出已在当前源码实现，采用 v7 schema。
频率、时区、权限、执行与本地原生验收见[新增功能契约](recurring-expenses-project-links.md)；
实际 Preview 发布单独记录在当前验收文档，不以此前 v6 发布替代。

状态（2026-10-07）：Server/Web 已实现、验证、推送 main 并发布 Preview。
原数据库在同一累计 journal 下完成 v4→v5 和 v5→v6 升级，独立项目使用
真实 nullable GitHub anchor，保留账本身份和财务历史。当前验收见
[Server 验收](../validation/local-acceptance.md) 与
[Web 验收](../../../pullwise-web/docs/validation/local-acceptance.md)。
2026-10-06 的多人角色通过 35 次真实本地 Worker HTTP 与模拟账号浏览器验证；线上访客
入口与实际升级/发布另有独立证据，不宣称双真实账号邀请验收。
见 `docs/validation/workspaces-preview-release-2026-10-06.json`。

历史（2026-10-06 初始需求）：本文件最初在当前 Preview 修复和原版本
验收结束前记录下一版本计划，当时多仓库、Organization 与多人账本尚未
实现。下文是随后完成的实现契约和当前发布边界。

## 账本身份与共享范围

现有账本的 `owner_id` 同时作为稳定的 workspace ID。原个人账本有一个
隐式 Owner，不需要迁移支出归属或新增 Owner 成员行。成员申请获批后访问
同一个账本；账本已有及未来的项目、分类、支出、报表和 CSV 都按角色共享。
邀请创建和接受页面明确警告这一共享范围，不复制原账目，也不另建一份
团队历史。成员自己的个人账本仍然独立。

邀请只选择角色，不预先指定 GitHub 用户。随机邀请 token 只返回一次，
数据库只保存 hash；有效期为 24 小时。打开链接后先恢复登录或登录并
回到邀请页，申请使用真实登录账号的身份。申请不会创建成员或授予账本
权限；原邀请人在持久申请列表中看到申请人的姓名和 GitHub 账号，选择
同意或拒绝。只有原邀请人当前权限及原始成员 revision 仍有效时才能审批。
同意会在一个原子 batch 中创建或恢复成员并关闭链接；其他待申请者无法
再通过这条链接加入。拒绝不关闭链接，同一账号重复申请不覆盖拒绝结果。
同一链接最多接收 100 个不同账号的申请。通知在登录、导航、返回页面和
手动刷新时更新，没有定时轮询。撤销、过期、旧版本或邀请者失权均拒绝
审批。历史指定账号的邀请继续保留接收人约束，已加入的成员保持原权限。
GitHub Organization 成员资格不会自动授予账本角色；加入账本也不授予
GitHub 仓库权限。

## 当前角色与权限

| 能力 | Owner | Admin | Editor | Viewer |
| --- | --- | --- | --- | --- |
| 查看项目、支出、报表和 CSV | 是 | 是 | 是 | 是 |
| 新增、修改、移除账目 | 是 | 是 | 是 | 否 |
| 管理项目、分类和仓库关联 | 是 | 是 | 否 | 否 |
| 邀请、调整、移除 Editor/Viewer | 是 | 是 | 否 | 否 |
| 邀请、授予、撤销或移除 Admin | 是 | 否 | 否 | 否 |
| 账本所有权与该账本订阅付款 | 是 | 否 | 否 | 否 |

Owner 身份由原账本所有者确定，不可通过成员 API 更换或移除。Admin
只能管理 Editor/Viewer，不能管理另一个 Admin 或提升自己。成员角色和
移除操作使用 `If-Match` revision，并在账本写入的同一原子 batch 中复核
成员和凭证。每次角色变化、移除和重新加入都会推进 membership revision。
后续请求不能继续使用旧权限；expense audit 与 workspace event 记录真实
操作成员，不能将成员动作记作 Owner。

账本成员共同使用 Owner 的项目/记录配额、写入额度、套餐权益和模型预算，
更换成员不能扩大额度。Owner 的平台订阅支持该账本；成员自己的个人
订阅和账本分开。一般成员、邀请和仓库关联变更计入 Owner 商业写入额度。
移除成员、撤销邀请及撤销自己的 API key 属于紧急撤权，商业额度耗尽时
仍可执行；当前身份、版本和全局 D1 验证预算检查仍必须通过。

## 多仓库与 Organization

项目可以独立于 GitHub 创建：填写非空项目名称，可选描述，不关联任何
仓库或 Organization。独立项目的兼容 GitHub ID/名称字段为真实 NULL，
仓库数组为空，`githubAccess=not_linked`；未归档时按账本角色、Key 范围
和 Owner 套餐正常新增或移入支出、查看报表及导出，不查询 GitHub。
GitHub 登录身份与仓库安装授权是不同要求；本次不取消账户登录。

可选关联 1–30 个不同的已授权 GitHub repository ID；关联时项目 `name`
保持兼容可选，`description` 和 `githubOrganizationId` 仍可选。组织关联
必须伴随非空仓库集合，不能只关联组织。稳定 numeric
repository/Organization ID 是外部身份，名称只用于展示。仓库可以来自
调用者有权限的个人或 Organization 安装；Organization 关联是显式元数据，
不自动选择全部仓库或纳入以后新增的仓库。

创建和关联变更逐仓库使用实际操作成员的 GitHub App user token 验证权限，
不能借用 Owner 凭证。组织选择也必须存在于该成员当前可访问的组织列表。
关联变更保持 project ID，并与项目 `If-Match` revision 和账本权限复核
一起原子提交。旧 `githubRepoId` 单仓库创建入参继续兼容，与新
`githubRepoIds` 不能同时提交。

创建时省略仓库字段或传 `githubRepoIds: []` 表示独立项目，名称不能只
含空白。修改时省略仓库字段保留原关联；明确传空数组则解除全部仓库并
清除组织关联，最终名称必须非空，可在同一 PATCH 提供名称。后续可重新
关联仓库，项目、支出、revision 和历史金额不会被复制或另建项目替代。

同一账本内，一个仓库最多关联一个项目，包括已归档项目；不同账本的
关联互不影响。`GET /repositories` 返回 `isBound`，Web 创建选择器排除
已被绑定的候选仓库，不能把绑定仓库误报成没有 GitHub 权限。更改项目
关联后，原支出仍属于同一个项目。报表直接按 expense/project 汇总，不能
连接仓库关联表后重复累计金额。

成员失去部分仓库权限时，仅显示该成员仍有权限的 GitHub 详情；失权或
未知仓库的受保护名称、installation/account/Organization 元数据被隐藏。
按角色查看、修改、移除和导出既有财务记录仍可使用。对仓库关联项目，新增支出或将
支出移入新项目目标，需要该项目未归档且至少一个当前关联仓库仍获该成员
授权；GitHub 未知结果不能当作授权通过。所有关联都失权时不会抹去历史
金额，也不会把项目变成空账目。
失权、未知结果或空的可见授权列表不能自动把仓库关联项目变成独立项目；
独立项目写入的原子目标检查同时要求 active、项目 revision、NULL anchor
和没有任何绑定，防止并发关联变化绕过 GitHub 授权。

## API 与自动化边界

业务契约以 `openapi/ledger-v1.yaml` 为准。Cookie 客户端通过
`X-Pullwise-Workspace` 选择账本；未选择时使用个人账本。浏览器原生 CSV
下载使用 `workspaceId` query parameter，冲突的 header/query 选择被拒绝。
`GET /api/v1/me` 返回实际登录身份、当前 workspace/role/revision、有效
scopes 和所选账本 Owner 的权益。

成员与邀请管理仅允许受保护的 Cookie Session，Cookie 写请求仍要求可信
Origin。入口包括 `GET /api/v1/workspaces`，
`GET /api/v1/workspaces/{workspaceId}/members`，成员 `PATCH/DELETE`，
邀请 `GET/POST /api/v1/workspaces/{workspaceId}/invites` 与
`DELETE /api/v1/workspaces/{workspaceId}/invites/{inviteId}`，以及
`POST /api/v1/workspace-invitations/preview` 和 `/accept`。成员及邀请
创建 body 为 `{"role":"viewer"}`。预览与申请 body 为 `{"token":"pwi_…"}`，
首次申请返回 202 和 pending request。全局申请列表为
`GET /api/v1/workspace-invitation-requests`，当前账本申请列表为
`GET /api/v1/workspaces/{workspaceId}/join-requests`，均只返回当前登录人
创建的有效邀请的待审批申请，最多 100 条并提供 `hasMore`。
审批为 `POST /api/v1/workspaces/{workspaceId}/invites/{inviteId}/requests/{requestId}/approve`
或 `/reject`，使用申请的 `If-Match`。成员、撤销邀请及审批的变更均要求
当前 revision，实际权限只在审批同意后获得。

新建 workspace-scoped API key 绑定 workspace ID 和当前
`workspaceMemberRevision`。有效权限为 key scopes、当前成员角色、项目
范围和 Shared Pool 许可的交集。项目 allowlist 不授予 Shared Pool 权限；
workspace header 不能覆盖 key 的账本绑定。原无 workspace 的 key 默认
仍绑定个人账本。角色变更、移除或重新加入使旧团队 key 失效，需要重新
创建 key；移除成员后仍可用个人登录撤销自己的失效 key。

Web 提供账本切换、成员和邀请页面、组织筛选、仓库多选及项目关联设置。
切换 workspace、membership revision 或权限时清除受保护数据、草稿和
一次性凭证，取消旧请求并忽略迟到结果；Server 每次请求仍独立核对角色。

## 迁移与发布证据

当前 canonical schema 为六个 migrations、18 张表和 33 个 SQLite indexes。
`0006_blank_projects.sql` 在同一原子 batch 内允许项目 anchor 为 NULL；
Preview v5→v6 升级已验收，账本身份、关联项目及财务/审计记录保持原样。
生产仍保持 `PULLWISE_D1_ACCESS_ENABLED=0`。以下是保留的
2026-10-06 多人账本 v4→v5 升级证据，不作为当前 schema 版本。

新增 `0005_workspaces_repositories.sql`，不修改已发布的 0001–0004。
0005 新增四张表：`workspace_members`、`workspace_invites`、
`workspace_events`、`ledger_project_repositories`；在 `ledger_projects`
追加 `name` 和 `github_organization_id` 两列。原单仓库映射回填为独立
关联，保留 owner ID、project ID、expense/category/event/idempotency
记录及原 API-key 项目范围。迁移不重写任何支出或财务历史。

当时 canonical schema 为五个 migrations、18 张表和 33 个 SQLite indexes。
已存在的 Preview 保留精确 legacy-v4 schema/fingerprint，使用一次性编译
的原子 0005 升级；不能重新初始化现有数据库。升级在原
ValidationBudget namespace/name、数据库和累计 journal 中预留，继续使用
100,000 Rows Read / 1,000 Rows Written 上限，不重置、不重试、不增加 cron。
按既有 project 数量 `P` 的保守 written bound 为 `128 + 21P`，每次升级
编译的 read bound 必须不超过 10,000，并满足 journal 剩余额度。

本地真实 D1 升级 SQL 与预算测量已通过：四个 batch 共 324 read/25 write，
低于两项目样本的 4,852 read/170 write 预留，历史记录保持原样。
Miniflare 缺少 attempts 字段，其固定源证明无重试；测量保留 null，未伪造
次数。已部署代码的读取阶段仍要求 native attempts=1，精确固定的写入
batch 按官方不可自动重试规则处理缺失次数；首个失败本地 claim 保留不动。
远程 Preview 升级已通过严格读取 attempts 门槛，schemaVersion=5。Server
全套 658 项及 Web 全套 418 项检查通过，最后布局修复的 159 项相关测试、
六个手机/一个桌面构建浏览器场景及实际线上访客入口通过。Preview 升级
和 100% 发布证据见两端验证记录；生产保持
`PULLWISE_D1_ACCESS_ENABLED=0`，本轮远程操作仅限 Preview。

GitHub App 当前最小配置及官方依据见
[权限说明](../design/github-project-ledger/github-app-permissions.md)。
