export type PixelIconName =
  | "alert"
  | "check"
  | "chevron"
  | "evidence"
  | "file"
  | "graph"
  | "lock"
  | "message"
  | "node"
  | "pause"
  | "run"
  | "server"
  | "shield"
  | "stop"
  | "target"
  | "terminal"
  | "traffic"
  | "warning";

export function PixelIcon({
  name,
  className = "",
}: {
  name: PixelIconName;
  className?: string;
}) {
  return (
    <span
      className={`pixel-icon pixel-icon--${name}${className ? ` ${className}` : ""}`}
      aria-hidden="true"
    />
  );
}
