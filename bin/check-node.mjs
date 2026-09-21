import { reportUnsupportedNode } from "./node-runtime.mjs";

if (reportUnsupportedNode()) process.exit(1);
