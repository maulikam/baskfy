import { describe, expect, it } from "vitest";

import {
  EMPTY_ASK_STATE,
  buildRunInput,
  parseAguiEvent,
  reduceAgui,
  threadMessages,
  type AguiEvent,
  type AskState,
} from "@/lib/opportunities/agui";

function run(events: AguiEvent[], start: AskState = EMPTY_ASK_STATE): AskState {
  return events.reduce(reduceAgui, start);
}

describe("parseAguiEvent", () => {
  it("accepts known event types and rejects junk", () => {
    expect(parseAguiEvent('{"type":"RUN_STARTED","threadId":"t","runId":"r"}')).toEqual({
      type: "RUN_STARTED",
      threadId: "t",
      runId: "r",
    });
    expect(parseAguiEvent("not json")).toBeNull();
    expect(parseAguiEvent('{"type":"SOMETHING_ELSE"}')).toBeNull();
    expect(parseAguiEvent("[1,2]")).toBeNull();
  });
});

describe("reduceAgui", () => {
  it("streams assistant text and closes the run", () => {
    const state = run([
      { type: "RUN_STARTED", threadId: "t", runId: "r" },
      { type: "TEXT_MESSAGE_START", messageId: "m1", role: "assistant" },
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "Found " },
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "3 setups." },
      { type: "TEXT_MESSAGE_END", messageId: "m1" },
      { type: "RUN_FINISHED", threadId: "t", runId: "r" },
    ]);
    expect(state.running).toBe(false);
    expect(state.items).toEqual([
      { kind: "message", id: "m1", role: "assistant", content: "Found 3 setups.", streaming: false },
    ]);
  });

  it("decodes a scan_swing tool result into structured data", () => {
    const result = { scan_id: "s1", opportunities: [], counts: {} };
    const state = run([
      { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "scan_swing", parentMessageId: "m1" },
      { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: '{"horizon_' },
      { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: 'max":15}' },
      { type: "TOOL_CALL_END", toolCallId: "c1" },
      { type: "TOOL_CALL_RESULT", messageId: "m2", toolCallId: "c1", content: JSON.stringify(result), role: "tool" },
    ]);
    const tool = state.items[0];
    expect(tool?.kind).toBe("tool");
    if (tool?.kind !== "tool") return;
    expect(tool.args).toBe('{"horizon_max":15}');
    expect(tool.result?.tool).toBe("scan_swing");
  });

  it("keeps an unparseable tool result as raw text instead of throwing", () => {
    const state = run([
      { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "compare_options" },
      { type: "TOOL_CALL_RESULT", messageId: "m2", toolCallId: "c1", content: "oops" },
    ]);
    const tool = state.items[0];
    expect(tool?.kind === "tool" ? tool.result : null).toEqual({ tool: "unknown", raw: "oops" });
  });

  it("records a RUN_ERROR message and stops running", () => {
    const state = run([
      { type: "RUN_STARTED", threadId: "t", runId: "r" },
      { type: "RUN_ERROR", message: "provider down", code: "UNAVAILABLE" },
    ]);
    expect(state).toMatchObject({ running: false, error: "provider down" });
  });
});

describe("threadMessages", () => {
  it("sends user and assistant text back, never tool payloads", () => {
    const start: AskState = {
      ...EMPTY_ASK_STATE,
      items: [{ kind: "message", id: "u1", role: "user", content: "Find setups", streaming: false }],
    };
    const state = run(
      [
        { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "scan_swing" },
        { type: "TEXT_MESSAGE_CONTENT", messageId: "a1", delta: "Here they are." },
      ],
      start,
    );
    const input = buildRunInput("t", "r2", threadMessages(state));
    expect(input.messages).toEqual([
      { id: "u1", role: "user", content: "Find setups" },
      { id: "a1", role: "assistant", content: "Here they are." },
    ]);
    expect(input).toMatchObject({ threadId: "t", runId: "r2", tools: [], context: [], state: {}, forwardedProps: {} });
  });
});
