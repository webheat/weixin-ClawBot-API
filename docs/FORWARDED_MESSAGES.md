# 微信转发消息处理

> 用户把别人发的消息 / 公众号文章 / 链接 / 小程序通过微信"转发"按钮转给 bot
> 时，bot 现在能识别出转发内容里的标题、描述、URL 等元数据，喂给 AI / ima 检索。
> 本文档记录**协议层形态**、**当前支持范围**、**未支持边角**。

## 1. 协议层说明（item_list 形态）

iLink 2.4.6 协议 `item_list[].type` 完整枚举见
`weixin-openclaw-api-py-docs.md:996-1019`，**没有专门的"转发/链接"type**。微信客户端
把转发内容塞进通用 item 的额外字段：

| 转发形态 | item 实际携带的关键字段 | 来源 |
|---|---|---|
| 一段聊天记录转发 | 多个 `text_item` 依次出现在 `item_list` | `docs §5.4` 通用 text_item |
| 引用回复（"我同意" + "张三：…"） | 主 item 的 `text_item` + `ref_msg.{title, message_item}` | `docs:1021` |
| 链接卡片 | `title` + `description` / `des` + `url`（type 通常为 2） | 微信客户端约定 |
| 公众号文章 | `app_msg.{title, des}` + `url`（公众号 mp.weixin.qq.com/s/...） | 微信客户端约定 |
| 小程序卡片 | `title` + `app_msg.title` + 部分 `url` | 微信客户端约定 |

`ref_msg` 完整结构（`docs:1021`）：

```
ref_msg = {
    "title": "被引用消息的摘要标题",
    "message_item": { ...被引用 message_item 完整副本... }
}
```

注意 `message_item` 是**完整副本**，所以原 text 可能在 `ref_msg.message_item.text_item.text`
和 item 顶层 `text_item.text` 两处都出现——必须去重，否则会浪费 LLM token。

## 2. 当前支持范围

`bot.py:extract_message_text` 现在按以下顺序读取每个 item：

1. `text_item.text`（直接文本 / 语音转写）
2. `voice_item.text`（语音转写）
3. `ref_msg.title` + `ref_msg.message_item.text_item.text`（引用 / 转发聊天记录）
4. `title` / `description` / `des`（卡片直挂字段）
5. `app_msg.title` / `app_msg.des` / `app_msg.description`（公众号 / 小程序嵌套）
6. `url` → 转为 `[链接] url`（不下载、不抓取）

最终 `parts` 用 `\n` 拼成一段纯文本，喂给 AI（与正常文字消息走同一条
`ai.chat → _AIWithIma` 链路）。**URL 不会被解析或访问**，AI 只会拿到 `[链接] xxx` 这条
文本作为信息线索。

## 3. extract_message_text 字段读取顺序

实现在 `bot.py:821`，关键点是：

- **去重**用 `text not in parts` 而非 `parts[-1] != text`：因为引用回复场景下
  原 text 与 `ref_msg.message_item.text_item.text` 被 `ref_msg.title` 隔开，
  连续检查会漏判；单条消息最多 20 个 item，O(n²) 完全可接受。
- **URL 转写**：识别到 url 时不直接拼字符串，改成 `[链接] url`，让 AI 知道这是个
  链接而不是普通文本；同样避免破坏 ima 检索的 keyword 提取（关键词会从 URL host
  段提取）。
- **未知 item type 不报错**：现有逻辑遇到不识别的 type 走 `continue` 跳过；
  现在改用字段驱动而非 type 驱动，**type 不再决定能否抽取文本**。

## 4. 不支持的边角

| 形态 | 当前行为 |
|---|---|
| 纯图片转发（无 title / description） | 忽略，不发送不匹配的能力提示 |
| 视频 / 文件转发 | 忽略；AES-128-ECB 解密 + CDN 下载未实现 |
| 转发一个**无任何文本**的卡片（极少见） | 忽略 |
| 转发聊天记录里混着图片 | 图片部分被忽略，文字部分正常抽取 |
| 转发链接 + 自己附言 | 全部抽出，附言在前、卡片在后（见 Case 7 测试样例） |

## 5. 测试方法

测试是 8 个 case 的 Python 内联断言，构造不同形态的 `item_list` 喂给
`bot.extract_message_text`，验证返回字符串。

构造样例：

```python
# 引用回复（ref 与 text 重叠 → 去重）
{"item_list": [{
    "type": 1,
    "text_item": {"text": "我同意"},
    "ref_msg": {"title": "张三:", "message_item": {"text_item": {"text": "我同意"}}},
}]}
# → "我同意\n张三:"

# 公众号文章（app_msg 嵌套 + ref_msg）
{"item_list": [{
    "type": 1, "text_item": {"text": "看看这篇文章"},
    "ref_msg": {"title": "2026 AI 趋势报告",
                "message_item": {"text_item": {"text": "看看这篇文章"}}},
    "app_msg": {"title": "2026 AI 趋势报告", "des": "深度长文"},
    "url": "https://mp.weixin.qq.com/s/abc123",
}]}
# → "看看这篇文章\n2026 AI 趋势报告\n深度长文\n[链接] https://mp.weixin.qq.com/s/abc123"
```

完整 8-case 清单（含纯文字转发 / 引用回复 / 链接卡片 / 公众号 / 小程序 / 群聊转发
+ 附言 / 空 item / None item）见 git commit message 提交记录。

## 6. 已知限制 / 未来扩展

- **URL 网页抓取**：当前只把 URL 作为文本提示给 AI；如需让 bot 真正读链接内容
  （新闻摘要、商品价格、文档全文），需要新增 utils/html_fetch.py（含白名单、
  超时、长度限制、robots.txt 遵守、内容审查），并在 `bot.py:_AIWithIma` 之外
  单独走预取阶段——独立 feature，建议 P2。
- **CDN 媒体下载**：图片 / 文件 / 语音的 AES-128-ECB 解密 + CDN download
  仍未实现；`weixin-openclaw-api-py-docs.md §2.9` 有完整协议说明。这是 CLAUDE.md
  Critical invariants 第 8 条的范围，独立 feature。
- **小程序元数据**：`app_msg` 在不同微信版本下字段差异较大，当前读取的
  `title` / `des` 是最稳的两个；如果发现小程序卡片被错误分类为空 item，
  把真实报文脱敏后贴到 docs/knowledge/qa-NNN-小程序-字段名.md 供后续扩展。
