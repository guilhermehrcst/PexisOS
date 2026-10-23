import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { App } from "../App";
import { assessMetric } from "../domain/evidence";
import { AS_OF, WINDOW_START, buildScenario } from "../domain/scenario";
import type { Sample } from "../domain/types";
import { MetricCard } from "./MetricCard";
import { ReportDialog } from "./ReportDialog";
import { Sparkline } from "./Sparkline";
import { TelemetryChart } from "./TelemetryChart";

/** UI regressions for the independent review of 2f5abbb (F5, F8, F9 and follow-ups). */

const ticks = (from: number, to: number, v: (t: number) => number | null): Sample[] => {
  const out: Sample[] = [];
  for (let t = from; t <= to; t += 5000) out.push({ t, v: v(t) });
  return out;
};

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("F5: the card uses the same deviation rule as the engine", () => {
  it("exactly 10% below baseline is not highlighted", () => {
    const rpm = { ...buildScenario("nominal").series.rpm, samples: ticks(WINDOW_START, AS_OF, () => 1620) };
    const { container } = render(<MetricCard series={rpm} assessment={assessMetric(rpm, AS_OF, 5000)} />);
    expect(container.querySelector(".metric__delta")).not.toHaveClass("is-significant");
  });
});

describe("F8: the demo nature of the report is visible, not only announced", () => {
  it("shows the no-AI note next to the call to action", () => {
    render(<App />);
    const note = screen.getByText(/No AI model is called/);
    expect(note).not.toHaveClass("sr-only");
    expect(screen.getByRole("button", { name: /Generate AI Report/ })).toHaveAccessibleDescription(/No AI model is called/);
  });
});

describe("F9: the chart cursor belongs to one series", () => {
  it("resets to the latest sample when the metric changes", () => {
    const scenario = buildScenario("rpm-deviation");
    const { rerender } = render(<TelemetryChart scenario={scenario} metric="rpm" onMetric={() => {}} />);
    const chart = screen.getByRole("img", { name: /Rotational Speed, 10:00 to 10:30 UTC/ });
    fireEvent.keyDown(chart, { key: "Home" });
    expect(chart).toHaveAccessibleName(/at 10:00:00/);
    rerender(<TelemetryChart scenario={scenario} metric="temperature" onMetric={() => {}} />);
    expect(screen.getByRole("img", { name: /Temperature, 10:00 to 10:30 UTC/ })).toHaveAccessibleName(/at 10:30:00/);
  });
});

describe("sparkline", () => {
  it("breaks the line at a data gap instead of bridging it", () => {
    const rpm = {
      ...buildScenario("nominal").series.rpm,
      samples: ticks(WINDOW_START, AS_OF, (t) => (t > WINDOW_START + 600_000 && t < WINDOW_START + 900_000 ? null : 1800)),
    };
    const { container } = render(<Sparkline series={rpm} />);
    const paths = container.querySelectorAll("path");
    expect(paths).toHaveLength(1); // no area fill across the gap
    expect(paths[0]?.getAttribute("d")?.match(/M/g)).toHaveLength(2);
  });
  it("a continuous series is one line with an area fill", () => {
    const { container } = render(<Sparkline series={buildScenario("nominal").series.rpm} />);
    const paths = container.querySelectorAll("path");
    expect(paths).toHaveLength(2);
    expect(paths[1]?.getAttribute("d")?.match(/M/g)).toHaveLength(1);
  });
});

describe("report download", () => {
  it("releases the object URL only after the click has been handled", () => {
    vi.useFakeTimers();
    const revoke = vi.fn();
    Object.assign(URL, { createObjectURL: vi.fn(() => "blob:report"), revokeObjectURL: revoke });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<ReportDialog report="# r" fileName="r.md" onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Download .md/ }));
    expect(revoke).not.toHaveBeenCalled();
    act(() => { vi.runAllTimers(); });
    expect(revoke).toHaveBeenCalledWith("blob:report");
  });
});

describe("PR #22 review: keyboard and landmarks", () => {
  it("Enter after Escape does not act on hidden search results", async () => {
    const user = userEvent.setup();
    render(<App />);
    const box = screen.getByRole("combobox", { name: "Search devices and evidence" });
    await user.type(box, "blad");
    await user.keyboard("{Escape}");
    expect(box).toHaveAttribute("aria-expanded", "false");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: /^Blades/ })).toHaveAttribute("aria-expanded", "false");
    await user.keyboard("{ArrowDown}");
    expect(box).toHaveAttribute("aria-expanded", "true");
  });

  it("a popover closes when keyboard focus leaves it", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: /Notifications: 1 item needs attention/ }));
    expect(screen.getByRole("region", { name: "Notifications" })).toBeInTheDocument();
    act(() => screen.getByRole("button", { name: /Generate AI Report/ }).focus());
    expect(screen.queryByRole("region", { name: "Notifications" })).not.toBeInTheDocument();
  });

  it("the main landmark contains the readings and the analysis", () => {
    render(<App />);
    const main = screen.getByRole("main");
    expect(within(main).getByRole("region", { name: "Telemetry readings" })).toBeInTheDocument();
    expect(within(main).getByRole("region", { name: "Analysis" })).toBeInTheDocument();
  });

  it("the insight actions are plain buttons, and the report returns focus to their trigger", async () => {
    const user = userEvent.setup();
    render(<App />);
    const trigger = screen.getByRole("button", { name: "Insight actions" });
    expect(trigger).not.toHaveAttribute("aria-haspopup");
    await user.click(trigger);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open full report" }));
    expect(screen.getByRole("dialog", { name: "Evidence report" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("search navigation does not animate the scroll for reduced-motion users", async () => {
    const user = userEvent.setup();
    const scroll = vi.fn();
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), media: q }) as MediaQueryList);
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    onTestFinished(() => { Element.prototype.scrollIntoView = original; });
    render(<App />);
    await user.type(screen.getByRole("combobox", { name: "Search devices and evidence" }), "blad");
    await user.keyboard("{Enter}");
    expect(scroll).toHaveBeenCalledWith({ behavior: "auto", block: "center" });
  });
});

describe("chart gaps", () => {
  it("a single missing tick breaks the line", () => {
    const base = buildScenario("nominal");
    const missing = WINDOW_START + 600_000;
    const scenario = { ...base, series: { ...base.series, rpm: { ...base.series.rpm, samples: ticks(WINDOW_START, AS_OF, (t) => (t === missing ? null : 1800)) } } };
    const { container } = render(<TelemetryChart scenario={scenario} metric="rpm" onMetric={() => {}} />);
    const line = container.querySelector(".chart__line");
    expect(line?.getAttribute("d")?.match(/M/g)).toHaveLength(2);
  });
});
