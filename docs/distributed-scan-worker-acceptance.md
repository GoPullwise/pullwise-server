# Pullwise 分布式 Scan Worker 总体验收标准

日期：2026-05-29 | 状态更新：2026-05-30

> 阶段 1 和阶段 2 已基本实现，阶段 3 大部分完成。剩余未实现项：
> - `/admin/workers/{id}/test` 诊断端点
> - worker update 失败回滚机制

## 总目标

3 个阶段全部完成后，系统应从“server 本机跑 scan”变成：

```text
web -> server -> global job queue <- workers
                         ↑          |
                         └ results/progress/heartbeat
```

职责边界：

- server 负责登录、支付、quota、REST API、全局任务队列、worker 管理、状态展示和结果保存。
- worker 负责 clone repo、运行 Codex CLI、整理结果、上传进度和最终 findings。

## 阶段 1：任务队列与 Worker 执行闭环

- [x] 用户提交 scan 后，server 只创建 queued scan/job，不在本机直接执行 scan。
- [x] 所有用户提交的 scan 进入 server 维护的全局 FIFO 队列。
- [x] 队列状态持久化，server 重启后 queued/running/done/failed 状态不丢失。
- [x] 支持全局 queued/running 限制。
- [x] 支持单用户 queued/running 限制。
- [x] web 能看到 queued 状态、queue position、ahead count。
- [x] worker 能通过 heartbeat 上报 max_concurrent_jobs、running_jobs、free_slots。
- [x] worker 能按 free slots 一次 claim 一个或多个任务。
- [x] claim 是原子的，多个 worker 同时 claim 不会拿到同一个 job。
- [x] job 被 claim 后，server 记录 claimed_by_worker_id、claimed_at、attempt。
- [x] job 被 claim 后，web 能看到任务从 queued 变为 running/processing。
- [x] server 给 worker 下发本次任务所需的短期 GitHub clone token，不下发长期 GitHub App 私钥。
- [x] worker 能 clone repo、运行 Codex CLI、解析 findings。
- [x] worker 能上传 phase/progress/message，web 能看到进度变化。
- [x] worker 能上传最终 result，server 保存 findings/issues 并标记 scan done/failed。
- [x] result 上传幂等：重复上传同一 job_id + attempt_id + checksum 不会重复写 issue、重复扣 quota 或重复完成 scan。
- [x] result checksum 不一致时，server 能拒绝或标记冲突。
- [x] worker 掉线或 heartbeat 超时后，server 能标记 job lost/timed_out。
- [x] 未超过最大重试次数的 lost/timed_out job 会重新入队。
- [x] 超过最大重试次数的 job 会标记 failed。
- [x] 用户取消 scan 后，queued job 不会被 claim；running job 会进入取消/停止流程。
- [x] worker 不会 claim disabled worker 不允许处理的任务。
- [x] scan API 返回的状态、queue、progress、summary 与真实 job 状态一致。

## 阶段 2：Worker 管理与可观测性

- [x] server 持久化 worker registry。
- [x] worker registry 至少包含 worker id、name、token hash、provider、enabled、status、heartbeat、capacity、version、region、last error、created/updated/disabled/deleted 时间。
- [x] worker token 只保存 hash，不保存明文。
- [x] 创建 worker 或 rotate token 时，明文 token 只返回一次。
- [x] 支持 admin 权限判断。
- [x] 非 admin 不能访问 /admin/* worker 管理接口。
- [x] admin 可以创建 worker。
- [x] admin 可以查看 worker 列表和详情。
- [x] admin 可以更新 worker metadata。
- [x] admin 可以启用 worker。
- [x] admin 可以禁用 worker。
- [x] admin 可以 soft delete worker。
- [x] admin 可以 rotate worker token。
- [x] disabled/deleted worker 不能 claim 新任务。
- [x] worker heartbeat 能更新 registry 中的 capacity、running jobs、free slots、version、hostname、region、last heartbeat。
- [x] server 能计算 worker 状态：idle、busy、degraded、offline、disabled。
- [x] heartbeat 超时的 worker 会显示 offline。
- [x] 有 last_error 或版本不兼容的 worker 会显示 degraded。
- [x] capacity 用满的 worker 会显示 busy。
- [ ] /admin/workers/{id}/test 能基于 heartbeat、token 使用情况、version、provider、capacity、last error 给出检测结果。
- [x] public status API 能返回 scan system summary。
- [x] public status 至少包含 ok/degraded/down、online worker count、total worker count、total capacity、running jobs、queued jobs、degraded/offline count。
- [x] admin status API 能返回 worker 详情。
- [x] web status 普通用户能看到 scan 系统状态、queue length、active scans、available capacity。
- [x] web status 管理员能看到 worker 列表、状态、capacity、last heartbeat、provider/version、region、最近错误。
- [x] worker 管理操作都有审计日志。
- [x] 审计日志记录 actor、action、worker id、request id、changed fields、timestamp、success/failure、error。
- [x] public API 不泄露 worker token、hostname、内部错误细节或敏感日志。
- [x] worker name、region、hostname、error 输出经过清洗。
- [x] admin 禁用/删除 worker 后，web status 能及时反映。

## 阶段 3：Worker 一键部署与运维

- [x] admin 创建 worker 后，server 返回可执行部署命令。
- [x] 部署命令包含 server URL、worker id，并通过环境变量或交互输入接收一次性 token。
- [x] 部署命令不会在日志中泄露 token。
- [x] install 脚本能检查 OS/CPU 架构。
- [x] install 脚本能安装或检查 Python/Node/Git。
- [x] install 脚本能安装或检查 Codex CLI。
- [x] install 脚本能创建专用系统用户，如 pullwise-worker。
- [x] install 脚本能创建配置目录、日志目录、checkout 目录。
- [x] worker env 会写入安全路径，权限最小化。
- [x] worker 作为 systemd service 运行。
- [x] systemd service 支持 start/stop/restart/status。
- [x] systemd service 自动重启 worker。
- [x] worker 首次启动后能向 server heartbeat。
- [x] server/web 能看到新 worker online、degraded 或 not_ready。
- [x] Codex CLI 未登录时，doctor 能明确识别，worker 不应被误判 ready。
- [x] 管理员完成 codex login 后，doctor 能确认 Codex ready。
- [x] worker 能按配置上报 max_concurrent_jobs。
- [x] pullwise-worker doctor 能检查 server URL、token、heartbeat、Git、Codex、Codex login、目录权限、磁盘空间、systemd 状态。
- [x] pullwise-worker status 能显示本地服务状态。
- [x] pullwise-worker restart 能重启服务。
- [x] pullwise-worker update 能升级 worker 程序并保留 env 配置。
- [ ] update 失败时能回滚或保留旧版本可运行。
- [x] pullwise-worker uninstall 能停止并移除 service。
- [x] uninstall 不误删非 worker 目录。
- [x] worker 日志支持 rotation。
- [x] 日志不记录 worker token、GitHub clone token、Codex session、repo secret。
- [x] scan checkout 成功后会清理。
- [x] failed checkout 可按保留策略短期保留。
- [x] 支持定期清理超时 checkout。
- [x] 支持最大磁盘占用限制或告警。
- [x] worker 部署后能通过阶段 1 的 claim/progress/result 流程真实跑完一个 scan。
- [x] 多台 worker 部署后，server 能同时接收 heartbeat 并分配任务。
- [x] 增加 worker 后，系统总 capacity 会增加，并在 web status 显示。

## 端到端验收场景

- [x] 单 worker 场景：创建 worker -> 部署 -> heartbeat online -> 用户提交 scan -> worker claim -> 上传 progress -> 上传 result -> web 显示 done。
- [x] 多 worker 场景：部署 2 台以上 worker -> 同时提交多个 scan -> 不同 worker 按 capacity 领取任务 -> 无重复 claim -> 全部完成。
- [x] 队列场景：提交超过 capacity 的 scan -> 多余任务保持 queued -> web 显示 queue position -> worker 完成任务后自动领取下一个。
- [x] 用户限流场景：单用户超过 queued/running 限制 -> server 拒绝或延迟入队，并返回明确错误。
- [x] 全局限流场景：全局 queued/running 达上限 -> server 拒绝新任务或返回系统繁忙。
- [x] Worker 掉线场景：worker claim 后停止服务 -> heartbeat 超时 -> job lost/timed_out -> 未超 retry 的任务重新入队 -> 其他 worker 可继续处理。
- [x] Worker 禁用场景：admin 禁用 worker -> 该 worker 不能 claim 新任务 -> status 显示 disabled。
- [x] Token rotation 场景：admin rotate token -> 旧 token 失效 -> 新 token 配置后 worker 恢复 heartbeat/claim。
- [ ] Result 幂等场景：worker 重复上传同一 result -> issues 不重复写入，scan 状态保持一致。
- [ ] 安全场景：非 admin 调用 /admin/workers 被拒绝；public status 不泄露 worker token、hostname、内部错误。

## 当前自动化验收证据

以下为当前仓库内已用自动化测试覆盖的验收链路。它们证明代码路径成立，但不替代真实 Linux worker、真实 GitHub token、真实 Codex CLI 登录后的部署验收。

- `tests.test_worker_pull_routes`
  - worker heartbeat -> claim -> progress -> result -> scan done。
  - 多 worker 按 capacity claim 多个 queued job，无重复 claim。
  - 超过 capacity 的 job 保持 queued，前序任务完成后可继续 claim。
  - 全局 queued 限流会在创建 job 前拒绝新 scan。
  - result 重复上传幂等，同 attempt/checksum 不重复写 issue。
  - result checksum 冲突返回 conflict。
  - result attempt 必须匹配当前 claim attempt；旧 attempt late result 被拒绝。
  - 取消后的 running job 拒绝 late result，不会把 cancelled scan 改回 done。
  - worker token 不能冒充其他 worker，也不能上传其他 worker claim 的 job。
  - claim payload 能下发短期 GitHub clone token，不下发 GitHub App 私钥。
  - 并发 claim 不会重复领取同一个 job。
  - 超过最大 retry 的 timed out job 标记 failed。
- `tests.test_scan_recovery`
  - server 重启后 running scan 会回到 queued。
  - server 重启恢复会同步 requeue 未过期但仍 claimed 的 SQLite job。
  - job lease timeout 会 requeue scan/job。
  - worker heartbeat timeout 会 requeue scan/job。
  - 终态 done/failed/cancelled scan 不会被恢复逻辑改写。
- `tests.test_worker_admin_routes`
  - admin 创建 worker、查看详情、更新 metadata、启用、禁用、soft delete、rotate token。
  - token 只保存 hash，明文 token 只在创建/rotate 返回。
  - rotate token 后旧 token 失效，新 token 能恢复 heartbeat。
  - 非 admin 不能访问 admin worker 管理接口。
  - disabled/deleted worker 不能 claim 新任务。
  - heartbeat 更新 capacity、running jobs、free slots、version、hostname、region、doctor 状态。
  - worker 状态可计算 idle、busy、degraded、offline、disabled。
  - 多个 online worker 会增加 public status 的 total capacity 和 available capacity。
  - public status 不泄露 hostname、last error 或 token；admin status 返回 worker 详情。
  - worker 管理操作写 audit log，并覆盖 actor、action、worker id、request id、changed fields、timestamp、success/failure、error。
- `pullwise-worker/tests/test_worker_main.py`
  - worker run_job 上传 progress/result 并清理 checkout。
  - clone 使用 server 下发的短期 clone token。
  - Codex CLI 执行结果可解析为 findings。
  - doctor 能识别 Codex 未登录和 ready 状态。
  - status/start/stop/restart/update/uninstall lifecycle 命令路径存在。
  - update 失败会保留 env 配置。
  - scan summary 日志会脱敏 worker token 和 clone token。
  - cleanup 只删除 worker checkout 范围内目录。
  - deploy assets 覆盖 install、systemd、logrotate、cleanup、update、restart、uninstall。
- `pullwise-web` queue/status tests
  - web 能渲染 queued 状态、queue position、ahead count 和 capacity limits。
  - web status 普通用户能看到 scan system summary。
  - web status 管理员能看到 worker 列表、capacity、last heartbeat、provider/version、region、最近错误。

## 仍需实机验收

以下实机项由部署/运维方后续在真实环境手工执行，不阻塞当前仓库内自动化验收完成。

- 在真实 Linux 主机上运行 `/install-worker.sh`，验证 OS/CPU、Python/Node/Git/Codex CLI、systemd、logrotate、目录权限。
- 使用真实 GitHub App installation token clone 私有 repo。
- 使用真实已登录 Codex CLI 完成一次 scan。
- 部署 2 台以上真实 worker，验证 server 同时接收 heartbeat、capacity 增加、web status 刷新。
- 验证生产日志中不出现 worker token、GitHub clone token、Codex session 或 repo secret。
- 验证磁盘空间告警/最大占用策略在真实 checkout 压力下符合预期。
