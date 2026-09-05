# EFB 微信会话自动恢复

本项目用于飞牛 NAS 上 EFB 与 ComWechat 的微信会话检测和恢复，适配 Windows 微信退出后仍保留本机会话的情况。

Watchdog 只能操作已经保留有效本机会话的微信窗口，不能绕过微信服务端风控，也不能替代重新扫码登录。

## 功能范围

- 接收 EFB 发出的全天微信离线事件。
- 每 2 分钟只读检查登录状态，不依赖离线事件是否到达。恢复开关只控制点击，不关闭状态检测。
- 凌晨自动恢复默认时段为 `02:50–03:50`；关闭全天恢复后，仅在启用的凌晨时段自动操作。
- 依次识别并点击“确定”和“进入微信”。
- 兼容 ComWeChat 新旧版本的绿色“进入微信”按钮颜色，避免欢迎页按钮因颜色变化而漏检。
- 全天事件恢复与凌晨自主检测使用独立失败计数。
- 任一来源连续失败 3 次后，只暂停该来源，避免重复点击或无限重启。
- 失败状态持久化到 `/state/recovery-state.json`，Watchdog 或 NAS 重启后不会将同一轮失败重新计数。
- 没有可识别的登录按钮时进入人工等待，不请求重启微信；该保护在重启后保留，登录成功或明确的手动重新启用才解除。
- 下一次凌晨窗口开始时自动重置上一晚的暂停状态。
- 登录恢复后清除失败状态，并删除旧诊断画面。
- 只保留最新一张失败诊断画面，避免持续占用 NAS 空间。
- 支持 Telegram 中的总开关、全天事件恢复开关和凌晨自主检测开关。
- 开关状态写入持久化文件，容器或 NAS 重启后仍然保留。

## 恢复流程

### 全天事件恢复

1. EFB 检测到微信未登录，通过容器内部接口触发 Watchdog。
2. Watchdog 调用 ComWechat 登录接口复核状态。
3. 确认仍处于离线状态后，检测退出提示并点击“确定”。
4. 检测欢迎页并点击“进入微信”。
5. 再次调用登录接口确认结果。
6. 恢复成功后清除失败状态；已点击但未恢复时按冷却间隔有限重试，没有可识别按钮时等待人工处理。

全天事件恢复不受凌晨自主检测时段限制。

### 凌晨自主检测

凌晨自动操作只在 `02:50–03:50` 运行。到达结束时间后停止该来源的自动操作；只读状态检测仍继续。

如果 Windows 微信提示自动登录失效或要求重新扫码，Watchdog 停止重复点击并保留诊断信息。
此时使用 `/login` 获取新的二维码。

## 失败处理

| 项目 | 行为 |
| --- | --- |
| 重试间隔 | `POLL_SECONDS`，默认 120 秒。 |
| 点击冷却 | `CLICK_COOLDOWN_SECONDS`，默认 120 秒。 |
| 暂停阈值 | `MAX_RECOVERY_FAILURES`，默认连续失败 3 次。 |
| 全天事件来源 | 独立计数；普通重复离线事件不清除人工等待保护。 |
| 凌晨检测来源 | 只暂停凌晨自主检测；下一次凌晨窗口开始时重置。 |
| 诊断画面 | 只保留 `last-login-failure.png`，恢复成功后清理。 |

Watchdog 不会因为恢复失败而无限重启 ComWechat。容器重启和微信界面恢复由不同的健康守护逻辑负责。

## 连接状态与通知

`/status` 接口的 `connection` 字段记录检查时间、当前状态、人工等待标记及最近 30 次断线区间，与历史 `login_event` 分开。超过 3 个检查周期且至少 300 秒未更新时标记为过期。

每次发现离线后尽量复用一张 Telegram 状态卡，提供登录二维码入口。首次发送超时后不盲目重发，以免重复通知；这时仍可使用 `/status` 查看状态。检测失败不等同于微信退出，没有可点击按钮也不等同于已确认官方风控或二维码。

消息接口恢复不代表已收到新消息，也不能保证补齐离线期间的消息。界面应单独显示接收中断区间和首条新消息验证状态。

## 镜像

```text
ghcr.io/shaoyou11/efb-watchdog:latest
```

`latest` 对应本仓库 `main` 分支通过测试后的最新构建，同时生成提交版本标签，便于故障时回滚。

## 配置

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 运行时区。 |
| `VNC_SERVER` | `127.0.0.1::5905` | 微信桌面的 VNC 地址。 |
| `WECHAT_LOGIN_URL` | 容器内登录接口 | ComWechat 登录状态接口。 |
| `DAILY_START` | `02:50` | 凌晨自主检测开始时间。 |
| `DAILY_END` | `03:50` | 凌晨自主检测结束时间。 |
| `POLL_SECONDS` | `120` | 检测和重试间隔，单位为秒。 |
| `LOGIN_CONFIRM_PROBES` | `3` | 登录成功前的连续状态确认次数。 |
| `LOGIN_CONFIRM_INTERVAL_SECONDS` | `3` | 连续登录状态确认间隔，单位为秒。 |
| `CLICK_COOLDOWN_SECONDS` | `120` | 两次自动点击之间的冷却时间，单位为秒。 |
| `MAX_RECOVERY_FAILURES` | `3` | 单一恢复来源连续失败暂停阈值；达到后保持暂停，直到明确的重新恢复事件。 |
| `TRIGGER_PORT` | `18989` | EFB 离线事件触发接口端口。 |
| `STATE_PATH` | `/state/settings.json` | 开关状态持久化文件。 |
| `RECOVERY_STATE_PATH` | `/state/recovery-state.json` | 失败计数、暂停状态和恢复来源持久化文件。 |
| `CONNECTION_STATE_PATH` | `/state/connection-state.json` | 实时连接状态、断线记录和通知卡片标识。 |
| `DIAGNOSTIC_PATH` | `/diagnostics/last-login-failure.png` | 最新失败诊断画面。 |
| `HEARTBEAT_PATH` | `/tmp/watchdog-heartbeat` | 健康检查心跳文件。 |

真实密码、Token、聊天 ID 和运行配置不提交到公开仓库，只保存在 NAS 的私有配置目录。

## Telegram 控制

EFB Telegram 主端提供 `/watchdog` 管理入口，可分别控制：

- 总开关。
- 全天事件恢复。
- 凌晨自主检测。

设置保存在 `watchdog/state/settings.json`。设置关闭后，Watchdog 不会主动执行对应来源的恢复动作。

## 持久化

Compose 至少挂载以下目录：

```yaml
volumes:
  - ./watchdog/state:/state
  - ./watchdog/diagnostics:/diagnostics
```

其中：

- `/state/settings.json` 保存 Telegram 控制开关。
- `/state/recovery-state.json` 保存当前恢复来源和失败状态；不包含密码、Token、二维码或聊天内容。
- `/diagnostics/last-login-failure.png` 只保存最新失败画面。

## 健康检查与测试

健康检查使用 `/tmp/watchdog-heartbeat` 判断进程是否仍在运行。EFB 负责检查微信登录状态并发送全天事件，
Watchdog 负责复核界面和执行有限恢复，两者不共享失败计数。

本地运行测试：

```bash
python -m unittest test_watchdog.py
```

每次推送到 `main` 后，GitHub Actions 先运行测试，再构建并发布 GHCR 镜像。公开仓库保存程序源码；
私有配置库保存 NAS 部署配置、持久化说明和灾备副本。
