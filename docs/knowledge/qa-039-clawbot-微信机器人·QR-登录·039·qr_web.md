---
title: clawbot 微信机器人·QR 登录·039·qr_web
topic: 
category: QR 登录
keywords: [qr_web, 18300]
---

# clawbot 微信机器人·QR 登录·039·qr_web

## 问
clawbot 支持网页扫码登录吗？（第 2 个变体）

## 答
支持。qr_web.py 起一个 127.0.0.1:18300 的 aiohttp，nginx ^~ /clawbot/ 反代出去；多用户由 qr_portal.py 在 :18300 单入口分发。 变体说明：当 KB 中存在多条相似 Q&A 时，

## 关键词
qr_web、18300

## 主题
clawbot 微信机器人
