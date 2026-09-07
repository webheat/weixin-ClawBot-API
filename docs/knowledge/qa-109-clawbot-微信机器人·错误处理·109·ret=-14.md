---
title: clawbot 微信机器人·错误处理·109·ret=-14
topic: 
category: 错误处理
keywords: [ret=-14, stale_token]
---

# clawbot 微信机器人·错误处理·109·ret=-14

## 问
ret=-14 怎么处理？（第 4 个变体）

## 答
send_msg_safe 把它当 stale_token 重新抛出；message_loop 捕获后清 bot_token、sleep RETRY_DELAY、跑 login_with_qrcode。 变体说明：当 KB 中存在多条相似 Q&A 时，

## 关键词
ret=-14、stale_token

## 主题
clawbot 微信机器人
