---
name: xmind-to-testcase-excel
description: "Read attached or local XMind requirement mind maps and generate structured Excel software test cases. Use when Codex needs to convert .xmind files into Chinese test-case .xlsx workbooks, including requests for custom columns, XMind requirement analysis, or direct XMind-to-Excel test-case generation."
---

# XMind to Test-case Excel

Convert an XMind requirement tree into validated test-case JSON, then build and visually verify one Excel workbook. Let the current AI generate the cases; do not call an external model API.

## Workflow

1. Locate the `.xmind` input. Ask only when no input exists, multiple inputs are plausible, or the requested columns contradict each other.
2. Resolve this skill directory as `SKILL_DIR` and create a unique empty temporary job directory outside the final output path. Use absolute paths when invoking bundled scripts.
3. Run:

   ```bash
   python "$SKILL_DIR/scripts/extract_xmind.py" INPUT.xmind --work-dir JOB_DIR --chunk-chars 6000
   ```

   Read `JOB_DIR/manifest.json`. Treat `oversized_leaf_count > 0` as a warning, not silent success: process the leaf if it fits context, otherwise report which path must be split in XMind.
4. Read [testcase-generation.md](references/testcase-generation.md) completely before generating cases.
5. Select one schema for the entire job:
   - With no custom-column request, set `SCHEMA` to `$SKILL_DIR/references/default-schema.json` and use [default-schema.json](references/default-schema.json) unchanged.
   - With a custom-column request, interpret it once, write `JOB_DIR/schema.json` containing only `{"columns":[...]}`, and set `SCHEMA` to that file. Do not change it after the first case chunk.
6. Process manifest chunks in order. Read each referenced Markdown file, generate one strict JSON result at `JOB_DIR/cases/<chunk-id>.json`, then immediately run:

   ```bash
   python "$SKILL_DIR/scripts/prepare_cases.py" validate-chunk --manifest JOB_DIR/manifest.json --schema SCHEMA --input CHUNK.json
   ```

   Repair the file from the validator's exact error. Stop without producing a partial workbook after two consecutive failed validations for the same chunk.
7. Merge only after every manifest chunk validates:

   ```bash
   python "$SKILL_DIR/scripts/prepare_cases.py" merge --manifest JOB_DIR/manifest.json --schema SCHEMA --cases-dir JOB_DIR/cases --output JOB_DIR/merged.json
   ```

8. Load the bundled workspace spreadsheet dependencies. In the writable job directory, link `node_modules` to the loader-provided Node package directory and copy `scripts/build_workbook.mjs` into the job directory so its bare import resolves there. Run it with the loader-provided Node executable:

   ```bash
   node build_workbook.mjs --input JOB_DIR/merged.json --output OUTPUT.xlsx --preview JOB_DIR/preview.png
   ```

   The builder avoids overwriting an existing output by appending a numeric suffix. Use `--overwrite` only when the user explicitly asks to replace the file.
9. Read the builder's JSON output to get the actual collision-safe Excel path, then run the artifact-tool 2.8.6 compatibility fix with the same Python used for extraction:

   ```bash
   python "$SKILL_DIR/scripts/fix_freeze_panes.py" ACTUAL_OUTPUT.xlsx
   ```

   This standard-library step only restores the frozen first-row pane that the current artifact-tool exporter omits; do not use it to alter workbook data or styling.
10. Inspect the builder's workbook/table summary and formula-error scan. View `preview.png`; fix severe clipping, unreadable layout, missing headers, or broken output before delivery.
11. Return a concise summary and one standalone Markdown link to the final `.xlsx`. Do not link manifest, Markdown chunks, JSON, builder files, or the preview unless requested.

## Output Rules

- Honor a user-specified output path. Otherwise write `outputs/<XMind名称>-测试用例.xlsx` in the active workspace.
- Generate exactly one worksheet named `测试用例`.
- Keep temporary AI files out of version control and out of the final response.
- Never expose secrets; this workflow requires no `DEEPSEEK_API_KEY` or other model API key.
