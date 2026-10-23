import type { Snapshot } from "./formula-tokens";

/**
 * Undo/redo for the formula builder. Every edit is one undo step, except consecutive edits sharing a
 * `group` (typing the digits of one number), which merge into one. Moving the caret is not an edit
 * and ends a group.
 */
export interface History {
  past: Snapshot[];
  present: Snapshot;
  future: Snapshot[];
  group: string | null;
}

export type HistoryAction =
  /** `next` may be a function of the current snapshot, so callers never act on a stale one. */
  | { type: "edit"; next: Snapshot | ((present: Snapshot) => Snapshot); group?: string }
  | { type: "caret"; caret: number }
  | { type: "undo" }
  | { type: "redo" }
  | { type: "reset"; present: Snapshot };

const LIMIT = 200;

export const initHistory = (present: Snapshot): History => ({ past: [], present, future: [], group: null });

export function historyReducer(state: History, action: HistoryAction): History {
  switch (action.type) {
    case "edit": {
      const { group = null } = action;
      const next = typeof action.next === "function" ? action.next(state.present) : action.next;
      if (next.tokens === state.present.tokens) return { ...state, present: next, group: null };
      if (group !== null && group === state.group) return { ...state, present: next, future: [] };
      return { past: [...state.past, state.present].slice(-LIMIT), present: next, future: [], group };
    }
    case "caret": {
      const caret = Math.max(0, Math.min(action.caret, state.present.tokens.length));
      return { ...state, present: { ...state.present, caret }, group: null };
    }
    case "undo": {
      const prev = state.past[state.past.length - 1];
      if (!prev) return state;
      return { past: state.past.slice(0, -1), present: prev, future: [state.present, ...state.future], group: null };
    }
    case "redo": {
      const [next, ...future] = state.future;
      if (!next) return state;
      return { past: [...state.past, state.present], present: next, future, group: null };
    }
    case "reset":
      return initHistory(action.present);
  }
}
