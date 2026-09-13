/**
 * Robust SSE reader: buffers across chunk boundaries and parses every
 * `data: {...}` event exactly once. (The old inline parser split raw chunks
 * on \n, which could drop or corrupt events that straddled network chunks.)
 */
/** One SSE payload from fetch-stream / refresh-stream (all fields optional —
 * events vary by status). */
export interface SseEvent {
  status?: string;
  channel_id?: number;
  title?: string;
  stage?: string;
  scanned?: number;
  found?: number;
  priced?: number;
  unpriceable?: number;
  immature?: number;
  total_calls?: number;
  message?: string;
  channels?: { channel_id: number; title: string }[];
}

export async function consumeSse(
  response: Response,
  onEvent: (data: SseEvent) => void,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            onEvent(JSON.parse(line.slice(6)));
          } catch {
            /* ignore malformed frames */
          }
        }
      }
    }
  }
}
