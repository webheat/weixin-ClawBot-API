# 微信扫码成功但无回复：诊断复现手册

本分支仅增加链路日志，不变更微信协议、账号持久化和消息处理策略。

## 部署

从本仓库检出 `fix/wechat-connection-diagnostics-20261026`，使用原有共享进程部署流程重启服务。不要删除状态文件或要求所有已登录用户重新扫码。记录实际部署的 commit。

## 复现

1. 在网页为一个测试微信账号完成扫码确认。
2. 在微信中发送 `/help`，确认是否收到命令帮助。
3. 再发送普通文字（例如“你好，回复测试”），记录发消息的北京时间。
4. 如果出现“暂无法连接 OpenClaw”，记录截图出现时间及受影响账号（只提供匿名编号）。
5. 有多个账号时，同时对两个测试账号执行，核对是否彼此影响。

## 采集日志

```bash
systemctl status clawbot-shared.service --no-pager
journalctl -u clawbot-shared.service --since '30 minutes ago' --no-pager -o cat \
  | grep -E 'diag stage=|notify_lifecycle|send_reliable|task_failed|Traceback|ERROR|WARNING'
curl -s http://127.0.0.1:18300/healthz
```

如果部署服务名不同，先用 `systemctl list-units '*clawbot*'` 找到实际单元。

## 日志解释

- `notifystart ok=False`：扫码完成但生命周期通知失败；扫码成功不等于消息连接可用。
- `message_worker_created`：仅表示协程已创建，不表示长轮询成功。
- `getupdates_messages`：至少有一次拉取返回了消息。
- `inbound`：消息进入处理器；`ai_begin` / `ai_complete`：AI 请求处理边界。
- `reply_sent`：服务端 sendmessage 调用通过现有业务成功校验，不代表对方客户端一定显示。
- `worker_failed`：后台工作任务异常退出，优先检查同时间堆栈。

日志默认沿用现有 logging 配置，部分旧日志可能含有消息预览、用户标识或接口路径。**发送任何日志给第三方前务必脱敏**：删除 token、Cookie、API Key、二维码、完整账号 ID、手机号、聊天正文和知识库内容。

## 当前限制

当前 `healthz` 仅代表 Web 进程可响应，不代表 getupdates、AI 或发信链路健康。本改动只为采集证据，不能保证修复实际连接故障。
