export interface Tree { id: number; title: string; root_node_id: number; archived: boolean }
export type NodeStatus = 'idle' | 'pending' | 'complete' | 'error' | 'interrupted';
export type TitleState = 'legacy' | 'manual' | 'empty' | 'pending' | 'ai' | 'fallback';
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
  revision_of?: number | null;
  request_id?: string | null;
}
export interface TreeNode extends LearningFields {
  id: number; tree_id: number; parent_id: number | null; title: string;
  seed_text: string | null; has_summary: boolean;
}
export interface ToolStep { tool: string; result: string | null }
/** Numbers only: how an answer's request was budgeted, what the provider reported and how long each phase took. */
export interface AnswerDiagnostics {
  estimated_input_tokens: number;
  actual_input_tokens: number | null;
  cached_input_tokens: number | null;
  output_tokens: number | null;
  compacted: boolean;
  tool_rounds: number;
  calibration_ratio: number;
  timings_ms: { retrieval: number; tools: number; summary: number; model: number; total: number };
}
/** A memory record an answer was given; ids and titles only, never memory text. */
export interface AnswerSource {
  memory_id: number;
  source_node_id: number | null;
  tree_id: number | null;
  tree_title: string;
  scope: 'path' | 'tree' | 'other';
}
/** One topic search result; message hits carry the message id so the map can jump to the passage. */
export interface SearchHit {
  node_id: number;
  message_id: number | null;
  kind: 'title' | 'message' | 'note' | 'memory';
  snippet: string;
  rank: number;
}
export interface RetrievalSettings { include_siblings: boolean; include_other_trees: boolean; revision: string }
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
}
export interface ModelInput { label: string; base_url: string; llm_model: string; api_key: string; is_default: boolean; protocol: ModelProtocol; max_tokens: number; context_window: number }
/** A context-window suggestion from the provider or the approximate built-in catalog; the form keeps the final say. */
export interface ModelDiscovery {
  context_window: number | null; max_output_tokens: number | null;
  source: 'provider' | 'catalog' | 'unknown'; note: string;
}
export interface McpServer { id: number; label: string; command: string; args: string[]; enabled: boolean }
export interface McpInput { label: string; command: string; args: string[]; enabled: boolean }
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
}
export interface MemoryRetrievalStatus {
  state: 'ready' | 'preparing' | 'degraded' | 'disabled' | 'not_installed';
  embedding_ready: boolean;
  reranker_ready: boolean;
}
