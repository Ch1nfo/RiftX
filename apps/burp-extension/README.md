# RiftX Burp Connector

Build the Montoya extension with JDK 21+ and Gradle:

```bash
gradle jar
```

Load `build/libs/riftx-burp-extension-2.0.0-alpha.0.jar` from Burp's Extensions tab.
The extension sends selected request/response pairs to the unified RiftX Connector API,
can append to an existing Run or create a new Run, follows Run SSE progress, cancels a
Run, and opens its WebUI. It does not embed or launch an Agent runtime.

The dependency-free HTTP parser/client core can be tested with:

```bash
./scripts/test-core.sh
```
