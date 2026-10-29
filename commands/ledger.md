---
description: Show or edit the epistemic monitor's decision ledger (list, show, add, supersede, graph, resolve, dedupe, retire, pin, interventions, status, doctor)
allowed-tools: Bash(python3 -I *)
---

Run the ledger command and show the user its output verbatim in a code block:

```
python3 -I "${CLAUDE_PLUGIN_ROOT}/bin/em-hook" ledger $ARGUMENTS
```

Subcommands: `list [--all]`, `show <id>`, `add --type <decision|constraint|premise|commitment|process> --statement "<text>" [--rationale "<text>"] [--path <glob>]... [--user]`, `supersede <id> --reason "<why>" [--by user|agent]`, `graph`, `resolve <id> --keep|--retire`, `dedupe [--apply]` (merge entries that say the same thing), `retire <id>... --reason "<why>"` or `retire --provenance agent --reason "<why>"` (close entries without a replacement), `pin`, `interventions [n]`, `status`, `doctor` (one live call per component to verify keys and providers).

If no arguments were given, run `list`. If the user asked to change the ledger (add or supersede), run the change and then `show <id>` so they see the resulting state. Use `--user` / `--by user` only when the user themselves asked for the change in this conversation.
