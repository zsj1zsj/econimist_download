---
title: {{title | json}}
book: {{book.title | json}}
highlight_count: {{highlight_count}}
---

# {{title | markdown}}

来源：{{book.title | markdown}}

## 文章正文

{{content}}

## 高亮回顾（{{highlight_count}} 条）

## 摘录
{% for highlight in highlights %}
 - {{highlight.text}}
{% endfor %}
