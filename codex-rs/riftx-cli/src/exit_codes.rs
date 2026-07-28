use std::error::Error;
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub(crate) enum CliExitCode {
    Internal = 1,
    Config = 2,
    Daemon = 3,
    Request = 4,
    LocalIo = 5,
}

#[derive(Debug)]
struct ClassifiedError {
    code: CliExitCode,
    detail: String,
}

impl fmt::Display for ClassifiedError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.detail)
    }
}

impl Error for ClassifiedError {}

pub(crate) trait WithExitCode<T> {
    fn with_exit_code(self, code: CliExitCode) -> anyhow::Result<T>;
}

impl<T, E> WithExitCode<T> for Result<T, E>
where
    E: Into<anyhow::Error>,
{
    fn with_exit_code(self, code: CliExitCode) -> anyhow::Result<T> {
        self.map_err(|source| {
            let source: anyhow::Error = source.into();
            ClassifiedError {
                code,
                detail: format!("{source:#}"),
            }
            .into()
        })
    }
}

/// Map a CLI failure to the stable process exit code documented by RiftX.
pub fn exit_code_for_error(error: &anyhow::Error) -> u8 {
    if let Some(classified) = error
        .chain()
        .find_map(|cause| cause.downcast_ref::<ClassifiedError>())
    {
        return classified.code as u8;
    }
    if error
        .chain()
        .any(|cause| cause.downcast_ref::<std::io::Error>().is_some())
    {
        return CliExitCode::LocalIo as u8;
    }
    CliExitCode::Internal as u8
}
