import { Check, ChevronDown } from "lucide-react";
import { useCallback, useId, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { useDismiss } from "../hooks/useDismiss";
import "./Select.css";

export interface SelectOption<T extends string> {
  readonly value: T;
  readonly label: string;
  readonly hint?: string;
  readonly disabled?: boolean;
}

export interface SelectGroup<T extends string> {
  readonly label?: string;
  readonly options: readonly SelectOption<T>[];
}

interface SelectProps<T extends string> {
  readonly label: string; // accessible name
  readonly value: T;
  readonly groups: readonly SelectGroup<T>[];
  readonly onChange: (value: T) => void;
  readonly renderValue?: (option: SelectOption<T> | undefined) => ReactNode;
  readonly className?: string;
  readonly align?: "start" | "end";
  readonly footer?: ReactNode;
}

/** Accessible single-select dropdown (button + listbox, arrow keys, Enter, Escape). */
export function Select<T extends string>({ label, value, groups, onChange, renderValue, className, align = "start", footer }: SelectProps<T>) {
  const [open, setOpen] = useState(false);
  const all = groups.flatMap((g) => g.options);
  const enabled = all.filter((o) => !o.disabled);
  const [active, setActive] = useState<T>(value);
  const root = useRef<HTMLDivElement | null>(null);
  const button = useRef<HTMLButtonElement | null>(null);
  const listId = useId();
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, root, close);
  const current = all.find((o) => o.value === value);

  const choose = (v: T) => {
    onChange(v);
    setOpen(false);
    button.current?.focus();
  };
  const move = (delta: number) => {
    const i = enabled.findIndex((o) => o.value === active);
    const next = enabled[(i + delta + enabled.length) % enabled.length];
    if (next) setActive(next.value);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) { setActive(value); setOpen(true); return; }
      move(e.key === "ArrowDown" ? 1 : -1);
    } else if ((e.key === "Enter" || e.key === " ") && open) {
      e.preventDefault();
      if (enabled.some((o) => o.value === active)) choose(active);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div className={`select ${className ?? ""}`} ref={root}>
      <button
        ref={button}
        type="button"
        className="select__button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open ? `${listId}-${active}` : undefined}
        aria-label={`${label}: ${current?.label ?? value}`}
        onClick={() => { setActive(value); setOpen((o) => !o); }}
        onKeyDown={onKey}
      >
        {renderValue ? renderValue(current) : <span className="select__value">{current?.label}</span>}
        <ChevronDown className="select__chevron" size={16} strokeWidth={2.2} aria-hidden="true" />
      </button>
      {open && (
        <div className={`select__popover select__popover--${align}`}>
          <ul id={listId} role="listbox" aria-label={label}>
            {groups.map((g, gi) => (
              <li key={g.label ?? gi} role="presentation">
                {g.label && <div className="select__group" role="presentation">{g.label}</div>}
                <ul role="group" aria-label={g.label}>
                  {g.options.map((o) => (
                    <li
                      key={o.value}
                      id={`${listId}-${o.value}`}
                      role="option"
                      aria-selected={o.value === value}
                      aria-disabled={o.disabled || undefined}
                      className={`select__option${o.value === active ? " is-active" : ""}${o.disabled ? " is-disabled" : ""}`}
                      onMouseEnter={() => !o.disabled && setActive(o.value)}
                      onClick={() => !o.disabled && choose(o.value)}
                    >
                      <span className="select__option-text">
                        <span>{o.label}</span>
                        {o.hint && <span className="select__hint">{o.hint}</span>}
                      </span>
                      {o.value === value && <Check size={15} strokeWidth={2.4} aria-hidden="true" />}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
          {footer && <div className="select__footer">{footer}</div>}
        </div>
      )}
    </div>
  );
}
