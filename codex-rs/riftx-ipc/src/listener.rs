use crate::LocalIpcEndpoint;
use axum::serve::Listener;
use std::io;
use std::time::Duration;

pub struct LocalIpcListener {
    endpoint: LocalIpcEndpoint,
    inner: PlatformListener,
}

impl LocalIpcListener {
    pub async fn bind(endpoint: LocalIpcEndpoint) -> io::Result<Self> {
        let inner = bind(&endpoint).await?;
        Ok(Self { endpoint, inner })
    }
}

impl Listener for LocalIpcListener {
    type Io = PlatformStream;
    type Addr = String;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            match accept(&mut self.inner, &self.endpoint).await {
                Ok(stream) => return (stream, self.endpoint.to_string()),
                Err(_) => tokio::time::sleep(Duration::from_secs(1)).await,
            }
        }
    }

    fn local_addr(&self) -> io::Result<Self::Addr> {
        Ok(self.endpoint.to_string())
    }
}

#[cfg(unix)]
type PlatformListener = tokio::net::UnixListener;
#[cfg(unix)]
type PlatformStream = tokio::net::UnixStream;

#[cfg(unix)]
async fn bind(endpoint: &LocalIpcEndpoint) -> io::Result<PlatformListener> {
    use std::io::ErrorKind;
    use std::os::unix::fs::FileTypeExt;
    use std::os::unix::fs::PermissionsExt;

    tokio::fs::create_dir_all(endpoint.runtime_dir()).await?;
    let runtime_dir_metadata = tokio::fs::symlink_metadata(endpoint.runtime_dir()).await?;
    if !runtime_dir_metadata.is_dir() || runtime_dir_metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            format!(
                "local IPC runtime path must be a real directory: {}",
                endpoint.runtime_dir().display()
            ),
        ));
    }
    tokio::fs::set_permissions(
        endpoint.runtime_dir(),
        std::fs::Permissions::from_mode(0o700),
    )
    .await?;
    let socket_path = endpoint.socket_path();
    if let Ok(metadata) = tokio::fs::symlink_metadata(&socket_path).await {
        if !metadata.file_type().is_socket() {
            return Err(io::Error::new(
                ErrorKind::AlreadyExists,
                format!(
                    "local IPC path exists and is not a socket: {}",
                    socket_path.display()
                ),
            ));
        }
        match tokio::net::UnixStream::connect(&socket_path).await {
            Ok(_) => {
                return Err(io::Error::new(
                    ErrorKind::AddrInUse,
                    format!(
                        "RiftX daemon is already listening at {}",
                        socket_path.display()
                    ),
                ));
            }
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::ConnectionRefused | ErrorKind::NotFound
                ) =>
            {
                tokio::fs::remove_file(&socket_path).await?;
            }
            Err(error) => return Err(error),
        }
    }
    let listener = tokio::net::UnixListener::bind(&socket_path)?;
    tokio::fs::set_permissions(&socket_path, std::fs::Permissions::from_mode(0o600)).await?;
    Ok(listener)
}

#[cfg(unix)]
async fn accept(
    listener: &mut PlatformListener,
    _endpoint: &LocalIpcEndpoint,
) -> io::Result<PlatformStream> {
    tokio::net::UnixListener::accept(listener)
        .await
        .map(|(stream, _address)| stream)
}

#[cfg(windows)]
type PlatformListener = tokio::net::windows::named_pipe::NamedPipeServer;
#[cfg(windows)]
type PlatformStream = tokio::net::windows::named_pipe::NamedPipeServer;

#[cfg(windows)]
async fn bind(endpoint: &LocalIpcEndpoint) -> io::Result<PlatformListener> {
    create_pipe(&endpoint.pipe_name())
}

#[cfg(windows)]
async fn accept(
    listener: &mut PlatformListener,
    endpoint: &LocalIpcEndpoint,
) -> io::Result<PlatformStream> {
    listener.connect().await?;
    let replacement = create_pipe(&endpoint.pipe_name())?;
    Ok(std::mem::replace(listener, replacement))
}

#[cfg(windows)]
fn create_pipe(name: &str) -> io::Result<PlatformListener> {
    use std::ffi::c_void;
    use std::ptr;
    use tokio::net::windows::named_pipe::ServerOptions;
    use windows_sys::Win32::Foundation::GetLastError;
    use windows_sys::Win32::Foundation::HLOCAL;
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Authorization::ConvertStringSecurityDescriptorToSecurityDescriptorW;
    use windows_sys::Win32::Security::PSECURITY_DESCRIPTOR;
    use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;

    // Protect the pipe and grant access only to LocalSystem and the object owner.
    let sddl = "D:P(A;;GA;;;SY)(A;;GA;;;OW)"
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let mut descriptor: PSECURITY_DESCRIPTOR = ptr::null_mut();
    let converted = unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut descriptor,
            ptr::null_mut(),
        )
    };
    if converted == 0 {
        return Err(io::Error::from_raw_os_error(unsafe {
            GetLastError() as i32
        }));
    }
    let mut attributes = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor,
        bInheritHandle: 0,
    };
    let result = unsafe {
        ServerOptions::new()
            .reject_remote_clients(true)
            .create_with_security_attributes_raw(
                name,
                &mut attributes as *mut SECURITY_ATTRIBUTES as *mut c_void,
            )
    };
    unsafe {
        LocalFree(descriptor as HLOCAL);
    }
    result
}
