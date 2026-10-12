# EPUB 文章导出工具

支持按标题提取 EPUB 文章、导出微信读书高亮、下载经济学人 EPUB，以及按模板导出有高亮的文章全文。需要 Python 3.10+，只使用标准库，不需要安装第三方依赖。

## 开始前与场景索引

下列命令使用 PowerShell 语法，请在脚本所在目录运行；包含空格的书名和路径需要加引号。示例中的“书名”“实际bookId”等占位内容需要替换。
这些示例是不同场景的独立用法，不要求全部按顺序执行。同一本书的输出可能已经存在：优先选择新的输出目录；只有确认备份了手写内容后，才使用 `--force`。
默认配置和默认模板相对脚本目录定位；其他相对路径、默认下载目录和默认导出目录相对运行命令时所在目录定位。

| 你要做什么 | 入口与说明 |
| --- | --- |
| 已有 EPUB，只提取指定标题的文章 | [按标题提取](#按标题提取)；不需要 Key，不联网 |
| 查询书名/ID、导出单本或全部高亮 | [微信读书高亮导出](#微信读书高亮导出)；需要微信读书 Key |
| 没有 Key，只想检查高亮输出格式 | 高亮脚本的 `--demo`；数据为模拟内容，不是账号数据 |
| 导出所有有高亮的文章全文 | [导出有高亮的文章全文](#导出有高亮的文章全文)；支持实时高亮或已有 JSON |
| 只下载某一期 EPUB | [按日期下载经济学人 EPUB](#按日期下载经济学人-epub)；不需要 Key |
| 本地缺 EPUB，下载后继续导出 | 全文脚本的 `--download-epub`，仍支持自定义模板 |
| 改文章布局、循环展示高亮 | [自定义文章模板](#自定义文章模板) |
| 查询选项、默认值及组合限制 | [完整参数参考](#完整参数参考) |
| 输出存在、下载失败或文章未匹配 | [常见问题](#常见问题) |

## 按标题提取

```powershell
python epub_to_markdown.py .\TheEconomist.2026.10.10.epub "In China, people get unusually long public holidays"
```

也可以指定输出文件：

```powershell
python epub_to_markdown.py .\TheEconomist.2026.10.10.epub "In China, people get unusually long public holidays" -o .\china-holidays.md

# 只导出文字，不提取图片
python .\epub_to_markdown.py .\TheEconomist.2026.10.10.epub "In China, people get unusually long public holidays" --no-images -o .\china-holidays-text.md
```

不指定 `-o/--output` 时，输出位于 EPUB 同目录，文件名为 `EPUB文件名（不含扩展名） - 文章标题.md`。
此独立脚本会直接覆盖同名 Markdown 和同名图片，**没有 `--force` 保护，也没有 `--force` 参数**。请自行备份或指定不同的输出文件名，并确保输出父目录存在。
默认文件名直接使用文章标题；若标题含 Windows 文件名不允许的字符（例如冒号），请用 `-o` 指定合法文件名。

默认会把文章图片提取到与 Markdown 同名的 `_assets` 文件夹。若只想导出文字，可以加 `--no-images`。
图片使用 EPUB 内完整路径的短哈希命名，避免不同目录下的同名图片互相覆盖，也减少 Windows 路径过长的问题。
正文保留显式换行、加粗、斜体、链接及有序/无序列表（含嵌套和起始序号）；高亮匹配仍使用不含 Markdown 标记和链接地址的纯文本。

标题匹配不区分大小写，也会忽略多余空格和常见标点差异。
此脚本按匹配分数选择整个 HTML/XHTML 文件，不会按目录锚点切分；同分时选择遇到的第一个文件。
因此适合“一篇文章一个 HTML”的 EPUB。若一个文件含多篇文章或有重复标题，需要人工核对输出；有高亮时可使用下方的全文导出脚本进一步定位。

## 微信读书高亮导出

新增 `weread_highlights.py`，使用 Python 3.10+ 标准库，无需安装依赖。
通过微信读书官方 Agent Gateway 获取自己账号的高亮，按章节和原文位置输出 Markdown。
可按书名、bookId 或批量导出；可选附带个人想法/点评和 JSON。

### 1. 配置 API Key

打开 [微信读书官方配置页面](https://weread.qq.com/r/weread-skills)，登录后获取 `wrk-` 开头的 API Key。
将 Key 填入脚本同目录的 `weread_config.json` 并保存：

```json
{
  "api_key": "wrk-你的实际Key"
}
```

脚本自动读取此文件，从其他目录调用脚本时也能找到它。
该文件以明文保存 Key，已加入 `.gitignore`；`weread_config.example.json` 是不含 Key 的配置模板。

```powershell
# 查看有笔记的书（包含 bookId、高亮数、想法数）
python .\weread_highlights.py --list

# 按书名导出
python .\weread_highlights.py --book "书名"
```

Key 的读取优先级：`--ask-key` 隐藏输入 > 非空的 `WEREAD_API_KEY` 环境变量 > 配置文件。
`--ask-key` 的输入只对当次运行生效，不会改写配置。
如需其他配置路径，可以用 `--config .\another-config.json`（相对路径按运行目录解析）；
默认配置缺失可使用其他认证方式，显式指定的配置在需要读取但不存在时会报错。
优先精确匹配书名，其次采用唯一的部分匹配。如果匹配到多本书，会列出候选 ID，使用 `--book-id` 选择。

不想把 Key 写进文件时，可隐藏输入；也可以使用另一份配置文件：

```powershell
python .\weread_highlights.py --list --ask-key
python .\weread_highlights.py --book "书名" --config .\another-config.json
```

若需要通过环境变量提供 Key，下面的 PowerShell 示例会隐藏输入，并在命令结束后移除本次设置的变量；无需把真实 Key 写进命令历史：

```powershell
$wereadSecret = Read-Host "微信读书 API Key" -AsSecureString
$env:WEREAD_API_KEY = [System.Net.NetworkCredential]::new("", $wereadSecret).Password
python .\weread_highlights.py --list
Remove-Item Env:WEREAD_API_KEY
Remove-Variable wereadSecret
```

此示例会替换当前终端已有的同名环境变量，使用后移除，不恢复旧值；不希望改变环境变量时请使用 `--ask-key`。

### 2. 常用命令

填写配置文件后即可运行下列命令：

```powershell
# 单本导出到指定 Markdown 文件
python .\weread_highlights.py --book "书名" -o .\my-highlights.md

# 使用 --list 返回的 bookId（普通数字 ID 和上传书籍的 CB_ ID 都按字符串传递）
python .\weread_highlights.py --book-id "实际bookId" --include-notes --json

# 全部有笔记的书，每本一个文件
python .\weread_highlights.py --all --output-dir .\weread_exports

# 再次同步，明确允许覆盖已导出的文件
python .\weread_highlights.py --all --include-notes --json --force

# 离线检查格式，不联网、不需要 Key；输出内容明确为模拟数据
python .\weread_highlights.py --demo --json -o .\weread-demo.md

# 慢网络下将单次请求超时改为 60 秒
python .\weread_highlights.py --book "书名" --timeout 60

# 保存指定书籍的高亮快照，供后续全文脚本离线使用
python .\weread_highlights.py --book "TheEconomist.2026.10.10" --json --output-dir .\weread_snapshots

# 运行离线回归测试
python -m unittest -v test_weread_highlights.py
```

默认输出到运行命令时所在目录的 `weread_exports` 文件夹，文件名为 `书名--bookId.md`。
自动生成的文件名会替换非法字符并将书名截短到最多 60 个字符；指定 `-o` 时使用该路径，不再使用 `--output-dir`，且后缀必须为 `.md`。
`-o` 不能与 `--all` 或 `--list` 同用。`--json` 是额外保存 JSON，不是只导出 JSON；`--list` 只列书，`--demo` 不访问账号且仅提供固定的模拟高亮。
默认保护已有文件，只有添加 `--force` 才会覆盖。JSON 使用同名 `.json` 文件，包含高亮 ID、章节、位置、颜色等接口返回的字段。
`--include-notes` 会分页获取自己的想法和点评，并在所属章节中独立列出；没有章节信息的内容会归入单独分组。
导出时间使用 UTC+08:00。错误时返回非零退出码，批量导出会继续处理其余书籍并汇总失败数量。

### 范围与验证

只导出账号自己的高亮；`--include-notes` 额外导出自己的想法/点评。
`noteCount` 是高亮数；`bookmarkCount` 是位置书签数，官方接口不提供位置书签内容。
笔记本列表按 `lastSort` 分页；想法按 `synckey` 分页；高亮接口按官方文档一次返回该书的划线列表。
脚本不会将 API 错误静默处理为空笔记；遇到无法识别的数据结构或停滞的分页会报错。
截至 2026-10-10，已验证当前账号中 `TheEconomist.2026.10.10` 的 8 条真实高亮导出，并完成“下载 EPUB → 匹配文章 → 按模板导出”的验证。
其他账号或书籍仍需各自的有效 Key 和对应版本 EPUB；上述结果不代表所有书籍均已验证。

### GitHub 参考

- [Tencent/WeChatReading](https://github.com/Tencent/WeChatReading)：腾讯官方项目；本脚本按其 [notes.md](https://github.com/Tencent/WeChatReading/blob/main/skills/notes.md) 接口协议独立实现，版本字段为 1.0.4。
- [obsidian-weread-plugin](https://github.com/zhaohongxuan/obsidian-weread-plugin)：现成的 Obsidian 同步插件，支持 API Key。
- [weread-notes-export-skill](https://github.com/dongwei6688/weread-notes-export-skill)：第三方 Python Markdown 导出项目，可参考其功能。

接口文档核对日期：2026-10-10。此处没有安装这些项目或它们的 Skill。

## 导出有高亮的文章全文

`export_highlighted_articles.py` 把前两个工具串起来：读取指定书籍的微信读书高亮 → 按章节归组 → 定位本地 EPUB 中的文章 → 导出文章全文和图片。
需要保留同目录的 `epub_to_markdown.py`、`weread_highlights.py`、`download_economist.py`、`article_template.py` 和 `templates` 文件夹，复用 `weread_config.json` 中的 API Key。

```powershell
# 自动在当前目录找同名 EPUB，实时获取最新高亮
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10"

# 指定 EPUB 路径（支持不同文件名）
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --epub .\TheEconomist.2026.10.10.epub

# 同名书可按微信读书 ID 选择
python .\export_highlighted_articles.py --book-id "实际bookId" --epub .\book.epub

# 使用之前导出的 JSON 离线运行，不联网、不读取 Key
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --highlights-json .\weread_exports\TheEconomist.2026.10.10--CB_2IZ0ZD0XU57376v75dAJWFYM.json
```

上面的离线 JSON 必须由高亮脚本加 `--json` 生成；全文脚本自己的 `matches.json` 不能代替它。
如果使用前面的 `weread_snapshots` 示例，请把 JSON 路径改为该目录中实际生成的文件。实时查询书名支持唯一部分匹配；离线 `--book` 必须与 JSON 书名规范化后相同，不能只写部分书名。`--book-id` 则按 ID 核对。

其他常用场景：

```powershell
# 纯文字全文，保存到新的目录
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --no-images --output-dir .\articles_text

# 隐藏输入 Key；同样可以指定其他配置文件
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --ask-key --output-dir .\articles_prompt
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --config .\another-config.json --output-dir .\articles_custom_config

# 单次网络请求最多等待 60 秒；开启下载时也使用这个超时
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --timeout 60 --output-dir .\articles_slow_network

# 再次同步并应用模板：仅在已备份手写内容后运行
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --template .\templates\reading_note.md --output-dir .\template_preview --force
```

默认输出到 `highlighted_articles/书名--bookId/`，包含：

- `index.md`：有高亮的文章清单、全文链接和未匹配说明。
- 每篇文章一个 Markdown，默认模板包含完整文章和末尾的“我的高亮”；自定义模板决定实际输出哪些内容，图片在对应 `_assets` 子目录。
- `matches.json`：文章标题、稳定 ID、EPUB 内部路径、Markdown 文件名、匹配方式、对应高亮及失败记录。

先按章节标题匹配，忽略大小写、空格和标点差异，再核对该组全部高亮是否存在于原文。
标题无法匹配时，要求该组每条高亮规范化后至少有 20 个字符，再通过全部高亮原文唯一定位文章；长度不够就报未匹配。
重复标题会继续用高亮内容消歧，不是必然失败。只有仍无法唯一匹配、或候选文章不包含全部高亮时，才写入未匹配记录，并以非零退出码结束，不会随意选第一篇。
只导出有高亮的文章；同一篇有多条高亮只导出一次。没有高亮时只生成说明和匹配报告，但当前流程仍先查找并读取 EPUB；缺书时仍需提供 EPUB，或开启下载。
此脚本只处理高亮，不提供 `--include-notes`，也不会把输入 JSON 中的想法/点评传给文章模板；需要导出想法时使用高亮脚本。

支持按独立 HTML/XHTML 文件分章，以及 EPUB 3 导航或 EPUB 2 NCX 目录中的连续锚点分章。
嵌套目录按层级确定范围：父文章包含子小节，直到下一个同级或更高级目录项；同时存在 EPUB 3 导航和 NCX 时优先使用前者。
有多个一级标题但没有目录锚点边界的单个文件会跳过，避免混入其他文章；复杂 EPUB 结构可能需要进一步适配。
`--epub-dir` 可指定查找目录（不递归，按文件名匹配）；`--output-dir` 可指定输出根目录。
只要该书的目标子目录已存在（即使为空），默认就停止导出。重新导出请更换输出目录或添加 `--force`。
`--force` 会更新本次文章 Markdown、索引、匹配 JSON 和同名图片，不自动删除旧文章/旧图片，以本次 `index.md` 为准；它不是增量合并手写笔记的功能。
新文章文件名为 `文章标题--article-稳定ID.md`，ID 由书籍 ID、EPUB 内路径和锚点生成，不受高亮顺序或新增文章影响。
再次导出会读取已有 `matches.json`，沿用对应文章原来的文件名（兼容旧版带序号的文件），避免升级或调整顺序时产生重复文章。
请保留此报告；若报告损坏、书籍不一致或包含不安全的文件名，脚本会在写入前报错。目录按 EPUB 索引顺序列出文章，不按高亮返回顺序排序。
`--no-images` 可仅导出文字。离线 JSON 必须对应指定的书籍。

## 按日期下载经济学人 EPUB

下载源固定为你提供的 [hehonghui/awesome-english-ebooks 的 01_economist 目录](https://github.com/hehonghui/awesome-english-ebooks/tree/master/01_economist)。
通过 [GitHub Contents API](https://docs.github.com/en/rest/repos/contents#get-repository-content) 查找实际存在的目录和下载地址，不克隆整个仓库。
支持根目录的 `te_YYYY.MM.DD` 以及 `年份/te_YYYY.MM.DD` 历史归档，只选择对应日期的 EPUB。

### 只下载，不访问微信读书

```powershell
python .\download_economist.py --date 2026-10-10

# 也可按书名下载；默认下载目录为 epubs
python .\download_economist.py --book "TheEconomist.2026.10.10.epub" --output-dir .\epubs

# 下载历史期刊，自动查找年份归档
python .\download_economist.py --date 2025-01-04 --output-dir .\epubs

# 指定网络请求超时
python .\download_economist.py --date 2026-10-10 --timeout 60 --output-dir .\epubs
```

书名支持 `TheEconomist.2026.10.10`、`The Economist 2026-10-10`、`te_2026.10.10`、`经济学人 2026年10月10日` 等形式。
只提供日期时使用 `--date`；缺日期、无效日期、非经济学人书名都会报错，不会猜测最新一期。

### 下载缺失 EPUB 后继续导出

```powershell
# 读取微信读书高亮；本地缺书时下载；再按模板导出
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --download-epub --epub-dir .\epubs --template .\templates\reading_note.md

# 使用已有高亮 JSON；仅在本地缺 EPUB 时访问 GitHub，不访问微信读书
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --highlights-json .\weread_exports\TheEconomist.2026.10.10--CB_2IZ0ZD0XU57376v75dAJWFYM.json --download-epub --epub-dir .\epubs --output-dir .\download_preview
```

不加 `--download-epub` 时保持原来的本地查找行为，使用 `--highlights-json` 仍完全离线。
加开关后也优先使用本地唯一匹配；多个匹配会报错，不会通过下载绕过歧义。显式传入 `--epub` 时严格使用该路径，路径不存在就报错。
集成导出的下载目录是 `--epub-dir`（默认当前目录）；独立下载脚本的默认目录则是 `epubs`。

下载保护：

- 不读取、不发送微信读书 API Key，也无需 GitHub Token。
- 仓库没有指定日期时明确报错，不用临近日期、PDF 或 MOBI 替代。
- 下载到临时文件后，核对大小、Git blob SHA-1 以及 EPUB 的 mimetype、container、OPF 结构；可识别的内部书名日期也会核对。
- 只有校验通过才发布正式文件；失败时清理本次临时文件。单次下载上限为 100 MiB。
- 已有有效文件直接复用，不再次请求下载源；已有损坏文件或并发出现的同名文件保留并报错，不自动覆盖。
- 只访问指定仓库的 GitHub API / raw 地址，不跟随重定向；限流、超时和连接失败会给出明确提示。`--timeout` 可指定大于 0、不超过 120 秒的请求超时（支持小数）。

下载脚本没有 `--force`；全文脚本的 `--force` 仅影响导出产物（Markdown、索引、JSON、图片），不会强制覆盖 EPUB，也不会保护手工写入 Markdown 的笔记。
下载源可用性和文件版本由仓库维护者决定；同日期并不保证与微信读书版本完全相同，导出阶段仍会逐条核对高亮。

下载后只按文章标题提取、不使用微信读书，也可以组合两个独立脚本：

```powershell
python .\download_economist.py --date 2026-10-10 --output-dir .\epubs
python .\epub_to_markdown.py .\epubs\TheEconomist.2026.10.10.epub "In China, people get unusually long public holidays" -o .\downloaded-china-holidays.md
```

## 自定义文章模板

直接编辑 `templates/article.md` 即可改变默认文章布局。也可以复制一份模板，用 `--template` 指定：

```powershell
python .\export_highlighted_articles.py --book "TheEconomist.2026.10.10" --template .\templates\reading_note.md --output-dir .\template_preview
```

`templates/reading_note.md` 是可直接使用的读书笔记示例，包含元信息、正文、逐条高亮和“我的理解”留白。
不指定参数时，默认模板路径相对脚本目录；显式传入的相对路径按运行目录解析。
模板只控制每篇文章 Markdown 的内容，文件名、图片目录和 `index.md` 不受影响。
对已有输出重新应用模板，需要换输出目录或加 `--force`；重新导出会覆盖手动写入的笔记。

### 模板示例

```jinja
# {{title | markdown}}

来源：{{book.title | markdown}}

{{content}}

## 高亮（{{highlight_count}} 条）

{% for highlight in highlights %}
### {{loop.index}}. 摘录

{{highlight.text | blockquote}}

我的想法：

{% endfor %}
```

`{{content}}` 是从 EPUB 提取并转换的正文 Markdown，包含原有段落、副标题、日期和图片引用，不含脚本添加的一级文章标题和高亮列表。用 `{{title}}` 自行安排标题。

### 可用变量

| 变量 | 含义 |
| --- | --- |
| `title` | 文章标题 |
| `content` | EPUB 文章正文 Markdown |
| `book.title`、`book.author`、`book.id` | 书名、作者、微信读书 ID |
| `epub_name`、`epub_source` | EPUB 文件名、文章在 EPUB 内部的路径（含锚点） |
| `exported_at` | 本次文章导出时间（UTC+08:00） |
| `highlights_fetched_at` | 高亮数据获取时间；离线导出时可能早于当前时间 |
| `highlight_count` | 当前文章高亮条数 |
| `highlights` | 当前文章的高亮列表，按原文位置排序 |
| `highlight.text` | 循环中的高亮原文，保留多行 |
| `highlight.id`、`highlight.range` | 高亮 ID、原文位置范围 |
| `highlight.chapter_uid`、`highlight.color` | 章节 ID、接口返回的颜色样式值 |
| `highlight.created_at` | 划线时间（UTC+08:00）；缺失或无效时为空 |
| `loop.index`、`loop.index0` | 循环序号，分别从 1 和 0 开始 |
| `loop.first`、`loop.last`、`loop.length` | 是否首项、是否末项、列表长度 |

模板应保存为 UTF-8 文本，支持 UTF-8 BOM。`highlight` 是示例循环变量，可以换成 `h`，相应改为 `{{h.text}}`；`loop` 为保留变量名，不能作为循环变量。
可嵌套循环，最多 20 层；空列表不输出循环内容。单独占一行的 `for/endfor` 标签不额外留下空行。

### 过滤器与语法范围

| 写法 | 用途 |
| --- | --- |
| `{{title}}` | 原样输出，不自动转义 |
| `{{title \| markdown}}` | 转义普通文本中的 Markdown 特殊字符 |
| `{{highlight.text \| blockquote}}` | 自动转义并将每一行变成 Markdown 引用，推荐用于高亮 |
| `{{book.title \| json}}` | JSON 编码；可安全用作 YAML 元信息的双引号字符串 |
| `{{highlights \| json}}` | 将集合输出为 JSON；不能直接使用 `{{highlights}}` 输出集合 |

这是仅依赖标准库的轻量语法，支持变量、点号字段、上述过滤器、`for/endfor`；不是完整的 Jinja，不支持 `if`、函数调用、任意表达式或模板包含。
正文和高亮中的 `{{...}}` 会作为普通内容保留，不会再次执行模板。
大部分语法/变量错误会显示行号；未知变量不会被静默替换为空（循环体仅在有条目时求值）。语法在读取账号前检查，所有匹配文章的模板渲染均在写入文章 Markdown、索引、匹配 JSON 和图片前完成。
若开启自动下载，EPUB 可能已经在渲染变量前下载完成；变量拼写错误不会删除这份已下载的 EPUB。没有匹配文章时，不会对文章上下文中的变量进行渲染检查。

## 完整参数参考

所有脚本均支持 `-h/--help`。参数值含空格时请加引号；下表中的“无”表示默认不指定该选项，布尔开关默认关闭。

### 标题提取参数

| 参数 | 默认值与行为 |
| --- | --- |
| `epub` | 必填位置参数，本地 EPUB 路径 |
| `title` | 必填位置参数，文章标题 |
| `-o` / `--output` | 默认在 EPUB 同目录生成 `EPUB文件名 - 文章标题.md`；已有文件直接覆盖 |
| `--no-images` | 不提取图片；默认提取 |

### 微信读书高亮参数

`--list`、`--book`、`--book-id`、`--all`、`--demo` 必须且只能选择一个。

| 参数 | 默认值与行为 |
| --- | --- |
| `--list` | 列出有笔记的书，不导出文件 |
| `--book` | 按书名精确匹配，或唯一的部分匹配 |
| `--book-id` | 直接按书籍 ID 导出，ID 按字符串处理 |
| `--all` | 导出所有有笔记的书；单本失败后继续其他书 |
| `--demo` | 离线模拟高亮；无需 Key；不获取真实想法/点评 |
| `-o` / `--output` | 无；单本输出路径，必须以 `.md` 结尾；不能与 `--all/--list` 同用 |
| `--output-dir` | `weread_exports`；指定 `-o` 后不使用此目录 |
| `--include-notes` | 同时请求个人想法/点评，仅真实导出模式生效 |
| `--json` | 导出 Markdown 时额外保存同名 JSON，不是 JSON-only 模式 |
| `--force` | 允许覆盖本次同名 Markdown/JSON；默认保护已有文件 |
| `--ask-key` | 隐藏输入 Key，优先级最高，不保存；离线模拟模式不读取 |
| `--config` | 脚本旁的 `weread_config.json`；非空环境变量优先；离线模拟模式不读取 |
| `--timeout` | 30 秒；必须大于 0、不超过 120 秒，支持小数；单次请求超时，不是整本导出总时限 |

### 高亮文章全文参数

`--book` 和 `--book-id` 必须且只能选择一个，即使使用离线 JSON 也需要选择。

| 参数 | 默认值与行为 |
| --- | --- |
| `--book` | 指定微信读书书名，可带 `.epub` 后缀；离线时须与 JSON 书名匹配 |
| `--book-id` | 指定书籍 ID；离线时须与 JSON 中的 ID 相同 |
| `--epub` | 无；显式 EPUB 路径优先，路径无效时不自动下载替代 |
| `--epub-dir` | 当前目录；查找同名 EPUB，不递归；也是集成下载的保存目录 |
| `--download-epub` | 仅本地缺书时下载带日期的经济学人，不影响其他书籍的本地导出 |
| `--output-dir` | `highlighted_articles`；实际产物位于其下的 `书名--bookId` 子目录 |
| `--highlights-json` | 无；使用已有高亮快照，跳过微信读书请求和 Key 读取；只有显式开启下载时才可能访问 GitHub |
| `--config` | 与高亮脚本共用默认配置及认证优先级 |
| `--ask-key` | 隐藏输入 Key；使用 `--highlights-json` 时不读取 |
| `--timeout` | 30 秒；大于 0、不超过 120 秒；用于微信读书和启用的 GitHub 下载请求 |
| `--no-images` | 不提取图片；不会删除上次导出的图片 |
| `--template` | 脚本旁的 `templates/article.md`；显式相对路径按运行目录解析 |
| `--force` | 允许在已有书籍输出目录中更新本次文件；覆盖手写内容，保留无关旧文件 |

没有 `--all`、`-o` 或 `--include-notes`：全文脚本一次处理一本书，输出目录内每篇文章一个文件。

### 独立下载参数

`--book` 和 `--date` 必须且只能选择一个。此脚本不接受 `--config`、`--ask-key` 或 `--force`。

| 参数 | 默认值与行为 |
| --- | --- |
| `--book` | 带日期的经济学人书名，不查询微信读书 |
| `--date` | 指定期刊日期；支持 `YYYY-MM-DD`、`YYYY.MM.DD`，也支持同分隔符的 `/`、`_` 格式和中文年月日 |
| `--output-dir` | `epubs`；文件名统一为 `TheEconomist.YYYY.MM.DD.epub` |
| `--timeout` | 30 秒；必须大于 0、不超过 120 秒，支持小数；不是整次下载总时限 |

查看脚本自己的帮助：

```powershell
python .\epub_to_markdown.py --help
python .\weread_highlights.py --help
python .\export_highlighted_articles.py --help
python .\download_economist.py --help
```

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 命令找不到脚本或输入文件 | 先切换到脚本目录，或使用绝对路径；含空格的路径加引号。确认 Python 为 3.10+ |
| Key 缺失或认证失败 | 获取当前有效 Key，核对 `--ask-key > WEREAD_API_KEY > 配置文件` 优先级；旧环境变量可能盖过新配置。不要把 Key 发到聊天或提交到仓库 |
| 微信读书书名匹配到多本 | 先用 `weread_highlights.py --list` 查看 ID，再用 `--book-id`，不要随意选第一本 |
| 本地 EPUB 没找到或有多个匹配 | 用 `--epub` 指定确切文件；经济学人缺书时可加 `--download-epub`。显式路径不存在时，先修正路径 |
| 书名含日期，但自动下载不支持 | 下载器只接受经济学人及受支持的日期格式；其他书籍仍可用本地 `--epub` 导出 |
| 仓库没有该日期或同一期存在多个候选 | 核对期刊日期，或手动取得正确版本后指定 `--epub`；脚本不会用邻近日期替代 |
| GitHub 限流、网络超时或证书错误 | 稍后重试；检查网络、代理和证书；慢网络可使用 `--timeout 60`。微信读书 Key 不能解决 GitHub 限流 |
| 下载提示文件损坏、校验失败或日期不一致 | 不使用该文件继续导出。已有文件不会被替换；保留原件，选择新的下载目录重试。新下载失败时本次临时文件会清理 |
| 输出文件或书籍输出目录已经存在 | 优先使用新输出路径。确需更新时先备份，再对高亮/全文脚本加 `--force`；独立标题提取脚本会直接覆盖，不支持此参数 |
| 全文有的文章成功、有的未匹配 | 查看 `index.md` 的“未匹配”和 `matches.json`；核对书籍版本、章节标题和原文。已匹配文章仍会保存，进程返回非零退出码 |
| 标题未匹配，且高亮过短 | 该组每条高亮规范化后须至少 20 个字符才可全文搜索定位；确认版本和标题，不要通过修改原始高亮伪造匹配 |
| 同日期 EPUB 仍匹配不到文章 | 不同来源可能是不同版本，或 EPUB 目录结构不受支持；指定与微信读书上传版本对应的原文件 |
| 已有 `matches.json` 无效 | 保留报告和笔记，检查书籍 ID、JSON 格式及文件名；无法修复时导出到新目录，不建议直接删除旧报告重跑 |
| 离线 JSON 提示书籍不一致 | 使用高亮脚本 `--json` 生成的快照，传入对应完整书名或 ID；`matches.json` 不能作为高亮输入 |
| 没有高亮却仍提示需要 EPUB | 当前全文流程仍先定位并读取 EPUB。提供文件后会输出“暂无高亮”的说明和报告，不生成文章 |
| 模板标签、变量或编码错误 | 保存为 UTF-8，核对 `for/endfor` 和可用变量表；不支持 `if`、函数或任意表达式。文件/语法错误先于账号访问；变量错误先于文章产物写入，但可能已下载 EPUB |
| 想保留“我的理解”等手写笔记 | 暂无自动合并保护。重新导出到新目录进行比较，或先备份；不要直接对手写笔记使用 `--force` |
| 自动化需要判断是否成功 | 正常成功返回 0，业务失败通常返回 1，参数错误返回 2；全文部分匹配失败也返回 1。标题脚本输入 EPUB 不存在时返回 2，Ctrl+C 不应当作成功 |

网络重试不是无限等待；`--timeout` 只控制单次请求，不能保证整个任务在这个时间内完成。
输出不是整本书级别的事务：若磁盘写入失败，可能保留已写好的部分产物。保留现场，解决原因后使用新目录重试。

## 回归测试

```powershell
python -m unittest -v test_download_economist.py test_epub_to_markdown.py test_export_highlighted_articles.py test_article_template.py test_weread_highlights.py
```

全部测试使用模拟数据和临时文件，不联网或读取真实 API Key。覆盖下载日期匹配、历史归档、校验失败与临时文件清理、缓存与并发覆盖保护、目录边界、HTML 格式、图片重名、稳定文件名、旧版报告兼容及模板行为。
`--force` 仍会覆盖文章中的手写内容；自动保留“我的理解”等手写笔记尚未实现，请先备份或使用新的输出目录。
