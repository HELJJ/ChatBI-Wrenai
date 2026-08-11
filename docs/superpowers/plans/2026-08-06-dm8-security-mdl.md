# DM8 Security MDL Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate and verify a distributable Wren v5 MDL package for nine DM8 security-domain tables.

**Architecture:** A deterministic Python generator consumes the three supplied CSV metadata files and writes the Wren project sources. Structural assertions protect the agreed counts and exact physical identifiers; the Wren CLI then validates and compiles the package without connecting to DM8.

**Tech Stack:** Python 3.11, standard-library CSV processing, PyYAML, Wren CLI.

## Global Constraints

- Preserve exact DM8 physical table and column identifier case.
- Generate exactly nine models, 204 columns, nine primary keys, and ten logical relationships.
- Never infer missing column descriptions or database-enforced foreign keys.
- Keep query guidance read-only and scoped to the nine models.

---

### Task 1: Deterministic MDL generator

**Files:**
- Create: `scripts/generate_dm8_security_mdl.py`
- Consume: the user-supplied `columns.csv`, `constraints.csv`, and `comments.csv`
- Produce: `outputs/dm8-security-mdl/`

**Interfaces:**
- Consumes three CSV paths and an output directory from command-line arguments.
- Produces a complete Wren project source tree plus structural assertions.

- [ ] Implement exact table-to-model mappings and DM8 type normalization.
- [ ] Write nine `models/<model>/metadata.yml` files.
- [ ] Write ten approved relationships to `relationships.yml`.
- [ ] Write project metadata and security-domain knowledge rules.
- [ ] Assert table, column, primary-key, relationship, and comment counts.

### Task 2: Structural and Wren validation

**Files:**
- Verify: `outputs/dm8-security-mdl/`

**Interfaces:**
- Consumes the generated project sources.
- Produces `target/mdl.json` and command evidence.

- [ ] Inspect generated YAML for exact model and relationship names.
- [ ] Run `wren context validate --path outputs/dm8-security-mdl`.
- [ ] Run `wren context build --path outputs/dm8-security-mdl`.
- [ ] Confirm `target/mdl.json` exists and contains nine models.

### Task 3: Distribution package

**Files:**
- Produce: `outputs/dm8-security-mdl.zip`
- Produce: `outputs/dm8-security-mdl/README.md`

**Interfaces:**
- Consumes the validated project.
- Produces a copy-ready ZIP and Linux smoke-test instructions.

- [ ] Document copy, profile binding, rebuild, dry-plan, dry-run, and MCP startup commands.
- [ ] Create the ZIP without credentials or source CSV paths.
- [ ] List archive contents and confirm no secret-bearing files are present.
