#!/usr/bin/env python3
"""Mail bridge worker: SMTP send + IMAP read, driven by JSON files.

Usage: mail.py <command> <input.json> <output.json>

Commands: send, list, read, download, folders
Every command receives account settings plus command-specific fields and
writes one JSON object to the output file. Never prints secrets.
"""
import base64
import datetime
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

def imap_utf7_encode(name):
    """Encode a mailbox name into IMAP modified UTF-7."""
    out = []
    buffer = []

    def flush():
        if not buffer:
            return
        raw = "".join(buffer).encode("utf-16-be")
        token = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append("&" + token + "-")
        del buffer[:]

    for char in name:
        code = ord(char)
        if char == "&":
            flush()
            out.append("&-")
        elif 0x20 <= code <= 0x7E:
            flush()
            out.append(char)
        else:
            buffer.append(char)
    flush()
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


def mailbox_for(conn, name, readonly=True):
    """Select a mailbox, accepting either raw name or UTF-7 encoded name."""
    candidates = [name]
    try:
        candidates.append(imap_utf7_encode(name))
    except Exception:
        pass
    last = None
    for candidate in candidates:
        try:
            typ, data = conn.select(f'"{candidate}"', readonly=readonly)
            if typ == "OK":
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"cannot select mailbox {name!r}: {last}")


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(value):
    """Accept YYYY-MM-DD or DD-Mon-YYYY and return the IMAP date form."""
    text = str(value).strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if not match:
        return text
    year, month, day = match.groups()
    return f"{day}-{MONTHS[int(month) - 1]}-{year}"


def quoted(value):
    """Quote one IMAP search argument."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def search_criteria(cfg):
    criteria = []
    for key, prefix in (("from", "FROM"), ("to", "TO"),
                        ("subject", "SUBJECT"), ("text", "TEXT")):
        value = cfg.get(key)
        if value:
            criteria.extend([prefix, quoted(value)])
    if cfg.get("since"):
        criteria.extend(["SINCE", quoted(imap_date(cfg["since"]))])
    if cfg.get("before"):
        criteria.extend(["BEFORE", quoted(imap_date(cfg["before"]))])
    return criteria


def parse_when(value):
    """Parse an RFC 2822 or ISO-8601 timestamp into an aware datetime."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def describe(conn, uid, raw_meta, header_bytes):
    message = email.message_from_bytes(header_bytes or b"")
    stamp = parse_when(decode_header(message.get("Date")))
    return {
        "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
        "from": decode_header(message.get("From")),
        "to": decode_header(message.get("To")),
        "subject": decode_header(message.get("Subject")),
        "date": stamp.astimezone().isoformat() if stamp else internal_date(raw_meta),
    }



def run_search(conn, criteria):
    """Server-side SEARCH; falls back when the server refuses UTF-8."""
    try:
        typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria)
    except UnicodeEncodeError:
        typ, data = conn.uid("SEARCH", *criteria)
    if typ != "OK":
        head = data[0] if data else b""
        reason = head.decode("utf-8", "replace") if isinstance(head, bytes) else str(head)
        raise RuntimeError(f"server refused search: {reason}")
    return (data[0] or b"").split(), True



def build_message(cfg):
    msg = EmailMessage()
    msg["From"] = cfg.get("from") or cfg["user"]
    to = cfg.get("to")
    msg["To"] = ", ".join(to) if isinstance(to, list) else (to or "")
    cc = cfg.get("cc")
    if cc:
        msg["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
    bcc = cfg.get("bcc")
    if bcc:
        msg["Bcc"] = ", ".join(bcc) if isinstance(bcc, list) else bcc
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
    return msg


def deliver(cfg, msg):
    server = smtp_connect(cfg)
    try:
        refused = server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return {"sent": True, "refused": list(refused.keys()) if refused else []}


def cmd_send(cfg):
    return deliver(cfg, build_message(cfg))


DRAFT_LINKS = {
    "qq.com": "https://mail.qq.com/",
    "foxmail.com": "https://mail.qq.com/",
    "gmail.com": "https://mail.google.com/mail/u/0/#drafts",
    "outlook.com": "https://outlook.live.com/mail/0/drafts",
    "hotmail.com": "https://outlook.live.com/mail/0/drafts",
    "163.com": "https://mail.163.com/",
    "126.com": "https://mail.163.com/",
}


def draft_link(user):
    domain = (user or "").split("@")[-1].strip().lower()
    return DRAFT_LINKS.get(domain, "")


def cmd_draft(cfg):
    msg = build_message(cfg)
    mailbox = cfg.get("draft_mailbox") or "Drafts"
    conn = imap_connect(cfg)
    try:
        encoded = quoted(imap_utf7_encode(mailbox))
        typ, data = conn.list('""', encoded)
        if typ != "OK" or not any(data or []):
            raise RuntimeError(f"draft mailbox not found: {mailbox}")
        stamp = datetime.datetime.now().astimezone()
        typ, data = conn.append(encoded, "(\\Draft)",
                                imaplib.Time2Internaldate(stamp.timestamp()),
                                msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"APPEND failed: {status_text(data)}")
        match = re.search(r"APPENDUID\s+\d+\s+(\d+)", status_text(data))
        uid = match.group(1) if match else ""
        return {
            "uid": uid,
            "mailbox": mailbox,
            "to": msg["To"] or "",
            "subject": msg["Subject"] or "",
            "link": draft_link(cfg.get("user")),
            "note": "Draft saved. Open the link, find it under Drafts, edit and send from there.",
        }
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def queue_path(cfg):
    root = cfg.get("queueDir") or cfg.get("queue_dir") or os.path.join(
        os.path.expanduser("~"), ".dsh", "mail-bridge", "queue")
    os.makedirs(root, exist_ok=True)
    return root


def cmd_schedule(cfg):
    when = cfg.get("send_at")
    if not when:
        raise RuntimeError("send_at is required")
    stamp = parse_when(when)
    if stamp is None:
        raise RuntimeError(f"cannot parse send_at: {when}")
    now = datetime.datetime.now().astimezone()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=now.tzinfo)
    root = queue_path(cfg)
    job_id = "%s-%s" % (stamp.strftime("%Y%m%dT%H%M%S"),
                        base64.b32encode(os.urandom(3)).decode().lower().rstrip("="))
    payload = {key: cfg[key] for key in ("to", "cc", "bcc", "subject", "text", "html",
                                         "attachments", "reply_to") if key in cfg}
    job = {"id": job_id, "send_at": stamp.isoformat(), "created_at": now.isoformat(),
           "payload": payload}
    target = os.path.join(root, job_id + ".json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False, indent=2)
    return {"id": job_id, "send_at": job["send_at"], "to": payload.get("to"),
            "subject": payload.get("subject", "")}


def cmd_dispatch(cfg):
    root = queue_path(cfg)
    now = datetime.datetime.now().astimezone()
    sent, failed, pending = [], [], 0
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                job = json.load(fh)
            stamp = datetime.datetime.fromisoformat(job["send_at"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=now.tzinfo)
        except Exception as exc:  # noqa: BLE001
            os.replace(path, path + ".broken")
            failed.append({"id": name, "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        if stamp > now:
            pending += 1
            continue
        try:
            payload = dict(job["payload"])
            for key in ("user", "pass", "smtp_host", "smtp_port", "from"):
                if key in cfg and key not in payload:
                    payload[key] = cfg[key]
            result = cmd_send(payload)
            os.remove(path)
            sent.append({"id": name, "send_at": job["send_at"],
                         "late": (now - stamp).total_seconds() > 60, "result": result})
        except Exception as exc:  # noqa: BLE001
            os.replace(path, path + ".failed")
            failed.append({"id": name, "send_at": job["send_at"],
                           "error": "%s: %s" % (type(exc).__name__, exc)})
    return {"sent": sent, "failed": failed, "pending": pending}


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


def download_one(cfg):
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


def cmd_download(cfg):
    uids = uid_list(cfg)
    if not uids:
        raise RuntimeError("download needs uid or uids")
    results, all_saved = [], []
    for uid in uids:
        sub = dict(cfg)
        sub.pop("uids", None)
        sub["uid"] = uid
        data = download_one(sub)
        results.append({"uid": uid, "saved": data["saved"]})
        all_saved.extend(data["saved"])
    return {"uid": uids[0], "uids": uids, "saved": all_saved, "results": results}


def fetch_headers(conn, uid):
    """Fetch one message header; returns a record dict or None."""
    uid = uid.decode() if isinstance(uid, bytes) else str(uid)
    typ, raw = conn.uid("FETCH", uid,
                        "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
    if typ != "OK" or not raw or not isinstance(raw[0], tuple):
        return None
    meta, header = raw[0][0], raw[0][1]
    match = re.search(rb"UID (\d+)", meta)
    actual = match.group(1) if match else uid
    return describe(conn, actual, meta, header)


def fetch_header_batch(conn, uids):
    """Fetch headers for many uids with a few round trips."""
    records = []
    for start in range(0, len(uids), 80):
        chunk = [u.decode() if isinstance(u, bytes) else str(u)
                 for u in uids[start:start + 80]]
        typ, data = conn.uid(
            "FETCH", ",".join(chunk),
            "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
        if typ != "OK":
            continue
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            meta, header = item
            match = re.search(rb"UID (\d+)", meta)
            if not match:
                continue
            records.append(describe(conn, match.group(1), meta, header))
    return records


def fetch_body_text(conn, uid):
    try:
        typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[TEXT])")
    except imaplib.IMAP4.error:
        return ""
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return ""
    return data[0][1].decode("utf-8", "replace")


def local_search(conn, cfg, scan_limit):
    """Fallback: pull recent headers, filter them in Python."""
    typ, data = conn.uid("SEARCH", None, "ALL")
    uids = (data[0] or b"").split() if data else []
    recent = uids[-scan_limit:]
    wants = {key: (cfg.get(key) or "").strip().lower()
             for key in ("from", "to", "subject", "text")}
    since = str(cfg.get("since") or "")
    before = str(cfg.get("before") or "")
    records = fetch_header_batch(conn, recent)
    records.sort(key=lambda item: int(item["uid"]), reverse=True)
    hits = []
    for record in records:
        if wants["from"] and wants["from"] not in record["from"].lower():
            continue
        if wants["to"] and wants["to"] not in record["to"].lower():
            continue
        if wants["subject"] and wants["subject"] not in record["subject"].lower():
            continue
        if since or before:
            stamp = parse_when(record["date"])
            if stamp is None:
                continue
            day = stamp.date().isoformat()
            if since and day < since:
                continue
            if before and day >= before:
                continue
        if wants["text"] and wants["text"] not in fetch_body_text(conn, record["uid"]).lower():
            continue
        hits.append(record)
    return hits


def cmd_search(cfg):
    mailbox = cfg.get("mailbox") or "INBOX"
    limit = int(cfg.get("limit", 20))
    scan_limit = int(cfg.get("scan_limit", 500))
    conn = imap_connect(cfg)
    try:
        mailbox_for(conn, mailbox)
        criteria = search_criteria(cfg)
        if not criteria:
            raise RuntimeError("search needs one of: from, to, subject, text, since, before")
        note = ""
        typ, data = conn.uid("SEARCH", None, "ALL")
        all_uids = (data[0] or b"").split() if data else []
        matched = []
        try:
            matched, _ok = run_search(conn, criteria)
            if len(matched) >= len(all_uids) > 1:
                raise RuntimeError("server matched every message")
        except RuntimeError as exc:
            note = f"{exc}; scanned {min(scan_limit, len(all_uids))} recent messages locally"
            records = local_search(conn, cfg, scan_limit)
            return {"mailbox": mailbox, "matched": len(records),
                    "messages": records[:limit], "note": note}
        records = []
        for uid in matched[-limit:][::-1]:
            record = fetch_headers(conn, uid)
            if record is not None:
                records.append(record)
        return {"mailbox": mailbox, "matched": len(matched), "messages": records,
                "note": note}
    finally:
        try:
            conn.logout()
        except Exception:
            pass



def uid_list(cfg):
    uids = cfg.get("uids")
    if uids:
        return [str(item) for item in uids]
    if cfg.get("uid"):
        return [str(cfg["uid"])]
    return []


def cmd_mark(cfg):
    uids = uid_list(cfg)
    if not uids:
        raise RuntimeError("mark needs uid or uids")
    changes = []
    if "read" in cfg and cfg["read"] is not None:
        changes.append(("\\Seen", bool(cfg["read"])))
    if "flagged" in cfg and cfg["flagged"] is not None:
        changes.append(("\\Flagged", bool(cfg["flagged"])))
    if not changes:
        raise RuntimeError("mark needs read and/or flagged")
    conn = imap_connect(cfg)
    updated = []
    try:
        mailbox_for(conn, cfg.get("mailbox") or "INBOX", readonly=False)
        for uid in uids:
            uid = uid.decode() if isinstance(uid, bytes) else str(uid)
            for flag, enabled in changes:
                conn.uid("STORE", uid, "+FLAGS" if enabled else "-FLAGS", f"({flag})")
            updated.append(uid)
        return {"updated": updated}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def status_text(data):
    if not data:
        return "unknown IMAP error"
    first = data[0]
    return first.decode("utf-8", "replace") if isinstance(first, bytes) else str(first)


def cmd_move(cfg):
    uids = uid_list(cfg)
    target = cfg.get("to")
    if not uids or not target:
        raise RuntimeError("move needs uid/uids and a destination in 'to'")
    encoded = quoted(imap_utf7_encode(target))
    conn = imap_connect(cfg)
    moved, failed, reasons = [], [], []
    try:
        mailbox_for(conn, cfg.get("mailbox") or "INBOX", readonly=False)
        for uid in uids:
            uid = uid.decode() if isinstance(uid, bytes) else str(uid)
            try:
                typ, data = conn.uid("MOVE", uid, encoded)
                if typ == "OK":
                    moved.append(uid)
                    continue
                reasons.append(status_text(data))
            except imaplib.IMAP4.error as exc:
                reasons.append(str(exc))
            try:
                typ, data = conn.uid("COPY", uid, encoded)
                if typ != "OK":
                    reasons.append(status_text(data))
                    failed.append(uid)
                    continue
                conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                try:
                    conn.uid("EXPUNGE", uid)
                except imaplib.IMAP4.error:
                    conn.expunge()
                moved.append(uid)
            except imaplib.IMAP4.error as exc:
                failed.append(f"{uid}: {exc}")
        return {"moved": moved, "failed": failed, "to": target,
                "note": "; ".join(sorted(set(reasons)))}
    finally:
        try:
            conn.logout()
        except Exception:
            pass



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
    "search": cmd_search,
    "mark": cmd_mark,
    "move": cmd_move,
    "draft": cmd_draft,
    "schedule": cmd_schedule,
    "dispatch": cmd_dispatch,
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
