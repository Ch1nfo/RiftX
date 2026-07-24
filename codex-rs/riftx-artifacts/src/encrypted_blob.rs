use super::ArtifactError;
use codex_riftx_crypto::EngagementRecordCipher;
use sha2::Digest;
use sha2::Sha256;
use std::path::Path;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::task::Context;
use std::task::Poll;
use tokio::io::AsyncRead;
use tokio::io::AsyncReadExt;
use tokio::io::AsyncWriteExt;
use tokio::io::ReadBuf;
use uuid::Uuid;
use zeroize::Zeroizing;

const FORMAT_HEADER: &[u8; 4] = b"RXF1";
const RECORD_KIND: &str = "artifact_chunks";
const CHUNK_BYTES: usize = 64 * 1024;
const ENVELOPE_OVERHEAD_BYTES: usize = 4 + 12 + 16;
const MAX_ENVELOPE_BYTES: usize = CHUNK_BYTES + ENVELOPE_OVERHEAD_BYTES;

/// A verified temporary plaintext artifact that deletes itself when dropped.
pub struct DecryptedArtifact {
    file: Option<tokio::fs::File>,
    path: PathBuf,
}

impl AsyncRead for DecryptedArtifact {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        match self.file.as_mut() {
            Some(file) => Pin::new(file).poll_read(cx, buffer),
            None => Poll::Ready(Err(std::io::Error::other(
                "decrypted artifact file is closed",
            ))),
        }
    }
}

impl Drop for DecryptedArtifact {
    fn drop(&mut self) {
        drop(self.file.take());
        let _ = std::fs::remove_file(&self.path);
    }
}

pub(crate) async fn encrypt_source(
    cipher: Arc<dyn EngagementRecordCipher>,
    engagement_id: &str,
    digest: &str,
    source: &Path,
    destination: &Path,
    limit: u64,
) -> Result<(String, u64), ArtifactError> {
    let mut input = tokio::fs::File::open(source)
        .await
        .map_err(|source_error| ArtifactError::Io {
            path: source.to_path_buf(),
            source: source_error,
        })?;
    let mut output = private_output(destination).await?;
    output
        .write_all(FORMAT_HEADER)
        .await
        .map_err(|source| ArtifactError::Io {
            path: destination.to_path_buf(),
            source,
        })?;
    let mut hasher = Sha256::new();
    let mut size = 0_u64;
    let mut index = 0_u64;
    let mut buffer = Zeroizing::new(vec![0_u8; CHUNK_BYTES]);
    loop {
        let count = input
            .read(&mut buffer)
            .await
            .map_err(|error| ArtifactError::Io {
                path: source.to_path_buf(),
                source: error,
            })?;
        if count == 0 {
            output
                .write_all(&0_u32.to_be_bytes())
                .await
                .map_err(|source| ArtifactError::Io {
                    path: destination.to_path_buf(),
                    source,
                })?;
            output.flush().await.map_err(|source| ArtifactError::Io {
                path: destination.to_path_buf(),
                source,
            })?;
            return Ok((hex_digest(hasher.finalize()), size));
        }
        size = size.saturating_add(u64::try_from(count).unwrap_or(u64::MAX));
        if size > limit {
            return Err(ArtifactError::CapacityExceeded { limit });
        }
        hasher.update(&buffer[..count]);
        let cipher = cipher.clone();
        let engagement_id = engagement_id.to_string();
        let record_id = format!("{digest}:{index}");
        let plaintext = Zeroizing::new(buffer[..count].to_vec());
        let envelope = tokio::task::spawn_blocking(move || {
            cipher.seal_record(&engagement_id, RECORD_KIND, &record_id, &plaintext)
        })
        .await
        .map_err(|error| ArtifactError::CryptoTask(error.to_string()))??;
        let envelope_len =
            u32::try_from(envelope.len()).map_err(|_| ArtifactError::InvalidFormat)?;
        output
            .write_all(&envelope_len.to_be_bytes())
            .await
            .map_err(|source| ArtifactError::Io {
                path: destination.to_path_buf(),
                source,
            })?;
        output
            .write_all(&envelope)
            .await
            .map_err(|source| ArtifactError::Io {
                path: destination.to_path_buf(),
                source,
            })?;
        index = index
            .checked_add(/*rhs*/ 1)
            .ok_or(ArtifactError::InvalidFormat)?;
    }
}

pub(crate) async fn decrypt_to_temporary(
    cipher: Arc<dyn EngagementRecordCipher>,
    engagement_id: &str,
    digest: &str,
    source: &Path,
    temporary_root: &Path,
    expected_size: u64,
) -> Result<DecryptedArtifact, ArtifactError> {
    let temporary = temporary_root.join(format!(".export-{}", Uuid::new_v4()));
    let result = decrypt_file(
        cipher,
        engagement_id,
        digest,
        source,
        &temporary,
        expected_size,
    )
    .await;
    if let Err(error) = result {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error);
    }
    let file = match open_temporary(&temporary).await {
        Ok(file) => file,
        Err(source) => {
            let _ = tokio::fs::remove_file(&temporary).await;
            return Err(ArtifactError::Io {
                path: temporary,
                source,
            });
        }
    };
    #[cfg(unix)]
    tokio::fs::remove_file(&temporary)
        .await
        .map_err(|source| ArtifactError::Io {
            path: temporary.clone(),
            source,
        })?;
    Ok(DecryptedArtifact {
        file: Some(file),
        path: temporary,
    })
}

async fn decrypt_file(
    cipher: Arc<dyn EngagementRecordCipher>,
    engagement_id: &str,
    digest: &str,
    source: &Path,
    destination: &Path,
    expected_size: u64,
) -> Result<(), ArtifactError> {
    let mut input = tokio::fs::File::open(source)
        .await
        .map_err(|source_error| ArtifactError::Io {
            path: source.to_path_buf(),
            source: source_error,
        })?;
    let mut header = [0_u8; FORMAT_HEADER.len()];
    read_exact(&mut input, &mut header, source).await?;
    if &header != FORMAT_HEADER {
        return Err(ArtifactError::InvalidFormat);
    }
    let mut output = private_output(destination).await?;
    let mut hasher = Sha256::new();
    let mut size = 0_u64;
    let mut index = 0_u64;
    loop {
        let mut length = [0_u8; 4];
        read_exact(&mut input, &mut length, source).await?;
        let envelope_len = u32::from_be_bytes(length) as usize;
        if envelope_len == 0 {
            let mut trailing = [0_u8; 1];
            if input
                .read(&mut trailing)
                .await
                .map_err(|error| ArtifactError::Io {
                    path: source.to_path_buf(),
                    source: error,
                })?
                != 0
            {
                return Err(ArtifactError::InvalidFormat);
            }
            output.flush().await.map_err(|source| ArtifactError::Io {
                path: destination.to_path_buf(),
                source,
            })?;
            if size != expected_size || hex_digest(hasher.finalize()) != digest {
                return Err(ArtifactError::DigestMismatch);
            }
            return Ok(());
        }
        if envelope_len > MAX_ENVELOPE_BYTES {
            return Err(ArtifactError::InvalidFormat);
        }
        let mut envelope = vec![0_u8; envelope_len];
        read_exact(&mut input, &mut envelope, source).await?;
        let cipher = cipher.clone();
        let engagement_id = engagement_id.to_string();
        let record_id = format!("{digest}:{index}");
        let plaintext = tokio::task::spawn_blocking(move || {
            cipher.open_record(&engagement_id, RECORD_KIND, &record_id, &envelope)
        })
        .await
        .map_err(|error| ArtifactError::CryptoTask(error.to_string()))??;
        size = size.saturating_add(u64::try_from(plaintext.len()).unwrap_or(u64::MAX));
        if size > expected_size {
            return Err(ArtifactError::DigestMismatch);
        }
        hasher.update(&plaintext);
        output
            .write_all(&plaintext)
            .await
            .map_err(|source| ArtifactError::Io {
                path: destination.to_path_buf(),
                source,
            })?;
        index = index
            .checked_add(/*rhs*/ 1)
            .ok_or(ArtifactError::InvalidFormat)?;
    }
}

async fn open_temporary(path: &Path) -> Result<tokio::fs::File, std::io::Error> {
    let mut options = tokio::fs::OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        const FILE_FLAG_DELETE_ON_CLOSE: u32 = 0x0400_0000;
        const FILE_SHARE_READ_WRITE_DELETE: u32 = 0x0000_0007;
        options
            .custom_flags(FILE_FLAG_DELETE_ON_CLOSE)
            .share_mode(FILE_SHARE_READ_WRITE_DELETE);
    }
    options.open(path).await
}

async fn private_output(path: &Path) -> Result<tokio::fs::File, ArtifactError> {
    let mut options = tokio::fs::OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    options.mode(0o600);
    options
        .open(path)
        .await
        .map_err(|source| ArtifactError::Io {
            path: path.to_path_buf(),
            source,
        })
}

async fn read_exact(
    input: &mut tokio::fs::File,
    buffer: &mut [u8],
    path: &Path,
) -> Result<(), ArtifactError> {
    input
        .read_exact(buffer)
        .await
        .map(|_| ())
        .map_err(|source| ArtifactError::Io {
            path: path.to_path_buf(),
            source,
        })
}

fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    bytes
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
