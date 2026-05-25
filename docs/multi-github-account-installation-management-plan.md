# Pullwise 多 GitHub 账户与 App Installation 管理方案

日期：2026-05-25

## 问题描述

用户在同一个浏览器里登录了多个 GitHub 账户。GitHub 弹窗里会显示一个当前激活账户，例如 `Continue as @active-user`，也会允许选择其它账户。Pullwise 现在支持连接多个 GitHub App installations，也能在 Settings / Repositories 里展示每个 installation 的 Manage 链接。

实际问题是：

- Pullwise 已经保存了多个 installation 的 GitHub `html_url`。
- 用户点击某个 installation 的 Manage。
- 如果浏览器当前 GitHub active account 不是这个 installation 所属账户，或者不是该 org 的 owner/admin，GitHub 直接打开对应 settings URL 时可能返回 404。
- 用户看到的是 GitHub 404，不知道是账户没切对、没有权限，还是 Pullwise 链接坏了。

本质上，GitHub installation 管理页 URL 不是一个“全局可打开链接”，它依赖当前 GitHub Web session 的 active account 和权限。Pullwise 当前把 `installationHtmlUrl` 当成普通外链使用，所以在多 GitHub 账户场景下会不稳定。

## 结论

最佳方案是：**不要直接把 GitHub installation settings URL 暴露为主 Manage 入口。**

Pullwise 应该提供自己的 Manage flow：

```text
用户点击 Manage
-> Pullwise 打开受控 popup
-> GitHub OAuth account picker 强制/优先出现
-> 用户选择正确 GitHub identity
-> Pullwise 后端验证该 identity 能访问目标 installation
-> 验证成功后，再把 popup redirect 到 GitHub installation settings URL
-> popup 关闭或页面 focus 后，Pullwise sync repositories
```

这样即使浏览器当前 active account 不对，用户也会先经过 GitHub 账户选择，而不是直接撞到 404。

## 当前实现现状

### pullwise-server

关键位置：

- `pullwise_server/app.py`
  - `handle_github_repository_authorize()` 支持 `manage=1` 和 `add=1`。
  - 当 `manage=1` 且已有单个 installation 时，会返回：

    ```json
    {
      "connected": true,
      "mode": "github-app-existing-manage",
      "url": "https://github.com/settings/installations/999"
    }
    ```

  - 当有多个 installations 时，会返回：

    ```json
    {
      "mode": "github-app-existing-manage-list",
      "installations": [
        {
          "installationId": "111",
          "installationAccount": "octocat",
          "installationHtmlUrl": "https://github.com/settings/installations/111"
        }
      ]
    }
    ```

  - callback 里已经有安全校验：不能信任 `installation_id`，会通过当前用户 GitHub token 查询 installations 后确认。
- `pullwise_server/github_auth.py`
  - `build_app_install_url(state)` 构造 GitHub App installation URL。
  - `list_current_app_installations_for_user()` 能用当前 GitHub OAuth token 查询该用户可见 installations。
  - `user_can_access_installation()` 能判断当前 OAuth user 是否能访问某个 installation。

### pullwise-web

关键位置：

- `src/lib/auth.js`
  - `connectGitHubRepositories({ manage: true })` 拿到后端返回的 `url` 后直接调用 `openGitHubInstallPopup(result.url)`。
- `src/components/github-installations.jsx`
  - `GitHubInstallationsList` 直接把每个 installation 的 `installationHtmlUrl` 渲染成 `<a href target="_blank">Manage</a>`。
- `src/lib/install-popup.js`
  - popup 关闭后会尝试 `auth.getSession()` / `repositories.sync()` 来刷新 Pullwise 状态。

当前最大缺口：

- Pullwise 没有在打开 GitHub manage URL 前确认“当前 popup 中选择的 GitHub identity 是否能访问目标 installation”。
- 多 installation 列表里的 Manage 是普通外链，完全绕过了 Pullwise 受控 popup flow。
- Pullwise 内部没有把“一个 Pullwise 用户可关联多个 GitHub OAuth identities”建模出来。

## 设计目标

1. 支持一个 Pullwise 用户连接多个 GitHub identities。
2. 支持一个 Pullwise workspace 绑定多个 GitHub App installations。
3. 点击 Manage 某个 installation 时，不再因为浏览器当前 GitHub active account 不匹配而直接 404。
4. 如果用户选错 GitHub 账户，Pullwise 要显示可理解的错误，而不是让用户落到 GitHub 404。
5. 对 organization installation，要区分：
   - 当前 GitHub identity 看不到这个 installation。
   - 当前 GitHub identity 是 org 成员但没有管理权限。
   - installation 已被删除或 suspended。
6. 继续支持 popup 关闭后自动 sync repositories。
7. 不把 GitHub OAuth token 或 installation token 暴露给前端。

## 目标数据模型

这份设计可以与资源维度 quota/workspace 方案共存。建议在后端显式建模 GitHub identity。

### GitHubIdentity

一个 Pullwise 用户可以绑定多个 GitHub OAuth 身份。

字段：

- `id`
- `user_id`
- `github_user_id`
- `github_login`
- `github_html_url`
- `avatar_url`
- `access_token`
- `oauth_scope`
- `token_updated_at`
- `last_verified_at`
- `status`: `active` / `revoked` / `needs_reauth`

约束：

- `(user_id, github_user_id)` 唯一。

作用：

- 区分 Pullwise 登录用户和多个 GitHub 账户。
- 管理某个 installation 前，先让用户选择/验证对应 identity。

### GitHubInstallation

字段：

- `id`
- `github_app_installation_id`
- `installation_account_login`
- `installation_account_id`
- `installation_target_type`: `User` / `Organization`
- `installation_html_url`
- `repository_selection`
- `permissions`
- `last_synced_at`
- `status`: `active` / `suspended` / `deleted` / `unknown`

约束：

- `github_app_installation_id` 唯一。

### GitHubIdentityInstallationAccess

记录哪个 GitHub identity 最近被验证可访问哪个 installation。

字段：

- `github_identity_id`
- `github_app_installation_id`
- `can_access`: boolean
- `can_manage`: boolean 或 `unknown`
- `verified_at`
- `verification_method`: `user_installations_api` / `setup_callback`
- `last_error_code`

说明：

- GitHub API 可以确认用户能否看到 installation。
- 是否能“管理”该 installation 在 GitHub Web UI 里可能还受 org role 影响。Pullwise 可以把 `can_manage` 先设为 `unknown`，但至少能避免明显错误账户。

### GitHubManageState

短期 state，用于 popup 流程。

字段：

- `state`
- `user_id`
- `purpose`: `manage_installation` / `add_installation` / `link_identity`
- `expected_installation_id`
- `expected_account_login`
- `expected_github_identity_id`
- `redirect_to`
- `created_at`
- `expires_at`

作用：

- 防 CSRF。
- 防止用户选错账户后误绑定。
- 在 OAuth callback 和 GitHub App setup callback 之间串联上下文。

## 推荐用户体验

### Settings / Repositories 安装列表

现在：

```text
GoPullwise / Organization / selected / 1 repository / Manage
```

建议改成：

```text
GoPullwise
Organization / selected / 1 repository
Last verified by @alice
[Manage in GitHub] [Sync]
```

如果这个 installation 没有关联可验证 identity：

```text
GoPullwise
Organization / selected / 1 repository
Needs a GitHub account with admin access
[Choose GitHub account]
```

如果用户点击 Manage：

```text
Open GitHub as an account that can manage GoPullwise.
If GitHub shows a different account, choose the correct one before continuing.
```

但这个提示应该是辅助，核心仍是受控 OAuth 验证。

### Manage 行为

不要直接打开：

```text
https://github.com/settings/installations/{id}
https://github.com/organizations/{org}/settings/installations/{id}
```

改为打开 Pullwise 自己的 URL：

```text
/integrations/github/installations/{installationId}/manage
```

这个 URL 在 popup 内执行 OAuth account picker 和后端验证，验证成功后再 redirect 到 GitHub。

## 核心流程

### 流程 A：管理一个已连接 installation

1. 用户点击 `Manage in GitHub`。
2. 前端调用：

   ```http
   POST /integrations/github/installations/{installationId}/manage-sessions
   ```

3. 后端创建 `GitHubManageState`，返回 Pullwise popup URL：

   ```json
   {
     "mode": "github-installation-manage",
     "url": "https://app.pullwise.dev/api/integrations/github/manage/start?state=..."
   }
   ```

4. 前端用 `openGitHubInstallPopup(url)` 打开 popup。
5. `/integrations/github/manage/start` 重定向到 GitHub OAuth：

   ```text
   https://github.com/login/oauth/authorize
     ?client_id=...
     &redirect_uri=https://app.pullwise.dev/api/auth/github/callback
     &state=...
     &prompt=select_account
   ```

6. 用户在 GitHub account picker 中选择账户。
7. OAuth callback 获取 GitHub profile，后端执行：
   - upsert `GitHubIdentity`
   - 校验 selected identity 是否符合 expected identity/account
   - 调用 `list_current_app_installations_for_user(token)`
   - 确认 `expected_installation_id` 在返回列表中
8. 如果验证失败，redirect 回 Pullwise popup return page：

   ```text
   /?screen=repos&github_error=github_account_mismatch
   ```

9. 如果验证成功，后端拿到该 installation 的 `html_url`，再 redirect popup 到 GitHub settings URL。
10. 用户在 GitHub 完成管理。
11. 用户关闭 popup 或 Pullwise 主窗口重新 focus。
12. 前端调用 `repositories.sync({ installationId })` 和 `integrations.list()` 刷新状态。

### 流程 B：添加另一个 GitHub account 或 organization

1. 用户点击 `Add GitHub account or organization`。
2. 后端创建 state，purpose 为 `add_installation`。
3. 推荐先走 OAuth account picker：
   - 用户选择要用于授权的 GitHub identity。
   - 后端保存该 identity。
4. 然后 redirect 到 GitHub App install URL：

   ```text
   https://github.com/apps/{app_slug}/installations/new?state=...
   ```

5. 用户选择 personal account 或 organization，安装或更新 GitHub App。
6. GitHub setup callback 带回 `installation_id`。
7. 后端不能信任该参数，必须用当前 selected identity 的 user access token 查询 `/user/installations` 并确认 installation 属于该 identity 可见范围。
8. 成功后绑定 installation，sync repositories。

### 流程 C：用户选错 GitHub 账户

如果用户想管理 `GoPullwise`，但 OAuth account picker 里选了 `OtherUser`：

后端返回：

```json
{
  "code": "GITHUB_ACCOUNT_MISMATCH",
  "message": "You selected @OtherUser. Choose a GitHub account that can manage GoPullwise."
}
```

前端显示：

```text
GitHub account mismatch.
You selected @OtherUser, but this installation belongs to GoPullwise.
Choose a GitHub account with access to GoPullwise, then try again.
```

不要让用户继续打开 GitHub settings URL。

### 流程 D：组织权限不足

如果 selected identity 能看到 org，但不能管理 App installation，GitHub 可能仍在 settings 页面拒绝或 404。

Pullwise 可做的处理：

- 在打开前说明需要 org owner/admin 权限。
- 如果 `/user/installations` 看不到目标 installation，直接返回：

  ```text
  GITHUB_INSTALLATION_NOT_VISIBLE
  ```

- 如果用户确认自己是 org admin，但仍失败，提供 fallback：

  ```text
  Open GitHub App install/update flow
  ```

也就是重新进入：

```text
https://github.com/apps/{app_slug}/installations/new?state=...
```

让 GitHub 自己展示可选 accounts/orgs，而不是深链到某个 settings URL。

## API 合同建议

### `GET /integrations`

给每个 installation 增加 manage 状态：

```json
{
  "github": {
    "connected": true,
    "identities": [
      {
        "id": "ghi_1",
        "githubUserId": "123",
        "login": "alice",
        "status": "active",
        "lastVerifiedAt": 1779670000
      }
    ],
    "installations": [
      {
        "installationId": "999",
        "installationAccount": "GoPullwise",
        "installationTargetType": "Organization",
        "repositoryCount": 2,
        "manage": {
          "mode": "verified_identity",
          "githubIdentityId": "ghi_1",
          "githubLogin": "alice",
          "lastVerifiedAt": 1779670000
        }
      }
    ]
  }
}
```

`manage.mode` 可选：

- `verified_identity`
- `needs_identity`
- `needs_reauth`
- `unknown`

### `POST /integrations/github/installations/{id}/manage-sessions`

请求：

```json
{
  "githubIdentityId": "ghi_1",
  "returnUrl": "https://app.pullwise.dev/?screen=settings"
}
```

响应：

```json
{
  "mode": "github-installation-manage",
  "url": "https://app.pullwise.dev/api/integrations/github/manage/start?state=...",
  "installationId": "999"
}
```

### `GET /integrations/github/manage/start?state=...`

服务端路由，不给前端直接处理：

- 校验 state。
- redirect 到 GitHub OAuth authorize，带 `prompt=select_account`。

### GitHub OAuth callback 扩展

当前 callback 只处理普通登录。需要支持 state purpose：

- `login`
- `link_identity`
- `manage_installation`
- `add_installation`

当 purpose 是 `manage_installation`：

1. fetch GitHub user profile。
2. upsert GitHubIdentity。
3. 验证目标 installation 是否在该 identity 的 installations 列表里。
4. 成功后 redirect 到 GitHub installation `html_url`。
5. 失败后 redirect 到 popup return URL，并带结构化 `github_error`。

### `POST /repositories/sync`

建议支持可选参数：

```json
{
  "installationId": "999",
  "githubIdentityId": "ghi_1"
}
```

目的：

- Manage popup 关闭后只同步刚管理的 installation。
- 多账户场景下避免用错误 identity 重新绑定所有 installations。

## 需要修改的地方与功能目的

### pullwise-server

| 文件 | 修改内容 | 功能目的 |
| --- | --- | --- |
| `pullwise_server/db.py` | 增加 GitHub identities、installation access、manage states 的存储 | 支持一个 Pullwise 用户绑定多个 GitHub 账户 |
| `pullwise_server/app.py` | `/auth/github/authorize` 和 callback 支持 `purpose` | 区分登录、连接 identity、管理 installation |
| `pullwise_server/app.py` | 新增 `POST /integrations/github/installations/{id}/manage-sessions` | 用 Pullwise 受控 popup 替代直接 GitHub 外链 |
| `pullwise_server/app.py` | 新增 `/integrations/github/manage/start` | 在 popup 内启动 GitHub OAuth account picker |
| `pullwise_server/app.py` | manage callback 验证 selected identity 能访问 expected installation | 避免打开错误账户下会 404 的 GitHub settings URL |
| `pullwise_server/app.py` | `/integrations` payload 增加 identities 和 installation manage 状态 | 前端展示哪个 GitHub identity 可管理哪个 installation |
| `pullwise_server/app.py` | `/repositories/sync` 支持 installation/identity scoped sync | 管理单个 installation 后只刷新相关范围 |
| `pullwise_server/github_auth.py` | OAuth authorize helper 支持 `prompt=select_account` 与 purpose state | 让用户在 GitHub popup 中选择正确账号 |
| `pullwise_server/github_auth.py` | 安装列表保留 `html_url`，但仅作为验证后 redirect 目标 | 不再直接暴露为默认 Manage 链接 |
| `tests/test_security_contracts.py` | 增加多账户 manage flow、错账户、不可见 installation、scoped sync 测试 | 防止回退到直接 settings URL |

### pullwise-web

| 文件 | 修改内容 | 功能目的 |
| --- | --- | --- |
| `src/components/github-installations.jsx` | 把 `<a href={installationHtmlUrl}>Manage</a>` 改为 button，调用 `onManage(installation)` | 阻断直接外链导致的 GitHub 404 |
| `src/lib/auth.js` | 新增 `manageGitHubInstallation(installationId, identityId)` | 使用后端 manage-session URL 打开 popup |
| `src/lib/install-popup.js` | 保持 popup message/close/sync 机制，增加 manage-specific error code 展示 | 复用现有安装 popup 基础 |
| `src/screens/issues.jsx` | Settings integrations tab 展示 identities、每个 installation 的 manage 状态 | 用户知道要选哪个 GitHub 账户 |
| `src/screens/flow.jsx` | Repositories 页 installation list 也使用受控 Manage button | 两个入口行为一致 |
| `src/lib/pullwise-data.js` | normalize integrations identities/installations manage payload | UI 不直接依赖原始后端字段 |
| `src/api/pullwise.js` | 增加 manage session API | 前端通过 Pullwise 获取 popup URL |
| `src/screens/settings.test.jsx` | 测试 Manage 按钮调用受控 flow，不再渲染 raw href | 锁住 404 修复 |
| `src/lib/auth.test.js` | 测试 manage-session popup、错账户错误、scoped sync | 覆盖核心交互 |

## 错误码与前端提示

建议后端返回这些 `github_error`：

| code | 场景 | 前端提示 |
| --- | --- | --- |
| `github_account_mismatch` | 用户在 GitHub picker 选择了不匹配账户 | 选择能管理该 installation 的 GitHub 账户 |
| `github_installation_not_visible` | 当前 identity 的 `/user/installations` 看不到目标 installation | 当前 GitHub 账户无权访问该 installation |
| `github_org_admin_required` | 推断需要 org owner/admin | 请用 org owner/admin 账户管理 |
| `github_identity_reauth_required` | OAuth token 失效或 scope 不足 | 重新连接该 GitHub 账户 |
| `github_installation_deleted` | installation 已不存在 | 从 Pullwise 移除该 installation 或重新安装 |
| `github_app_installation_not_completed` | GitHub install/update 被取消 | 重新打开 GitHub 安装流程 |

## 为什么不能只靠提示用户切账号

只提示用户“请切到正确 GitHub 账户”不够，因为：

- GitHub active account 是浏览器状态，Pullwise 无法从普通外链中校验。
- 用户看到 GitHub 404 后通常不知道哪个账号错了。
- 多 org / 多 personal account 场景下，正确账户可能不是 installation owner，而是 org owner/admin。
- 直接外链无法把错误带回 Pullwise，也无法自动 sync。

受控 flow 的优势：

- 先通过 GitHub OAuth 明确 selected identity。
- 后端用 API 验证 installation 可见性。
- 错误留在 Pullwise，文案可控。
- 成功后才 redirect GitHub settings，显著减少 404。

## 和“多个 GitHub account”能力的关系

Pullwise 应该把多 GitHub account 做成一等能力：

```text
Pullwise account
  -> GitHub identity @alice
      -> personal installation @alice
      -> org installation GoPullwise
  -> GitHub identity @bob
      -> personal installation @bob
      -> org installation GoTagma
```

用户不应该靠浏览器当前 GitHub active account 来隐式决定 Pullwise 要管理哪个 installation。Pullwise UI 应该明确显示：

- 这个 installation 属于哪个 account/org。
- 最近由哪个 GitHub identity 验证。
- 当前是否需要重新选择 GitHub 账户。

## 分阶段实施计划

### 阶段 1：先止血，避免直接 404

目标：

- 所有 Manage 入口都不再直接渲染 GitHub settings URL。

任务：

1. `GitHubInstallationsList` 的 Manage 从 `<a>` 改成 button。
2. 新增前端 `manageGitHubInstallation()`。
3. 后端新增 manage-session endpoint。
4. 第一版 manage-session 可以先返回 GitHub App install/update URL，而不是 settings deep link。
5. popup 关闭后 sync repositories。

验收：

- 用户点击 Manage 不会直接落 GitHub 404。
- 用户可以通过 GitHub 自己的 account picker 选择账号或 org。

### 阶段 2：加入 GitHub identity 验证

目标：

- 在打开 settings URL 前确认选中的 GitHub identity 能访问目标 installation。

任务：

1. 新增 `GitHubIdentity` 存储。
2. OAuth callback 支持 `purpose=manage_installation`。
3. 使用 `prompt=select_account`。
4. 用 selected identity token 查询 `/user/installations`。
5. 验证目标 installation 后 redirect settings URL。
6. 错误时返回结构化 `github_error`。

验收：

- 选错账号时 Pullwise 显示 `github_account_mismatch`，不会打开 settings URL。
- 选对账号时能打开对应 installation manage 页面。

### 阶段 3：完整多账户 UI

目标：

- Pullwise UI 清楚展示多个 GitHub identities 和 installations 的关系。

任务：

1. Settings 增加 GitHub identities 列表。
2. Installation row 展示 `Last verified by @login`。
3. Manage 时允许用户选择 identity。
4. `Add account or organization` 改成先连接/选择 GitHub identity，再进入 install/update。

验收：

- 一个 Pullwise 用户能清楚看到多个 GitHub 账号。
- 不同 installations 可以绑定不同 GitHub identity。

### 阶段 4：和 workspace / quota 模型合并

目标：

- GitHub identity、installation、workspace、repository 四者关系稳定。

任务：

1. installation 绑定 workspace。
2. identities 作为 workspace membership / repo authorization 的验证来源。
3. Billing 和 quota 不跟 identity 绑定，仍按 workspace/repo。
4. 删除旧的单 `user.githubRepositoryAccess` 主路径，只保留兼容迁移。

验收：

- 换 GitHub identity 不影响 workspace 订阅和 repo 免费额度。
- 管理 installation 使用 identity，计费使用 workspace。

## 测试覆盖面

### 后端

新增或更新测试：

- manage-session 不返回 raw installationHtmlUrl 给前端直接打开。
- manage-session 创建 state，包含 expected installation/account。
- OAuth manage callback 选对 GitHub identity 后 redirect 到 verified installation `html_url`。
- OAuth manage callback 选错 identity 返回 `github_account_mismatch`。
- OAuth manage callback 看不到 installation 返回 `github_installation_not_visible`。
- setup callback 仍不信任 `installation_id`，必须用 selected identity token 验证。
- 多 installations 返回 identities 与 manage 状态。
- scoped `repositories.sync` 只刷新目标 installation。

### 前端

新增或更新测试：

- `GitHubInstallationsList` 不再渲染 `href=installationHtmlUrl` 的 Manage link。
- Manage button 调用 `manageGitHubInstallation(installationId, identityId)`。
- manage popup 成功后调用 repositories sync。
- `github_account_mismatch` 显示可理解错误。
- `github_installation_not_visible` 显示需要切换 GitHub 账户或 org admin。
- Settings 和 Repositories 两处 installation list 行为一致。

## 关键验收场景

1. **浏览器 active account 不匹配**
   - 浏览器 GitHub 当前是 `@alice`。
   - 用户在 Pullwise 点击 `@bob` personal installation 的 Manage。
   - GitHub OAuth picker 出现。
   - 用户选择 `@bob`。
   - Pullwise 验证 `@bob` 能访问 installation。
   - 才打开 GitHub settings。

2. **用户选错账户**
   - 目标 installation 是 `GoPullwise`。
   - 用户在 picker 选了 `@wrong-user`。
   - Pullwise 显示账户不匹配，不打开 GitHub settings。

3. **组织权限不足**
   - 用户选了 org member，但不是 owner/admin。
   - Pullwise 至少能识别 installation 不可见，或提示需要 org admin。
   - 用户不会只看到 GitHub 404。

4. **多个 installations**
   - Pullwise 显示 `GoPullwise`、`GoTagma`、`@alice`、`@bob`。
   - 每行 Manage 都走受控 popup。
   - 管理一个 installation 后只同步相关 installation 或安全刷新全集。

5. **安装/更新被取消**
   - GitHub 返回 `setup_action=request` 或 popup 被关闭。
   - Pullwise 显示取消/未完成，不污染已有授权。

## 不建议方案

### 不建议继续直接打开 `installationHtmlUrl`

原因：

- 这是当前 404 的直接来源。
- URL 是否可访问取决于 GitHub Web active account。
- Pullwise 无法从 GitHub 404 页面拿回结构化错误。

### 不建议要求用户手动切浏览器 GitHub 账号

原因：

- 用户体验差。
- 多账户、多 org 时容易选错。
- 无法自动验证，也无法自动 sync。

### 不建议把多个 GitHub 账号合并成一个 `githubRepositoryAccess`

原因：

- 当前模型已经出现多 installation aggregate，但缺少 identity 维度。
- 后续 workspace/quota/permission 都需要知道哪个 identity 最近验证过哪个 installation。

## 参考依据

- GitHub OAuth authorize 支持 `prompt=select_account`，可强制显示账户选择器：`https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps`
- GitHub App setup URL callback 会带 `installation_id`，但服务端不能信任这个参数，必须用用户 token 验证 installation 与用户的关系：`https://docs.github.com/apps/creating-github-apps/setting-up-a-github-app/about-the-setup-url`
- GitHub App installation API 返回 installation `html_url`、account、target type、permissions 等信息：`https://docs.github.com/v3/apps/installations`

## 最终交付定义

完成后应满足：

- Pullwise 不再把 GitHub installation settings URL 作为默认 Manage 外链。
- 用户点击任意 installation 的 Manage，都会先经过 Pullwise 受控 popup。
- Pullwise 能支持一个用户绑定多个 GitHub OAuth identities。
- Manage 前会验证 selected GitHub identity 能访问目标 installation。
- 选错账户、权限不足、installation 不可见都显示 Pullwise 可控错误。
- 管理完成或 popup 关闭后，Pullwise 自动 sync repositories/integrations。
- Settings 和 Repositories 两个入口行为一致。

