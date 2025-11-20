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
from email.utils import parsedate_to_datetime
import mailbox
import os
from pathlib import Path
import re
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


def build_output_path(
    output_dir: Path,
    sender: str,
    date_str: str,
    subject: str,
    attachment_name: str,
    year_subdirs: bool,
    sender_subdirs: bool,
    existing: set[Path],
) -> Path:
    """Construct a unique output path for an attachment."""

    safe_sender = sanitize_text(sender, fallback="unknown")
    safe_subject = sanitize_text(subject, fallback="no_subject")
    safe_attachment = sanitize_text(attachment_name, fallback="attachment")
    base_name = f"{date_str}_{safe_sender}_{safe_subject}_{safe_attachment}"

    parts = [output_dir]
    if year_subdirs and date_str[:4].isdigit():
        parts.append(Path(date_str[:4]))
    if sender_subdirs:
        parts.append(Path(safe_sender))
    target_dir = Path(*parts)

    candidate = target_dir / base_name
    counter = 1
    final_path = candidate
    while final_path in existing:
        counter += 1
        final_path = candidate.with_name(f"{candidate.name}_{counter}")
    existing.add(final_path)
    return final_path


def write_attachment(
    path: Path,
    data: bytes,
    metadata: str,
    dry_run: bool,
) -> None:
    """Write the attachment and its sidecar metadata file."""

    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
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


def process_mbox(
    mbox_path: Path,
    output_dir: Path,
    max_size_mb: Optional[float],
    year_subdirs: bool,
    sender_subdirs: bool,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Process the mbox file and return the count of extracted attachments."""

    if not mbox_path.exists():
        raise FileNotFoundError(f"mbox file not found: {mbox_path}")

    existing_paths: set[Path] = set()
    total_extracted = 0

    mbox = mailbox.mbox(
        mbox_path,
        factory=lambda f: email.message_from_binary_file(f, policy=policy.default),
    )
    try:
        for index, message in enumerate(mbox, start=1):
            body_text = extract_body_text(message)
            sender = decode_header_value(message.get("From")) or "unknown"
            subject = decode_header_value(message.get("Subject")) or ""
            email_date = parse_email_date(message.get("Date"))
            date_str = email_date.strftime("%Y%m%d") if email_date else "undated"

            for part, data in iter_attachments(message):
                attachment_name = decode_header_value(part.get_filename()) or "attachment"
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
                    sender=sender,
                    date_str=date_str,
                    subject=subject,
                    attachment_name=attachment_name,
                    year_subdirs=year_subdirs,
                    sender_subdirs=sender_subdirs,
                    existing=existing_paths,
                )

                metadata_text = format_metadata(
                    message=message,
                    part=part,
                    attachment_name=attachment_name,
                    attachment_size=len(data),
                    body_text=body_text,
                )

                write_attachment(output_path, data, metadata_text, dry_run=dry_run)
                total_extracted += 1

                if verbose:
                    action = "Would write" if dry_run else "Wrote"
                    print(f"{action} attachment to {output_path}")
    finally:
        mbox.close()
    return total_extracted


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir

    try:
        count = process_mbox(
            mbox_path=args.mbox,
            output_dir=output_dir,
            max_size_mb=args.max_size_mb,
            year_subdirs=args.year_subdirs,
            sender_subdirs=args.sender_subdirs,
            dry_run=args.dry_run,
            verbose=args.verbose,
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
