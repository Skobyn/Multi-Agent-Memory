# Agent instructions: set up Epistemic Monitor on this machine

Paste everything below the line into a coding agent (Claude Code, run from this checkout). Fill in
the two values in the first block first. The agent edits your shell profile and writes two config
files; it never commits anything and never prints a key.

---

Set up the Epistemic Monitor plugin for me on this machine. Facts:

- The checkout is the directory you are running in (confirm with `pwd`; it contains `bin/em-hook`).
- My TypeSafe API key is in the macOS login Keychain as a generic password whose item name
  (service) is `typesafe_api_key`. Never print the key; only ever show its first 6 characters.
- My shell is zsh. I run the advisor and extractor on my Claude subscription through the plugin's
  `claude-cli` provider, Jev via TypeSafe, and no embeddings. Do not set `ANTHROPIC_API_KEY` or
  `OPENROUTER_API_KEY` anywhere.

Do these steps in order and show me the output of each check:

1. Verify the Keychain item exists: `security find-generic-password -s typesafe_api_key -w | cut -c1-6`.
   If it fails, stop and tell me (the item name must match exactly; dashes and underscores differ).

2. Append this block to `~/.zshrc` (create the file if missing; do not duplicate it if it is already
   there), with `<checkout>` replaced by the absolute path from `pwd`:

   ```
   # Epistemic Monitor
   export TYPESAFE_API_KEY="$(security find-generic-password -s typesafe_api_key -w)"
   export CLAUDE_CODE_PLUGIN_DIRS="<checkout>:<checkout>/mods/epistemic-panel"
   ```

3. Write `config.json` in the checkout root (it is git-ignored; never commit it) with exactly:

   ```json
   {
     "decision_model": {"provider": "typesafe", "model": "jev-latest"},
     "advisor":   {"provider": "claude-cli", "model": "claude-opus-5-5"},
     "extractor": {"provider": "claude-cli", "model": "claude-haiku-5-5"},
     "embeddings": {"provider": "none"}
   }
   ```

   Write the same JSON to `~/.claude/epistemic.json` so it also applies to a marketplace install.

4. Run the offline smoke test: `bash scripts/smoke.sh`. Every line should end in `ok`.

5. Run the live check with the key exported in your own shell:

   ```
   export TYPESAFE_API_KEY="$(security find-generic-password -s typesafe_api_key -w)"; python3 -I bin/em-hook doctor
   ```

   Expected: the keys line shows `TYPESAFE_API_KEY=<6 chars>…`, `PASS decision model typesafe:jev-latest`
   with a p value, `PASS advisor` and `PASS extractor` both resolved as `claude-cli`, `INFO embeddings`
   disabled, `RESULT: PASS`. If any line is FAIL or WARN, show me the full line and stop.

6. Confirm `git status` shows no new tracked changes (`config.json` must appear only as ignored).

Finish by telling me to open a new terminal window (so `~/.zshrc` is reloaded) and run `claude`,
then `/ledger status` and `/epistemic` inside it.

---

## Variations

- **Alias instead of the environment variable.** Replace the `CLAUDE_CODE_PLUGIN_DIRS` line with
  `alias claudem='claude --plugin-dir <checkout> --plugin-dir <checkout>/mods/epistemic-panel'`
  and launch with `claudem`.
- **Marketplace install instead of the checkout.** Skip step 2's plugin line and step 3's
  `config.json`; keep `~/.claude/epistemic.json`; then inside Claude Code run
  `/plugin marketplace add Skobyn/Multi-Agent-Memory`, `/plugin install epistemic-monitor@epistemic-monitor`
  and `/plugin install epistemic-panel@epistemic-monitor`.
- **API keys instead of the subscription.** Add Keychain items and `export` lines for
  `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY`, set the advisor and extractor providers to `anthropic`
  or `openrouter`, and (optionally) embeddings to `openrouter`.
- **Linux.** Replace the Keychain lookups with your secret store (for example `pass show typesafe`).
