# File Upload and Path Audit References

- Use `entrypoint-discovery` for upload, download, archive, import, export, and file-operation entrypoints.
- Model content, metadata, logical key, filesystem path, storage root, serving root, and downstream parser as distinct objects.
- Record decoding and normalization order, final-path containment, link and race assumptions, collision policy, overwrite behavior, privileges, and later consumers.
- Filename, extension, MIME, or string concatenation alone is not evidence of traversal, arbitrary write, disclosure, or execution.
- Do not create files, extract archives, start processors, or execute the target project in this Pack.
