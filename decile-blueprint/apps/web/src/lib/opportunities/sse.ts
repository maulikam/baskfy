export interface SseMessage {
  event: string;
  data: string;
  id: string | null;
}

export interface SseParser {
  push(chunk: string): void;
  flush(): void;
}

export function createSseParser(onMessage: (message: SseMessage) => void): SseParser {
  let buffer = "";
  let event = "";
  let data: string[] = [];
  let id: string | null = null;

  function dispatch(): void {
    if (data.length > 0) {
      onMessage({ event: event || "message", data: data.join("\n"), id });
    }
    event = "";
    data = [];
  }

  function line(raw: string): void {
    if (raw === "") {
      dispatch();
      return;
    }
    if (raw.startsWith(":")) return;
    const colon = raw.indexOf(":");
    const field = colon === -1 ? raw : raw.slice(0, colon);
    let value = colon === -1 ? "" : raw.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id") id = value;
  }

  return {
    push(chunk: string) {
      buffer += chunk;
      let index = buffer.search(/\r\n|\r|\n/);
      while (index !== -1) {
        const isCrLf = buffer[index] === "\r" && buffer[index + 1] === "\n";
        if (buffer[index] === "\r" && index === buffer.length - 1) break;
        line(buffer.slice(0, index));
        buffer = buffer.slice(index + (isCrLf ? 2 : 1));
        index = buffer.search(/\r\n|\r|\n/);
      }
    },
    flush() {
      if (buffer.length > 0) {
        line(buffer.replace(/\r$/, ""));
        buffer = "";
      }
      dispatch();
    },
  };
}

export async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onMessage: (message: SseMessage) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = createSseParser(onMessage);
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.flush();
  } finally {
    reader.releaseLock();
  }
}
