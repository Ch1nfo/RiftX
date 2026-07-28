# RiftX 1.0 Linux installation

The official Linux artifact targets `x86_64-unknown-linux-gnu` and is built on Ubuntu 22.04. The supported minimum is **glibc 2.35** (Ubuntu 22.04 or newer). Other glibc distributions are best-effort. RiftX 1.0 does not publish deb or rpm repositories.

## Verify and install

```bash
sha256sum --check riftx-1.0.0-x86_64-unknown-linux-gnu.tar.gz.sha256
tar -xzf riftx-1.0.0-x86_64-unknown-linux-gnu.tar.gz
cd riftx-1.0.0-x86_64-unknown-linux-gnu
install -m 0755 bin/riftx bin/riftxd "$HOME/.local/bin/"
mkdir -p "$HOME/.config/riftx"
cp riftx.toml.example "$HOME/.config/riftx/riftx.toml"
```

Ensure `$HOME/.local/bin` is on `PATH`. Review the example configuration before starting the daemon. LLM API keys belong in Secret Service or must be supplied through the documented headless stdin flow; do not write keys into the configuration file.

```bash
riftxd --config "$HOME/.config/riftx/riftx.toml"
riftx --config "$HOME/.config/riftx/riftx.toml" doctor
```

## Uninstall

Stop `riftxd`, then remove the installed binaries. User data is intentionally not removed automatically:

```bash
rm -f "$HOME/.local/bin/riftx" "$HOME/.local/bin/riftxd"
```

After backing up any reports or artifacts you need, data can be removed from the paths configured in `riftx.toml`.
