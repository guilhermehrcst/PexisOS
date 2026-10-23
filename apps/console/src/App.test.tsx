import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App, scenarioFromSearch } from "./App";

const setup = (scenario?: Parameters<typeof App>[0]["initialScenario"]) => {
  const user = userEvent.setup();
  render(<App {...(scenario ? { initialScenario: scenario } : {})} />);
  return user;
};

// The scenario lives in the URL; reset it so tests do not leak state into each other.
beforeEach(() => window.history.replaceState(null, "", "/"));

describe("PexisOS Console", () => {
  it("renders the evidence diagnostic with values derived from synthetic data", () => {
    setup();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/evidence\s*diagnostic/i);
    const metrics = screen.getByRole("region", { name: "Telemetry readings" });
    for (const label of [/Rotational Speed: 1420 RPM/, /Temperature: 34 °C/, /Input Voltage: 12.1 V/, /Current Draw: 0.18 A/, /Vibration: 0.12 g/]) {
      expect(within(metrics).getByRole("article", { name: label })).toBeInTheDocument();
    }
    const rpmCard = within(metrics).getByRole("article", { name: /Rotational Speed/ });
    expect(within(rpmCard).getByText("\u221221%")).toBeInTheDocument();
    expect(screen.getByText(/Synthetic demonstration data/)).toBeInTheDocument();
  });

  it("reports evidence coverage as checks evaluated, not as an arbitrary progress figure", () => {
    setup();
    const bar = screen.getByRole("progressbar", { name: /evidence coverage/i });
    expect(bar).toHaveAttribute("aria-valuenow", "5");
    expect(bar).toHaveAttribute("aria-valuemax", "7");
    expect(screen.getByText("5 of 7 checks")).toBeInTheDocument();
  });

  it("shows subsystem health as a count with an explicit rule", () => {
    setup();
    expect(screen.getByRole("img", { name: "4 of 5 subsystems nominal" })).toBeInTheDocument();
    expect(screen.getByText(/not a health score/)).toBeInTheDocument();
  });

  it("opens a hotspot's evidence and labels inferred statuses", async () => {
    const user = setup();
    const motor = screen.getByRole("button", { name: /^Motor/ });
    await user.click(motor);
    expect(motor).toHaveAttribute("aria-expanded", "true");
    const detail = screen.getByRole("region", { name: "Motor evidence" });
    expect(within(detail).getByText("Inferred from other readings")).toBeInTheDocument();
    await user.click(motor);
    expect(screen.queryByRole("region", { name: "Motor evidence" })).not.toBeInTheDocument();
  });

  it("switching to a stale scenario withholds conclusions about the present", async () => {
    const user = setup();
    await user.click(screen.getByRole("combobox", { name: /Mode and scenario/ }));
    await user.click(screen.getByRole("option", { name: "Stale RPM feed" }));
    expect(screen.getByText("RPM data stale")).toBeInTheDocument();
    expect(screen.getByText(/stopped reporting 95 s ago/)).toBeInTheDocument();
    expect(screen.getByText(/cannot assess the current speed/)).toBeInTheDocument();
    expect(window.location.search).toBe("?scenario=stale-rpm");
  });

  it("an unavailable sensor shows no value and an explicit empty chart", async () => {
    const user = setup("vibration-unavailable");
    expect(screen.getByRole("article", { name: /Vibration: unavailable/ })).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: /Telemetry metric/ }));
    await user.click(screen.getByRole("option", { name: "Vibration" }));
    const chart = screen.getByRole("region", { name: /Fan Telemetry/ });
    expect(within(chart).getByRole("status")).toHaveTextContent(/sensor unavailable/i);
  });

  it("generates a deterministic report that says no AI model was called, and restores focus", async () => {
    const user = setup();
    const cta = screen.getByRole("button", { name: /Generate AI Report/ });
    await user.click(cta);
    const dialog = screen.getByRole("dialog", { name: "Evidence report" });
    expect(within(dialog).getByText(/deterministic template · no AI model was called/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/Report text/)).toHaveTextContent(/fan_rpm remains below the baseline for 210 s/);
    expect(within(dialog).getByRole("button", { name: "Close report" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(cta).toHaveFocus();
  });

  it("the chart tooltip follows the keyboard", async () => {
    setup();
    const chart = screen.getByRole("img", { name: /Rotational Speed, 10:00 to 10:30 UTC/ });
    expect(chart).toHaveAccessibleName(/1420 RPM at 10:30:00/);
    chart.focus();
    fireEvent.keyDown(chart, { key: "Home" });
    expect(chart).toHaveAccessibleName(/at 10:00:00/);
    fireEvent.keyDown(chart, { key: "ArrowRight" });
    expect(chart).toHaveAccessibleName(/at 10:00:05/);
  });

  it("search navigates to a component", async () => {
    const user = setup();
    const box = screen.getByRole("combobox", { name: "Search devices and evidence" });
    await user.type(box, "blad");
    expect(screen.getByRole("option", { name: /Blades/ })).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: /^Blades/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("navigation does not fake unavailable sections", () => {
    setup();
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(within(nav).getAllByRole("link")).toHaveLength(1);
    expect(within(nav).getByText("Reports").closest("[aria-disabled]")).toHaveAttribute("aria-disabled", "true");
  });

  it("view controls work and stop at their limits", async () => {
    const user = setup();
    const zoomIn = screen.getByRole("button", { name: "Zoom in" });
    for (let i = 0; i < 6; i++) await user.click(zoomIn);
    expect(zoomIn).toBeDisabled();
    const toggle = screen.getByRole("button", { name: "3D perspective view" });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("notifications list only subsystems that need attention", async () => {
    const user = setup();
    const bell = screen.getByRole("button", { name: /Notifications: 1 item needs attention/ });
    await user.click(bell);
    const panel = screen.getByRole("region", { name: "Notifications" });
    expect(within(panel).getByText("RPM Sensor")).toBeInTheDocument();
    await act(async () => { await user.keyboard("{Escape}"); });
    expect(screen.queryByRole("region", { name: "Notifications" })).not.toBeInTheDocument();
  });
});

describe("URL scenario parsing", () => {
  it("accepts known scenarios and fails closed on anything else", () => {
    expect(scenarioFromSearch("?scenario=nominal")).toBe("nominal");
    expect(scenarioFromSearch("?scenario=<script>")).toBe("rpm-deviation");
    expect(scenarioFromSearch("")).toBe("rpm-deviation");
  });
});
