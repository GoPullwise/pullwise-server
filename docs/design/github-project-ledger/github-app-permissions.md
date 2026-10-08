# 当前版本的 GitHub App 权限

2026-10-06。当前 Organization 关联、多仓库项目和账本邀请不需要新增
Organization 或 Account 权限。以下是代码实际调用所需的最小配置。

| GitHub App 设置区域 | 权限 | 当前需要 |
| --- | --- | --- |
| Repository permissions | Metadata | Read-only，GitHub App 默认必需 |
| Organization permissions | Members 及其他组织权限 | No access |
| Account permissions | Email addresses 及其他账户权限 | No access |
| Enterprise permissions | 全部 | No access |
| Repository permissions | Contents、Pull requests、Issues、Actions、Administration 等 | No access |

当前 Server 仅使用 user access token 调用 `GET /user`、
`GET /user/installations`、`GET /user/installations/{id}/repositories`，
以及邀请时的公开 `GET /users/{login}`。当前用户、安装发现和公开用户
查询无额外 fine-grained permission 要求；仓库列表要求 Metadata read。
Organization ID/login/type 来自当前授权安装的 account metadata，未调用
组织成员、团队、仓库内容、PR、Issues 或 Actions 接口。

如未来增加 `GET /orgs/{org}/members` 或组织团队目录功能，再增加
Organization permissions → Members → Read-only。如未来读取私有邮箱，再
增加 Account permissions → Email addresses → Read-only 并接入相应接口。
这些功能当前未实现，不是本版安装要求。GitHub App user token 使用 App 的
fine-grained permissions；旧授权 URL 的 OAuth scope 字符串不会额外赋权。

如需关联目标 Organization 的仓库，还需在该 Organization 安装 App，
并选择需要使用的仓库。独立项目不需要仓库安装或 Organization 关联。
Only select repositories 可以限制安装范围；All repositories 由组织策略决定。
Organization Owner 可安装或批准请求，也可限制仓库管理员的安装权限。
如 App 要安装到注册账户之外的账户或组织，注册设置
Where can this GitHub App be installed? 需允许 Any account。
这是安装范围设置，不会公开仓库数据或改变 Pullwise 的账本角色。

用户授权 App、App 安装到组织以及安装选中的仓库分别校验；可见仓库是
调用成员现有 GitHub access 与 App 安装范围的交集。Organization 的 GitHub
成员身份不会自动授予 Pullwise Owner/Admin/Editor/Viewer。账本角色由
Pullwise 显式邀请和成员权限管理决定。

## 正式 App 的现有设置与修改目标

2026-10-06 09:25 UTC 的公开 GitHub App 元数据读取确认：正式 App 为
`GoPullwise`，slug `gopullwise`，App ID `3631508`，注册账户为
Organization `GoPullwise`。公开注册权限为 Contents write、Pull requests
write、Email addresses read 和 Metadata read，events 为空。

按当前实现，修改目标为 Contents → No access、Pull requests → No access、
Email addresses → No access，保留 Metadata → Read-only。Organization
permissions 无需增加。公开元数据不包含安装范围设置，未将 Any account
视作已核实的现有值。

设置入口：[正式 GoPullwise App 权限](https://github.com/organizations/GoPullwise/settings/apps/gopullwise/permissions)。
此入口需要有注册 App 管理权限的 GitHub 网页登录会话。当前连接提供仓库
读写接口，不提供 App 注册设置编辑接口或已登录的 GitHub 浏览器会话；
本次只读核对尚未保存上述 App 设置修改，也不表示已发送安装审批请求。
GitHub 官方说明撤除权限立即生效，不需要安装方批准；新增权限才需要
安装方接受。权限页面修改后点击 Save changes 保存。

该设置核对属于 GitHub App 管理操作，独立于 Preview 发布验收。生产 D1
仍暂停，未因用户授权修改 App 设置而启用。

官方依据：

- [仓库列表及 Metadata read](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-user-access-token)
- [用户可访问的 App 安装](https://docs.github.com/en/rest/apps/installations#list-app-installations-accessible-to-the-user-access-token)
- [当前用户](https://docs.github.com/en/rest/users/users#get-the-authenticated-user)、[公开用户查询](https://docs.github.com/en/rest/users/users#get-a-user)
- [GitHub App user access token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
- [安装 App 到组织](https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party)
- [请求 Organization Owner 安装](https://docs.github.com/en/apps/using-github-apps/requesting-a-github-app-from-your-organization-owner)
- [组织成员接口](https://docs.github.com/en/rest/orgs/members)
- [修改 App 注册权限](https://docs.github.com/en/apps/maintaining-github-apps/modifying-a-github-app-registration#changing-the-permissions-of-a-github-app)
- [App 管理者资格](https://docs.github.com/en/apps/maintaining-github-apps/about-github-app-managers)
