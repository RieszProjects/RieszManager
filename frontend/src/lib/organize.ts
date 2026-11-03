/** Visibility and ordering rules for projects and sessions in lists. */
import { useMemo } from "react";
import type { Project, SessionSummary } from "@/api/types";
import { useApp } from "@/store/app";
import { useOrganize } from "@/store/organize";
import { useUi } from "@/store/ui";

/**
 * Items with a manual position keep it; items without one (new since the last
 * reorder) come first, in the list's default order.
 */
export function applyOrder<T>(items: T[], key: (item: T) => string, positions: Record<string, number>, fallback: (a: T, b: T) => number): T[] {
  return [...items].sort((a, b) => {
    const pa = positions[key(a)];
    const pb = positions[key(b)];
    if (pa === undefined && pb === undefined) return fallback(a, b);
    if (pa === undefined) return -1;
    if (pb === undefined) return 1;
    return pa - pb;
  });
}

export const byActivity = (a: Project, b: Project) => (b.lastActivity ?? 0) - (a.lastActivity ?? 0);
export const byRecency = (a: SessionSummary, b: SessionSummary) => b.mtime - a.mtime;

/** Hooks returning stable predicates/sorters that follow the current archive settings. */
export function useOrganizeView() {
  const projects = useApp((s) => s.projects);
  const archived = useOrganize((s) => s.archived);
  const positions = useOrganize((s) => s.positions);
  const showArchived = useUi((s) => s.showArchived);

  return useMemo(() => {
    const projectById = new Map(projects.map((p) => [p.id, p]));
    const isProjectArchived = (id: string | null | undefined) => Boolean(id && archived.project.has(id));
    const isSessionArchived = (s: SessionSummary) => archived.session.has(s.sessionId) || isProjectArchived(s.project);

    const projectVisible = (p: Project) => p.exists && (showArchived || !archived.project.has(p.id));
    const sessionVisible = (s: SessionSummary) => {
      const project = s.project ? projectById.get(s.project) : undefined;
      if (project && !project.exists) return false; // its folder was deleted
      return showArchived || !isSessionArchived(s);
    };
    const orderProjects = (list: Project[]) => applyOrder(list, (p) => p.id, positions.project, byActivity);
    const orderSessions = (list: SessionSummary[]) => applyOrder(list, (s) => s.sessionId, positions.session, byRecency);

    return { projectVisible, sessionVisible, orderProjects, orderSessions, isProjectArchived, isSessionArchived, showArchived };
  }, [projects, archived, positions, showArchived]);
}
