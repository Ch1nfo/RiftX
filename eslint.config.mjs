import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // The compiler already gates unused locals/params (noUnusedLocals);
      // ESLint mirrors it so editors flag it before a full typecheck.
      // `_`-prefixed names are reserved for intentionally unused bindings,
      // and `{ [id]: _removed, ...rest }` destructure-exclusion is allowed.
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_", ignoreRestSiblings: true }],
    },
  },
  {
    ignores: [".next/**", "node_modules/**", "test-results/**", "recommended-skills/**"],
  },
];

export default eslintConfig;
