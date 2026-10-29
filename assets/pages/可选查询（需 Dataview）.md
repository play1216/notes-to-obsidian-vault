---
类型: 说明
tags:
  - 索引
---

# 可选查询（需 Dataview）

这份文件里的代码块**默认不会生效**——只有装了 Dataview 插件才会渲染成表格。没装也不影响库的任何其他功能，[[首页]]、[[复习台]]、[[统计]] 都是静态的，随时能看。

装法：设置 → 第三方插件 → 关闭安全模式 → 浏览 → 搜 `Dataview` → 安装 → 启用。

## 今天该做哪 10 条

按难度倒序，从待摸底里挑。

```dataview
TABLE {{ID}}, 章节, 难度, 知识点
FROM "{{ITEMS}}"
WHERE 状态 = "待摸底"
SORT 难度 DESC, 日期 DESC
LIMIT 10
```

## 某一章还没做的

```dataview
TABLE {{ID}}, 难度, 知识点, 方法
FROM "{{ITEMS}}"
WHERE 状态 = "待摸底"
SORT 难度 DESC
```

## 难度 4 星以上的

```dataview
TABLE {{ID}}, 章节, 难度, 状态
FROM "{{ITEMS}}"
WHERE 难度 >= 4
SORT 难度 DESC, 日期 ASC
```

## 某个方法用过哪些条目

也可以直接看 `{{METHODS}}` 那篇的静态表，或者用反链面板。要动态的话：

```dataview
TABLE {{ID}}, 章节, 难度
FROM "{{ITEMS}}"
WHERE contains(方法, "同增异减")
SORT 日期 ASC
```

## 章节归属待确认的

迁移时自动归类把握不大的条目，都打了 `章节待确认` 标记。

```dataview
TABLE {{ID}}, 章节, 知识点
FROM "{{ITEMS}}"
WHERE 章节待确认 = true
SORT 日期 ASC
```

## 进度

```dataview
TABLE length(rows) AS 条目数
FROM "{{ITEMS}}"
GROUP BY 章节
```

## 提醒

Dataview 的 `FROM` 路径按文件夹过滤。以后你要是把笔记在文件夹之间拖动（比如纠正章节），查询会自动跟上——因为查的是文件当前位置，不是记录在头部的 `章节` 字段。这两者可能不一致，**以文件夹为准**，改章节时记得连 `章节` 字段一起改。
