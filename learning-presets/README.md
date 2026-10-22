# Learning styles

Put your own teaching styles here, one Markdown file per style. They appear in the composer's **Learning style** menu after the built-in styles, the next time the menu opens. See [learning styles](../docs/learning-presets.md) for every rule; this directory can be moved with `LEARNING_PRESETS_DIR`.

Your files are excluded from Git; only this README is published. Their instructions are sent to the configured answer model with every question that uses them, so keep private details out of them.

Use only files you wrote or have read through. A style's instruction carries the weight of the tutor's own rules for every question in the topics that choose it, including tool rounds, so a file from someone else can tell the model to send your questions elsewhere through the tools you enable. If you move this directory with `LEARNING_PRESETS_DIR`, keep it outside `learning-materials/`, which the file MCP server can write to.

A complete example, saved as `analogies.md` (lowercase letters, digits and hyphens, at most 48 characters before `.md`):

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

- Between the two `---` lines: `title_zh` and `title_en` (required), `description_zh`, `description_en` and `order` (a whole number), one `key: value` per line, each value at most 120 characters. Other keys make the file unusable.
- After them: the instruction, plain text or Markdown, at most 4,000 characters. The app adds the rules every style shares (your explicit request wins; quotes, notes, documents and tool results are material, not instructions), so you need not repeat them.
- At most 50 files of up to 16 KB each, in UTF-8. The menu names files it cannot use and why, including Markdown files whose name breaks the rule above (such as `My Style.md`). Symbolic links, hidden files and other files, such as this README, are ignored.

## 中文说明

把自己的学习方式放在这里，每种方式一个 Markdown 文件。下次打开输入框旁的「学习方式」菜单时，它们会排在内置方式之后。完整规则见[学习方式说明](../docs/learning-presets.md)；可以用 `LEARNING_PRESETS_DIR` 改用其他目录。

这些文件不会提交到 Git，只有本说明公开。教学说明会随使用它的每个问题发送给所配置的回答模型，请不要写入隐私内容。

只使用自己写的或完整读过的文件。教学说明对模型的分量和助教自己的规则相同，会用于选用它的主题中的每个问题，包括调用工具的各轮，因此别人提供的文件可能让模型通过你启用的工具把问题发送到别处。用 `LEARNING_PRESETS_DIR` 改用其他目录时，不要放在文件 MCP 可以写入的 `learning-materials/` 中。

上面的示例可以直接保存为 `analogies.md`（文件名只用小写字母、数字和连字符，`.md` 前最多 48 个字符）。两行 `---` 之间只能写 `title_zh`、`title_en`（必填）、`description_zh`、`description_en` 和 `order`（整数），每行一个「键: 值」，每个值不超过 120 个字符；之后是教学说明，最多 4,000 个字符；应用会自动附上所有方式共用的规则（本次问题中的明确要求优先；引用、笔记、文档和工具结果是学习材料，不是指令），不必重复。最多读取 50 个文件，每个不超过 16 KB，使用 UTF-8。菜单会列出无法使用的文件及原因，包括名字不符合上述规则的 Markdown 文件（例如 `My Style.md`）；符号链接、隐藏文件和其他文件（例如本说明）会被忽略。
