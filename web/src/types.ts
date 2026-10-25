/** `learning_preset`: the learning style new questions in this topic use; null = the default way. */
export interface Tree { id: number; title: string; root_node_id: number; archived: boolean; learning_preset?: string | null }
export type NodeStatus = 'idle' | 'pending' | 'complete' | 'error' | 'interrupted';
export type TitleState = 'legacy' | 'manual' | 'empty' | 'pending' | 'ai' | 'fallback';
/** The learner's own mark on a node: not yet / roughly / can explain. Absent or null = not marked. */
export type Understanding = 'open' | 'partial' | 'clear';
export interface SourceAnchor {
  source_message_id?: number;
  source_start?: number;
  source_end?: number;
  learning_note?: string;
}
export interface NavigationAnchor {
  nodeId: number; messageId?: number; start?: number; end?: number; text?: string; nonce: number;
}
export interface LearningFields {
  title_state?: TitleState;
  kind?: 'root' | 'followup' | 'branch' | 'revision';
  status?: NodeStatus;
  error?: string | null;
  source_node_id?: number | null;
  source_message_id?: number | null;
  source_start?: number | null;
  source_end?: number | null;
  learning_note?: string | null;
  understanding?: Understanding | null;
  /** The learning style the question was asked with, as stored; null = the default way. */
  learning_preset?: string | null;
  revision_of?: number | null;
  request_id?: string | null;
}
export interface TreeNode extends LearningFields {
  id: number; tree_id: number; parent_id: number | null; title: string;
  seed_text: string | null; has_summary: boolean;
}
/**
 * One tool call of an answer and a preview of its result. A truncated preview may point at
 * the stored complete text: `call_index` within `request_id`, on the step's or the row's node.
 */
export interface ToolStep {
  tool: string; result: string | null;
  call_index?: number; truncated?: boolean; request_id?: string; node_id?: number;
  /** The learner's decision when the call needed approval (declined and expired calls did not run). */
  confirmation?: ToolConfirmationOutcome;
}
export type ToolConfirmationOutcome = 'allowed' | 'declined' | 'expired';
/** A tool call of the running answer that waits for the learner's approval. */
export interface ToolConfirmation { index: number; name: string; args: unknown }
/** One page of a stored complete tool result. */
export interface ToolResultPage {
  node_id: number; request_id: string; call_index: number; tool: string; truncated: boolean;
  text: string; start: number; end: number; total_chars: number; next_start: number | null;
}
/** One provider request's input: the app's estimate and, when reported, the provider's counts. */
export interface RequestInput {
  estimated_input_tokens: number;
  actual_input_tokens: number | null;
  cached_input_tokens: number | null;
}
/** One provider request of an answer in order, with what it reported and the tool calls it led to. */
export interface RoundTrace extends RequestInput {
  output_tokens: number | null;
  /** The request had to be trimmed to fit the hard budget. */
  trimmed: boolean;
  tool_calls: number;
  /** Running time of those calls; waits for approval are left out. */
  tool_ms: number;
}
/** Numbers only: how an answer's requests were budgeted, what the provider reported and how long each phase took. */
export interface AnswerDiagnostics {
  /** Summed over every provider request of the answer (each tool round and the final answer); a reported total is null when any request left that count out. */
  estimated_input_tokens: number;
  actual_input_tokens: number | null;
  cached_input_tokens: number | null;
  output_tokens: number | null;
  /** How many provider requests the totals add up; absent from diagnostics stored before it was recorded. */
  requests?: number;
  /** The request with the largest estimate: the context a single request carried. */
  largest_request?: RequestInput | null;
  /** Every provider request in order; absent from older diagnostics. */
  rounds?: RoundTrace[];
  compacted: boolean;
  /** Memory records that reached the prompt, counted when the answer was produced; the "Referenced" line shows those that still exist, so it can list fewer after a deletion. Absent from older diagnostics. */
  memory_count?: number;
  tool_rounds: number;
  /** The agent budget that ended the tool rounds early (time limit or tool-result size); null when none did. */
  tool_budget_stop?: 'time' | 'tokens' | null;
  calibration_ratio: number;
  /** The soft request target the answer was assembled for (configured or automatic); absent from older diagnostics. */
  target_input_tokens?: number;
  /** Model summaries of older history the prompt carried; absent from older diagnostics. */
  summary_blocks?: number;
  /** approval: time tool calls waited for the learner's decision; absent from older diagnostics. */
  timings_ms: { retrieval: number; tools: number; summary: number; approval?: number; model: number; total: number };
}
/** A memory record an answer was given; ids and titles only, never memory text. */
export interface AnswerSource {
  memory_id: number;
  source_node_id: number | null;
  tree_id: number | null;
  tree_title: string;
  scope: 'path' | 'tree' | 'other';
  /** The learner has since stopped this memory from being used in answers (read live). */
  muted?: boolean;
}
/**
 * One topic search result. Question (user message) and message (answer) hits carry the message id,
 * so the map can jump to the passage.
 */
export interface SearchHit {
  node_id: number;
  message_id: number | null;
  kind: 'title' | 'question' | 'message' | 'note' | 'memory';
  snippet: string;
  rank: number;
}
export interface RetrievalSettings { include_siblings: boolean; include_other_trees: boolean; revision: string }
export type ReturnExcerptCategory = 'correction' | 'condition' | 'definition' | 'question' | 'explanation';
/** What a branch added, extracted locally from its stored answers when returning to the source. */
export interface ReturnSummary {
  branch_node_id: number;
  branch_title: string;
  understanding: Understanding | null;
  note: string | null;
  excerpts: { category: ReturnExcerptCategory; text: string; node_id: number; message_id: number | null }[];
  source: { node_id: number; message_id: number | null; start: number | null; end: number | null; text: string | null };
}
/** A teaching style: built in, or a custom file whose id is `custom:<file name>`. */
export interface LearningPreset {
  id: string;
  source: 'builtin' | 'custom';
  title_zh: string; title_en: string;
  description_zh: string; description_en: string;
  instruction: string;
}
/** Usable styles in display order, and custom files that could not be used (a fixed reason each). */
export interface LearningPresetList {
  presets: LearningPreset[];
  errors: { file: string; reason: string }[];
}
export interface DocumentSummary {
  id: string; name: string; media_type: string; size: number; characters: number; warnings: string[];
}
export interface DocumentDetail extends DocumentSummary {
  sections: { label: string; text: string }[];
}
export interface Message {
  id: number; role: 'user' | 'assistant'; content: string; answered_by: string | null;
  images?: string[] | null; reasoning?: string | null; steps?: ToolStep[] | null;
  diagnostics?: AnswerDiagnostics | null;
  sources?: AnswerSource[] | null;
  documents?: DocumentSummary[];
}
export interface NodeDetail extends LearningFields {
  id: number; tree_id: number; parent_id: number | null; title: string;
  seed_text: string | null; summary: string | null; messages: Message[];
}
export interface ThreadNode extends LearningFields {
  node_id: number; parent_id: number | null; title: string; seed_text: string | null;
  question: string | null; images: string[] | null; answer: string | null;
  reasoning: string | null; steps: ToolStep[] | null; answered_by: string | null;
  sources?: AnswerSource[] | null;
  question_message_id?: number | null; answer_message_id?: number | null;
  documents?: DocumentSummary[];
  attempts?: { message_id: number; status: string; content: string }[];
  diagnostics?: AnswerDiagnostics | null;
}
export type ModelProtocol = 'openai' | 'anthropic';
export interface ModelCfg {
  id: number; label: string; base_url: string; llm_model: string; key_hint: string; is_default: boolean;
  protocol: ModelProtocol; max_tokens: number; context_window: number;
  /** Soft target for one request's input; null = automatic. */
  target_input_tokens: number | null;
  /** This model writes every answer's context summaries (at most one model does). */
  use_for_summaries: boolean;
}
export interface ModelInput { label: string; base_url: string; llm_model: string; api_key: string; is_default: boolean; protocol: ModelProtocol; max_tokens: number; context_window: number; target_input_tokens: number | null; use_for_summaries: boolean }
/** A context-window suggestion from the provider or the approximate built-in catalog; the form keeps the final say. */
export interface ModelDiscovery {
  context_window: number | null; max_output_tokens: number | null;
  source: 'provider' | 'catalog' | 'unknown'; note: string;
}
export type McpTransport = 'stdio' | 'http';
/** Which of a server's tool calls wait for approval: not marked read-only, every one, or none. */
export type McpConfirmPolicy = 'writes' | 'always' | 'never';
/** A configured MCP server. Remote (http) servers list their header names only, never values. */
export interface McpServer {
  id: number; label: string; command: string; args?: string[]; enabled: boolean;
  transport?: McpTransport; url?: string | null; header_names?: string[]; allow_private?: boolean;
  confirm_tools?: McpConfirmPolicy;
}
/** A blank header value keeps the stored value of that header; omitted headers keep every stored one. */
export interface McpInput {
  label: string; command: string; args: string[]; enabled: boolean;
  transport?: McpTransport; url?: string; headers?: Record<string, string>; allow_private?: boolean;
  confirm_tools?: McpConfirmPolicy;
}
export interface Memory { id: number; kind: string; content: string; tree_id: number | null }
export interface PreferenceProfile { content: string; revision: string; managed: boolean }
export interface PreferenceLearning { enabled: boolean; revision: string }
export interface PreferenceSupplement {
  id: number;
  content: string;
  scope: "global" | "topic";
  tree_id: number | null;
  source_node_id: number | null;
  source_tree_id: number | null;
  tree_title: string | null;
  source_tree_title: string | null;
  evidence: string;
  status: "active" | "pending";
  user_edited: boolean;
  revision: string;
  created_at: string;
  updated_at: string;
}
export interface PreferenceSupplementsPage {
  items: PreferenceSupplement[];
  total: number;
  page: number;
  page_size: number;
}
export type FactType = 'definition' | 'condition' | 'misconception' | 'open_question' | 'learner_state' | 'conclusion';
export interface MemoryFact extends Memory {
  source_node_id: number | null;
  tree_title: string | null;
  created_at: string;
  fact_type?: FactType | string;
  /** Id of the later fact that corrected this one; such facts no longer reach answers. */
  superseded_by?: number | null;
  /** The learner stopped this fact from being used in answers; it stays listed. */
  muted?: boolean;
}
export interface MemoryFactsPage {
  items: MemoryFact[];
  total: number;
  page: number;
  page_size: number;
  topics: { tree_id: number | null; title: string; count: number }[];
}
/** How a phone on the same Tailscale network can open this workspace. */
export interface PhoneAccess {
  state: 'not_installed' | 'offline' | 'not_served' | 'ready' | 'public';
  url?: string;
  host?: string | null;
  command?: string;
  /** The address plus "#token=…", given to this computer only; the QR code encodes it. */
  access_url?: string;
  /** Fixed message when the token cannot be stored. */
  access_error?: string;
  /** This computer manages the token (ACCESS_TOKEN_MODE=required), in any state. */
  can_rotate?: boolean;
  /** For pairing links to other addresses; this computer only, never while public. */
  pairing_token?: string;
  /** ALLOWED_HOSTS names, the only other hosts such a link may use besides *.ts.net. */
  pairing_hosts?: string[];
}
export interface MemoryRetrievalStatus {
  state: 'ready' | 'preparing' | 'degraded' | 'disabled' | 'not_installed';
  embedding_ready: boolean;
  reranker_ready: boolean;
}
