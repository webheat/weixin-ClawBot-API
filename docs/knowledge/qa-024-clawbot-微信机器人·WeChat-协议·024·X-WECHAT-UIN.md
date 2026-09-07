---
title: clawbot 微信机器人·WeChat 协议·024·X-WECHAT-UIN
topic: 
category: WeChat 协议
keywords: [X-WECHAT-UIN, 随机]
---

# clawbot 微信机器人·WeChat 协议·024·X-WECHAT-UIN

## 问
X-WECHAT-UIN 头每次都要换吗？

## 答
是。bot.py 的 make_headers 每次请求都新生成随机 uint32 → base64，不能缓存复用。

## 关键词
X-WECHAT-UIN、随机

## 主题
clawbot 微信机器人
