"""Command line tool to extract attachments from mbox archives for Paperless-ngx.

The script reads a `.mbox` file and writes attachments to an output directory.
Optional sidecar metadata files capture email context to help Paperless-ngx
categorize documents based on filenames and text content.
"""
from __future__ import annotations

import argparse
import email
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
import mailbox
import os
from pathlib import Path
import re
from collections import Counter
from typing import Iterable, Optional


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Extract attachments from an mbox file with Paperless-friendly metadata."
    )
    parser.add_argument(
        "--mbox",
        required=True,
        type=Path,
        help="Path to the mbox archive to read.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory to place extracted attachments and metadata files.",
    )
    parser.add_argument(
        "--max-size-mb",
        type=float,
        default=None,
        help="Skip attachments larger than this size (in megabytes).",
    )
    parser.add_argument(
        "--include-types",
        action="append",
        default=None,
        help=(
            "Only save attachments whose MIME type or file extension matches. "
            "Provide values like 'application/pdf', 'image/', or '.csv'. "
            "Can be specified multiple times or as a comma-separated list."
        ),
    )
    parser.add_argument(
        "--exclude-types",
        action="append",
        default=None,
        help=(
            "Skip attachments whose MIME type or file extension matches. "
            "Provide values like 'image/', 'text/html', or '.ics'. "
            "Takes precedence over --include-types."
        ),
    )
    parser.add_argument(
        "--list-types",
        action="store_true",
        help="Scan the archive and print a summary of attachment MIME types and extensions.",
    )
    parser.add_argument(
        "--list-correspondents",
        action="store_true",
        help=(
            "Scan the archive and print unique correspondents with their names, "
            "email addresses, and message/attachment counts."
        ),
    )
    parser.add_argument(
        "--name-template",
        default="{date}_{sender}_{subject}_{attachment}",
        help=(
            "Template for the saved attachment filename (without directory). "
            "Available placeholders: {date}, {sender}, {subject}, {attachment}. "
            "Include {attachment} to preserve the original extension."
        ),
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show a simple progress bar while scanning the mbox archive.",
    )
    parser.add_argument(
        "--year-subdirs",
        action="store_true",
        help="Place attachments into year-based subdirectories (YYYY).",
    )
    parser.add_argument(
        "--sender-subdirs",
        action="store_true",
        help="Place attachments into subdirectories based on the sender address.",
    )
    parser.add_argument(
        "--domain-subdirs",
        action="store_true",
        help="Place attachments into subdirectories based on the sender domain.",
    )
    parser.add_argument(
        "--export-metadata",
        action="store_true",
        help=(
            "Write sidecar metadata files next to attachments. By default, "
            "metadata files are not created."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan the mbox without writing any files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress information while processing messages.",
    )
    return parser.parse_args()


def _split_type_values(values: Optional[list[str]]) -> set[str]:
    """Normalize comma-separated type selectors into a set."""

    if not values:
        return set()
    tokens: list[str] = []
    for value in values:
        tokens.extend(part.strip() for part in value.split(","))
    return {token.lower() for token in tokens if token}


def normalize_selector(selector: str) -> tuple[str, str]:
    """Return (kind, value) where kind is 'mime' or 'ext'."""

    selector = selector.lower()
    if selector.startswith("."):
        return ("ext", selector.lstrip("."))
    return ("mime", selector)


def attachment_matches(selector: str, content_type: str, filename: str) -> bool:
    """Check whether an attachment matches a selector token."""

    kind, value = normalize_selector(selector)
    if kind == "mime":
        return content_type.startswith(value)
    suffix = Path(filename).suffix.lower().lstrip(".")
    return bool(suffix) and suffix == value


def sanitize_text(text: str, fallback: str = "untitled") -> str:
    """Return a filesystem-friendly string.

    Replaces sequences of unsafe characters with a single underscore and trims
    surrounding underscores. Returns the fallback when the result is empty.
    """

    cleaned = re.sub(r"[^\w\-.]+", "_", text, flags=re.UNICODE).strip("._")
    return cleaned or fallback


def decode_header_value(raw_value: Optional[str]) -> str:
    """Decode MIME-encoded header text into a readable string."""

    if not raw_value:
        return ""
    try:
        decoded = str(make_header(decode_header(raw_value)))
    except Exception:
        decoded = raw_value
    return decoded


def parse_addresses(raw_header: Optional[str]) -> list[tuple[str, str]]:
    """Return (name, email) tuples parsed from a header value."""

    if not raw_header:
        return []
    decoded = decode_header_value(raw_header)
    return [
        (name.strip(), address.strip())
        for name, address in getaddresses([decoded])
        if address.strip()
    ]


def parse_email_date(date_header: Optional[str]):
    """Parse the Date header into a datetime, if possible."""

    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header)
    except Exception:
        return None


def extract_body_text(message: Message) -> str:
    """Return the best-effort plain text body for metadata."""

    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue
            if part.get_content_type() == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
    else:
        if message.get_content_type() == "text/plain":
            payload = message.get_payload(decode=True)
            if payload is not None:
                return payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    return ""


def iter_attachments(message: Message) -> Iterable[tuple[Message, bytes]]:
    """Yield (part, data) for each attachment-like part in the message."""

    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition not in {"attachment", "inline"} and not filename:
            continue
        data = part.get_payload(decode=True)
        if data is None:
            continue
        yield part, data


def should_save_attachment(
    content_type: str,
    filename: str,
    include_types: set[str],
    exclude_types: set[str],
) -> bool:
    """Return True when the attachment passes include/exclude filters."""

    for selector in exclude_types:
        if attachment_matches(selector, content_type, filename):
            return False
    if not include_types:
        return True
    return any(
        attachment_matches(selector, content_type, filename)
        for selector in include_types
    )


def build_output_path(
    output_dir: Path,
    sender: str,
    sender_domain: str,
    date_str: str,
    subject: str,
    attachment_name: str,
    name_template: str,
    year_subdirs: bool,
    sender_subdirs: bool,
    domain_subdirs: bool,
    existing: set[Path],
) -> Path:
    """Construct a unique output path for an attachment."""

    safe_sender = sanitize_text(sender, fallback="unknown")
    safe_domain = sanitize_text(sender_domain, fallback="unknown")
    safe_subject = sanitize_text(subject, fallback="no_subject")
    safe_attachment = sanitize_text(attachment_name, fallback="attachment")
    tokens = {
        "date": date_str or "undated",
        "sender": safe_sender,
        "subject": safe_subject,
        "attachment": safe_attachment,
    }
    try:
        formatted = name_template.format(**tokens)
    except KeyError as exc:
        missing = str(exc.args[0]) if exc.args else "unknown"
        raise ValueError(
            f"Unknown placeholder in --name-template: {missing}. "
            "Allowed: {date}, {sender}, {subject}, {attachment}"
        ) from exc
    except Exception as exc:
        raise ValueError(f"Invalid --name-template: {exc}") from exc

    base_name = sanitize_text(formatted, fallback="attachment")

    parts = [output_dir]
    if year_subdirs and date_str[:4].isdigit():
        parts.append(Path(date_str[:4]))
    if domain_subdirs:
        parts.append(Path(safe_domain))
    if sender_subdirs:
        parts.append(Path(safe_sender))
    target_dir = Path(*parts)

    candidate = target_dir / base_name
    suffix_chain = "".join(candidate.suffixes)
    base_stem = candidate.name[: -len(suffix_chain)] if suffix_chain else candidate.name
    counter = 1
    final_path = candidate
    while final_path in existing:
        counter += 1
        final_path = candidate.with_name(f"{base_stem}_{counter}{suffix_chain}")
    existing.add(final_path)
    return final_path


def write_attachment(
    path: Path,
    data: bytes,
    metadata: Optional[str],
    dry_run: bool,
) -> None:
    """Write the attachment and its sidecar metadata file."""

    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if metadata is not None:
        metadata_path = path.with_suffix(path.suffix + ".metadata.txt")
        metadata_path.write_text(metadata, encoding="utf-8")


def format_metadata(
    message: Message,
    part: Message,
    attachment_name: str,
    attachment_size: int,
    body_text: str,
) -> str:
    """Compose metadata text for the sidecar file."""

    headers = {
        "Subject": decode_header_value(message.get("Subject")),
        "From": decode_header_value(message.get("From")),
        "To": decode_header_value(message.get("To")),
        "Cc": decode_header_value(message.get("Cc")),
        "Date": decode_header_value(message.get("Date")),
        "Message-Id": message.get("Message-ID", ""),
    }
    lines = ["Email Metadata:"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}")

    lines.append("")
    lines.append("Attachment Metadata:")
    lines.append(f"Filename: {attachment_name}")
    lines.append(f"Content-Type: {part.get_content_type()}")
    lines.append(f"Size (bytes): {attachment_size}")

    if body_text.strip():
        lines.append("")
        lines.append("Email Body (text/plain):")
        lines.append(body_text)

    return "\n".join(lines)


def display_progress(current: int, total: Optional[int]) -> None:
    """Render a lightweight progress bar for the current position."""

    if total and total > 0:
        percent = min(100, int((current / total) * 100))
        bar_length = 30
        filled = int(bar_length * percent / 100)
        bar = "#" * filled + "-" * (bar_length - filled)
        print(f"[{bar}] {percent:3d}% ({current}/{total})", end="\r", flush=True)
    else:
        print(f"Processed {current} message(s)...", end="\r", flush=True)


def print_correspondents_summary(correspondents: dict[str, dict[str, int | str]]) -> None:
    """Print correspondents sorted by attachment count, then messages."""

    if not correspondents:
        print("No correspondents found.")
        return

    print("Correspondents (by attachments, then messages):")
    sorted_entries = sorted(
        correspondents.values(),
        key=lambda entry: (entry["attachment_count"], entry["message_count"], entry["address"]),
        reverse=True,
    )
    for entry in sorted_entries:
        name = entry["name"] or "(no name)"
        address = entry["address"]
        attachment_count = entry["attachment_count"]
        message_count = entry["message_count"]
        print(
            f"  {name} <{address}>: attachments={attachment_count}, messages={message_count}"
        )
    print()


def process_mbox(
    mbox_path: Path,
    output_dir: Path,
    max_size_mb: Optional[float],
    include_types: set[str],
    exclude_types: set[str],
    year_subdirs: bool,
    sender_subdirs: bool,
    domain_subdirs: bool,
    export_metadata: bool,
    name_template: str,
    dry_run: bool,
    verbose: bool,
    list_types: bool,
    list_correspondents: bool,
    show_progress: bool,
) -> int:
    """Process the mbox file and return the count of extracted attachments."""

    if not mbox_path.exists():
        raise FileNotFoundError(f"mbox file not found: {mbox_path}")

    existing_paths: set[Path] = set()
    total_extracted = 0
    mime_counter: Counter[str] = Counter()
    extension_counter: Counter[str] = Counter()
    correspondents: dict[str, dict[str, int | str]] = {}

    mbox = mailbox.mbox(
        mbox_path,
        factory=lambda f: email.message_from_binary_file(f, policy=policy.default),
    )
    try:
        total_messages = len(mbox)
    except Exception:
        total_messages = None
    try:
        for index, message in enumerate(mbox, start=1):
            if show_progress:
                display_progress(index, total_messages)
            body_text = extract_body_text(message) if export_metadata else ""
            sender_header = decode_header_value(message.get("From"))
            addresses = parse_addresses(sender_header)
            sender_display = sender_header or "unknown"
            sender_value = sender_display
            sender_domain = "unknown"
            if addresses:
                primary_name, primary_address = addresses[0]
                sender_value = primary_name or primary_address or sender_display or "unknown"
                if "@" in primary_address:
                    sender_domain = primary_address.split("@", 1)[1]
            for name, address in addresses or [("", "")]:
                key = address.lower() or "unknown"
                entry = correspondents.setdefault(
                    key,
                    {
                        "name": name or "",
                        "address": address or "unknown",
                        "message_count": 0,
                        "attachment_count": 0,
                    },
                )
                if not entry["name"] and name:
                    entry["name"] = name
                entry["message_count"] += 1
            subject = decode_header_value(message.get("Subject")) or ""
            email_date = parse_email_date(message.get("Date"))
            date_str = email_date.strftime("%Y%m%d") if email_date else "undated"

            for part, data in iter_attachments(message):
                attachment_name = decode_header_value(part.get_filename()) or "attachment"
                content_type = part.get_content_type().lower()
                mime_counter[content_type] += 1
                suffix = Path(attachment_name).suffix.lower().lstrip(".")
                if suffix:
                    extension_counter[suffix] += 1

                for name, address in addresses or [("", "")]:
                    key = address.lower() or "unknown"
                    entry = correspondents.setdefault(
                        key,
                        {
                            "name": name or "",
                            "address": address or "unknown",
                            "message_count": 0,
                            "attachment_count": 0,
                        },
                    )
                    if not entry["name"] and name:
                        entry["name"] = name
                    entry["attachment_count"] += 1

                if not should_save_attachment(
                    content_type=content_type,
                    filename=attachment_name,
                    include_types=include_types,
                    exclude_types=exclude_types,
                ):
                    if verbose:
                        print(
                            f"Skipping attachment due to type filter: message {index}, "
                            f"name {attachment_name} ({content_type})"
                        )
                    continue

                size_mb = len(data) / (1024 * 1024)
                if max_size_mb is not None and size_mb > max_size_mb:
                    if verbose:
                        print(
                            f"Skipping attachment over size limit ({size_mb:.2f} MB): "
                            f"message {index}, name {attachment_name}"
                        )
                    continue

                output_path = build_output_path(
                    output_dir=output_dir,
                    sender=sender_value,
                    sender_domain=sender_domain,
                    date_str=date_str,
                    subject=subject,
                    attachment_name=attachment_name,
                    name_template=name_template,
                    year_subdirs=year_subdirs,
                    sender_subdirs=sender_subdirs,
                    domain_subdirs=domain_subdirs,
                    existing=existing_paths,
                )

                metadata_text = (
                    format_metadata(
                        message=message,
                        part=part,
                        attachment_name=attachment_name,
                        attachment_size=len(data),
                        body_text=body_text,
                    )
                    if export_metadata
                    else None
                )

                write_attachment(output_path, data, metadata_text, dry_run=dry_run)
                total_extracted += 1

                if verbose:
                    action = "Would write" if dry_run else "Wrote"
                    print(f"{action} attachment to {output_path}")
    finally:
        mbox.close()
        if show_progress and total_messages:
            print(" " * 80, end="\r", flush=True)
            print()

    if list_types:
        print("Attachment MIME type counts:")
        for mime, count in mime_counter.most_common():
            print(f"  {mime}: {count}")
        print("\nAttachment extension counts:")
        for ext, count in extension_counter.most_common():
            print(f"  .{ext}: {count}")
        print()
    if list_correspondents:
        print_correspondents_summary(correspondents)
    return total_extracted


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir
    include_types = _split_type_values(args.include_types)
    exclude_types = _split_type_values(args.exclude_types)

    try:
        count = process_mbox(
            mbox_path=args.mbox,
            output_dir=output_dir,
            max_size_mb=args.max_size_mb,
            include_types=include_types,
            exclude_types=exclude_types,
            year_subdirs=args.year_subdirs,
            sender_subdirs=args.sender_subdirs,
            domain_subdirs=args.domain_subdirs,
            export_metadata=args.export_metadata,
            name_template=args.name_template,
            dry_run=args.dry_run,
            verbose=args.verbose,
            list_types=args.list_types,
            list_correspondents=args.list_correspondents,
            show_progress=args.progress,
        )
    except Exception as exc:  # pragma: no cover - CLI entry point error handling
        print(f"Error: {exc}", file=os.sys.stderr)
        return 1

    if args.verbose:
        summary_action = "would extract" if args.dry_run else "extracted"
        print(f"Successfully {summary_action} {count} attachment(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
