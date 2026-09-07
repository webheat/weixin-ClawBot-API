---
title: clawbot 微信机器人·微信长轮询·002·cursor
topic: 
category: 微信长轮询
keywords: [cursor, sync_buf]
---

# clawbot 微信机器人·微信长轮询·002·cursor

## 问
getupdates 返回的消息去重逻辑在哪里？

## 答
靠 sync_buf / get_updates_buf 这两个 opaque cursor，服务端保证单调推进。客户端只做透明转发，存储到 weixin_state.json。

## 关键词
cursor、sync_buf

## 主题
clawbot 微信机器人
