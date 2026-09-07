---
title: clawbot 微信机器人·多用户·012·多用户
topic: 
category: 多用户
keywords: [多用户, /etc/clawbot]
---

# clawbot 微信机器人·多用户·012·多用户

## 问
多用户模式下每个用户怎么隔离？

## 答
state/config/log 都按用户名分文件（weixin_state_<name>.json 等），env 走 /etc/clawbot/<name>.env + ima.env 共享兜底，端口从 env 取。

## 关键词
多用户、/etc/clawbot

## 主题
clawbot 微信机器人
