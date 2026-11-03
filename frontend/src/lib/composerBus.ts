/**
 * Lets other parts of the page put text into the composer on screen (for
 * example the doll's quick phrases). The composer mounted last receives it.
 */
type Inserter = (text: string) => void;

const stack: Inserter[] = [];

export const composerBus = {
  /** Returns false when no composer is on screen. */
  insert(text: string): boolean {
    const target = stack[stack.length - 1];
    if (!target) return false;
    target(text);
    return true;
  },
  register(inserter: Inserter): () => void {
    stack.push(inserter);
    return () => {
      const i = stack.lastIndexOf(inserter);
      if (i >= 0) stack.splice(i, 1);
    };
  },
};
