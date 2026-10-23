import { Activity, BarChart3, Box, FileText, House, Settings } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import "./Sidebar.css";

interface Item {
  readonly id: string;
  readonly label: string;
  readonly icon: LucideIcon;
  readonly available: boolean;
}

const ITEMS: readonly Item[] = [
  { id: "overview", label: "Overview", icon: House, available: true },
  { id: "entities", label: "Entities", icon: Box, available: false },
  { id: "diagnostics", label: "Diagnostics", icon: Activity, available: false },
  { id: "evidence", label: "Evidence", icon: FileText, available: false },
  { id: "reports", label: "Reports", icon: BarChart3, available: false },
  { id: "settings", label: "Settings", icon: Settings, available: false },
];

/** Primary navigation. Only Overview exists in Console V1; the rest are marked unavailable, not faked. */
export function Sidebar() {
  return (
    <nav className="sidebar" aria-label="Primary">
      <ul>
        {ITEMS.map(({ id, label, icon: Icon, available }) => (
          <li key={id}>
            {available ? (
              <a className="sidebar__item is-active" href="#overview" aria-current="page">
                <Icon size={21} strokeWidth={2.2} aria-hidden="true" />
                <span>{label}</span>
              </a>
            ) : (
              <span className="sidebar__item" aria-disabled="true" title={`${label}: not part of Console V1`}>
                <Icon size={21} strokeWidth={2} aria-hidden="true" />
                <span>{label}</span>
                <span className="sr-only">(not available in this version)</span>
              </span>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
