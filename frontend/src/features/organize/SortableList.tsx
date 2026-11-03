/**
 * Vertical drag-to-reorder list.
 * Mouse: drag after moving a few pixels (plain clicks still work).
 * Touch: long-press, then drag (normal swipes keep scrolling).
 */
import {
  closestCenter, DndContext, KeyboardSensor, MouseSensor, TouchSensor, useSensor, useSensors, type DragEndEvent,
} from "@dnd-kit/core";
import { restrictToParentElement, restrictToVerticalAxis } from "@dnd-kit/modifiers";
import { arrayMove, SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { HTMLAttributes, ReactNode } from "react";

/** Set while a drag is in progress so other gesture handlers (swipe drawers) stand down. */
export const DRAGGING_CLASS = "rz-dragging";

export interface SortableRender {
  dragging: boolean;
  /** Spread onto the element that should start a drag (only passed in `handle` mode). */
  handleProps: HTMLAttributes<HTMLElement>;
}

interface Props<T> {
  items: T[];
  getId: (item: T) => string;
  onReorder: (ids: string[]) => void;
  children: (item: T, render: SortableRender) => ReactNode;
  disabled?: boolean;
  /** Only the element given `handleProps` starts a drag, instead of the whole item. */
  handle?: boolean;
}

function SortableItem({ id, disabled, handle, children }: {
  id: string; disabled?: boolean; handle?: boolean; children: (render: SortableRender) => ReactNode;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id, disabled });
  const dragProps = { ...attributes, ...listeners, tabIndex: -1 } as HTMLAttributes<HTMLElement>;
  return (
    <div
      ref={setNodeRef}
      className={`rz-sortable${isDragging ? " dragging" : ""}`}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      {...(handle ? {} : dragProps)}
    >
      {children({ dragging: isDragging, handleProps: handle ? dragProps : {} })}
    </div>
  );
}

export function SortableList<T>({ items, getId, onReorder, children, disabled, handle }: Props<T>) {
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 320, tolerance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const ids = items.map(getId);
  const end = (event: DragEndEvent) => {
    document.body.classList.remove(DRAGGING_CLASS);
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const from = ids.indexOf(String(active.id));
    const to = ids.indexOf(String(over.id));
    if (from >= 0 && to >= 0) onReorder(arrayMove(ids, from, to));
  };
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      modifiers={[restrictToVerticalAxis, restrictToParentElement]}
      onDragStart={() => {
        document.body.classList.add(DRAGGING_CLASS);
        navigator.vibrate?.(8);
      }}
      onDragEnd={end}
      onDragCancel={() => document.body.classList.remove(DRAGGING_CLASS)}
    >
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {items.map((item) => (
          <SortableItem key={getId(item)} id={getId(item)} disabled={disabled} handle={handle}>
            {(render) => children(item, render)}
          </SortableItem>
        ))}
      </SortableContext>
    </DndContext>
  );
}
