/**
 * tools/demo-ui/web/src/pages/Feed.tsx  (cockpit "Stream")
 * ─────────────────────────────────────────────────────────
 * Full-page live decision-plane event stream, rebuilt on the shared kit in the
 * dark "Mission Control" theme. Reads like a console log: monospace rows,
 * channel-colored left rails, hard timestamps, newest-first.
 *
 * Events come from the hoisted DemoStateContext (`useDemo().feed`) — the same
 * SSE source the original page used. Navigating away and back doesn't reset the
 * buffer; it's capped at FEED_MAX in the context.
 *
 * Preserved from the original page:
 *   - Live SSE feed off useDemo (feed + sseConnected).
 *   - Channel color-coding via the shared channelColor() helper.
 *   - Per-event timestamp + summary.
 *   - Awaiting / connecting empty state when there are no events.
 *
 * Added (still console-grade, no new deps):
 *   - Per-channel filter chips (routing / anomaly / forecast / scale / policy)
 *     with live per-channel counts.
 *   - Auto-scroll lock with a pause toggle (newest-first, so "scroll" here means
 *     "jump to top on new events"); pausing freezes the rail at its current
 *     position so an observer can read older lines.
 *   - Click-to-expand event detail (raw channel + full summary).
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { Badge, Card, StatusPill } from "../ui";
import { useDemo } from "../state/DemoStateContext";
import { channelColor, type FeedItem } from "../utils";


/* The five decision-plane channels, in reading order. Labels are the bare
   suffix (the part after "smartload."). channelColor() is the source of truth
   for routing/anomaly/policy/scale; forecast isn't wired there yet, so it gets
   a local accent that stays in the mint/amber/blue family of the dark theme. */
const FORECAST_CLR = "#a78bfa"; // distinct from the four utils-mapped channels
const CHANNELS = [
  { key: "smartload.routing", label: "routing" },
  { key: "smartload.anomaly", label: "anomaly" },
  { key: "smartload.forecast", label: "forecast" },
  { key: "smartload.scale", label: "scale" },
  { key: "smartload.policy", label: "policy" },
] as const;

type ChannelKey = (typeof CHANNELS)[number]["key"];

/** Accent for a channel — defers to utils, with a local forecast fallback. */
function railColor(channel: string): string {
  if (channel === "smartload.forecast") return FORECAST_CLR;
  return channelColor(channel);
}

/** Bare suffix of a channel id ("smartload.routing" -> "routing"). */
function channelLabel(channel: string): string {
  return channel.replace("smartload.", "");
}


export default function Feed() {
  const { feed, sseConnected } = useDemo();

  // Filter set: which channels are visible. Empty = show everything (no filter).
  const [active, setActive] = useState<Set<ChannelKey>>(new Set());
  // Auto-scroll lock. When on, the rail snaps to the newest line (the top).
  const [autoScroll, setAutoScroll] = useState(true);
  // Expanded event ids (click a row to reveal its detail).
  const [open, setOpen] = useState<Set<string>>(new Set());

  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Per-channel counts over the full (unfiltered) buffer — drives the chips.
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const item of feed) c[item.channel] = (c[item.channel] ?? 0) + 1;
    return c;
  }, [feed]);

  // The rows actually rendered, after applying the channel filter.
  const visible: FeedItem[] = useMemo(() => {
    if (active.size === 0) return feed;
    return feed.filter((item) => active.has(item.channel as ChannelKey));
  }, [feed, active]);

  // Auto-scroll: feed is newest-first, so "follow" means pin to scrollTop 0.
  useEffect(() => {
    if (autoScroll && scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [visible, autoScroll]);

  function toggleChannel(key: ChannelKey) {
    setActive((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleOpen(id: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const streamStatus = sseConnected
    ? feed.length > 0
      ? "ok"
      : "neutral"
    : "warn";
  const streamLabel = sseConnected
    ? feed.length > 0
      ? "live"
      : "connected"
    : "connecting";

  return (
    <Card
      title="Stream"
      eyebrow="// live sse feed"
      flush
      style={{ display: "flex", flexDirection: "column", height: "100%" }}
      actions={
        <>
          <Badge tone="neutral">
            {visible.length}
            {active.size > 0 ? ` / ${feed.length}` : ""} events
          </Badge>
          <StatusPill status={streamStatus}>{streamLabel}</StatusPill>
        </>
      }
    >
      {/* ── Control bar: channel filters + auto-scroll lock ─────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 8,
          padding: "12px 18px",
          borderBottom: "1px solid var(--sl-hairline)",
          background: "var(--sl-surface-sunk)",
        }}
      >
        <span
          style={{
            fontFamily: "var(--sl-font-mono)",
            fontSize: 9.5,
            letterSpacing: "1.2px",
            textTransform: "uppercase",
            color: "var(--sl-text-low)",
            marginRight: 2,
          }}
        >
          channels
        </span>
        {CHANNELS.map((ch) => {
          const isActive = active.has(ch.key);
          const isFiltering = active.size > 0;
          const accent = railColor(ch.key);
          const n = counts[ch.key] ?? 0;
          // Visually mute chips that are filtered out while a filter is on.
          const dim = isFiltering && !isActive;
          return (
            <button
              key={ch.key}
              type="button"
              onClick={() => toggleChannel(ch.key)}
              aria-pressed={isActive}
              title={`${dim ? "show" : "filter"} ${ch.label} events`}
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 10.5,
                fontWeight: 600,
                letterSpacing: "0.4px",
                padding: "4px 9px",
                borderRadius: "var(--sl-radius-sm)",
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: 7,
                color: dim ? "var(--sl-text-low)" : accent,
                background: isActive ? "var(--sl-surface)" : "transparent",
                border: `1px solid ${isActive ? accent : "var(--sl-hairline)"}`,
                opacity: dim ? 0.55 : 1,
                transition: "opacity var(--sl-dur-fast), border-color var(--sl-dur-fast)",
              }}
            >
              <span
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: accent,
                  boxShadow: dim ? "none" : `0 0 7px ${accent}`,
                  flex: "0 0 auto",
                }}
              />
              {ch.label}
              <span style={{ color: "var(--sl-text-low)", fontWeight: 500 }}>{n}</span>
            </button>
          );
        })}

        <div style={{ flex: 1 }} />

        {active.size > 0 ? (
          <button
            type="button"
            onClick={() => setActive(new Set())}
            style={{
              fontFamily: "var(--sl-font-mono)",
              fontSize: 10.5,
              fontWeight: 600,
              padding: "4px 9px",
              borderRadius: "var(--sl-radius-sm)",
              cursor: "pointer",
              color: "var(--sl-text-mid)",
              background: "transparent",
              border: "1px solid var(--sl-hairline)",
            }}
          >
            clear
          </button>
        ) : null}

        <button
          type="button"
          onClick={() => setAutoScroll((v) => !v)}
          aria-pressed={autoScroll}
          title={autoScroll ? "pause auto-follow" : "resume auto-follow"}
          style={{
            fontFamily: "var(--sl-font-mono)",
            fontSize: 10.5,
            fontWeight: 600,
            letterSpacing: "0.4px",
            padding: "4px 10px",
            borderRadius: "var(--sl-radius-sm)",
            cursor: "pointer",
            display: "inline-flex",
            alignItems: "center",
            gap: 7,
            color: autoScroll ? "var(--sl-ok)" : "var(--sl-warn)",
            background: autoScroll ? "var(--sl-ok-tint)" : "var(--sl-warn-tint)",
            border: `1px solid ${autoScroll ? "var(--sl-mint-line)" : "var(--sl-warn)"}`,
          }}
        >
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: autoScroll ? "50%" : 1,
              background: autoScroll ? "var(--sl-ok)" : "var(--sl-warn)",
              boxShadow: `0 0 7px ${autoScroll ? "var(--sl-ok)" : "var(--sl-warn)"}`,
              flex: "0 0 auto",
            }}
          />
          {autoScroll ? "following" : "paused"}
        </button>
      </div>

      {/* ── The log surface ─────────────────────────────────────────────────── */}
      <div
        ref={scrollRef}
        style={{
          flex: 1,
          minHeight: 240,
          overflowY: "auto",
          background: "var(--sl-surface-sunk)",
        }}
      >
        {visible.length === 0 ? (
          <EmptyState
            sseConnected={sseConnected}
            filtered={feed.length > 0 && active.size > 0}
            onClear={() => setActive(new Set())}
          />
        ) : (
          <div style={{ display: "flex", flexDirection: "column" }}>
            {visible.map((item) => (
              <EventRow
                key={item.id}
                item={item}
                expanded={open.has(item.id)}
                onToggle={() => toggleOpen(item.id)}
              />
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}


/* ── A single log line ─────────────────────────────────────────────────────
   Mono row, channel-colored left rail, hard timestamp, summary. Click to
   expand the raw channel + full summary detail. */
function EventRow({
  item,
  expanded,
  onToggle,
}: {
  item: FeedItem;
  expanded: boolean;
  onToggle: () => void;
}) {
  const accent = railColor(item.channel);
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onToggle}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle();
        }
      }}
      style={{
        borderLeft: `3px solid ${accent}`,
        borderBottom: "1px solid var(--sl-hairline-soft)",
        padding: "8px 16px 8px 13px",
        cursor: "pointer",
        background: expanded ? "var(--sl-surface)" : "transparent",
        transition: "background var(--sl-dur-fast)",
      }}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "78px 88px 1fr",
          gap: 12,
          alignItems: "baseline",
          fontFamily: "var(--sl-font-mono)",
          fontSize: 12,
        }}
      >
        <span style={{ color: "var(--sl-text-low)", whiteSpace: "nowrap" }}>{item.ts}</span>
        <span style={{ color: accent, fontWeight: 600 }}>{channelLabel(item.channel)}</span>
        <span
          style={{
            color: "var(--sl-text)",
            wordBreak: "break-word",
            whiteSpace: expanded ? "normal" : "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {item.summary}
        </span>
      </div>

      {expanded ? (
        <div
          style={{
            marginTop: 8,
            paddingTop: 8,
            borderTop: "1px solid var(--sl-hairline-soft)",
            display: "grid",
            gridTemplateColumns: "auto 1fr",
            columnGap: 14,
            rowGap: 4,
            fontFamily: "var(--sl-font-mono)",
            fontSize: 11,
          }}
        >
          <DetailKey>channel</DetailKey>
          <span style={{ color: accent }}>{item.channel}</span>
          <DetailKey>time</DetailKey>
          <span style={{ color: "var(--sl-text-mid)" }}>{item.ts}</span>
          <DetailKey>summary</DetailKey>
          <span style={{ color: "var(--sl-text-mid)", wordBreak: "break-word" }}>
            {item.summary}
          </span>
        </div>
      ) : null}
    </div>
  );
}

function DetailKey({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        color: "var(--sl-text-low)",
        letterSpacing: "1px",
        textTransform: "uppercase",
        fontSize: 9.5,
      }}
    >
      {children}
    </span>
  );
}


/* ── Empty / awaiting state ─────────────────────────────────────────────────
   Three faces: BFF down (connecting), connected-but-quiet (awaiting events),
   and "your filter hid everything" (offer to clear). */
function EmptyState({
  sseConnected,
  filtered,
  onClear,
}: {
  sseConnected: boolean;
  filtered: boolean;
  onClear: () => void;
}) {
  let headline: string;
  let detail: string;
  if (filtered) {
    headline = "No events on the selected channels";
    detail = "Loosen the channel filter to see the rest of the stream.";
  } else if (sseConnected) {
    headline = "Awaiting events";
    detail = "The stream is live. Decisions appear here as the loop runs.";
  } else {
    headline = "Connecting to the event stream";
    detail = "No connection to the decision plane yet — retrying automatically.";
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        height: "100%",
        minHeight: 240,
        gap: 12,
        padding: 32,
        color: "var(--sl-text-low)",
      }}
    >
      <span
        style={{
          width: 10,
          height: 10,
          borderRadius: "50%",
          background: sseConnected ? "var(--sl-ok)" : "var(--sl-warn)",
          boxShadow: `0 0 12px ${sseConnected ? "var(--sl-ok)" : "var(--sl-warn)"}`,
        }}
      />
      <div style={{ fontSize: 14, fontWeight: 600, color: "var(--sl-text-mid)" }}>{headline}</div>
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 11.5,
          maxWidth: 360,
          lineHeight: 1.6,
        }}
      >
        {detail}
      </div>
      {filtered ? (
        <button
          type="button"
          onClick={onClear}
          style={{
            marginTop: 4,
            fontFamily: "var(--sl-font-mono)",
            fontSize: 11,
            fontWeight: 600,
            padding: "5px 12px",
            borderRadius: "var(--sl-radius-sm)",
            cursor: "pointer",
            color: "var(--sl-text)",
            background: "var(--sl-surface)",
            border: "1px solid var(--sl-hairline)",
          }}
        >
          clear filter
        </button>
      ) : null}
    </div>
  );
}
