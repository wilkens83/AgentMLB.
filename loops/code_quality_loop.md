Work in a loop until the module clears all quality criteria below.

GOAL:
Implement [MODULE_NAME] according to project specifications.

CRITERIA:
1. All functions include type hints and docstrings.
2. Every external API/database call is wrapped in explicit `try/except/finally` blocks.
3. No hardcoded credentials or unhandled edge cases (e.g., empty DataFrames, missing columns).
4. Code passes Python syntax compilation without warnings.

EACH PASS:
1. DRAFT: Write or update the file.
2. TEST: Run `python3 -m py_compile [FILE]` and execute related unit tests.
3. SCORE: Rate 1–10 against each criterion.
4. CALL: If all criteria score 8+, write "DONE". Otherwise, fix the weakest item and repeat (max 3 iterations).
