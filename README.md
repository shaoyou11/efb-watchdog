# EFB 微信会话自动恢复

用于飞牛 NAS 上 EFB/ComWeChat 的微信登录状态检测与会话恢复，适配 Wine/Windows 微信退出后仍保留本机会话的场景。

## 主要功能

- 全天接收 EFB 发出的微信离线事件，先调用登录接口复核，再执行恢复。
- 每天 `02:50-03:50` 每 2 分钟进行一次凌晨自主检测。
- 自动识别并依次点击“确定”和“进入微信”。
- 兼容 ComWeChat 新旧版本的绿色“进入微信”按钮颜色，避免欢迎页按钮因颜色变化而漏检。
- 全天事件恢复与凌晨自主检测使用独立失败计数，互不锁死。
- 任一来源连续失败 3 次后只暂停该来源，等待一个 2 分钟冷却后自动复位重试，避免长期锁死。
- 新的全天离线事件会重新启用一次完整恢复流程；连续失败后的定时复位也会自动重新尝试。
- 下一次凌晨窗口开始时会自动重置上一晚的暂停状态。
- 登录恢复后自动清除失败状态和诊断画面。
- 只保留最新一张失败诊断画面，避免持续占用 NAS 空间。
- 支持 Telegram 中的总开关、全天事件恢复开关和凌晨自主检测开关。
- 开关状态写入持久化文件，容器或 NAS 重启后仍然保留。

## 工作流程

1. EFB 检测到微信未登录，通过容器内部接口触发 Watchdog。
2. Watchdog 调用 ComWeChat 登录接口复核状态。
3. 确认离线后，通过 VNC 检测退出提示并点击“确定”。
4. 再次检测欢迎页并点击“进入微信”。
5. 恢复成功后清除失败状态；失败则等待 2 分钟后重试。

凌晨自主检测只在配置时段运行，全天事件恢复不受凌晨时段限制。

## 镜像

```text
ghcr.io/shaoyou11/efb-watchdog:latest
```

`latest` 始终对应本仓库 `main` 分支通过测试后的最新构建，同时生成提交版本标签，便于故障时回滚。

## 主要配置

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 时区 |
| `VNC_SERVER` | `127.0.0.1::5905` | 微信桌面的 VNC 地址 |
| `WECHAT_LOGIN_URL` | 容器内登录接口 | ComWeChat 登录状态接口 |
| `DAILY_START` | `02:50` | 凌晨自主检测开始时间 |
| `DAILY_END` | `03:50` | 凌晨自主检测结束时间 |
| `POLL_SECONDS` | `120` | 检测和重试间隔 |
| `CLICK_COOLDOWN_SECONDS` | `120` | 两次自动点击之间的冷却时间 |
| `MAX_RECOVERY_FAILURES` | `3` | 单一恢复来源连续失败暂停阈值，之后按点击冷却时间自动复位 |
| `TRIGGER_PORT` | `18989` | EFB 离线事件触发接口端口 |
| `STATE_PATH` | `/state/settings.json` | 开关状态持久化文件 |
| `DIAGNOSTIC_PATH` | `/diagnostics/last-login-failure.png` | 最新失败诊断画面 |
| `HEARTBEAT_PATH` | `/tmp/watchdog-heartbeat` | 健康检查心跳文件 |

真实密码、Token、聊天 ID 和运行配置不提交到本公开仓库，应只保存在 NAS 的私有配置目录中。

## 持久化建议

容器至少应持久化以下目录：

```yaml
volumes:
  - ./watchdog/state:/state
  - ./watchdog/diagnostics:/diagnostics
```

其中：

- `/state/settings.json` 保存 Telegram 控制开关。
- `/diagnostics/last-login-failure.png` 仅保存最新失败画面。

## 本地测试

```bash
python -m unittest test_watchdog.py
```

每次推送到 `main` 后，GitHub Actions 会先运行测试，再构建并发布 GHCR 镜像。公开仓库保存程序源码；私有配置库只保存 NAS 部署配置、持久化说明和灾备副本。
