---
title: clawbot 微信机器人·IMA 检索·004·media_id
topic: 
category: IMA 检索
keywords: [media_id, highlight_content]
---

# clawbot 微信机器人·IMA 检索·004·media_id

## 问
search_knowledge 为什么没有 content / snippet 字段？

## 答
IMA 服务端不返回正文，只返回 media_id / title / highlight_content / parent_folder_id / media_type 共 5 个字段。要拿全文需另调 get_doc_content。

## 关键词
media_id、highlight_content

## 主题
clawbot 微信机器人
