---
title: clawbot 微信机器人·IMA 写入·016·import_doc
topic: 
category: IMA 写入
keywords: [import_doc, add_knowledge]
---

# clawbot 微信机器人·IMA 写入·016·import_doc

## 问
怎么往 ima 知识库灌笔记？

## 答
两步：先 POST /openapi/note/v1/import_doc 拿到 note_id，再 POST /openapi/wiki/v1/add_knowledge 带 note_info.content_id 挂到 KB。

## 关键词
import_doc、add_knowledge

## 主题
clawbot 微信机器人
