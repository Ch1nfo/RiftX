import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { setWorkingDirectory } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

const run = promisify(execFile);

async function chooseDirectory(language: "zh" | "en") {
  const title = language === "zh" ? "选择 RiftX 工作目录" : "Choose RiftX working directory";
  try {
    if (process.platform === "darwin") {
      const { stdout } = await run("osascript", ["-e", `POSIX path of (choose folder with prompt ${JSON.stringify(title)})`]);
      return stdout.trim();
    }
    if (process.platform === "win32") {
      const script = `Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description = '${title}'; if ($dialog.ShowDialog() -eq 'OK') { Write-Output $dialog.SelectedPath }`;
      const { stdout } = await run("powershell.exe", ["-NoProfile", "-Command", script]);
      return stdout.trim();
    }
    const { stdout } = await run("zenity", ["--file-selection", "--directory", `--title=${title}`]);
    return stdout.trim();
  } catch (error) {
    const detail = error as { code?: string | number; stderr?: string; message?: string };
    if (Number(detail.code) === 1 || `${detail.stderr ?? ""}${detail.message ?? ""}`.includes("-128")) return "";
    throw error;
  }
}

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({})) as { language?: unknown };
  try {
    const cwd = await chooseDirectory(body.language === "en" ? "en" : "zh");
    if (!cwd) return Response.json({ cancelled: true });
    return Response.json(await setWorkingDirectory(cwd));
  } catch (error) {
    return errorResponse(error, "Could not open the system folder picker");
  }
}
