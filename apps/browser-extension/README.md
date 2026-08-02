# RiftX Browser Connector

Build the Manifest V3 DevTools extension:

```bash
pnpm --filter @riftx/browser-extension build
```

Load `apps/browser-extension/dist` as an unpacked extension, open DevTools, then use the
**RiftX** panel. The panel captures only XHR/Fetch entries, lets the user select captures,
append them to an existing Run or create a new Run, follows Run progress over SSE,
cancels a Run, and opens its WebUI. It does not embed or launch an Agent runtime.
