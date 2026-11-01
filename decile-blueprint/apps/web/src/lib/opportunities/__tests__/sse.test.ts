import { describe, expect, it } from "vitest";

import { createSseParser, readSseStream, type SseMessage } from "@/lib/opportunities/sse";

function collect(chunks: string[]): SseMessage[] {
  const out: SseMessage[] = [];
  const parser = createSseParser((m) => out.push(m));
  for (const chunk of chunks) parser.push(chunk);
  parser.flush();
  return out;
}

describe("createSseParser", () => {
  it("reads named events and defaults the name to message", () => {
    const out = collect(['event: progress\ndata: {"done":1}\n\n', 'data: {"type":"RUN_STARTED"}\n\n']);
    expect(out).toEqual([
      { event: "progress", data: '{"done":1}', id: null },
      { event: "message", data: '{"type":"RUN_STARTED"}', id: null },
    ]);
  });

  it("reassembles an event split across arbitrary chunk boundaries", () => {
    const whole = 'event: result\ndata: {"scan_id":"abc","n":12}\n\n';
    const chunks = whole.split("").reduce<string[]>((acc, ch, i) => {
      if (i % 3 === 0) acc.push(ch);
      else acc[acc.length - 1] += ch;
      return acc;
    }, []);
    expect(collect(chunks)).toEqual([{ event: "result", data: '{"scan_id":"abc","n":12}', id: null }]);
  });

  it("joins multi-line data, ignores comments and handles CRLF", () => {
    const out = collect([": keep-alive\r\n", "event: error\r\ndata: line1\r\n", "data: line2\r\n\r\n"]);
    expect(out).toEqual([{ event: "error", data: "line1\nline2", id: null }]);
  });

  it("does not split a CRLF that straddles two chunks into an extra blank line", () => {
    const out = collect(["data: a\r", "\ndata: b\r\n\r\n"]);
    expect(out).toEqual([{ event: "message", data: "a\nb", id: null }]);
  });

  it("dispatches a trailing event without a final blank line on flush", () => {
    expect(collect(["event: result\ndata: {}"])).toEqual([{ event: "result", data: "{}", id: null }]);
  });

  it("drops events that carry no data", () => {
    expect(collect(["event: progress\n\n"])).toEqual([]);
  });
});

describe("readSseStream", () => {
  it("decodes a byte stream whose multi-byte characters split across chunks", async () => {
    const bytes = new TextEncoder().encode('data: {"text":"₹5,000"}\n\n');
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 16));
        controller.enqueue(bytes.slice(16));
        controller.close();
      },
    });
    const out: SseMessage[] = [];
    await readSseStream(stream, (m) => out.push(m));
    expect(out).toEqual([{ event: "message", data: '{"text":"₹5,000"}', id: null }]);
  });
});
