export interface Tree { id: number; title: string; root_node_id: number }
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
export interface Message {
  id: number; role: 'user' | 'assistant'; content: string; answered_by: string | null;
  images?: string[] | null; reasoning?: string | null; steps?: ToolStep[] | null;
}
export interface NodeDetail extends LearningFields {
  id: number; tree_id: number; parent_id: number | null; title: string;
  seed_text: string | null; summary: string | null; messages: Message[];
}
export interface ThreadNode extends LearningFields {
  node_id: number; parent_id: number | null; title: string; seed_text: string | null;
  question: string | null; images: string[] | null; answer: string | null;
  reasoning: string | null; steps: ToolStep[] | null; answered_by: string | null;
  question_message_id?: number | null; answer_message_id?: number | null;
  attempts?: { message_id: number; status: string; content: string }[];
}
export type ModelProtocol = 'openai' | 'anthropic';
export interface ModelCfg {
  id: number; label: string; base_url: string; llm_model: string; key_hint: string; is_default: boolean;
  protocol: ModelProtocol; max_tokens: number;
}
export interface ModelInput { label: string; base_url: string; llm_model: string; api_key: string; is_default: boolean; protocol: ModelProtocol; max_tokens: number }
export interface McpServer { id: number; label: string; command: string; args: string[]; enabled: boolean }
export interface McpInput { label: string; command: string; args: string[]; enabled: boolean }
export interface Memory { id: number; kind: string; content: string; tree_id: number | null }
export interface MemoryRetrievalStatus {
  state: 'ready' | 'preparing' | 'degraded' | 'disabled';
  embedding_ready: boolean;
  reranker_ready: boolean;
}
