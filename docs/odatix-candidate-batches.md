### Odatix candidate batches

Use scripts/line/odatix_batch.py when a decision requires several explicitly specified EDA candidates. Give it a clean, self-contained input tree, a Python entry point that invokes the existing build tool, expected outputs, and the measured tool identity. Keep fixed clocks, part, strategy, constraint files, Tcl, RTL and ROM contents inside that declared input tree or argument list. Treat a dependency outside that tree as unverified and omit tool_identity to disable coalescing.

The batch creates a new Odatix workspace and retains per-candidate command, status, log and artifact paths. It coalesces byte-identical payload and argument combinations within that batch only. Mark an intentional replicate independent: true. Read batch-result.json and individual outputs even when the Odatix process exits zero. Failed and incomplete producing jobs give the same outcome to their aliases. Board execution and original-Python acceptance remain in the existing board-host-io path.

Pass the Odatix launcher from its installed Python environment. The batch uses the sibling Python interpreter to start an Odatix session without attaching a terminal monitor, waits for its jobs, and stops that session before writing the result. The same invocation works with standard input closed and output redirected to a log.
