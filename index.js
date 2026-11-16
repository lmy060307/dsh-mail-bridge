import { spawn } from 'node:child_process'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const inject = ['tools']

const WORKER = fileURLToPath(new URL('./scripts/mail.py', import.meta.url))

function textRender(args, value) {
  return [{ type: 'text', text: JSON.stringify(value, null, 2) }]
}

function schema(properties, required) {
  return { type: 'object', properties, required, additionalProperties: false }
}

export function apply(ctx, config) {
  const settings = config ?? {}

  async function run(command, payload, signal) {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-mail-'))
    const inputFile = path.join(dir, 'in.json')
    const outputFile = path.join(dir, 'out.json')
    try {
      await fs.writeFile(inputFile, JSON.stringify({ ...settings, ...payload }), 'utf8')
      const python = settings.pythonPath ?? 'python'
      await new Promise((resolve, reject) => {
        const child = spawn(python, [WORKER, command, inputFile, outputFile], {
          stdio: 'ignore',
          windowsHide: true,
        })
        const timer = setTimeout(() => {
          child.kill()
          reject(new Error(`mail worker timed out after ${settings.timeoutMs ?? 60000}ms`))
        }, settings.timeoutMs ?? 60000)
        const abort = () => {
          child.kill()
          reject(new Error('mail worker aborted'))
        }
        signal?.addEventListener('abort', abort, { once: true })
        child.on('error', (error) => {
          clearTimeout(timer)
          signal?.removeEventListener('abort', abort)
          reject(error)
        })
        child.on('close', () => {
          clearTimeout(timer)
          signal?.removeEventListener('abort', abort)
          resolve()
        })
      })
      const raw = await fs.readFile(outputFile, 'utf8')
      const result = JSON.parse(raw)
      if (!result.ok) throw new Error(result.error ?? 'mail worker failed')
      return result.data
    } finally {
      await fs.rm(dir, { recursive: true, force: true }).catch(() => {})
    }
  }

  const tools = [
    {
      name: 'mail_send',
      description:
        'Send an email through the configured SMTP account. Supports plain text, HTML, multiple recipients and file attachments.',
      parameters: schema(
        {
          to: {
            oneOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }],
            description: 'Recipient address, or a list of addresses.',
          },
          subject: { type: 'string', description: 'Subject line.' },
          text: { type: 'string', description: 'Plain-text body.' },
          html: { type: 'string', description: 'Optional HTML body; text becomes the fallback part.' },
          cc: {
            oneOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }],
            description: 'Optional CC address or list.',
          },
          attachments: {
            type: 'array',
            items: { type: 'string' },
            description: 'Absolute file paths to attach.',
          },
        },
        ['to', 'subject'],
      ),
      output: {
        schema: {
          type: 'object',
          properties: { sent: { type: 'boolean' }, refused: { type: 'array', items: { type: 'string' } } },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('send', args, exec.signal)
      },
    },
    {
      name: 'mail_list',
      description:
        'List recent messages in a mailbox (default INBOX), newest first. Returns uid, from, to, subject and date; use the uid with mail_read or mail_download.',
      parameters: schema(
        {
          limit: { type: 'integer', description: 'How many messages to list, default 20.' },
          mailbox: { type: 'string', description: 'Mailbox name, default INBOX.' },
        },
        [],
      ),
      output: {
        schema: {
          type: 'object',
          properties: {
            mailbox: { type: 'string' },
            total: { type: 'integer' },
            messages: { type: 'array', items: { type: 'object' } },
          },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('list', args, exec.signal)
      },
    },
    {
      name: 'mail_read',
      description: 'Read one message by uid: headers, plain-text body, HTML body and the attachment list.',
      parameters: schema(
        {
          uid: { type: 'string', description: 'Message uid from mail_list.' },
          mailbox: { type: 'string', description: 'Mailbox name, default INBOX.' },
        },
        ['uid'],
      ),
      output: {
        schema: {
          type: 'object',
          properties: {
            uid: { type: 'string' },
            from: { type: 'string' },
            to: { type: 'string' },
            subject: { type: 'string' },
            date: { type: 'string' },
            text: { type: 'string' },
            html: { type: 'string' },
            attachments: { type: 'array', items: { type: 'object' } },
          },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('read', args, exec.signal)
      },
    },
    {
      name: 'mail_download',
      description:
        'Save every attachment of one or more messages to a directory and return the saved paths. Pass uid for a single message or uids for several.',
      parameters: schema(
        {
          uid: { type: 'string', description: 'Message uid from mail_list.' },
          uids: {
            type: 'array',
            items: { type: 'string' },
            description: 'Download the attachments of several messages at once.',
          },
          output_dir: { type: 'string', description: 'Absolute directory to write attachments into.' },
          mailbox: { type: 'string', description: 'Mailbox name, default INBOX.' },
        },
        ['output_dir'],
      ),
      output: {
        schema: {
          type: 'object',
          properties: {
            uid: { type: 'string' },
            uids: { type: 'array', items: { type: 'string' } },
            saved: { type: 'array', items: { type: 'object' } },
            results: { type: 'array', items: { type: 'object' } },
          },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('download', args, exec.signal)
      },
    },
    {
      name: 'mail_search',
      description:
        'Search messages in a mailbox by sender, recipient, subject, body text or date range. Falls back to scanning recent messages when the server rejects the search.',
      parameters: schema(
        {
          from: { type: 'string', description: 'Match the From header (substring).' },
          to: { type: 'string', description: 'Match the To header (substring).' },
          subject: { type: 'string', description: 'Match the subject (substring).' },
          text: { type: 'string', description: 'Match the message body (substring).' },
          since: { type: 'string', description: 'Earliest date, YYYY-MM-DD.' },
          before: { type: 'string', description: 'Latest date (exclusive), YYYY-MM-DD.' },
          limit: { type: 'integer', description: 'Max results, default 20.' },
          scan_limit: { type: 'integer', description: 'Messages scanned in local fallback, default 500.' },
          mailbox: { type: 'string', description: 'Mailbox name, default INBOX.' },
        },
        [],
      ),
      output: {
        schema: {
          type: 'object',
          properties: {
            mailbox: { type: 'string' },
            matched: { type: 'integer' },
            messages: { type: 'array', items: { type: 'object' } },
            note: { type: 'string' },
          },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('search', args, exec.signal)
      },
    },
    {
      name: 'mail_mark',
      description:
        'Change flags on one or more messages: mark read/unread, or set/clear the star.',
      parameters: schema(
        {
          uid: { type: 'string', description: 'Message uid from mail_list.' },
          uids: { type: 'array', items: { type: 'string' }, description: 'Apply to several messages.' },
          read: { type: 'boolean', description: 'true marks read, false marks unread.' },
          flagged: { type: 'boolean', description: 'true stars the message, false removes the star.' },
          mailbox: { type: 'string', description: 'Mailbox name, default INBOX.' },
        },
        [],
      ),
      output: {
        schema: {
          type: 'object',
          properties: { updated: { type: 'array', items: { type: 'string' } } },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('mark', args, exec.signal)
      },
    },
    {
      name: 'mail_move',
      description:
        'Move one or more messages to another mailbox. Falls back to copy + delete when the server has no MOVE support.',
      parameters: schema(
        {
          uid: { type: 'string', description: 'Message uid from mail_list.' },
          uids: { type: 'array', items: { type: 'string' }, description: 'Move several messages.' },
          to: {
            type: 'string',
            description: 'Destination mailbox name, for example a folder returned by mail_folders.',
          },
          mailbox: { type: 'string', description: 'Source mailbox, default INBOX.' },
        },
        ['to'],
      ),
      output: {
        schema: {
          type: 'object',
          properties: {
            moved: { type: 'array', items: { type: 'string' } },
            failed: { type: 'array', items: { type: 'string' } },
            to: { type: 'string' },
            note: { type: 'string' },
          },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('move', args, exec.signal)
      },
    },
    {
      name: 'mail_folders',
      description: 'List the mailboxes (folders) available on the IMAP account.',
      parameters: schema({}, []),
      output: {
        schema: {
          type: 'object',
          properties: { folders: { type: 'array', items: { type: 'string' } } },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('folders', args, exec.signal)
      },
    },
  ]

  for (const tool of tools) {
    ctx.effect(() => ctx.tools.register(tool))
  }
}
