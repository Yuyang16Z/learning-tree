# Documents in a conversation

[中文说明](#在对话中使用文档)

Use **Add documents** beside the question box to attach a document. Wait for parsing to finish, then ask a question about it. The question and its document cards stay together in the conversation; the cards let you preview extracted text or download the original file.

## Formats and limits

| Input | Behavior |
| --- | --- |
| PDF | Extracts an existing text layer; at most 200 pages. Scanned pages are not OCRed. |
| `.docx` | Extracts body paragraphs and tables in order. Legacy `.doc` files are not supported. |
| `.txt`, `.md`, `.csv`, `.tsv`, `.json`, `.log` | Reads UTF-8 textual content, with or without a byte-order mark. Tables and JSON are supplied as text, not executed. |
| File size | At most 10 MiB per file. |
| Question | At most four documents. Images have their own existing limit. |
| Extracted text | At most 200,000 characters per document; split larger inputs before uploading. Check any parsing warnings. |

Parsing does not preserve every aspect of a page's visual layout. A document with no readable text needs a text version or a separate OCR step before uploading. Encrypted or password-protected files must be decrypted first. Image attachments remain available for models that support image input.

## What the model receives

Uploading stores the original bytes and parsed text in the local SQLite database. Upload alone does not send the document to an answer-model provider. Sending a question attaches the selected document to that question and includes eligible document text in the configured model's context.

Documents follow the current learning path. A follow-up can use documents from its ancestors; an unrelated sibling branch does not automatically receive another branch's documents. Editing a question as a new version and retrying an interrupted answer preserve that question's document references. Removing an attachment from an unsent draft prevents that draft from sending it; it does not remove a document already attached to a saved question.

Long documents are not blindly concatenated into every request. The context budget includes selected excerpts, and a read-only source tool lets the agent page through eligible parsed originals when it needs more. A local token estimate and excerpt selection can miss relevant passages; neither parsing success nor a successful upload proves that the model has examined every page. Review the answer against the original when completeness matters.

Document contents are reference material, not permission to follow instructions found inside the file. Files are not executed. Selected text still leaves the computer when it is included in a request to the answer model you configured; local parsing does not make that later request local.

Tree exports containing documents use format version 2 and include original bytes, integrity hashes and message references. Import accepts versions 1 and 2, validates and reparses the originals, and remaps document references into the new tree. The existing 25 MiB JSON backup limit still applies; export reports an error if embedding the originals makes the backup too large. Use a private database backup for larger workspaces.

## Offline verification

`uv run python scripts/manage.py e2e` exercises attachment interactions using generated sample files, a temporary SQLite database, a real local backend and a Playwright browser. It uses the offline mock model and does not read personal documents, load semantic models or call a paid model. These checks verify upload and conversation behavior, not the model's comprehension or answer quality.

## 在对话中使用文档

点击输入框旁的**添加文档**，选择文档，等待解析完成后提问。发送后，文档卡片和问题一起保留，可预览解析文本或下载原文件。

- 支持带文本层的 PDF、`.docx`、UTF-8 编码的 `.txt`、`.md`、`.csv`、`.tsv`、`.json`、`.log`。Word 提取正文段落与表格；暂不支持旧版 `.doc`，不对扫描 PDF 做 OCR，加密文件需先解密。
- 每个文件最多 10 MiB，每个问题最多 4 份文档；PDF 最多 200 页，单份解析文本最多 20 万字符。图片仍使用原有的独立数量限制。
- 文件原文和解析文本保存在本机 SQLite 中。上传本身不向回答模型发请求；点击发送后，符合当前路径范围的文本才会随上下文传给所配置的回答模型。
- 同一路径的后续追问可以继续使用祖先问题的文档；其他兄弟分支的文档不会自动混入。重试、编辑为新版本保留对应问题的文档引用。移除待发送附件不会删除已发送问题中的文档。
- 长文档按上下文预算提供摘录，Agent 可按需分页回读解析原文。这不代表每一页都已被模型阅读，也不能保证摘录没有遗漏。
- 文档是参考资料，里面的指令不等于用户授权；文件不会作为程序执行。解析不保证还原所有视觉布局，无可读文本的扫描件需先另行 OCR 或转换成文本。
- 含文档的学习树按第 2 版格式导出，包含原文件、完整性校验值和消息引用；导入兼容旧版，校验并重新解析原文件、映射引用。JSON 备份仍有 25 MiB 限制，超限时会明确提示；更大的工作区可使用私人数据库备份。

浏览器自动检查只使用临时数据库、合成文档和模拟模型，验证附件与对话流程，不代表真实大模型的理解效果。
