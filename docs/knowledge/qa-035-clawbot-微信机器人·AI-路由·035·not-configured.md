---
title: clawbot 微信机器人·AI 路由·035·not-configured
topic: 
category: AI 路由
keywords: [not-configured, 透传]
---

# clawbot 微信机器人·AI 路由·035·not-configured

## 问
clawbot 在 IMA 未配置时怎么表现？（第 2 个变体）

## 答
ImaClient.configured() 返回 False 时 _AIWithIma 直接透传到 base.chat，日志记 mode=llm-only reason=ima-not-configured。 变体说明：当 KB 中存在多条相似 Q&A 时，

## 关键词
not-configured、透传

## 主题
clawbot 微信机器人
