import { useSyncExternalStore } from "react";

/**
 * Cross-component registry for "a blocking (application-modal) dialog is
 * currently open".
 *
 * The activation confirmation dialog in `studio-components.tsx` is rendered
 * through a portal into `document.body`, i.e. deliberately *outside* the
 * `.workbench-shell` subtree that `research-workbench.tsx` renders. That is
 * what lets the shell be marked `inert` without the dialog inerting itself.
 * But it also means the two components have no ancestor/descendant
 * relationship to pass this state through, while the shell owns behavior that
 * must be suppressed while such a dialog is open:
 *
 * - a `window` keydown listener implementing global shortcuts
 *   (`Ctrl`/`Cmd`+`K` opens the command palette, `Escape` closes the nav rail
 *   / palette). A global shortcut that opens a *second*
 *   modal from underneath the first defeats the first dialog's focus trap
 *   entirely: focus lands in a palette that is not inside the trap, is not
 *   inerted, and whose own Escape handling would close the wrong thing.
 * - the shell's own focusable content, which must be inert so keyboard and
 *   assistive-technology users cannot reach it behind the dialog.
 *
 * A module-level store keyed on a simple depth counter (rather than a boolean)
 * keeps the contract correct if two blocking dialogs are ever open at once:
 * the shell stays suppressed until the last one closes. `openBlockingModal`
 * returns an idempotent release function so a React effect can simply return
 * it as its cleanup.
 */

type Listener = () => void;

const listeners = new Set<Listener>();
let openCount = 0;

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

/** True while at least one blocking modal is registered as open. */
export function isBlockingModalOpen(): boolean {
  return openCount > 0;
}

/**
 * Register a blocking modal as open. Returns an idempotent release function;
 * calling it more than once (e.g. a double-invoked effect cleanup) will not
 * decrement the counter twice and prematurely un-suppress the shell.
 */
export function openBlockingModal(): () => void {
  openCount += 1;
  emit();
  let released = false;
  return () => {
    if (released) {
      return;
    }
    released = true;
    openCount -= 1;
    emit();
  };
}

export function subscribeBlockingModal(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Subscribe a component to blocking-modal state. The server snapshot is
 * always `false`: no modal can be open during server rendering, and returning
 * the live counter there would risk a hydration mismatch.
 */
export function useBlockingModalOpen(): boolean {
  return useSyncExternalStore(
    subscribeBlockingModal,
    isBlockingModalOpen,
    () => false,
  );
}
