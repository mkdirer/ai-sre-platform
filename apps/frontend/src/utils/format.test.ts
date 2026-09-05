import { describe, expect, it } from "vitest";
import { formatDuration, formatRelative, formatTime, severityRank } from "./format";

describe("format utils", () => {
  it("passes through unparseable timestamps", () => {
    expect(formatTime("not-a-date")).toBe("not-a-date");
    expect(formatRelative("not-a-date")).toBe("not-a-date");
    expect(formatDuration("not-a-date", "2026-09-06T12:05:00Z")).toBeNull();
  });

  it("formats relative ages with coarse buckets", () => {
    const now = new Date("2026-09-06T12:00:00Z").getTime();
    expect(formatRelative("2026-09-06T11:59:30Z", now)).toBe("30s ago");
    expect(formatRelative("2026-09-06T11:55:00Z", now)).toBe("5m ago");
    expect(formatRelative("2026-09-06T10:00:00Z", now)).toBe("2h ago");
    expect(formatRelative("2026-09-03T12:00:00Z", now)).toBe("3d ago");
  });

  it("formats durations compactly and rejects bad ranges", () => {
    expect(formatDuration("2026-09-06T12:00:00Z", "2026-09-06T12:00:00Z")).toBe("<1m");
    expect(formatDuration("2026-09-06T12:00:00Z", "2026-09-06T12:14:00Z")).toBe("14m");
    expect(formatDuration("2026-09-06T12:00:00Z", "2026-09-06T14:05:00Z")).toBe("2h 5m");
    expect(formatDuration("2026-09-06T12:00:00Z", null)).toBeNull();
    expect(formatDuration("2026-09-06T12:05:00Z", "2026-09-06T12:00:00Z")).toBeNull();
  });

  it("ranks known severities above unknown ones", () => {
    expect(severityRank("critical")).toBeLessThan(severityRank("warning"));
    expect(severityRank("warning")).toBeLessThan(severityRank("info"));
    expect(severityRank("mystery")).toBe(99);
  });
});
