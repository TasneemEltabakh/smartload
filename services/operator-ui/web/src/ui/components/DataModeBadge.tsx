/* ============================================================================
   DataModeBadge -- the calm live / demonstration indicator
   ----------------------------------------------------------------------------
   Replaces the alarming yellow "SAMPLE DATA" pill. Three intentional states:

     Live connected  -- mint, a steady (non-pulsing) LED. Real backend data.
     Connecting...   -- neutral, a soft pulse. Establishing the link.
     Demonstration   -- calm INFO tone (blue), never warning-yellow. Reads as a
                        deliberate, polished standalone mode -- not "broken".

   By default it reads the aggregate posture from the DataModeProvider, so the
   shell can drop a single <DataModeBadge/> into the Topbar. Props can override
   for a controlled placement.
   ============================================================================ */
import { useDataMode, type ConnectionState, type DataMode } from "../../lib/datamode";

export interface DataModeBadgeProps {
  /** Override the aggregate mode (otherwise read from DataModeProvider). */
  mode?: DataMode;
  /** Override the aggregate connection (otherwise read from DataModeProvider). */
  connection?: ConnectionState;
  className?: string;
}

interface Look {
  label: string;
  fg: string;
  bg: string;
  line: string;
  led: string;
  pulse: boolean;
  title: string;
}

function look(mode: DataMode, connection: ConnectionState): Look {
  if (mode === "live" && connection === "connected") {
    return {
      label: "Live connected",
      fg: "var(--sl-on-mint-tint)",
      bg: "var(--sl-mint-tint)",
      line: "var(--sl-mint-line)",
      led: "var(--sl-mint)",
      pulse: false,
      title: "Connected to the decision plane -- showing live data.",
    };
  }
  if (connection === "connecting") {
    return {
      label: "Connecting…",
      fg: "var(--sl-text-mid)",
      bg: "var(--sl-surface-sunk)",
      line: "var(--sl-hairline)",
      led: "var(--sl-text-low)",
      pulse: true,
      title: "Establishing a link to the decision plane…",
    };
  }
  // Offline / standalone -- intentional demonstration, calm info tone.
  return {
    label: "Demonstration",
    fg: "var(--sl-on-info-tint)",
    bg: "var(--sl-info-tint)",
    line: "var(--sl-info-line)",
    led: "var(--sl-info)",
    pulse: false,
    title: "Running on a representative dataset. Connect a backend for live data.",
  };
}

export function DataModeBadge({ mode, connection, className }: DataModeBadgeProps) {
  const ctx = useDataMode();
  const m = mode ?? ctx.mode;
  const c = connection ?? ctx.connection;
  const v = look(m, c);

  return (
    <span
      className={className}
      role="status"
      aria-live="polite"
      title={v.title}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        fontFamily: "var(--sl-font-mono)",
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.3px",
        color: v.fg,
        background: v.bg,
        border: `1px solid ${v.line}`,
        borderRadius: 20,
        padding: "5px 12px 5px 10px",
      }}
    >
      <span
        aria-hidden
        className={v.pulse ? "sl-pulse" : undefined}
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: v.led,
          flex: "0 0 auto",
        }}
      />
      {v.label}
    </span>
  );
}
