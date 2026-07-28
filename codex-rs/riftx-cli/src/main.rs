use std::process::ExitCode;

#[tokio::main]
async fn main() -> ExitCode {
    match codex_riftx_cli::run_from(std::env::args_os()).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("RiftX: {error:#}");
            ExitCode::from(codex_riftx_cli::exit_code_for_error(&error))
        }
    }
}
