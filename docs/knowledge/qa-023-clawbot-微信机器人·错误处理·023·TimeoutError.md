---
title: clawbot 微信机器人·错误处理·023·TimeoutError
topic: 
category: 错误处理
keywords: [TimeoutError, long_poll]
---

# clawbot 微信机器人·错误处理·023·TimeoutError

## 问
网络抖动 vs 长轮询超时怎么区分？

## 答
asyncio.TimeoutError + long_poll=True 视为正常心跳（_timeout: True）；其它 TimeoutError 进 retry ladder；网络异常走 network_type 标记。

## 关键词
TimeoutError、long_poll

## 主题
clawbot 微信机器人
