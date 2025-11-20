# mboxextractor

A small utility for extracting attachments from `.mbox` archives and preparing them
for import into Paperless-ngx. The script writes attachment files alongside
metadata sidecar files so Paperless-ngx can classify and search documents using
filenames and text content.

## Requirements

* Python 3.10+
* Standard library only; no additional dependencies required.

## Usage

```bash
python extract_mbox_attachments.py \
  --mbox /path/to/archive.mbox \
  --output-dir /path/to/output \
  [--include-types application/pdf,image/] \
  [--exclude-types .png,.jpg] \
  [--list-types] \
  [--progress] \
  [--max-size-mb 25] \
  [--year-subdirs] \
  [--sender-subdirs] \
  [--dry-run] \
  [--verbose]
```

### Options

* `--mbox` (required): Path to the `.mbox` file to process.
* `--output-dir` (required): Directory where attachments and metadata files are
  written.
* `--include-types`: Only save attachments whose MIME type or file extension
  matches (e.g., `application/pdf`, `image/`, `.csv`). Accepts multiple
  arguments or comma-separated lists.
* `--exclude-types`: Skip attachments whose MIME type or file extension matches
  (same format as `--include-types`). Exclusions take precedence.
* `--list-types`: Scan the archive and print a summary of attachment MIME types
  and file extensions without requiring a separate utility.
* `--progress`: Show a simple progress bar while scanning the mbox archive.
* `--max-size-mb`: Skip any attachment larger than the specified size (MB).
* `--year-subdirs`: Organize output into `YYYY/` subdirectories based on the
  email date.
* `--sender-subdirs`: Organize output into sender-based subdirectories.
* `--dry-run`: Parse the archive and report actions without writing files.
* `--verbose`: Print progress details.

### Notes

* Attachments are named using email date, sender, subject, and original
  filename, with filesystem-safe characters.
* Each attachment has a `.metadata.txt` sidecar file containing email headers,
  attachment details, and any available plain-text body to enhance Paperless-ngx
  indexing.
* The script is cross-platform and uses only the Python standard library, so it
  runs on macOS, Linux, and Windows.
