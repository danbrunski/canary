import { cn, noticeClass, type NoticeState } from "../styles";

/** The standard ok/error/busy banner. Renders nothing when `notice` is
 *  null, so call sites drop their `{notice && …}` wrapper. */
export function Notice({ notice, className }: { notice: NoticeState | null; className?: string }) {
  if (!notice) return null;
  return <div className={cn(noticeClass(notice.kind), className)}>{notice.text}</div>;
}
