import { forwardRef, type ComponentPropsWithoutRef } from "react";

import { cn, tableScrollClass } from "../styles";

type TableScrollProps = ComponentPropsWithoutRef<"div">;

/** The one horizontal-scroll box for a wide data table (P2.5). The table
 *  scrolls sideways inside it rather than widening the page, and below
 *  600px the first cell of every row stays pinned while the rest scrolls
 *  under it, so a row never loses its name. Wrap the `<table>` directly;
 *  the table keeps its own `min-w-*` for the width it needs. Extra classes
 *  (a border, a max height that makes it scroll vertically too) and div
 *  props (a virtualised table's `ref` and `onScroll`) pass through.
 *
 *  Focusable, so a keyboard user can scroll it with the arrow keys. */
export const TableScroll = forwardRef<HTMLDivElement, TableScrollProps>(function TableScroll(
  { className, children, ...rest },
  ref,
) {
  return (
    <div
      ref={ref}
      tabIndex={0}
      {...rest}
      className={cn(tableScrollClass, className)}
      data-table-scroll=""
    >
      {children}
    </div>
  );
});
