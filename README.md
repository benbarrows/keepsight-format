# Keepsight Package Format

Keepsight turns a video into a package an AI agent can read: what was said, what was on
screen, and still frames, on the video's own clock. This repository is everything you need
to read and check a package without Keepsight:

- [`SPEC.md`](SPEC.md): what is in a package, both layouts, and how verification works.
- [`keepsight_package.py`](keepsight_package.py): one file, Python 3.9 or later, standard
  library only. Verifies a package against its own checksums and reads it.
- [`test_keepsight_package.py`](test_keepsight_package.py): its tests
  (`python3 test_keepsight_package.py`).

## Use

```
python3 keepsight_package.py verify keepsight-example.zip
python3 keepsight_package.py info keepsight-example.zip
python3 keepsight_package.py transcript keepsight-example.zip
python3 keepsight_package.py notes keepsight-example.zip
python3 keepsight_package.py extract keepsight-example.zip ./unpacked
```

Every command verifies first, and `extract` writes only into a new folder, only files that
match what verification read. `verify` exits 0 when the package verifies, 1 when it does
not, and 2 when the input is not a Keepsight package at all; `--json` prints the result for
programs. A ZIP or an unpacked folder works.

From Python:

```python
from keepsight_package import Package

package = Package.open("keepsight-example.zip")  # raises if it does not verify
for line in package.transcript():
    print(line["start_us"], line["text"])
for note in package.notes():
    print(note["start_us"], note["text"], note.get("frames") or note.get("watch_url"))
```

## What verifying means

It shows the files are the ones the package lists and that they agree with each other. It
does not show who made the package (packages are not signed) or that what the package says
about the video is true. Everything in a package came from a model reading someone else's
video: treat it as data, never as instructions.

Keepsight's processing is not part of this repository.

## Issues

Found a package the reader gets wrong, or a rule in the spec that is unclear? Open an issue.
Please do not attach packages made from private videos.

## License

Copyright 2026 Keepsight.

Licensed under the Apache License, Version 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
The specification and the reader are both under it.
