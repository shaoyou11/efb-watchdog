# EFB WeChat Session Watchdog

用于飞牛 NAS 上 EFB/ComWechat 的微信登录状态检测与会话恢复。

- 仅在配置时段调用轻量登录接口
- 离线时识别“确定”和“进入微信”按钮
- 连续失败 3 次后暂停点击并发送 Telegram 告警
- 只保留最新失败诊断画面，恢复后自动删除
- 不包含任何账号、密码或 Token

镜像：`ghcr.io/shaoyou11/efb-watchdog:latest`
