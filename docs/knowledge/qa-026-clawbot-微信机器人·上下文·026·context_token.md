---
title: clawbot 微信机器人·上下文·026·context_token
topic: 
category: 上下文
keywords: [context_token, 覆盖]
---

# clawbot 微信机器人·上下文·026·context_token

## 问
context_token 怎么更新？

## 答
从服务端 push 的入站消息里拿，覆盖 runtime_state["contexts"][from_id]。重连后会话换了，token 也跟着换。

## 关键词
context_token、覆盖

## 主题
clawbot 微信机器人
