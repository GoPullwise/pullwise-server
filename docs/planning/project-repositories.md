# 当前版本：多人账本、项目多仓库与 Organization

状态（2026-10-06）：Server/Web 已完成本地实现，正在进行最终发布验证。
新版本尚未远程部署或迁移；本地完整检查、原生 D1 升级证明及 Preview
发布验收仍待记录。原版本验收见两端 `docs/validation/local-acceptance.md`，
不能用原版本的单人 Preview 证据代替本版团队权限和迁移验收。

历史（2026-10-06 初始需求）：本文件最初在当前 Preview 修复和原版本
验收结束前记录下一版本计划，当时多仓库、Organization 与多人账本尚未
实现。下文是随后完成的实现契约和当前发布边界。

## 账本身份与共享范围

现有账本的 `owner_id` 同时作为稳定的 workspace ID。原个人账本有一个
隐式 Owner，不需要迁移支出归属或新增 Owner 成员行。成员接受邀请后访问
同一个账本；账本已有及未来的项目、分类、支出、报表和 CSV 都按角色共享。
邀请创建和接受页面明确警告这一共享范围，不复制原账目，也不另建一份
团队历史。成员自己的个人账本仍然独立。

邀请通过 GitHub username 创建，服务端立即解析并固定接收人的稳定 numeric
GitHub user ID。随机邀请 token 只返回一次，数据库只保存 hash；有效期为
24 小时，只有该 GitHub 身份登录的用户可以预览和单次接受。接受前重新
校验邀请状态、有效期、邀请者当前权限及 revision。撤销、过期、重复接受、
接收人不符或邀请者失权均拒绝。GitHub Organization 成员资格不会自动
授予账本角色；接受账本邀请也不授予 GitHub 仓库权限。

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

每个项目显式关联 1–30 个不同的已授权 GitHub repository ID，可填写
可选的项目 `name`、`description` 和 `githubOrganizationId`。稳定 numeric
repository/Organization ID 是外部身份，名称只用于展示。仓库可以来自
调用者有权限的个人或 Organization 安装；Organization 关联是显式元数据，
不自动选择全部仓库或纳入以后新增的仓库。

创建和关联变更逐仓库使用实际操作成员的 GitHub App user token 验证权限，
不能借用 Owner 凭证。组织选择也必须存在于该成员当前可访问的组织列表。
关联变更保持 project ID，并与项目 `If-Match` revision 和账本权限复核
一起原子提交。旧 `githubRepoId` 单仓库创建入参继续兼容，与新
`githubRepoIds` 不能同时提交。

同一账本内，一个仓库最多关联一个项目，包括已归档项目；不同账本的
关联互不影响。`GET /repositories` 返回 `isBound`，Web 创建选择器排除
已被绑定的候选仓库，不能把绑定仓库误报成没有 GitHub 权限。更改项目
关联后，原支出仍属于同一个项目。报表直接按 expense/project 汇总，不能
连接仓库关联表后重复累计金额。

成员失去部分仓库权限时，仅显示该成员仍有权限的 GitHub 详情；失权或
未知仓库的受保护名称、installation/account/Organization 元数据被隐藏。
按角色查看、修改、移除和导出既有财务记录仍可使用。新增项目支出或将
支出移入新项目目标，需要该项目未归档且至少一个当前关联仓库仍获该成员
授权；GitHub 未知结果不能当作授权通过。所有关联都失权时不会抹去历史
金额，也不会把项目变成空账目。

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
现有记录变更要求 `If-Match`；预览和接受 body 为 `{"token":"pwi_…"}`。

新建 workspace-scoped API key 绑定 workspace ID 和当前
`workspaceMemberRevision`。有效权限为 key scopes、当前成员角色、项目
范围和 Shared Pool 许可的交集。项目 allowlist 不授予 Shared Pool 权限；
workspace header 不能覆盖 key 的账本绑定。原无 workspace 的 key 默认
仍绑定个人账本。角色变更、移除或重新加入使旧团队 key 失效，需要重新
创建 key；移除成员后仍可用个人登录撤销自己的失效 key。

Web 提供账本切换、成员和邀请页面、组织筛选、仓库多选及项目关联设置。
切换 workspace、membership revision 或权限时清除受保护数据、草稿和
一次性凭证，取消旧请求并忽略迟到结果；Server 每次请求仍独立核对角色。

## 迁移与当前发布状态

新增 `0005_workspaces_repositories.sql`，不修改已发布的 0001–0004。
0005 新增四张表：`workspace_members`、`workspace_invites`、
`workspace_events`、`ledger_project_repositories`；在 `ledger_projects`
追加 `name` 和 `github_organization_id` 两列。原单仓库映射回填为独立
关联，保留 owner ID、project ID、expense/category/event/idempotency
记录及原 API-key 项目范围。迁移不重写任何支出或财务历史。

新 canonical schema 为五个 migrations、18 张表和 33 个 SQLite indexes。
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
远程 Preview 升级尚待最终原生运行门槛，不能宣称迁移或发布成功。Server
全套 658 项及 Web 全套 418 项检查通过；最后手机布局复验和
Preview 升级/验收证据由两端验证记录补充；生产保持
`PULLWISE_D1_ACCESS_ENABLED=0`，本轮远程操作仅限 Preview。
