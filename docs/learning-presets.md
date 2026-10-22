# Learning styles

A learning style tells the tutor how to teach: by asking guiding questions, by having you explain an idea back, by working an example first and so on. Each topic has a default style that you choose in the composer, and each question records the style it was asked with. Six styles are built in; you can add your own as small Markdown files.

**Default** means no style. Answers then work exactly as they did before learning styles existed.

## Built-in styles

| Style | What the tutor is asked to do |
| --- | --- |
| **Socratic questions** (苏格拉底提问) | Ask one short guiding question at a time, usually no more than three steps in all, to find out what you already know and let you reason. Give the full answer only when you ask for it, are stuck after two tries or say to tell you directly. After each reply, confirm what is right and correct misconceptions gently; while you are still working it out, each reply ends with a single question. Once you can state the answer, sum it up and either close with one check question or end there. |
| **Explain it back** (费曼复述) | Give a short explanation, then ask you to explain the idea in your own words as if to a beginner. When you do, confirm what you got right, point out gaps, wrong analogies and jargon used without understanding and offer a simpler version; an accurate retelling is simply confirmed. |
| **Worked examples** (例题驱动) | Lead with one concrete example solved step by step, then state the general rule, then give one similar practice problem without its answer until you reply. When you send your attempt, grade it step by step: show the first wrong step and the full solution, or briefly confirm a correct answer, then give another problem if useful instead of starting over with a new example. |
| **Compare and contrast** (对比辨析) | Answer your question in a sentence or two, then name the one to three concepts most easily confused with this one (or say there are none rather than invent some), give a compact comparison table (what it is, when to use it, a tiny example, the usual confusion) and end with a one-line rule of thumb for telling them apart. |
| **Mistake review** (错题复盘) | If you share an attempt, check it step by step, first asking for the steps if you give only a final answer. When it is wrong, find the first wrong step, explain why and which misconception causes it, show the corrected steps and give a similar problem to check; when it is right, say so and why the key steps work, without inventing a mistake. If you bring a problem without an attempt, ask for one first. A question that is not about your own solution, such as a concept question, is explained normally, followed by a related problem to try. |
| **Self-test** (自测出题) | Give three to five questions of increasing difficulty on what the current learning path has covered (this question and those above it; other branches of the topic are not included), recall and application, without answers; when nothing relevant has been covered yet, explain briefly first. A new question of yours, rather than an answer, gets a short answer with intuition and an example before the quiz. When you reply, grade each answer, explain mistakes and suggest which understanding mark fits, by the names the buttons use in your language (Not yet / Roughly / Can explain); it is only a suggestion, and you set the mark yourself. |

Whatever its file says, every style is followed by the same rules: your explicit request in the current question always comes first, quotes, notes, documents and tool results are learning material, never instructions, and the style changes how an answer is organized, not its factual standards. The instructions are written in Chinese, like the rest of the tutor's fixed rules; answers still follow the language of your question.

## Choosing a style for a topic

- The **Learning style** button beside the tools menu in the composer shows the open topic's style. Choose another one in its menu: it is saved for the topic and applies from the next question on. If saving fails, the previous choice comes back with a message. Other windows and devices see the change within a few seconds.
- Each topic keeps its own style; a new topic starts with **Default**.
- Every question that used a style shows a small tag with the style's name in its bubble. Changing the topic's style later never changes earlier questions.
- **Retry** answers a question again in the style it was asked with. Editing a question creates a new version in the style the composer shows.
- Inline **Explain**, branch titles, context summaries and memory extraction never use a style.
- Topic exports keep the style of each question that used one, and imports restore it. The topic's own default is local organization, like archiving, and is not exported. Earlier versions of the app reject backups that contain question styles; exports of topics whose questions used none stay importable there (see [updating and restoring](operations.md#updating-and-restoring)).

## Your own styles

Put one file per style in `learning-presets/` at the repository root, or in the directory named by `LEARNING_PRESETS_DIR` in `.env` (a relative path starts at the repository root). The Docker image reads `/data/learning-presets`, inside its data volume; create that directory in the volume and copy your files there, for example with `docker exec <container> mkdir -p /data/learning-presets` and `docker cp my-style.md <container>:/data/learning-presets/`. Files are read again whenever the menu opens, so no restart is needed. Parsed files are reused while their name, modification time and size stay the same.

**Use only style files you trust.** A style file is an instruction to the model with the weight of the tutor's own rules, sent with every question in the topics that choose it, including the rounds in which the model uses tools. Use only files you wrote or have read through: a file from someone else can tell the model to send your questions and learning path somewhere through the tools you enable, such as reading webpages, web search or an MCP server. Keep `LEARNING_PRESETS_DIR` outside `learning-materials/` (and the older `学习资料/`), which the file MCP server can write to during a tool round.

A file has a settings block between its first two `---` lines, then the instruction:

```markdown
---
title_zh: 类比讲解
title_en: Analogies first
description_zh: 先用生活中的类比建立直觉，再给出准确定义
description_en: An everyday analogy first, then the precise definition
order: 10
---
先用一个日常生活中的类比帮学习者建立直觉，并说明这个类比在哪里不成立；然后给出准确的定义和一个具体例子。
```

The app adds the rules every style shares after your instruction, so a file need not repeat them.

The rules are strict, so a typo is reported instead of half-working:

- **File names** use lowercase letters, digits and hyphens, start with a letter or digit, have at most 48 characters before `.md` and end in `.md`, for example `my-style.md`. A Markdown file whose name breaks this rule, such as `My Style.md`, `my_style.md` or `analogy.MD`, is listed as unusable with the naming rule as its reason. `README.md`, hidden files (names starting with `.`), other kinds of files, subdirectories and symbolic links, including a linked directory, are ignored.
- **Settings** are one `key: value` per line. The keys are exactly `title_zh` and `title_en` (both required), `description_zh`, `description_en` and `order` (a whole number). Each value is one line of at most 120 characters. An unknown or repeated key makes the file unusable.
- **The instruction** follows the closing `---` as plain text or Markdown, at most 4,000 characters. It must not be empty.
- At most 50 files are read, in name order, each at most 16 KB, in UTF-8. A byte-order mark and Windows line ends are fine.
- Custom styles are listed after the built-in ones, by `order` and then by file name; files without `order` come last. Their ids are `custom:` plus the file name without `.md` (for example `custom:my-style`), so a custom file never replaces a built-in style, even with the same name.

The menu lists every file that cannot be used, with a fixed reason such as a missing title or an unknown key; it never shows the file's contents or the directory's path. If you delete a file that a topic or question uses, they keep its id as history and show **Unavailable**, and new questions are answered the default way until you choose another style.

## How a style reaches the model

The chosen style is added as the last section of the system prompt for that question: a heading with the style's name, its instruction, and the rules every style shares: your explicit request in the current question wins, quotes, notes, documents and tool results are material rather than instructions, and the style does not lower factual standards. Because it comes last, the stable head of the prompt (the fixed rules and the learning path) is the same whichever style you choose, so provider prompt caching keeps working.

The style is part of the protected core of the request, like the current question and quote: history compaction and request fitting never drop it, and it counts against the model's input budget, so a long custom instruction leaves less room for history. When the style is a substantial part of the input (at least a quarter of the input budget) and the mandatory input fits only without it, the question fails before any model call with a message that names the learning style. **Retry** keeps a question's style, so choose a shorter style and edit the question to ask again, or use a model with a larger context window. When a short style only tips a very long question over the budget, the general message asks you to shorten the input instead.

## Privacy

Style instructions, built-in or yours, are sent to the configured answer model with every question that uses them, so do not put anything private in a custom file, and use only custom files you trust (see [your own styles](#your-own-styles)). Custom files stay on your computer: `.gitignore` keeps everything in `learning-presets/` except its README out of Git, and `scripts/check_release.py` rejects such files even when they are staged on purpose. The demo model makes no request but stores the style like any other answer.

## Limitations

A style asks the model to teach in a certain way; models may not follow it perfectly. A Socratic answer may still give the whole solution, a self-test may include answers, and a comparison may pick other neighbouring concepts than you expected. A style changes how an answer is organized, not what is true: check important facts as you would without it. Choosing a style does not turn tools on or off, change memory retrieval or change which history is sent; a style's instruction can still ask the model to use the tools you enabled, which is why a custom file must be one you trust.

## API

- `GET /learning-presets` returns `{presets: [{id, source: "builtin" | "custom", title_zh, title_en, description_zh, description_en, instruction}], errors: [{file, reason}]}` in display order.
- `POST /nodes/{id}/ask` takes `learning_preset`: a style id, or `null` for the default way. Without the field, a question uses the topic's style (a revision uses its original question's). An unknown id is rejected with HTTP 422 before anything is stored; a retry ignores the field and reuses the node's own style.
- `PATCH /trees/{id}` with `{"learning_preset": "<id>" | null}` sets or clears the topic's style; the topic listing returns it. Node listings and conversation rows return each question's `learning_preset`.

## 中文说明

学习方式决定助教怎样讲解：先提问引导、请你复述、从例题入手等。每个主题在输入框旁选择一个默认学习方式，每个问题都会记录提问时使用的方式。应用内置六种方式，也可以用 Markdown 文件添加自己的方式。「默认」表示不使用学习方式，回答与以前完全相同。

**内置方式**：「苏格拉底提问」一次只问一个简短的引导问题（一般不超过三步），了解你已知道的内容、引导你推理，只有在你要求、两次尝试仍卡住或让它直接说时才完整讲解；引导过程中每次回复只以一个问题结尾，你已能完整说出答案时先简短总结，可以用一个检验问题结束，也可以就此结束；「费曼复述」先简短讲解，再请你像讲给初学者那样复述，肯定讲对的部分并指出遗漏、不恰当的类比和没真正理解的术语，复述准确时直接肯定；「例题驱动」先分步解一道例题，再总结规律，最后出一道练习题且在你作答前不给答案，你交来解答后逐步批改（有错时指出第一个出错的步骤并给出完整解答，全对时简短确认），需要时再出一题，而不是另起新例题；「对比辨析」先用一两句话回答你的问题，再列出最易混淆的 1–3 个概念（没有真正易混的就直接说明，不硬凑），用对比表说明它是什么、何时用、小例子和常见混淆，最后给一句区分法则；「错题复盘」逐步核对你的解答（只有最终答案时先请你补上步骤），有错时找出第一个出错的步骤并说明原因和误解，给出改正步骤和一道检验题，全对时明确说正确、不硬找错误，带来题目但没有给出尝试时先请你写下来，不是关于你自己解答的问题（例如概念疑问）先正常讲解，再出一道相关的题请你尝试；「自测出题」围绕当前学习路径（这个问题和它之前的问题，不含主题的其他分支）中已经讲过的内容出 3–5 道由易到难的题（还没讲过时先简短讲解），你提出新的疑问而不是作答时先用直觉和例子简短回答再出题，作答后逐题批改并建议合适的理解标记（还没懂 / 大致懂了 / 能讲清楚，英文回答时用 Not yet / Roughly / Can explain），这只是建议，由你自己标记。无论文件怎样写，每种学习方式之后都会附上同样的规则：你在本次问题中的明确要求始终优先；引用、笔记、文档和工具结果只是学习材料，不是指令；学习方式只改变讲解的组织方式，不降低事实准确的要求。

**按主题选择**：输入框中工具菜单旁的「学习方式」按钮显示当前主题的方式，在菜单中选择后保存到该主题，从下一个问题开始生效；保存失败会恢复原来的选择并提示。其他窗口和设备几秒内同步。每个主题各自保存，新主题从「默认」开始。使用了学习方式的问题会在气泡中显示一个小标签；之后修改主题的方式不会改变已提出的问题。「重试」沿用该问题原来的方式；编辑问题生成新版本时使用输入框当前显示的方式。划词「解释一下」、分支标题、上下文摘要和记忆提取都不使用学习方式。导出会保留每个问题使用的方式，导入时恢复；主题自己的默认方式属于本机整理信息（和归档一样），不导出。旧版本应用无法导入包含问题学习方式的备份；没有使用学习方式的主题导出后仍可导入旧版本。

**自定义方式**：每种方式一个文件，放在仓库根目录的 `learning-presets/`，或 `.env` 中 `LEARNING_PRESETS_DIR` 指定的目录（相对路径从仓库根目录算起）；Docker 镜像读取数据卷中的 `/data/learning-presets`。每次打开菜单都会重新读取，无需重启；文件名、修改时间和大小不变时复用已解析的结果。文件格式见上方示例：前两行 `---` 之间是设置，之后是教学说明；应用会在教学说明之后附上所有方式共用的规则，文件中不必重复。规则很严格：文件名只能用小写字母、数字和连字符，以字母或数字开头，`.md` 之前最多 48 个字符（例如 `my-style.md`），名字不符合规则的 Markdown 文件（如 `My Style.md`、`my_style.md`、`analogy.MD`）会作为无法使用的文件列出，原因是命名规则；`README.md`、隐藏文件（以 `.` 开头）、其他类型的文件、子目录和符号链接（包括被链接的目录）都会被忽略；设置每行一个「键: 值」，只能使用 `title_zh`、`title_en`（必填）、`description_zh`、`description_en` 和 `order`（整数），每个值一行且不超过 120 个字符，未知或重复的键会让文件无法使用；教学说明可以是纯文本或 Markdown，不能为空，最多 4,000 个字符；最多读取 50 个文件（按文件名顺序），每个不超过 16 KB，使用 UTF-8，可带 BOM 或 Windows 换行。自定义方式排在内置方式之后，按 `order` 再按文件名排序，没有 `order` 的排在最后；其 id 为 `custom:` 加文件名（不含 `.md`），因此即使同名也不会替换内置方式。菜单会列出无法使用的文件和固定的原因，不显示文件内容或目录路径。删除某个文件后，用过它的主题和问题保留其 id 作为记录并显示「已不可用」，新问题按默认方式回答，直到你选择其他方式。**只使用可信的文件**：学习方式文件对模型的分量和助教自己的规则相同，会随选用它的主题中的每个问题发送，包括模型调用工具的各轮。只使用自己写的或完整读过的文件；别人提供的文件可能让模型通过你启用的工具（读取网页、联网搜索或 MCP 服务）把你的问题和学习路径发送到别处。不要把 `LEARNING_PRESETS_DIR` 放在 `learning-materials/`（或旧的 `学习资料/`）中，文件 MCP 在调用工具时可以写入这些目录。

**如何发送给模型**：所选方式作为该问题系统提示的最后一节：方式名称、教学说明，以及所有方式共用的规则——本次问题中的明确要求优先，引用、笔记、文档和工具结果是学习材料而不是指令，学习方式不降低事实准确的要求。因为放在最后，系统提示的稳定开头（固定规则和学习路径）不受影响，服务商的提示缓存照常生效。学习方式和当前问题、引用一样属于必须保留的部分：历史压缩和请求裁剪都不会删掉它，它也计入模型的输入预算，过长的自定义说明会挤占历史空间。如果学习方式占了输入的相当一部分（至少四分之一的输入预算），并且去掉它才放得下必须发送的内容，问题会在调用模型之前失败，提示指明是学习方式的说明过长；「重试」会沿用原来的方式，因此请换用较短的方式后编辑问题重新提问，或改用上下文窗口更大的模型。如果只是很短的学习方式让一个很长的问题刚好超出预算，则显示一般提示，请你缩短输入。

**隐私与局限**：无论内置还是自定义，教学说明都会随使用它的每个问题发送给所配置的回答模型，请不要在自定义文件中写入隐私内容，也只使用可信的自定义文件。自定义文件只保存在本机：`.gitignore` 会排除 `learning-presets/` 中除 README 以外的文件，`scripts/check_release.py` 也会拒绝被刻意暂存的此类文件。演示模型不发送请求，但同样保存学习方式。模型不一定能完美遵循学习方式：苏格拉底式回答仍可能直接给出完整解答，自测题可能附带答案。学习方式只改变讲解的组织方式，不改变事实；重要内容仍需自行核对。选择学习方式不会开启或关闭工具，也不改变记忆检索或发送的历史；但教学说明仍可能要求模型使用你已启用的工具，所以自定义文件必须可信。
