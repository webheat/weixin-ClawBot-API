---
title: clawbot 微信机器人·微信长轮询·146·长轮询
topic: 
category: 微信长轮询
keywords: [长轮询, getupdates, timeout]
---

# clawbot 微信机器人·微信长轮询·146·长轮询

## 问
微信长轮询 getupdates 的 timeout 是多少？（第 6 个变体）

## 答
默认 LONG_POLL_TIMEOUT 通常 25s。配置在 bot.py 顶部常量；timeout 是 HTTP 连接最长等待时间，不是 getupdates 的入参。 变体说明：当 KB 中存在多条相似 Q&A 时，

## 关键词
长轮询、getupdates、timeout

## 主题
clawbot 微信机器人
