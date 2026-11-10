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
      description: 'Save every attachment of one message to a directory and return the saved paths.',
      parameters: schema(
        {
          uid: { type: 'string', description: 'Message uid from mail_list.' },
          output_dir: { type: 'string', description: 'Absolute directory to write attachments into.' },
          mailbox: { type: 'string', description: 'Mailbox name, default INBOX.' },
        },
        ['uid', 'output_dir'],
      ),
      output: {
        schema: {
          type: 'object',
          properties: {
            uid: { type: 'string' },
            saved: { type: 'array', items: { type: 'object' } },
          },
        },
        render: textRender,
      },
      async execute(args, exec) {
        return run('download', args, exec.signal)
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
