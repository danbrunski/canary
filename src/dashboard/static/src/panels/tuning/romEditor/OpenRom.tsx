import { useRef, type DragEvent, type ReactNode } from "react";

import { buttonClass, cn } from "../../../styles";

/** The editor's file entry point, the way any tool has one: a button that
 *  opens the browser's native file dialog (a plain file input, no `accept`
 *  filter, so an `.OsirisROM` or an odd extension is never hidden). What the
 *  file is, is the server's call: it detects the format from the bytes and
 *  refuses by name. Dropping a file on the area does the same. */
export function OpenRom({
  label,
  primary,
  disabled,
  dragging,
  onDraggingChange,
  onFile,
  children,
}: {
  label: string;
  primary: boolean;
  disabled: boolean;
  dragging: boolean;
  onDraggingChange: (dragging: boolean) => void;
  onFile: (file: File | undefined) => void;
  children?: ReactNode;
}) {
  const input = useRef<HTMLInputElement>(null);

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    onDraggingChange(false);
    if (!disabled) onFile(event.dataTransfer.files[0]);
  }

  return (
    <div
      role="group"
      aria-label="Open a ROM file"
      onDragOver={(event) => {
        event.preventDefault();
        onDraggingChange(true);
      }}
      onDragLeave={() => onDraggingChange(false)}
      onDrop={drop}
      className={cn(
        "flex flex-wrap items-center gap-3 rounded-lg border border-dashed p-3",
        dragging ? "border-accent-2 bg-accent-2/5" : "border-border",
        primary && "p-6",
      )}
    >
      <input
        ref={input}
        type="file"
        className="hidden"
        aria-label="ROM file"
        tabIndex={-1}
        disabled={disabled}
        onChange={(event) => {
          onFile(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
      <button
        type="button"
        className={cn(buttonClass, primary && "border-accent-2 px-6 py-3 text-base font-semibold")}
        disabled={disabled}
        onClick={() => input.current?.click()}
      >
        {label}
      </button>
      {children}
    </div>
  );
}
