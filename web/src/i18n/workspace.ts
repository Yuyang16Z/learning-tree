import { getLocale, type Locale } from ".";
import type { McpServer } from "../types";

/** Translate known application diagnostics only; never run this over conversation text. */
const diagnostics: [string, string][] = [
  ["已归档，可在设置的「已归档」中查看和恢复。", "Archived. View or restore it from Archived in Settings."],
  ["已恢复到我的主题。", "Restored to My topics."],
  ["每条消息最多附加 4 个文档。", "Attach up to 4 documents per message."],
  ["文档不存在或不属于当前学习主题，请重新上传。", "This document is missing or belongs to another topic. Upload it again."],
  ["文档提取文本超过 200,000 字符，请拆分后上传。", "Extracted text exceeds 200,000 characters. Split the document before uploading."],
  ["文件内容不是有效的 PDF。", "This file is not a valid PDF."],
  ["暂不支持加密或密码保护的 PDF，请先解密。", "Encrypted or password-protected PDFs are not supported. Decrypt the file first."],
  ["PDF 超过 200 页，请拆分后上传。", "This PDF exceeds 200 pages. Split it before uploading."],
  ["PDF 页面内容过大，请简化或拆分后上传。", "A PDF page is too large to process. Simplify or split the file."],
  ["PDF 没有可提取的文字，可能是扫描件；暂不支持 OCR，请上传文字版。", "This PDF has no extractable text and may be scanned. OCR is not supported; upload a text-based version."],
  ["文件内容不是有效的 DOCX；旧版 .doc 请另存为 .docx。", "This file is not a valid DOCX. Save older .doc files as .docx first."],
  ["文件内容不是有效的 Word DOCX 文档。", "This file is not a valid Word DOCX document."],
  ["DOCX 解压后内容过大，请精简图片或拆分后上传。", "The expanded DOCX is too large. Reduce images or split the document."],
  ["暂不支持加密或密码保护的 Word 文档。", "Encrypted or password-protected Word documents are not supported."],
  ["DOCX 压缩比例异常，请重新保存或拆分后上传。", "The DOCX compression ratio is unusual. Save it again or split the document."],
  ["DOCX 包含不支持的 XML 声明，请另存文档后上传。", "The DOCX contains unsupported XML declarations. Save a new copy before uploading."],
  ["Word 文档没有可提取的段落或表格文字；图片中的文字暂不支持 OCR。", "This Word document has no extractable paragraph or table text. OCR for images is not supported."],
  ["仅提取 Word 段落与表格文字，内嵌图片未进行 OCR。", "Only Word paragraphs and tables were extracted. Embedded images were not processed with OCR."],
  ["暂不支持旧版 .doc，请在 Word 中另存为 .docx 或 PDF 后上传。", "Older .doc files are not supported. Save as .docx or PDF in Word before uploading."],
  ["支持 PDF、DOCX、TXT、Markdown、CSV、TSV、JSON 和 LOG 文件。", "Supported formats: PDF, DOCX, TXT, Markdown, CSV, TSV, JSON and LOG."],
  ["文件为空，请选择有内容的文档。", "The file is empty. Choose a document with content."],
  ["单个文档不能超过 10 MiB。", "Each document must be 10 MiB or smaller."],
  ["文本文件需使用 UTF-8 编码，请转换编码后上传。", "Text files must use UTF-8 encoding. Convert the file before uploading."],
  ["文件包含二进制内容，请上传 UTF-8 纯文本文件。", "The file contains binary data. Upload a UTF-8 plain-text file."],
  ["文档没有可提取的文字。", "The document has no extractable text."],
  ["文档已损坏或无法解析，请重新导出为 PDF、DOCX 或 UTF-8 文本。", "The document is damaged or cannot be parsed. Export it again as PDF, DOCX or UTF-8 text."],
  ["文档解析失败，请精简文档后重试。", "Document parsing failed. Simplify the document and try again."],
  ["文档解析超时，请拆分或重新导出文档后上传。", "Document parsing timed out. Split or re-export the document before uploading."],
  ["文档解析失败或资源消耗过大，请拆分后上传。", "Document parsing failed or used too many resources. Split it before uploading."],
  ["文档不存在。", "Document not found."],
  ["文档已用于对话，请保留来源记录；删除学习主题时会一并清理。", "This document is used in a conversation. Keep it as a source record; deleting the topic will remove it."],
  ["暂时无法解释，请重试。", "Could not explain this selection. Try again."],
  ["分支创建失败，请重试。", "Could not create the branch. Try again."],
  ["回答未完成，可在聊天记录中重试。", "The response is incomplete. You can retry it in the conversation."],
  ["发送失败，草稿已保留。", "Could not send. Your draft has been kept."],
  ["保存失败，理解草稿已保留。", "Could not save. Your reflection draft has been kept."],
  ["浏览器存储空间不足，当前草稿尚未保存。可移除附图后重试。", "Browser storage is full. This draft has not been saved. Remove attachments and try again."],
  ["连接测试失败。", "Connection test failed."],
  ["请填写显示名、模型 ID、API 地址和密钥。", "Enter a display name, model ID, API URL, and key."],
  ["API 地址需要以 https:// 或 http:// 开头。", "The API URL must start with https:// or http://."],
  ["最大输出长度应为 1–131072 之间的整数。", "Max output tokens must be an integer between 1 and 131072."],
  ["保存失败，请重试。", "Could not save. Please try again."],
  ["请填写显示名和启动命令。", "Enter a display name and launch command."],
  ["连接测试失败，请重试。", "Connection test failed. Please try again."],
  ["删除失败，请重试。", "Could not delete. Please try again."],
  ["启动参数格式有误，请填写 JSON 数组，例如 [\"mcp-server-fetch\"]。", "Invalid arguments. Enter a JSON array, for example [\"mcp-server-fetch\"]."],
  ["启动参数必须是字符串数组；无参数时填写 []。", "Arguments must be a string array. Use [] for no arguments."],
  ["请先停止当前回答，再删除。", "Stop the current response before deleting."],
  ["主题已永久删除。", "Topic permanently deleted."],
  ["回答已结束，刷新记录失败，请重新选择节点。", "The response ended, but the history could not be refreshed. Select the node again."],
  ["停止请求未确认，连接已断开；刷新后可查看状态。", "The stop request was not confirmed. The connection is closed; refresh to check its status."],
  ["分支已永久删除。", "Branch permanently deleted."],
  ["学习记录已导出。", "Learning history exported."],
  ["学习记录已导入为新主题。", "Learning records imported as a new topic."],
  ["备份文件不能超过 25 MB。", "The backup file must be 25 MB or smaller."],
  ["无法读取这个 JSON 备份。", "This JSON backup could not be read."],
  ["请选择小于 4 MB 的图片。", "Choose an image smaller than 4 MB."],
  ["这段草稿暂时保留在当前页面，请保存后再关闭。", "This draft is only available on this page. Save it before closing."],
  ["记忆不存在", "Memory not found"], ["模型不存在", "Model not found"],
  ["知识树不存在", "Learning tree not found"], ["节点不存在", "Node not found"],
  ["MCP server 不存在", "MCP server not found"],
  ["请先在设置中添加模型", "Add a model in Settings first"],
  ["备份超过 25 MB，请分成较小的主题", "The backup exceeds 25 MB. Split it into smaller topics."],
  ["备份格式无效：需要学习树版本 1 的学习树 JSON", "Invalid backup format. A LearningTree version 1 JSON backup is required."],
  ["备份包含重复 ID", "The backup contains duplicate IDs."],
  ["备份缺少根节点", "The backup has no root node."],
  ["消息引用了不存在的节点", "A message references a missing node."],
  ["备份图片必须是内嵌图片，每条消息最多 8 张", "Backup images must be embedded, with at most 8 images per message."],
  ["备份的父子关系不存在或形成循环", "The backup contains missing parent nodes or a cycle."],
  ["备份引用了不存在的来源节点", "The backup references a missing source node."],
  ["备份的来源消息不匹配", "A source message in the backup does not match its node."],
  ["备份的原文位置无效", "The backup contains invalid source text positions."],
  ["导入了未完成的回答，可重试。", "An incomplete response was imported. You can retry it."],
  ["编辑请使用 revise 创建新版本；旧回答和分支会保留。", "Use revision mode to create a new version. Existing answers and branches will be kept."],
  ["原文位置需要对应消息", "Source positions require a source message."],
  ["引用必须来自当前节点的 AI 回答", "The quote must come from an AI answer on this node."],
  ["原文位置无效", "Invalid source text positions."],
  ["原文位置超出回答", "The source positions extend beyond the answer."],
  ["解释未完成，请稍后重试。", "The explanation is incomplete. Try again later."],
  ["已停止，可重试。", "Stopped. You can retry."], ["此请求已停止", "This request was stopped."],
  ["该请求已保存，请定位节点后重试。", "This request is already saved. Open its node to retry."],
  ["这个节点正在生成", "A response is being generated on this node."],
  ["只有失败或停止的回答可以重试", "Only failed or stopped responses can be retried."],
  ["此节点没有可重试的问题", "This node has no question to retry."],
  ["修改问题请使用 revise，重试会沿用原问题", "Use revision mode to change the question. Retrying keeps the original question."],
  ["请输入问题", "Enter a question."], ["原问题不属于这个节点", "The original question does not belong to this node."],
  ["模型没有返回回答，请重试。", "The model returned no answer. Try again."],
  ["生成中断，可重试。", "Generation was interrupted. You can retry."],
  ["连接中断，可重试。", "The connection was interrupted. You can retry."],
  ["回答保存失败，请检查磁盘空间后重试。", "The response could not be saved. Check disk space and try again."],
  ["模型连接在回答完成前断开，可以重试。", "The model disconnected before completing the response. You can retry."],
  ["模型回答未完整结束，可以重试。", "The model response is incomplete. You can retry."],
  ["模型在工具调用结束后没有生成最终回答，请重试。", "The model did not produce a final answer after the tool rounds ended. You can retry."],
  ["连接中断，已保留问题，可重试。", "The connection was interrupted. Your question has been kept; you can retry."],
  ["没有收到回答数据，请重试。", "No response data was received. Try again."],
  ["mock 可用", "Demo model is ready"], ["连接成功", "Connected"],
  ["请求参数不匹配，请检查协议、模型名和输出设置。", "Request parameters do not match. Check the API format, model name, and output settings."],
  ["API Key 无效，请检查密钥。", "Invalid API key. Check the key."],
  ["当前密钥没有此模型的访问权限。", "This API key does not have access to the model."],
  ["服务地址或模型不存在，请检查地址、协议和模型名。", "The server or model was not found. Check the URL, API format, and model name."],
  ["调用额度或速率受限，请稍后重试。", "A usage or rate limit was reached. Try again later."],
  ["模型服务暂时不可用，请稍后重试。", "The model service is temporarily unavailable. Try again later."],
  ["配置格式无效，请检查服务地址和协议。", "Invalid configuration. Check the server URL and API format."],
  ["连接失败，请检查服务地址和网络后重试。", "Could not connect. Check the server URL and network, then try again."],
  ["MCP 连接超时，请检查启动命令和服务状态后重试。", "MCP connection timed out. Check the launch command and server status before retrying."],
  ["MCP 连接已关闭", "MCP connection closed"],
  ["MCP 请求超时；执行结果未确认，请先检查状态再决定是否重试。", "MCP request timed out; its outcome is unknown. Check its status before retrying."],
  ["MCP 连接已关闭；执行结果未确认。", "MCP connection closed; its outcome is unknown."],
  ["MCP 连接中断；执行结果未确认。", "MCP connection interrupted; its outcome is unknown."],
  ["MCP 工具清单返回了重复的分页游标", "The MCP tool list returned a duplicate pagination cursor."],
  ["MCP 工具清单超过 100 页", "The MCP tool list exceeds 100 pages."],
  ["MCP 连接启动超时", "MCP startup timed out"],
  ["该 MCP 正忙，请稍后再试", "This MCP server is busy. Try again later."],
  ["MCP 等待超时；执行结果未确认，请检查状态后再重试。", "Waiting for MCP timed out; its outcome is unknown. Check its status before retrying."],
  ["网络请求失败，请检查连接后重试。", "Network request failed. Check the connection and try again."],
];
const managedNames: Record<string, [string, string]> = {
  fetch: ["网页阅读", "Web reader"], filesystem: ["学习资料", "Learning files"],
  memory: ["知识记忆", "Knowledge memory"], "sequential-thinking": ["分步思考", "Step-by-step thinking"],
  playwright: ["浏览器", "Browser"], time: ["时间查询", "Time"],
};

export function mcpDisplayName(server: McpServer, locale: Locale = getLocale()): string {
  const managed = server.args?.[0]?.replace(/\\/g, "/").endsWith("/integrations/mcp/launch.py");
  const pair = managed ? managedNames[server.args[1]] : undefined;
  // An edited label is user content, even on a managed server.
  return pair && server.label === pair[0] ? pair[locale === "en" ? 1 : 0] : server.label;
}

export function localizeError(message: string, locale: Locale = getLocale()): string {
  const prefix = message.startsWith("Error: ") ? "Error: " : "";
  const detail = prefix ? message.slice(prefix.length) : message;
  const raw = detail === "Failed to fetch" || detail === "Load failed" ? "网络请求失败，请检查连接后重试。" : detail;
  const pair = diagnostics.find(([zh, en]) => raw === zh || raw === en);
  if (pair) return pair[locale === "en" ? 1 : 0];
  const documentPages = raw.match(/^第 (.+) 页没有可提取的文字（可能为空白页或扫描图片）；未进行 OCR。$/) ?? raw.match(/^Pages (.+) have no extractable text \(blank or scanned\); OCR was not performed\.$/);
  if (documentPages) return locale === "en" ? `Pages ${documentPages[1]} have no extractable text (blank or scanned); OCR was not performed.` : `第 ${documentPages[1]} 页没有可提取的文字（可能为空白页或扫描图片）；未进行 OCR。`;
  const http = raw.match(/^HTTP (\d+)[:：](.*)$/s);
  if (http) return `HTTP ${http[1]}: ${localizeError(http[2].trim(), locale)}`;
  const connected = raw.match(/^连上了，发现 (\d+) 个工具$/) ?? raw.match(/^Connected, (\d+) tools available$/);
  if (connected) return locale === "en" ? `Connected, ${connected[1]} tools available` : `连上了，发现 ${connected[1]} 个工具`;
  const incomplete = raw.match(/^回答未完成（(.+)），可重试。$/) ?? raw.match(/^Response incomplete \((.+)\)\. You can retry\.$/);
  if (incomplete) return locale === "en" ? `Response incomplete (${incomplete[1]}). You can retry.` : `回答未完成（${incomplete[1]}），可重试。`;
  const premature = raw.match(/^模型输出提前结束（(.+)），可以重试。$/) ?? raw.match(/^Model output ended early \((.+)\)\. You can retry\.$/);
  if (premature) return locale === "en" ? `Model output ended early (${premature[1]}). You can retry.` : `模型输出提前结束（${premature[1]}），可以重试。`;
  const failedMcp = raw.match(/^MCP「(.+)」连接失败，请在设置中测试连接。$/) ?? raw.match(/^Could not connect to MCP “(.+)”\. Test the connection in Settings\.$/);
  if (failedMcp) return locale === "en" ? `Could not connect to MCP “${failedMcp[1]}”. Test the connection in Settings.` : `MCP「${failedMcp[1]}」连接失败，请在设置中测试连接。`;
  const missingCommand = raw.match(/^未找到 MCP 启动命令：(.+)。请确认已经安装。$/) ?? raw.match(/^MCP launch command not found: (.+)\. Check that it is installed\.$/);
  if (missingCommand) return locale === "en" ? `MCP launch command not found: ${missingCommand[1]}. Check that it is installed.` : `未找到 MCP 启动命令：${missingCommand[1]}。请确认已经安装。`;
  const failedRequest = raw.match(/^请求失败（(\d+)），请重试。$/) ?? raw.match(/^Request failed \((\d+)\)\. Try again\.$/);
  if (failedRequest) return locale === "en" ? `Request failed (${failedRequest[1]}). Try again.` : `请求失败（${failedRequest[1]}），请重试。`;
  return message;
}
