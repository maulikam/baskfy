import type { OptionsAnalysis, SwingScanResponse } from "@/lib/opportunities/types";

export type AguiRole = "user" | "assistant";

export interface AguiInputMessage {
  id: string;
  role: AguiRole;
  content: string;
}

export interface RunAgentInput {
  threadId: string;
  runId: string;
  messages: AguiInputMessage[];
  tools: [];
  context: [];
  state: { symbol?: string };
  forwardedProps: Record<string, never>;
}

export type AguiEvent =
  | { type: "RUN_STARTED"; threadId: string; runId: string }
  | { type: "RUN_FINISHED"; threadId: string; runId: string }
  | { type: "RUN_ERROR"; message: string; code?: string | null }
  | { type: "STEP_STARTED"; stepName: string }
  | { type: "STEP_FINISHED"; stepName: string }
  | { type: "TEXT_MESSAGE_START"; messageId: string; role: string }
  | { type: "TEXT_MESSAGE_CONTENT"; messageId: string; delta: string }
  | { type: "TEXT_MESSAGE_END"; messageId: string }
  | { type: "TOOL_CALL_START"; toolCallId: string; toolCallName: string; parentMessageId?: string | null }
  | { type: "TOOL_CALL_ARGS"; toolCallId: string; delta: string }
  | { type: "TOOL_CALL_END"; toolCallId: string }
  | { type: "TOOL_CALL_RESULT"; messageId: string; toolCallId: string; content: string; role?: string }
  | { type: "STATE_SNAPSHOT"; snapshot: unknown };

const KNOWN_TYPES = new Set<string>([
  "RUN_STARTED",
  "RUN_FINISHED",
  "RUN_ERROR",
  "STEP_STARTED",
  "STEP_FINISHED",
  "TEXT_MESSAGE_START",
  "TEXT_MESSAGE_CONTENT",
  "TEXT_MESSAGE_END",
  "TOOL_CALL_START",
  "TOOL_CALL_ARGS",
  "TOOL_CALL_END",
  "TOOL_CALL_RESULT",
  "STATE_SNAPSHOT",
]);

export function parseAguiEvent(data: string): AguiEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data) as unknown;
  } catch (_error) {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const type = (parsed as { type?: unknown }).type;
  if (typeof type !== "string" || !KNOWN_TYPES.has(type)) return null;
  return parsed as AguiEvent;
}

export type ToolResult =
  | { tool: "scan_swing"; data: SwingScanResponse }
  | { tool: "compare_options"; data: OptionsAnalysis }
  | { tool: "unknown"; raw: string };

export type ThreadItem =
  | { kind: "message"; id: string; role: AguiRole; content: string; streaming: boolean }
  | {
      kind: "tool";
      id: string;
      name: string;
      args: string;
      done: boolean;
      result: ToolResult | null;
    };

export interface AskState {
  items: ThreadItem[];
  running: boolean;
  step: string | null;
  error: string | null;
}

export const EMPTY_ASK_STATE: AskState = { items: [], running: false, step: null, error: null };

export function decodeToolResult(name: string, content: string): ToolResult {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content) as unknown;
  } catch (_error) {
    return { tool: "unknown", raw: content };
  }
  if (typeof parsed !== "object" || parsed === null) return { tool: "unknown", raw: content };
  if (name === "scan_swing" && "opportunities" in parsed) {
    return { tool: "scan_swing", data: parsed as SwingScanResponse };
  }
  if (name === "compare_options" && "candidates" in parsed) {
    return { tool: "compare_options", data: parsed as OptionsAnalysis };
  }
  return { tool: "unknown", raw: content };
}

function updateItem(
  items: ThreadItem[],
  id: string,
  update: (item: ThreadItem) => ThreadItem,
): ThreadItem[] {
  return items.map((item) => (item.id === id ? update(item) : item));
}

export function reduceAgui(state: AskState, event: AguiEvent): AskState {
  switch (event.type) {
    case "RUN_STARTED":
      return { ...state, running: true, error: null };
    case "RUN_FINISHED":
      return { ...state, running: false, step: null };
    case "RUN_ERROR":
      return { ...state, running: false, step: null, error: event.message };
    case "STEP_STARTED":
      return { ...state, step: event.stepName };
    case "STEP_FINISHED":
      return { ...state, step: state.step === event.stepName ? null : state.step };
    case "TEXT_MESSAGE_START":
      if (state.items.some((item) => item.id === event.messageId)) return state;
      return {
        ...state,
        items: [
          ...state.items,
          { kind: "message", id: event.messageId, role: "assistant", content: "", streaming: true },
        ],
      };
    case "TEXT_MESSAGE_CONTENT": {
      const exists = state.items.some((item) => item.id === event.messageId);
      const items: ThreadItem[] = exists
        ? state.items
        : [
            ...state.items,
            { kind: "message", id: event.messageId, role: "assistant", content: "", streaming: true },
          ];
      return {
        ...state,
        items: updateItem(items, event.messageId, (item) =>
          item.kind === "message" ? { ...item, content: item.content + event.delta } : item,
        ),
      };
    }
    case "TEXT_MESSAGE_END":
      return {
        ...state,
        items: updateItem(state.items, event.messageId, (item) =>
          item.kind === "message" ? { ...item, streaming: false } : item,
        ),
      };
    case "TOOL_CALL_START":
      return {
        ...state,
        items: [
          ...state.items,
          {
            kind: "tool",
            id: event.toolCallId,
            name: event.toolCallName,
            args: "",
            done: false,
            result: null,
          },
        ],
      };
    case "TOOL_CALL_ARGS":
      return {
        ...state,
        items: updateItem(state.items, event.toolCallId, (item) =>
          item.kind === "tool" ? { ...item, args: item.args + event.delta } : item,
        ),
      };
    case "TOOL_CALL_END":
      return {
        ...state,
        items: updateItem(state.items, event.toolCallId, (item) =>
          item.kind === "tool" ? { ...item, done: true } : item,
        ),
      };
    case "TOOL_CALL_RESULT":
      return {
        ...state,
        items: updateItem(state.items, event.toolCallId, (item) =>
          item.kind === "tool"
            ? { ...item, done: true, result: decodeToolResult(item.name, event.content) }
            : item,
        ),
      };
    case "STATE_SNAPSHOT":
      return state;
  }
}

export function threadMessages(state: AskState): AguiInputMessage[] {
  const out: AguiInputMessage[] = [];
  for (const item of state.items) {
    if (item.kind === "message" && item.content.trim() !== "") {
      out.push({ id: item.id, role: item.role, content: item.content });
    }
  }
  return out;
}

export function buildRunInput(
  threadId: string,
  runId: string,
  messages: AguiInputMessage[],
  symbol?: string,
): RunAgentInput {
  const state = symbol ? { symbol: symbol.toUpperCase() } : {};
  return { threadId, runId, messages, tools: [], context: [], state, forwardedProps: {} };
}
