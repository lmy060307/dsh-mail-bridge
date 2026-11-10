#!/usr/bin/env python3
"""Mail bridge worker: SMTP send + IMAP read, driven by JSON files.

Usage: mail.py <command> <input.json> <output.json>

Commands: send, list, read, download, folders
Every command receives account settings plus command-specific fields and
writes one JSON object to the output file. Never prints secrets.
"""
import base64
import email
import email.header
import email.utils
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage

TIMEOUT = 30


def decode_header(value):
    if not value:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            except (LookupError, AttributeError):
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).replace("\r", " ").replace("\n", " ").strip()


def imap_utf7_decode(value):
    """Decode an IMAP modified UTF-7 mailbox name into readable text."""
    if "&" not in value:
        return value
    out = []
    i = 0
    while i < len(value):
        char = value[i]
        if char != "&":
            out.append(char)
            i += 1
            continue
        end = value.find("-", i)
        if end < 0:
            out.append(value[i:])
            break
        if end == i + 1:
            out.append("&")
            i = end + 1
            continue
        chunk = value[i + 1:end].replace(",", "/")
        try:
            raw = base64.b64decode(chunk + "=" * (-len(chunk) % 4))
            out.append(raw.decode("utf-16-be"))
        except Exception:
            out.append(value[i:end + 1])
        i = end + 1
    return "".join(out)


def to_iso(value):
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""
    return parsed.isoformat() if parsed else ""


def internal_date(meta):
    """Pull INTERNALDATE out of an IMAP FETCH response prefix."""
    if isinstance(meta, bytes):
        meta = meta.decode("utf-8", "replace")
    match = re.search(r'INTERNALDATE "([^"]+)"', meta or "")
    return to_iso(match.group(1)) if match else ""


def text_of(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def imap_connect(cfg):
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(cfg.get("imap_host", "imap.qq.com"),
                             int(cfg.get("imap_port", 993)),
                             ssl_context=ctx, timeout=TIMEOUT)
    conn.login(cfg["user"], cfg["pass"])
    # Some servers (QQ/163) want the ID extension after login.
    try:
        conn._simple_command("ID", '("name" "dsh-mail-bridge" "version" "1.0")')
        conn.untagged_responses.pop("ID", None)
    except Exception:
        pass
    return conn


def smtp_connect(cfg):
    ctx = ssl.create_default_context()
    host = cfg.get("smtp_host", "smtp.qq.com")
    port = int(cfg.get("smtp_port", 465))
    if port == 587:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        server.starttls(context=ctx)
    else:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
    server.login(cfg["user"], cfg["pass"])
    return server


def mailbox_for(conn, name):
    """Select a mailbox, accepting either raw name or UTF-7 encoded name."""
    candidates = [name]
    try:
        candidates.append(imaplib.IMAP4.encode(name))
    except Exception:
        pass
    last = None
    for candidate in candidates:
        try:
            typ, data = conn.select(f'"{candidate}"', readonly=True)
            if typ == "OK":
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"cannot select mailbox {name!r}: {last}")


def cmd_send(cfg):
    msg = EmailMessage()
    msg["From"] = cfg.get("from") or cfg["user"]
    to = cfg["to"]
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    if cfg.get("cc"):
        cc = cfg["cc"]
        msg["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
    msg["Subject"] = cfg.get("subject", "")
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    text = cfg.get("text", "") or ""
    html = cfg.get("html")
    if html:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(text)
    for item in cfg.get("attachments") or []:
        path = item["path"] if isinstance(item, dict) else item
        if not os.path.isfile(path):
            raise RuntimeError(f"attachment not found: {path}")
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))
    server = smtp_connect(cfg)
    try:
        refused = server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return {"sent": True, "refused": list(refused.keys()) if refused else []}


def cmd_list(cfg):
    conn = imap_connect(cfg)
    try:
        mailbox_for(conn, cfg.get("mailbox") or "INBOX")
        limit = int(cfg.get("limit", 20))
        typ, data = conn.uid("SEARCH", None, "ALL")
        uids = data[0].split() if data and data[0] else []
        recent = uids[-limit:][::-1]
        records = []
        for uid in recent:
            typ, raw = conn.uid("FETCH", uid,
                                "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
            if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                continue
            message = email.message_from_bytes(raw[0][1])
            records.append({
                "uid": uid.decode(),
                "from": decode_header(message.get("From")),
                "to": decode_header(message.get("To")),
                "subject": decode_header(message.get("Subject")),
                "date": decode_header(message.get("Date")) or internal_date(raw[0][0]),
            })
        return {"mailbox": cfg.get("mailbox") or "INBOX", "total": len(uids),
                "messages": records}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def fetch_message(conn, uid):
    typ, raw = conn.uid("FETCH", uid, "(INTERNALDATE RFC822)")
    if typ != "OK" or not raw or not isinstance(raw[0], tuple):
        raise RuntimeError(f"cannot fetch uid {uid}")
    return email.message_from_bytes(raw[0][1]), internal_date(raw[0][0])


def cmd_read(cfg):
    conn = imap_connect(cfg)
    try:
        mailbox_for(conn, cfg.get("mailbox") or "INBOX")
        message, received = fetch_message(conn, str(cfg["uid"]))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    body_text, body_html, attachments = "", None, []
    for part in message.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename or "attachment" in disposition:
            attachments.append({
                "filename": decode_header(filename) if filename else "unnamed",
                "content_type": ctype,
                "size": len(part.get_payload(decode=True) or b""),
            })
        elif ctype == "text/plain" and not body_text:
            body_text = text_of(part)
        elif ctype == "text/html" and body_html is None:
            body_html = text_of(part)
    return {
        "uid": str(cfg["uid"]),
        "from": decode_header(message.get("From")),
        "to": decode_header(message.get("To")),
        "cc": decode_header(message.get("Cc")),
        "subject": decode_header(message.get("Subject")),
        "date": decode_header(message.get("Date")) or received,
        "text": body_text,
        "html": body_html or "",
        "attachments": attachments,
    }


def cmd_download(cfg):
    conn = imap_connect(cfg)
    try:
        mailbox_for(conn, cfg.get("mailbox") or "INBOX")
        message, _received = fetch_message(conn, str(cfg["uid"]))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    target = os.path.abspath(cfg["output_dir"])
    os.makedirs(target, exist_ok=True)
    saved = []
    index = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get("Content-Disposition") or "").lower()
        if not filename and "attachment" not in disposition:
            continue
        index += 1
        name = decode_header(filename) if filename else f"attachment-{index}"
        name = os.path.basename(name).replace("/", "_").replace("\\", "_") or f"attachment-{index}"
        path = os.path.join(target, name)
        if os.path.exists(path):
            stem, ext = os.path.splitext(name)
            path = os.path.join(target, f"{stem}-{index}{ext}")
        payload = part.get_payload(decode=True) or b""
        with open(path, "wb") as fh:
            fh.write(payload)
        saved.append({"path": path, "bytes": len(payload)})
    return {"uid": str(cfg["uid"]), "saved": saved}


def cmd_folders(cfg):
    conn = imap_connect(cfg)
    try:
        typ, data = conn.list()
        names = []
        for line in data or []:
            if not line:
                continue
            text = line.decode("utf-8", errors="replace")
            try:
                text = email.header.decode_header(text)[0][0]
                if isinstance(text, bytes):
                    text = text.decode("utf-8", errors="replace")
            except Exception:
                pass
            parts = text.rsplit('"', 2)
            names.append(imap_utf7_decode(parts[-2] if len(parts) >= 2 else text))
        return {"folders": names}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


COMMANDS = {
    "send": cmd_send,
    "list": cmd_list,
    "read": cmd_read,
    "download": cmd_download,
    "folders": cmd_folders,
}


KEY_ALIASES = {
    "smtpHost": "smtp_host",
    "smtpPort": "smtp_port",
    "imapHost": "imap_host",
    "imapPort": "imap_port",
}


def normalize(cfg):
    merged = dict(cfg)
    for src, dst in KEY_ALIASES.items():
        if src in merged and dst not in merged:
            merged[dst] = merged[src]
    return merged


def main():
    if len(sys.argv) != 4:
        print("usage: mail.py <command> <input.json> <output.json>", file=sys.stderr)
        return 2
    command, input_path, output_path = sys.argv[1:4]
    try:
        with open(input_path, "r", encoding="utf-8-sig") as fh:
            cfg = normalize(json.load(fh))
        handler = COMMANDS.get(command)
        if handler is None:
            raise RuntimeError(f"unknown command: {command}")
        result = {"ok": True, "data": handler(cfg)}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
