use crate::DiagnosticLevel;
use crate::DiscoveredTool;
use crate::TOOL_METADATA_SCHEMA_VERSION;
use crate::ToolDiagnostic;
use crate::ToolInventory;
use crate::ToolMetadata;
use crate::ToolScanConfig;
use crate::hex_digest;
use sha2::Digest;
use sha2::Sha256;
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::io::ErrorKind;
use std::path::Path;
use std::path::PathBuf;
use tokio::io::AsyncReadExt;

const MAX_METADATA_BYTES: u64 = 64 * 1024;

pub struct ToolScanner {
    config: ToolScanConfig,
}

impl ToolScanner {
    pub fn new(config: ToolScanConfig) -> Self {
        Self { config }
    }

    pub async fn scan(&self) -> ToolInventory {
        let mut diagnostics = Vec::new();
        let roots = self.effective_roots(&mut diagnostics);
        let mut path_entries = Vec::new();
        for root in &roots {
            discover_path_entries(root, &mut path_entries, &mut diagnostics).await;
        }
        for extra_path in &self.config.extra_paths {
            add_real_directory(extra_path, &mut path_entries, &mut diagnostics).await;
        }
        deduplicate_paths(&mut path_entries);

        let mut tools = Vec::new();
        for directory in &path_entries {
            scan_directory(directory, &mut tools, &mut diagnostics).await;
        }
        mark_shadowed_tools(&mut tools, &mut diagnostics);
        let snapshot_sha256 = inventory_digest(&tools);
        ToolInventory {
            roots,
            path_entries,
            tools,
            snapshot_sha256,
            diagnostics,
        }
    }

    fn effective_roots(&self, diagnostics: &mut Vec<ToolDiagnostic>) -> Vec<PathBuf> {
        if !self.config.directories.is_empty() {
            return self.config.directories.clone();
        }
        match default_tools_root() {
            Some(root) => vec![root],
            None => {
                diagnostics.push(diagnostic(
                    DiagnosticLevel::Error,
                    "defaultRootUnavailable",
                    None,
                    "the platform tools directory could not be determined",
                ));
                Vec::new()
            }
        }
    }
}

pub fn default_tools_root() -> Option<PathBuf> {
    #[cfg(target_os = "macos")]
    {
        return dirs::data_dir().map(|path| path.join("RiftX").join("tools"));
    }
    #[cfg(target_os = "windows")]
    {
        return dirs::data_local_dir().map(|path| path.join("RiftX").join("tools"));
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        return dirs::data_dir().map(|path| path.join("riftx").join("tools"));
    }
    #[allow(unreachable_code)]
    None
}

async fn discover_path_entries(
    root: &Path,
    path_entries: &mut Vec<PathBuf>,
    diagnostics: &mut Vec<ToolDiagnostic>,
) {
    if !add_real_directory(root, path_entries, diagnostics).await {
        return;
    }
    let mut children = read_directory(root, diagnostics).await;
    children.sort();
    for child in children {
        let Ok(metadata) = tokio::fs::symlink_metadata(&child).await else {
            continue;
        };
        if metadata.file_type().is_symlink() {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Warning,
                "symlinkSkipped",
                Some(child),
                "symbolic-link tool directories are not scanned",
            ));
            continue;
        }
        if !metadata.is_dir() {
            continue;
        }
        path_entries.push(child.clone());
        let bin = child.join("bin");
        add_real_directory(&bin, path_entries, diagnostics).await;
    }
}

async fn add_real_directory(
    path: &Path,
    path_entries: &mut Vec<PathBuf>,
    diagnostics: &mut Vec<ToolDiagnostic>,
) -> bool {
    match tokio::fs::symlink_metadata(path).await {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {
            path_entries.push(path.to_path_buf());
            true
        }
        Ok(_) => {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Warning,
                "directorySkipped",
                Some(path.to_path_buf()),
                "tool path is not a real directory",
            ));
            false
        }
        Err(error) if error.kind() == ErrorKind::NotFound => {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Info,
                "directoryMissing",
                Some(path.to_path_buf()),
                "tool directory does not exist",
            ));
            false
        }
        Err(error) => {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Error,
                "directoryUnreadable",
                Some(path.to_path_buf()),
                &error.to_string(),
            ));
            false
        }
    }
}

async fn read_directory(path: &Path, diagnostics: &mut Vec<ToolDiagnostic>) -> Vec<PathBuf> {
    let mut entries = Vec::new();
    let mut directory = match tokio::fs::read_dir(path).await {
        Ok(directory) => directory,
        Err(error) => {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Error,
                "directoryUnreadable",
                Some(path.to_path_buf()),
                &error.to_string(),
            ));
            return entries;
        }
    };
    loop {
        match directory.next_entry().await {
            Ok(Some(entry)) => entries.push(entry.path()),
            Ok(None) => return entries,
            Err(error) => {
                diagnostics.push(diagnostic(
                    DiagnosticLevel::Error,
                    "directoryUnreadable",
                    Some(path.to_path_buf()),
                    &error.to_string(),
                ));
                return entries;
            }
        }
    }
}

async fn scan_directory(
    directory: &Path,
    tools: &mut Vec<DiscoveredTool>,
    diagnostics: &mut Vec<ToolDiagnostic>,
) {
    let mut candidates = read_directory(directory, diagnostics).await;
    candidates.sort();
    for path in candidates {
        if tokio::fs::symlink_metadata(&path)
            .await
            .is_ok_and(|metadata| metadata.file_type().is_symlink())
        {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Warning,
                "symlinkSkipped",
                Some(path),
                "symbolic-link tools are not discovered",
            ));
            continue;
        }
        if is_metadata_path(&path) || !is_executable_file(&path).await {
            continue;
        }
        let Some(name) = tool_name(&path) else {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Warning,
                "nonUtf8Name",
                Some(path),
                "tool filename is not valid UTF-8",
            ));
            continue;
        };
        let sha256 = match hash_file(&path).await {
            Ok(hash) => hash,
            Err(error) => {
                diagnostics.push(diagnostic(
                    DiagnosticLevel::Error,
                    "toolUnreadable",
                    Some(path),
                    &error.to_string(),
                ));
                continue;
            }
        };
        let (metadata_path, metadata_sha256, metadata) =
            read_tool_metadata(&path, diagnostics).await;
        tools.push(DiscoveredTool {
            name,
            path,
            sha256,
            metadata_path,
            metadata_sha256,
            metadata,
            shadowed_by: None,
        });
    }
}

async fn is_executable_file(path: &Path) -> bool {
    let Ok(metadata) = tokio::fs::symlink_metadata(path).await else {
        return false;
    };
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(windows)]
    {
        let Some(extension) = path.extension().and_then(|value| value.to_str()) else {
            return false;
        };
        executable_extensions()
            .iter()
            .any(|candidate| candidate.eq_ignore_ascii_case(extension))
    }
}

#[cfg(windows)]
fn executable_extensions() -> Vec<String> {
    std::env::var("PATHEXT")
        .unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".to_string())
        .split(';')
        .filter_map(|extension| {
            let extension = extension.trim().trim_start_matches('.');
            (!extension.is_empty()).then(|| extension.to_string())
        })
        .collect()
}

fn tool_name(path: &Path) -> Option<String> {
    #[cfg(windows)]
    {
        path.file_stem()?.to_str().map(str::to_string)
    }
    #[cfg(unix)]
    {
        path.file_name()?.to_str().map(str::to_string)
    }
}

fn is_metadata_path(path: &Path) -> bool {
    path.file_name()
        .and_then(|value| value.to_str())
        .is_some_and(|name| name.ends_with(".riftx.toml"))
}

async fn read_tool_metadata(
    tool_path: &Path,
    diagnostics: &mut Vec<ToolDiagnostic>,
) -> (Option<PathBuf>, Option<String>, Option<ToolMetadata>) {
    let Some(file_name) = tool_path.file_name() else {
        return (None, None, None);
    };
    let mut metadata_name = file_name.to_os_string();
    metadata_name.push(".riftx.toml");
    let metadata_path = tool_path.with_file_name(metadata_name);
    let Ok(file_metadata) = tokio::fs::symlink_metadata(&metadata_path).await else {
        return (None, None, None);
    };
    if !file_metadata.is_file()
        || file_metadata.file_type().is_symlink()
        || file_metadata.len() > MAX_METADATA_BYTES
    {
        diagnostics.push(diagnostic(
            DiagnosticLevel::Error,
            "metadataRejected",
            Some(metadata_path),
            "metadata must be a regular file no larger than 64 KiB",
        ));
        return (None, None, None);
    }
    let bytes = match tokio::fs::read(&metadata_path).await {
        Ok(bytes) => bytes,
        Err(error) => {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Error,
                "metadataUnreadable",
                Some(metadata_path),
                &error.to_string(),
            ));
            return (None, None, None);
        }
    };
    let metadata_sha256 = hex_digest(Sha256::digest(&bytes));
    let parsed = std::str::from_utf8(&bytes)
        .ok()
        .and_then(|content| toml::from_str::<ToolMetadata>(content).ok());
    match parsed {
        Some(metadata) if metadata.schema_version != TOOL_METADATA_SCHEMA_VERSION => {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Error,
                "metadataSchemaUnsupported",
                Some(metadata_path),
                &format!(
                    "tool metadata schema version {} is unsupported; expected {}",
                    metadata.schema_version, TOOL_METADATA_SCHEMA_VERSION
                ),
            ));
            (None, None, None)
        }
        Some(metadata) if metadata.is_valid() => {
            (Some(metadata_path), Some(metadata_sha256), Some(metadata))
        }
        Some(_) | None => {
            diagnostics.push(diagnostic(
                DiagnosticLevel::Error,
                "metadataInvalid",
                Some(metadata_path),
                "metadata is not valid strict UTF-8 TOML",
            ));
            (None, None, None)
        }
    }
}

async fn hash_file(path: &Path) -> std::io::Result<String> {
    let mut file = tokio::fs::File::open(path).await?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer).await?;
        if count == 0 {
            return Ok(hex_digest(hasher.finalize()));
        }
        hasher.update(&buffer[..count]);
    }
}

fn mark_shadowed_tools(tools: &mut [DiscoveredTool], diagnostics: &mut Vec<ToolDiagnostic>) {
    let mut resolved = BTreeMap::<String, PathBuf>::new();
    for tool in tools {
        let key = normalized_tool_name(&tool.name);
        if let Some(selected) = resolved.get(&key) {
            tool.shadowed_by = Some(selected.clone());
            diagnostics.push(diagnostic(
                DiagnosticLevel::Warning,
                "toolShadowed",
                Some(tool.path.clone()),
                &format!(
                    "tool name {} resolves first to {}",
                    tool.name,
                    selected.display()
                ),
            ));
        } else {
            resolved.insert(key, tool.path.clone());
        }
    }
}

fn normalized_tool_name(name: &str) -> String {
    if cfg!(windows) {
        name.to_ascii_lowercase()
    } else {
        name.to_string()
    }
}

fn inventory_digest(tools: &[DiscoveredTool]) -> String {
    let mut hasher = Sha256::new();
    for tool in tools {
        hasher.update(tool.path.to_string_lossy().as_bytes());
        hasher.update([0]);
        hasher.update(tool.sha256.as_bytes());
        hasher.update([0]);
        if let Some(metadata_sha256) = &tool.metadata_sha256 {
            hasher.update(metadata_sha256.as_bytes());
        }
        hasher.update([0xff]);
    }
    hex_digest(hasher.finalize())
}

fn deduplicate_paths(paths: &mut Vec<PathBuf>) {
    let mut seen = BTreeSet::new();
    paths.retain(|path| seen.insert(path.clone()));
}

fn diagnostic(
    level: DiagnosticLevel,
    code: &str,
    path: Option<PathBuf>,
    message: &str,
) -> ToolDiagnostic {
    ToolDiagnostic {
        level,
        code: code.to_string(),
        path,
        message: message.to_string(),
    }
}
