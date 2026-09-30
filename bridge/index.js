// WhatsApp linked-device bridge for the Family Assistant.
//
// Connects to WhatsApp like WhatsApp Web (scan a QR code once), forwards
// incoming messages to the Python backend and sends replies on its behalf.
// Unofficial (Baileys): keep volumes low and human-like.

import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
  getContentType,
  isJidBroadcast,
  isJidGroup,
  isJidNewsletter,
  isLidUser,
  jidDecode,
  jidNormalizedUser,
  normalizeMessageContent,
  useMultiFileAuthState,
} from 'baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'

const here = path.dirname(fileURLToPath(import.meta.url))
const envFile = path.join(here, '..', '.env')
if (fs.existsSync(envFile)) process.loadEnvFile(envFile)

const PORT = Number(process.env.BRIDGE_PORT || 3001)
const TOKEN = process.env.BRIDGE_TOKEN || ''
const BACKEND = (process.env.BACKEND_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')
const MAX_PER_HOUR = Number(process.env.BRIDGE_MAX_PER_HOUR || 30)
const AUTH_DIR = path.join(here, 'auth')
const MAX_MEDIA_BYTES = 5 * 1024 * 1024
const MAX_AGE_MS = 10 * 60 * 1000

if (!TOKEN) {
  console.error('BRIDGE_TOKEN is empty in .env - set any long random string there.')
  process.exit(1)
}

const logger = pino({ level: process.env.BRIDGE_LOG_LEVEL || 'error' })
const sentIds = new Set() // ids of messages this bridge sent (to ignore their echoes)
const recent = new Map() // id -> WAMessage, for quoting replies in groups
const sendLog = [] // timestamps for the hourly rate limit
let sock = null
let ready = false

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const remember = (m) => {
  recent.set(m.key.id, m)
  if (recent.size > 300) recent.delete(recent.keys().next().value)
}
const phoneOf = (jid) => (jid ? jidDecode(jid)?.user || '' : '')

async function toPhoneJid(jid, alt) {
  if (alt && !isLidUser(alt)) return alt
  if (jid && isLidUser(jid)) {
    try {
      const pn = await sock.signalRepository.lidMapping.getPNForLID(jid)
      if (pn) return pn
    } catch {}
  }
  return jid
}

function textOf(content) {
  if (!content) return ''
  return (
    content.conversation ||
    content.extendedTextMessage?.text ||
    content.imageMessage?.caption ||
    content.videoMessage?.caption ||
    ''
  )
}

async function forward(m) {
  const key = m.key
  const chat = key.remoteJid
  if (!chat || chat === 'status@broadcast' || isJidBroadcast(chat) || isJidNewsletter(chat)) return
  if (sentIds.has(key.id)) return // our own reply echoing back
  const content = normalizeMessageContent(m.message)
  if (!content) return
  const type = getContentType(content)
  if (!type || type === 'protocolMessage' || type === 'reactionMessage' || type === 'senderKeyDistributionMessage') return
  remember(m)

  const me = sock.user || {}
  const myIds = [me.id, me.lid].filter(Boolean).map((j) => jidNormalizedUser(j))
  const isGroup = Boolean(isJidGroup(chat))
  const senderJid = isGroup
    ? await toPhoneJid(key.participant, key.participantAlt)
    : await toPhoneJid(chat, key.remoteJidAlt)
  const selfChat = !isGroup && myIds.includes(jidNormalizedUser(senderJid || chat))

  const ctx = content.extendedTextMessage?.contextInfo || content.imageMessage?.contextInfo || {}
  const mentioned = (ctx.mentionedJid || []).map((j) => jidNormalizedUser(j))
  const addressed =
    mentioned.some((j) => myIds.includes(j)) ||
    (ctx.participant && myIds.includes(jidNormalizedUser(ctx.participant))) ||
    (ctx.stanzaId && sentIds.has(ctx.stanzaId))

  let kind = 'unsupported'
  if (type === 'conversation' || type === 'extendedTextMessage') kind = 'text'
  else if (type === 'imageMessage') kind = 'image'
  else if (type === 'audioMessage') kind = 'audio'

  const payload = {
    message_id: key.id,
    phone: phoneOf(senderJid),
    chat_id: chat,
    is_group: isGroup,
    group_name: null,
    addressed_to_bot: Boolean(addressed),
    from_me: Boolean(key.fromMe),
    self_chat: selfChat,
    kind,
    text: textOf(content),
    caption: content.imageMessage?.caption || null,
    profile_name: m.pushName || null,
  }
  if (isGroup) {
    try {
      payload.group_name = (await sock.groupMetadata(chat)).subject
    } catch {}
  }
  if (kind === 'image' && !key.fromMe) {
    try {
      const buf = await downloadMediaMessage(m, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage })
      if (buf.length <= MAX_MEDIA_BYTES) {
        payload.media_b64 = buf.toString('base64')
        payload.media_mime = (content.imageMessage.mimetype || 'image/jpeg').split(';')[0]
      }
    } catch (e) {
      logger.warn({ err: e?.message }, 'media download failed')
    }
  }

  for (let attempt = 1; attempt <= 5; attempt++) {
    try {
      const res = await fetch(`${BACKEND}/bridge/incoming`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-bridge-token': TOKEN },
        body: JSON.stringify(payload),
      })
      if (res.ok) return
      console.error(`backend answered ${res.status} for message ${key.id}`)
      if (res.status < 500) return
    } catch (e) {
      console.error(`Python-сервер бота недоступен (попытка ${attempt}/5): ${e?.message}. Журнал: database/server.log`)
    }
    await sleep(2000 * attempt)
  }
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  sock = makeWASocket({
    auth: state,
    logger,
    browser: Browsers.macOS('Desktop'),
    markOnlineOnConnect: false,
    syncFullHistory: false,
  })
  sock.ev.on('creds.update', saveCreds)
  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('\nОтсканируйте QR-код в WhatsApp на телефоне: Настройки → Связанные устройства → Привязка устройства\n')
      qrcode.generate(qr, { small: true })
    }
    if (connection === 'open') {
      ready = true
      console.log(`✅ WhatsApp подключён как ${phoneOf(sock.user?.id)}. Бот работает. Не закрывайте это окно.`)
    }
    if (connection === 'close') {
      ready = false
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        console.error('Сессия WhatsApp завершена (устройство отвязано). Удалите папку bridge/auth и запустите снова.')
        process.exit(1)
      }
      console.log(code === DisconnectReason.restartRequired
        ? 'Привязка прошла, перезапускаю соединение (это нормально)...'
        : 'Соединение прервано, переподключаюсь...')
      setTimeout(start, 3000)
    }
  })
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify' && type !== 'append') return
    for (const m of messages) {
      // never react to old messages delivered after a reconnect
      const ts = Number(m.messageTimestamp || 0) * 1000
      if (ts && Date.now() - ts > MAX_AGE_MS) continue
      if (type === 'append' && !m.key?.fromMe) continue
      try {
        await forward(m)
      } catch (e) {
        console.error('failed to handle message', e?.message)
      }
    }
  })
}

async function resolveTarget(to) {
  if (to === 'me') return jidNormalizedUser(sock.user.id)
  if (to.includes('@')) return to
  return `${to.replace(/\D/g, '')}@s.whatsapp.net`
}

async function send({ to, text, quote_id }) {
  const now = Date.now()
  while (sendLog.length && now - sendLog[0] > 3600_000) sendLog.shift()
  if (sendLog.length >= MAX_PER_HOUR) return { status: 429, body: { error: 'hourly limit reached' } }
  const jid = await resolveTarget(to)
  const selfTarget = to === 'me'
  if (!selfTarget) {
    // look like a person: "typing..." and a pause proportional to the text length
    try {
      await sock.presenceSubscribe(jid)
      await sock.sendPresenceUpdate('composing', jid)
    } catch {}
    await sleep(Math.min(2500 + text.length * 60, 12000) + Math.random() * 2000)
    try {
      await sock.sendPresenceUpdate('paused', jid)
    } catch {}
  }
  const options = quote_id && recent.has(quote_id) ? { quoted: recent.get(quote_id) } : {}
  const sent = await sock.sendMessage(jid, { text }, options)
  sentIds.add(sent.key.id)
  if (sentIds.size > 2000) sentIds.delete(sentIds.values().next().value)
  sendLog.push(Date.now())
  return { status: 200, body: { id: sent.key.id } }
}

http
  .createServer(async (req, res) => {
    const reply = (status, body) => {
      res.writeHead(status, { 'content-type': 'application/json' })
      res.end(JSON.stringify(body))
    }
    if (req.method === 'GET' && req.url === '/health') return reply(200, { ready })
    if (req.headers['x-bridge-token'] !== TOKEN) return reply(401, { error: 'bad token' })
    if (req.method !== 'POST' || req.url !== '/send') return reply(404, { error: 'not found' })
    if (!ready) return reply(503, { error: 'whatsapp not connected' })
    let raw = ''
    for await (const chunk of req) raw += chunk
    try {
      const body = JSON.parse(raw)
      if (!body.to || !body.text) return reply(400, { error: 'to and text are required' })
      const out = await send(body)
      reply(out.status, out.body)
    } catch (e) {
      console.error('send failed', e?.message)
      reply(500, { error: 'send failed' })
    }
  })
  .listen(PORT, '127.0.0.1', () => console.log(`bridge API on http://127.0.0.1:${PORT}`))

start().catch((e) => {
  console.error('failed to start WhatsApp connection', e)
  process.exit(1)
})
