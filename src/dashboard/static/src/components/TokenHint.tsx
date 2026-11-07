/** Where the control token comes from. Shared by `TokenRequired` and the
 *  `TokenGate` popover so the two never disagree. */
export function TokenHint() {
  return (
    <>
      On the Pi, over SSH, run <code>canary-token</code> to print it, or pair this browser instead.
    </>
  );
}
