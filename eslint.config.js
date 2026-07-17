// ESLint flat config for the static frontend JS.
//
// The frontend ships as plain <script>-loaded files (app.js -> player.js ->
// map.js) that share ONE global scope -- not ES modules. So a function defined
// in one file and called from another is a normal cross-file reference, which
// ESLint (linting each file in isolation) would otherwise flag as no-undef.
// Rather than silence no-undef, the names that are genuinely shared across the
// files are declared as globals below (grep the files to maintain the list) --
// that way a REAL cross-file typo/rename (the failure mode of the split) still
// trips no-undef, while the legitimate shared surface is documented here.
//
// Scope: correctness only (no-undef, no-unused-vars). No stylistic rules --
// ruff owns Python style and there's no appetite for a JS style war here.

const globals = require("globals");

module.exports = [
  // genre_families.json lives in static/ too; it's data, not code -- never lint it.
  { ignores: ["vibedentify/static/genre_families.json"] },
  {
    files: ["vibedentify/static/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script", // plain scripts sharing one global scope, not modules
      globals: {
        ...globals.browser,
        // Cross-file shared surface: defined in one script, used in another.
        // Keep this list in sync with the files (grep for the names) -- a REAL
        // typo/rename across the split still trips no-undef in the caller.
        escapeHtml: "writable", // app.js  -> used by map.js
        familyOf: "writable", //   app.js  -> used by map.js
        fmtTime: "writable", //    app.js  -> used by player.js
        PLAYER: "writable", //     player.js -> used by app.js
        OBJ_URLS: "writable", //   player.js -> used by app.js
        attachPlayer: "writable", // player.js -> used by app.js
      },
    },
    rules: {
      "no-undef": "error",
      // Correctness only: catch real unused bindings, but not the intentional
      // throwaways -- `catch (e) {}` that swallows, and `_`-prefixed placeholders.
      "no-unused-vars": [
        "error",
        { caughtErrors: "none", argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
];
