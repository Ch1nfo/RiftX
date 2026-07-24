import { CirclePlus, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import type { Engagement, EngagementStatus } from "../models";

type Filter = "all" | EngagementStatus;

interface TaskSidebarProps {
  engagements: Engagement[];
  selectedId: string | null;
  loading: boolean;
  onSelect: (engagementId: string) => void;
  onCreate: () => void;
  onRefresh: () => void;
}

const filters: Array<{ value: Filter; label: string }> = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "draft", label: "Draft" },
];

export function TaskSidebar({
  engagements,
  selectedId,
  loading,
  onSelect,
  onCreate,
  onRefresh,
}: TaskSidebarProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const filtered = useMemo(
    () =>
      filter === "all"
        ? engagements
        : engagements.filter((engagement) => engagement.status === filter),
    [engagements, filter],
  );

  return (
    <aside className="task-sidebar" aria-label="Engagements">
      <div className="sidebar-heading">
        <h2>Tasks</h2>
        <button
          className="icon-button"
          type="button"
          title="Refresh tasks"
          aria-label="Refresh tasks"
          onClick={onRefresh}
          disabled={loading}
        >
          <RefreshCw size={16} className={loading ? "spin" : undefined} />
        </button>
      </div>
      <button className="new-task-button" type="button" onClick={onCreate}>
        <CirclePlus size={17} />
        New task
      </button>
      <div className="filter-tabs" role="tablist" aria-label="Task filter">
        {filters.map((item) => (
          <button
            key={item.value}
            type="button"
            role="tab"
            aria-selected={filter === item.value}
            className={filter === item.value ? "active" : undefined}
            onClick={() => setFilter(item.value)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div className="task-list">
        {filtered.map((engagement) => (
          <button
            className={`task-row${engagement.id === selectedId ? " selected" : ""}`}
            type="button"
            key={engagement.id}
            onClick={() => onSelect(engagement.id)}
          >
            <span className={`status-dot ${engagement.status}`} />
            <span className="task-row-copy">
              <strong>{engagement.name}</strong>
              <span>{engagement.objective.summary}</span>
            </span>
            <span className={`mode-label ${engagement.mode}`}>
              {engagement.mode}
            </span>
          </button>
        ))}
        {filtered.length === 0 && (
          <p className="empty-list">No tasks in this view.</p>
        )}
      </div>
    </aside>
  );
}
