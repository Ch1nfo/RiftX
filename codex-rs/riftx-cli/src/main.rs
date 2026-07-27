#[tokio::main]
async fn main() -> anyhow::Result<()> {
    codex_riftx_cli::run_from(std::env::args_os()).await
}
