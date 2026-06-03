/**
 * tools/demo-ui/web/src/pages/Feed.tsx
 * ─────────────────────────────────────
 * Full-page SSE event feed. The Overview page used to embed the feed in
 * a side column with a 220-px scroll cap; this page gives it the whole
 * viewport so an observer can watch a longer history without losing the
 * older events off the top of the list.
 *
 * Events come from the hoisted DemoStateContext — navigating away and back
 * doesn't reset the buffer (capped at FEED_MAX in the context).
 */

import { useDemo } from "../state/DemoStateContext";
import { CLR_MUTED, CLR_OK, channelColor } from "../utils";


export default function Feed() {
  const { feed, sseConnected } = useDemo();

  return (
    <div className="card" style={{ height: "100%" }}>
      <h2>Live Event Feed</h2>
      <div className="meta">
        <span style={{ color: sseConnected ? CLR_OK : CLR_MUTED }}>
          {sseConnected ? "● connected" : "○ connecting…"}
        </span>
        {feed.length > 0 && ` · ${feed.length} events`}
        <span className="muted" style={{ marginLeft: 12, fontSize: 11 }}>
          Channels: <span style={{ color: channelColor("smartload.routing") }}>routing</span> ·
          {" "}<span style={{ color: channelColor("smartload.anomaly") }}>anomaly</span> ·
          {" "}<span style={{ color: channelColor("smartload.policy") }}>policy</span>
        </span>
      </div>
      {feed.length === 0 ? (
        <div className="muted" style={{ fontStyle: "italic", padding: "16px 0", fontSize: 12 }}>
          {sseConnected ? "Waiting for events…" : "Connecting to event stream…"}
        </div>
      ) : (
        <div style={{
          display: "flex", flexDirection: "column", gap: 4, marginTop: 8,
        }}>
          {feed.map((item) => (
            <div key={item.id} style={{
              display: "flex", gap: 10, alignItems: "flex-start",
              fontSize: 12,
              borderLeft: `3px solid ${channelColor(item.channel)}`,
              paddingLeft: 8, paddingTop: 4, paddingBottom: 4,
            }}>
              <span style={{ color: CLR_MUTED, minWidth: 64, flexShrink: 0 }}>{item.ts}</span>
              <span style={{
                color: channelColor(item.channel), minWidth: 86, flexShrink: 0, fontWeight: 600,
              }}>
                {item.channel.replace("smartload.", "")}
              </span>
              <span style={{ color: "var(--text)", wordBreak: "break-all" }}>{item.summary}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
