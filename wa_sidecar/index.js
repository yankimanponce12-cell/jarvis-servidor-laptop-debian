'use strict';
// Jarvis · puente WhatsApp (dispositivo vinculado vía Baileys, idea tomada de SARA).
// Solo escucha en 127.0.0.1 y exige el token de wa_config.json. Lo arranca jarvis/wa.py.
//
//   GET  /status                      estado de la vinculación
//   POST /connect {phone}             vincula con código de 8 letras (se teclea en el teléfono)
//   POST /logout                      desvincula
//   GET  /events?since=N&wait=25      mensajes y llamadas nuevos (espera hasta `wait` s si no hay)
//   POST /send {to, text}             texto
//   POST /send {to, tipo, archivo, caption, mimetype, nombre}   imagen | audio | nota | video | documento
//   GET  /contacts?q=mama             busca en la libreta
//   GET  /chats                       último mensaje de cada chat
//   POST /read {jid}                  marca como leído lo recibido en ese chat
//   POST /reject {id, from}           cuelga una llamada entrante
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const pino = require('pino');
const {
  default: makeWASocket, useMultiFileAuthState, DisconnectReason,
  fetchLatestBaileysVersion, Browsers, downloadMediaMessage,
} = require('@whiskeysockets/baileys');

const PORT = parseInt(process.env.WA_PORT || '8790', 10);
const TOKEN = process.env.WA_TOKEN || '';
const DATA = process.env.WA_DATA || path.join(__dirname, 'datos');
const AUTH_DIR = path.join(DATA, 'auth');
const MEDIA_DIR = path.join(DATA, 'media');      // lo recibido; jarvis/wa.py lo borra al reenviarlo
const MEDIA_MAX = 45 * 1024 * 1024;              // Telegram no deja a los bots subir más de 50 MB
const MAX_BUF = 500;
const BOOT = crypto.randomBytes(4).toString('hex');
const logger = pino({ level: 'silent' });
const log = (...a) => console.log('[wa]', ...a);

if (!TOKEN) { console.error('[wa] falta WA_TOKEN'); process.exit(1); }
fs.mkdirSync(MEDIA_DIR, { recursive: true });

let sock = null, estado = 'desconectado', yo = null, seq = 0;
const eventos = [];                    // {id, tipo: mensaje|llamada|conexion, ...}
let esperando = [];                    // long polling de /events
const enviados = new Set();            // ids que mandó Jarvis (no reenviarlos como "nuevos")
const enviadosMsg = new Map();         // id -> mensaje: WhatsApp lo pide de nuevo si el otro no lo pudo descifrar
const ultimasClaves = new Map();       // jid -> claves recibidas sin leer (para /read)
const contactos = new Map();           // jid -> {jid, nombre (tu libreta), notify (como se llaman ellos)}
const CONT_FILE = path.join(DATA, 'contactos.json');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const limpiarJid = (j) => String(j || '').replace(/:\d+@/, '@');
const esPersona = (j) => /@(s\.whatsapp\.net|lid)$/.test(String(j || ''));
const plano = (s) => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();

try { for (const c of JSON.parse(fs.readFileSync(CONT_FILE, 'utf8'))) contactos.set(c.jid, c); } catch (_) {}
let contTimer = null;
function guardarContactos() {
  if (contTimer) return;
  contTimer = setTimeout(() => {
    contTimer = null;
    try { fs.writeFileSync(CONT_FILE, JSON.stringify([...contactos.values()]), { mode: 0o600 }); } catch (_) {}
  }, 5000);
}
const nombreDe = (c) => (c.nombre || c.notify || '');
function guardarContacto(jid, nombre, esLibreta) {
  jid = limpiarJid(jid);
  if (!jid || !nombre || !esPersona(jid)) return;
  const c = contactos.get(jid) || { jid, nombre: '', notify: '' };
  if (esLibreta) c.nombre = String(nombre); else c.notify = String(nombre);
  contactos.set(jid, c);
  guardarContactos();
}
function registrarContacto(c) {
  for (const id of [c.id, c.lid, c.phoneNumber].filter(Boolean)) {
    guardarContacto(id, c.name || c.verifiedName, true);
    guardarContacto(id, c.notify, false);
  }
}
const nombreJid = (jid, push) => nombreDe(contactos.get(jid) || {}) || push || jid.split('@')[0];

function emitir(ev) {
  ev.id = ++seq;
  ev.ts = ev.ts || Math.floor(Date.now() / 1000);
  eventos.push(ev);
  if (eventos.length > MAX_BUF) eventos.shift();
  const w = esperando; esperando = [];
  for (const f of w) f();
}

function contenido(m) {
  let x = m.message || {};
  x = x.ephemeralMessage?.message || x.viewOnceMessage?.message || x.viewOnceMessageV2?.message ||
      x.documentWithCaptionMessage?.message || x;
  return x;
}

// [tipo, nodo del mensaje] de lo que se puede descargar
function mediaDe(x) {
  if (x.imageMessage) return ['imagen', x.imageMessage];
  if (x.audioMessage) return [x.audioMessage.ptt ? 'nota' : 'audio', x.audioMessage];
  if (x.videoMessage) return ['video', x.videoMessage];
  if (x.documentMessage) return ['documento', x.documentMessage];
  if (x.stickerMessage) return ['sticker', x.stickerMessage];
  return [null, null];
}

function textoDe(x) {
  if (x.conversation) return x.conversation;
  if (x.extendedTextMessage?.text) return x.extendedTextMessage.text;
  const [, nodo] = mediaDe(x);
  if (nodo?.caption) return nodo.caption;
  if (x.locationMessage) return `📍 ubicación: https://maps.google.com/?q=${x.locationMessage.degreesLatitude},${x.locationMessage.degreesLongitude}`;
  if (x.liveLocationMessage) return '📍 compartió su ubicación en tiempo real';
  if (x.contactMessage) return `👤 contacto: ${x.contactMessage.displayName || ''}`;
  if (x.reactionMessage) return x.reactionMessage.text ? `reaccionó ${x.reactionMessage.text}` : '';
  if (x.pollCreationMessage || x.pollCreationMessageV3) return `📊 encuesta: ${(x.pollCreationMessage || x.pollCreationMessageV3).name}`;
  return '';
}

const EXT = { imagen: '.jpg', nota: '.ogg', audio: '.mp3', video: '.mp4', sticker: '.webp', documento: '' };

function limpiarMedia() {   // por si Jarvis no llegó a recogerlos
  try {
    for (const f of fs.readdirSync(MEDIA_DIR)) {
      const r = path.join(MEDIA_DIR, f);
      try { if (Date.now() - fs.statSync(r).mtimeMs > 30 * 60 * 1000) fs.unlinkSync(r); } catch (_) {}
    }
  } catch (_) {}
}
setInterval(limpiarMedia, 10 * 60 * 1000);

function yaRegistrado() {
  try { return !!JSON.parse(fs.readFileSync(path.join(AUTH_DIR, 'creds.json'), 'utf8')).registered; } catch (_) { return false; }
}

function descartarSocket() {
  if (!sock) return;
  try { sock.ev.removeAllListeners(); } catch (_) {}
  try { sock.end(undefined); } catch (_) {}
  sock = null;
}

async function alRecibir(s, m, type) {
  const jid = limpiarJid(m.key.remoteJid);
  if (!jid || jid === 'status@broadcast' || !m.message) return;
  if (m.key.id && enviados.has(m.key.id)) return;
  if (type !== 'notify' || m.key.fromMe) return;           // solo lo nuevo que te mandan a ti
  const grupo = jid.endsWith('@g.us');
  if (!grupo && !esPersona(jid)) return;                   // canales, difusión...
  const x = contenido(m);
  if (x.protocolMessage || x.senderKeyDistributionMessage && Object.keys(x).length === 1) return;
  const texto = textoDe(x);
  const [tipo, nodo] = mediaDe(x);
  if (!texto && !tipo) return;

  const autor = grupo ? limpiarJid(m.key.participant) : jid;
  if (m.pushName && autor) guardarContacto(autor, m.pushName, false);
  let media = null;
  if (tipo) {
    const tam = Number(nodo.fileLength) || 0;
    media = { tipo, mimetype: nodo.mimetype || '', nombre: nodo.fileName || '', segundos: Number(nodo.seconds) || 0 };
    if (tam > MEDIA_MAX) media.error = 'muy grande';
    else {
      try {
        const buf = await downloadMediaMessage(m, 'buffer', {}, { logger, reuploadRequest: s.updateMediaMessage });
        const ext = EXT[tipo] || (path.extname(nodo.fileName || '') || '');
        media.archivo = path.join(MEDIA_DIR, String(m.key.id || Date.now()).replace(/[^A-Za-z0-9]/g, '') + ext);
        fs.writeFileSync(media.archivo, buf, { mode: 0o600 });
      } catch (e) { media.error = 'no se pudo descargar'; log('media no descargada:', String(e?.message || e)); }
    }
  }
  if (!grupo) {
    const cl = ultimasClaves.get(jid) || [];
    cl.push(m.key); if (cl.length > 50) cl.shift();
    ultimasClaves.set(jid, cl);
  }
  let grupoNombre = null;
  if (grupo) { try { grupoNombre = (await s.groupMetadata(jid)).subject; } catch (_) {} }
  emitir({ tipo: 'mensaje', jid, wid: m.key.id || null, grupo, grupo_nombre: grupoNombre,
           nombre: nombreJid(autor || jid, m.pushName), texto, media,
           ts: Number(m.messageTimestamp) || undefined });
}

async function iniciar(phone) {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  log('WhatsApp Web', version.join('.'), '· vinculado:', !!state.creds.registered);
  descartarSocket();
  const s = makeWASocket({
    version, auth: state, logger, printQRInTerminal: false,
    browser: Browsers.ubuntu('Chrome'),      // obligatorio un navegador conocido para vincular con código
    markOnlineOnConnect: false,              // que tu teléfono siga recibiendo notificaciones
    syncFullHistory: false,
    getMessage: async (key) => enviadosMsg.get(key.id) || undefined,
  });
  sock = s;

  let pedido = false, ok, ko;
  const promesaCodigo = new Promise((a, b) => { ok = a; ko = b; });

  s.ev.on('creds.update', saveCreds);
  s.ev.on('contacts.upsert', (cs) => cs.forEach(registrarContacto));
  s.ev.on('contacts.update', (cs) => cs.forEach(registrarContacto));
  s.ev.on('messaging-history.set', ({ contacts }) => (contacts || []).forEach(registrarContacto));
  s.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const m of messages) {
      try { await alRecibir(s, m, type); } catch (e) { log('error con un mensaje:', String(e?.message || e)); }
    }
  });
  s.ev.on('call', (calls) => {
    for (const c of calls) {
      if (c.status !== 'offer' && c.status !== 'timeout') continue;
      const de = limpiarJid(c.from);
      emitir({ tipo: 'llamada', estado: c.status === 'offer' ? 'entrante' : 'perdida', llamada: c.id, jid: de,
               nombre: nombreJid(de), video: !!c.isVideo, grupo: !!c.isGroup });
    }
  });

  s.ev.on('connection.update', async (u) => {
    if (s !== sock) return;
    const code = u.lastDisconnect?.error?.output?.statusCode;
    if (u.connection || code) log('conexión', JSON.stringify({ connection: u.connection || null, code: code || null }));
    if (u.qr && phone && !state.creds.registered && !pedido) {
      pedido = true;
      try {
        const c = await s.requestPairingCode(phone);
        estado = 'esperando_codigo';
        ok(c);
      } catch (e) { ko(e); }
    }
    if (u.connection === 'open') {
      const antes = estado;
      estado = 'abierto';
      yo = limpiarJid(s.user?.id);
      if (antes === 'esperando_codigo') emitir({ tipo: 'conexion', estado: 'vinculado', yo });
      setTimeout(async () => {               // nombres de tu libreta, una vez por conexión
        try { await s.resyncAppState(['critical_unblock_low'], true); } catch (_) {}
      }, 8000);
      log('enlazado como', yo);
    } else if (u.connection === 'close') {
      if (code === DisconnectReason.loggedOut) {
        estado = 'desconectado'; yo = null;
        try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (_) {}
        emitir({ tipo: 'conexion', estado: 'desvinculado' });
      } else if (!state.creds.registered && code !== DisconnectReason.restartRequired) {
        estado = 'desconectado';             // vinculación fallida: no reintentar en bucle
        ko(new Error('WhatsApp cerró la conexión (código ' + code + ')'));
      } else {                               // incluye 515 (reinicio normal tras vincular) y caídas de red
        estado = 'reconectando';
        await sleep(3000);
        if (s === sock) iniciar().catch((e) => { estado = 'error'; log('error al reconectar', e.message); });
      }
    }
  });

  if (state.creds.registered || !phone) return null;
  return Promise.race([promesaCodigo,
    new Promise((_, rej) => setTimeout(() => rej(new Error('WhatsApp no respondió para generar el código')), 30000))]);
}

async function aJid(to) {
  to = String(to || '');
  if (to.includes('@')) return limpiarJid(to);
  let dig = to.replace(/\D/g, '');
  if (dig.length === 10) dig = '52' + dig;   // número de México sin lada de país
  const r = await sock.onWhatsApp(dig);
  if (!r || !r[0] || !r[0].exists) throw new Error('ese número no tiene WhatsApp');
  return r[0].jid;
}

function contenidoEnvio(b) {
  if (!b.tipo) return { text: String(b.text || '').slice(0, 6000) };
  if (!b.archivo || !fs.existsSync(b.archivo)) throw new Error('no encuentro el archivo a mandar');
  const archivo = { url: b.archivo };
  const caption = b.caption ? String(b.caption) : undefined;
  switch (b.tipo) {
    case 'imagen': return { image: archivo, caption };
    case 'video': return { video: archivo, caption };
    case 'nota': return { audio: archivo, ptt: true, mimetype: 'audio/ogg; codecs=opus' };
    case 'audio': return { audio: archivo, mimetype: b.mimetype || 'audio/mpeg' };
    case 'documento': return { document: archivo, mimetype: b.mimetype || 'application/octet-stream',
                               fileName: b.nombre || path.basename(b.archivo), caption };
    default: throw new Error('tipo desconocido: ' + b.tipo);
  }
}

function leer(req) {
  return new Promise((ok) => {
    let b = ''; req.on('data', (d) => (b += d));
    req.on('end', () => { try { ok(JSON.parse(b || '{}')); } catch { ok({}); } });
  });
}

http.createServer(async (req, res) => {
  const out = (c, o) => { res.writeHead(c, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); };
  const tok = String(req.headers['x-jarvis-token'] || '');
  if (tok.length !== TOKEN.length || !crypto.timingSafeEqual(Buffer.from(tok), Buffer.from(TOKEN)))
    return out(401, { error: 'token' });
  const url = new URL(req.url, 'http://x');
  const body = req.method === 'POST' ? await leer(req) : {};
  try {
    switch (url.pathname) {
      case '/status':
        return out(200, { estado, yo, boot: BOOT, vinculado: yaRegistrado(), contactos: contactos.size,
                          libreta: [...contactos.values()].filter((c) => c.nombre).length });
      case '/connect': {
        if (sock && (estado === 'abierto' || estado === 'reconectando')) return out(200, { estado, yo, code: null });
        if (sock) {
          descartarSocket();
          if (!yaRegistrado()) { try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (_) {} }
        }
        let phone = String(body.phone || '').replace(/\D/g, '') || undefined;
        if (phone && phone.length === 10) phone = '52' + phone;
        const code = await iniciar(phone);
        return out(200, { estado, code });
      }
      case '/logout': {
        try { if (sock) await sock.logout(); } catch (_) {}
        descartarSocket();
        try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (_) {}
        estado = 'desconectado'; yo = null;
        return out(200, { ok: true });
      }
      case '/events': {
        const since = parseInt(url.searchParams.get('since') || '0', 10);
        const wait = Math.min(30, Math.max(0, parseInt(url.searchParams.get('wait') || '0', 10)));
        const nuevos = () => eventos.filter((e) => e.id > since || since > seq);   // since > seq: el puente se reinició
        if (!nuevos().length && wait) {
          await new Promise((r) => { const t = setTimeout(r, wait * 1000); esperando.push(() => { clearTimeout(t); r(); }); });
        }
        return out(200, { boot: BOOT, ultimo: seq, eventos: nuevos() });
      }
      case '/send': {
        if (estado !== 'abierto') return out(200, { ok: false, error: 'WhatsApp no está conectado' });
        const jid = await aJid(body.to);
        const env = await sock.sendMessage(jid, contenidoEnvio(body));
        if (env?.key?.id) {
          enviados.add(env.key.id);
          if (env.message) enviadosMsg.set(env.key.id, env.message);
          if (enviadosMsg.size > 300) enviadosMsg.delete(enviadosMsg.keys().next().value);
          if (enviados.size > 300) enviados.delete(enviados.values().next().value);
        }
        return out(200, { ok: true, jid, nombre: nombreJid(jid) });
      }
      case '/contacts': {
        const toks = plano(url.searchParams.get('q')).split(/\s+/).filter(Boolean);
        const lista = [...contactos.values()]
          .filter((c) => { const n = plano(c.nombre + ' ' + c.notify); return toks.length && toks.every((t) => n.includes(t)); })
          .sort((a, b) => (b.nombre ? 1 : 0) - (a.nombre ? 1 : 0))
          .slice(0, 40)
          .map((c) => ({ jid: c.jid, nombre: nombreDe(c), libreta: !!c.nombre }));
        return out(200, { contactos: lista });
      }
      case '/chats': {
        const ult = new Map();
        for (const e of eventos) if (e.tipo === 'mensaje') ult.set(e.jid, e);
        return out(200, { chats: [...ult.values()].reverse().slice(0, 20)
          .map((e) => ({ jid: e.jid, nombre: e.grupo ? (e.grupo_nombre || e.nombre) : e.nombre, grupo: e.grupo,
                         texto: e.texto || (e.media ? `[${e.media.tipo}]` : ''), ts: e.ts })) });
      }
      case '/read': {
        const jid = limpiarJid(body.jid);
        const claves = ultimasClaves.get(jid) || [];
        if (claves.length && sock && estado === 'abierto') { await sock.readMessages(claves); ultimasClaves.delete(jid); }
        return out(200, { ok: true, leidos: claves.length });
      }
      case '/reject': {
        if (!sock || estado !== 'abierto') return out(200, { ok: false, error: 'WhatsApp no está conectado' });
        await sock.rejectCall(String(body.id), String(body.from));
        return out(200, { ok: true });
      }
      default: return out(404, { error: 'no existe' });
    }
  } catch (e) { return out(500, { error: String(e?.message || e) }); }
}).listen(PORT, '127.0.0.1', () => {
  log(`escuchando en 127.0.0.1:${PORT}`);
  if (yaRegistrado()) iniciar().catch((e) => { estado = 'error'; log('no pude conectar:', e.message); });
});

process.on('unhandledRejection', (e) => console.error('[wa] unhandledRejection', e));
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => { try { sock?.end(undefined); } catch (_) {} process.exit(0); });
