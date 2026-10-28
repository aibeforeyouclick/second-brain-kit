# Contributing

Thanks for helping. The most useful contribution is an **install report**: run the kit on your notes, let phase 6 fill `templates/INSTALL-REPORT.md`, read it, and open an issue with the "Install report" template. Counts and generic descriptions only: never paste note text, file names, or anything from your organization.

## Code changes
- Python standard library only. No network calls. Tools stay read-only unless a flag such as `--write` or `--apply` says otherwise.
- Never print a secret value; report `path:line` and the kind of finding.
- Add a unit test for every change: `python -I -m unittest discover -s tests`.
- Keep changes small and explain the real case that motivated them.

By contributing you agree that your contribution is licensed under the MIT License of this repository.
